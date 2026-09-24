"""跨月清算账本：命令、角色、并发控制与事件溯源状态机。

设计要点：
- 一切事实都是事件（见 events.py），账本状态由事件重放得到，重启后自动恢复；
- 月报提交即冻结，之后只接受追加调整（ADJUSTMENT_APPENDED），调整经审核
  （ADJUSTMENT_REVIEWED）才产生差额，归属原申报月份；
- 付款只增不改：已支付差异通过下一批次抵扣（OFFSET_APPLIED）或追缴
  （RECOVERY_POSTED）平衡，原付款事件永不改写；
- 申诉只冻结争议行，其余行照常结算；
- 角色分离：政策/绑定=ADMIN，机构录入=ORG_OPERATOR，审核=REVIEWER，
  付款=PAYER，追缴=RECOVERER，家长=PARENT（仅可见自身孩子的月份与退费）；
- 幂等与并发：event_id 重放不重复计费；同材料不同内容报 MaterialConflict；
  申报/容量/调整采用 expected_version 乐观并发，不匹配报 VersionConflict。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from .compute import compute_month, diff_lines, month_days, months_of_quarter
from .events import EventStore

ROLES = ("ADMIN", "ORG_OPERATOR", "REVIEWER", "PAYER", "RECOVERER", "PARENT")
STAFF_ROLES = ("ADMIN", "REVIEWER", "PAYER", "RECOVERER")


class LedgerError(Exception):
    """账本命令错误的基类。"""


class PermissionDenied(LedgerError):
    """角色无权执行该命令或查看该数据。"""


class VersionConflict(LedgerError):
    """expected_version 与当前版本不一致（并发申报冲突）。"""


class StateError(LedgerError):
    """状态机不允许的流转（如冻结后直接改出勤）。"""


class MaterialConflict(LedgerError):
    """相同材料号提交了不同内容。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """普惠托育补助跨月清算账本。"""

    def __init__(self, store: EventStore | None = None, clock=_utcnow):
        self.store = store or EventStore()
        self.clock = clock
        self._reset()
        for event in self.store.all():
            self._apply(event)
        self._counter = len(self.store.all())

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self.policies: dict[tuple[str, str], list[dict]] = {}
        self.capacities: dict[tuple[str, str], dict] = {}
        self.attendance: dict[str, dict[str, dict[str, dict]]] = {}
        self.closures: dict[str, set[str]] = {}
        self.withdrawals: dict[str, dict[str, str]] = {}
        self.refunds: dict[str, list[dict]] = {}
        self.parent_links: dict[str, set[str]] = {}
        self.claims: dict[str, dict] = {}
        self.claim_by_org_month: dict[tuple[str, str], str] = {}
        self.adjustments: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.recoveries: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.materials: dict[str, str] = {}
        self.appeals: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 事件发射与应用
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        while True:
            self._counter += 1
            candidate = f"{prefix}-{self._counter:06d}"
            if self.store.get(candidate) is None:
                return candidate

    def _emit(self, kind: str, subject_id: str, payload: dict,
              event_id: str | None = None) -> dict:
        event = {
            "event_id": event_id or self._next_id("evt"),
            "kind": kind,
            "occurred_at": self.clock(),
            "subject_id": subject_id,
            "payload": payload,
        }
        event, is_new = self.store.append(event)
        if is_new:
            self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['kind'].lower()}", None)
        if handler is not None:
            handler(event)

    # --- 各事件的状态迁移 ------------------------------------------------

    def _on_policy_published(self, event: dict) -> None:
        p = event["payload"]
        key = (p["institution_type"], p["age_band"])
        self.policies.setdefault(key, []).append(dict(p))

    def _on_capacity_declared(self, event: dict) -> None:
        p = event["payload"]
        self.capacities[(p["org_id"], p["month"])] = {
            "institution_type": p["institution_type"],
            "slots": dict(p["slots"]),
            "version": p["version"],
        }

    def _on_attendance_recorded(self, event: dict) -> None:
        p = event["payload"]
        self.attendance.setdefault(p["org_id"], {}).setdefault(p["child_id"], {})[
            p["date"]] = {"age_band": p["age_band"]}
        self.materials[f"att:{p['org_id']}:{p['child_id']}:{p['date']}"] = \
            event["event_id"]

    def _on_closure_recorded(self, event: dict) -> None:
        p = event["payload"]
        self.closures.setdefault(p["org_id"], set()).add(p["date"])

    def _on_withdrawal_recorded(self, event: dict) -> None:
        p = event["payload"]
        org, child = p["org_id"], p["child_id"]
        known = self.withdrawals.setdefault(org, {}).get(child)
        if known is None or p["effective_date"] < known:
            self.withdrawals[org][child] = p["effective_date"]
        if p.get("refund_cents"):
            self.refunds.setdefault(child, []).append({
                "month": p["effective_date"][:7],
                "refund_cents": p["refund_cents"],
            })

    def _on_child_parent_linked(self, event: dict) -> None:
        p = event["payload"]
        self.parent_links.setdefault(p["parent_id"], set()).add(p["child_id"])

    def _on_month_claimed(self, event: dict) -> None:
        p = event["payload"]
        lines = {}
        for line in p["lines"]:
            lines[line["child_id"]] = {
                **line,
                "status": "pending",
                "reviewed_amount_cents": None,
            }
        self.claims[p["claim_id"]] = {
            "claim_id": p["claim_id"],
            "claim_event_id": event["event_id"],
            "org_id": p["org_id"],
            "month": p["month"],
            "version": p["version"],
            "status": "frozen",
            "lines": lines,
            "inputs": p["inputs"],
            "adjustments": [],
            "approved_delta": {},
            "approved_seq": 0,
            "settled_cents": 0,
        }
        self.claim_by_org_month[(p["org_id"], p["month"])] = p["claim_id"]

    def _on_claim_reviewed(self, event: dict) -> None:
        p = event["payload"]
        claim = self.claims[p["claim_id"]]
        for decision in p["line_decisions"]:
            line = claim["lines"][decision["child_id"]]
            line["status"] = "approved" if decision["approved"] else "rejected"
            line["reviewed_amount_cents"] = decision["amount_cents"]
        self._refresh_claim_status(claim)

    def _on_appeal_opened(self, event: dict) -> None:
        p = event["payload"]
        claim = self.claims[p["claim_id"]]
        self.appeals[p["appeal_id"]] = {
            "appeal_id": p["appeal_id"],
            "claim_id": p["claim_id"],
            "lines": list(p["line_keys"]),
            "status": "open",
        }
        for child_id in p["line_keys"]:
            claim["lines"][child_id]["status"] = "appealed"

    def _on_appeal_resolved(self, event: dict) -> None:
        p = event["payload"]
        appeal = self.appeals[p["appeal_id"]]
        appeal["status"] = "resolved"
        claim = self.claims[p["claim_id"]]
        for decision in p["line_decisions"]:
            line = claim["lines"][decision["child_id"]]
            line["status"] = "approved" if decision["approved"] else "rejected"
            line["reviewed_amount_cents"] = decision["amount_cents"]
        self._refresh_claim_status(claim)

    def _on_adjustment_appended(self, event: dict) -> None:
        p = event["payload"]
        claim = self.claims[p["claim_id"]]
        self.adjustments[p["adjustment_id"]] = {
            **p,
            "status": "pending",
            "deltas": {},
            "approved_seq": None,
        }
        claim["adjustments"].append(p["adjustment_id"])
        claim["version"] = p["version"]
        if p.get("material_id"):
            self.materials[f"adj:{p['material_id']}"] = p["adjustment_id"]

    def _on_adjustment_reviewed(self, event: dict) -> None:
        p = event["payload"]
        adj = self.adjustments[p["adjustment_id"]]
        claim = self.claims[p["claim_id"]]
        adj["status"] = "approved" if p["approved"] else "rejected"
        adj["deltas"] = dict(p["deltas"])
        if p["approved"]:
            claim["approved_seq"] += 1
            adj["approved_seq"] = claim["approved_seq"]
            for child_id, delta in p["deltas"].items():
                claim["approved_delta"][child_id] = (
                    claim["approved_delta"].get(child_id, 0) + delta)
        claim["version"] = p["version"]

    def _on_payment_batch_opened(self, event: dict) -> None:
        p = event["payload"]
        self.batches[p["batch_id"]] = {
            **p,
            "status": "open",
            "posted_lines": set(),
            "posted_recoveries": set(),
            "posted_offsets": set(),
            "receipt_issued": False,
        }

    def _on_payment_posted(self, event: dict) -> None:
        p = event["payload"]
        batch = self.batches[p["batch_id"]]
        batch["posted_lines"].add(p["line_key"])
        claim = self.claims[p["line_key"]]
        claim["settled_cents"] += p["amount_cents"]
        claim["status"] = "settled"
        self._maybe_close_batch(batch)

    def _on_offset_applied(self, event: dict) -> None:
        p = event["payload"]
        batch = self.batches[p["batch_id"]]
        batch["posted_offsets"].add(p["recovery_id"])
        recovery = self.recoveries[p["recovery_id"]]
        recovery["balance_cents"] -= p["amount_cents"]
        if recovery["balance_cents"] == 0:
            recovery["status"] = "offset"
        self._maybe_close_batch(batch)

    def _on_recovery_posted(self, event: dict) -> None:
        p = event["payload"]
        self.recoveries[p["recovery_id"]] = {
            **p,
            "balance_cents": p["amount_cents"],
            "status": "open",
        }
        batch_id = p.get("batch_id")
        if batch_id:
            batch = self.batches[batch_id]
            batch["posted_recoveries"].add(p["recovery_id"])
            # 批次内追缴落地即结清对应申报的负差额：已结算冲减到与应付一致，
            # 后续批次不会就同一差额重复生成追缴
            claim = self.claims.get(p.get("claim_id") or "")
            if claim is not None:
                claim["settled_cents"] -= p["amount_cents"]
            self._maybe_close_batch(batch)

    def _on_receipt_issued(self, event: dict) -> None:
        p = event["payload"]
        self.receipts[p["receipt_id"]] = dict(p)
        if p.get("recovery_id"):
            recovery = self.recoveries[p["recovery_id"]]
            recovery["balance_cents"] = 0
            recovery["status"] = "paid"
        if p.get("batch_id"):
            batch = self.batches[p["batch_id"]]
            batch["receipt_issued"] = True
            self._maybe_close_batch(batch)

    def _refresh_claim_status(self, claim: dict) -> None:
        if all(line["status"] != "pending" for line in claim["lines"].values()):
            if claim["status"] == "frozen":
                claim["status"] = "reviewed"

    def _maybe_close_batch(self, batch: dict) -> None:
        done = (
            len(batch["posted_lines"]) == len(batch["lines"])
            and len(batch["posted_recoveries"]) == len(batch.get("recoveries", []))
            and len(batch["posted_offsets"]) == len(batch.get("offsets", []))
            and batch["receipt_issued"]
        )
        if done:
            batch["status"] = "paid"

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _require(actor: dict, *roles: str) -> None:
        if actor.get("role") not in roles:
            raise PermissionDenied(
                f"角色 {actor.get('role')} 无权执行，需要 {'/'.join(roles)}")

    def _find_claim(self, org_id: str, month: str) -> dict | None:
        claim_id = self.claim_by_org_month.get((org_id, month))
        return self.claims.get(claim_id) if claim_id else None

    def _live_inputs(self, org_id: str, month: str) -> dict:
        capacity = self.capacities.get((org_id, month), {})
        days = set(month_days(month))
        attendance = {
            child: {d: rec for d, rec in dates.items() if d in days}
            for child, dates in self.attendance.get(org_id, {}).items()
        }
        return {
            "attendance": {c: d for c, d in attendance.items() if d},
            "closures": sorted(d for d in self.closures.get(org_id, set())
                               if d in days),
            "withdrawals": dict(self.withdrawals.get(org_id, {})),
            "capacity": dict(capacity.get("slots", {})),
            "institution_type": capacity.get("institution_type"),
        }

    def _all_policies(self) -> list[dict]:
        return [p for versions in self.policies.values() for p in versions]

    # ------------------------------------------------------------------
    # 政策与基础资料（ADMIN / ORG_OPERATOR）
    # ------------------------------------------------------------------

    def publish_policy(self, actor: dict, *, policy_id: str, version: int,
                       institution_type: str, age_band: str, effective_from: str,
                       daily_rate_cents: int, closure_relief_rate: int = 0,
                       effective_to: str | None = None,
                       event_id: str | None = None) -> dict:
        """发布政策版本：按机构类型 × 年龄段 × 生效日生效。"""
        self._require(actor, "ADMIN")
        return self._emit("POLICY_PUBLISHED", policy_id, {
            "policy_id": policy_id, "version": version,
            "institution_type": institution_type, "age_band": age_band,
            "effective_from": effective_from, "effective_to": effective_to,
            "daily_rate_cents": daily_rate_cents,
            "closure_relief_rate": closure_relief_rate,
        }, event_id)

    def link_parent(self, actor: dict, *, child_id: str, parent_id: str,
                    event_id: str | None = None) -> dict:
        self._require(actor, "ADMIN")
        return self._emit("CHILD_PARENT_LINKED", child_id, {
            "child_id": child_id, "parent_id": parent_id}, event_id)

    def declare_capacity(self, actor: dict, *, org_id: str, month: str,
                         institution_type: str, slots: dict,
                         expected_version: int = 0,
                         event_id: str | None = None) -> dict:
        """机构月度名额声明；expected_version 防止并发覆盖。"""
        self._require(actor, "ORG_OPERATOR")
        current = self.capacities.get((org_id, month))
        current_version = current["version"] if current else 0
        if expected_version != current_version:
            raise VersionConflict(
                f"容量声明版本冲突：期望 {expected_version}，当前 {current_version}")
        return self._emit("CAPACITY_DECLARED", org_id, {
            "org_id": org_id, "month": month,
            "institution_type": institution_type, "slots": dict(slots),
            "version": current_version + 1,
        }, event_id)

    # ------------------------------------------------------------------
    # 出勤 / 停园 / 退托（ORG_OPERATOR）
    # ------------------------------------------------------------------

    def record_attendance(self, actor: dict, *, org_id: str, child_id: str,
                          date: str, age_band: str, material_id: str,
                          event_id: str | None = None) -> dict:
        """登记出勤证据。重复签到（同材料同内容）幂等；冻结后须走后补证明。"""
        self._require(actor, "ORG_OPERATOR")
        key = f"att:{org_id}:{child_id}:{date}"
        known_event_id = self.materials.get(key)
        if known_event_id is not None:
            existing = self.attendance[org_id][child_id][date]
            if existing["age_band"] != age_band:
                raise MaterialConflict(f"材料 {material_id} 与已登记内容不一致")
            # 相同材料重放即使发生在冻结之后也直接返回原事件，不重复计费
            return self.store.get(known_event_id)
        if self._find_claim(org_id, date[:7]) is not None:
            raise StateError(f"{date[:7]} 月报已冻结，请通过 append_adjustment 后补证明")
        return self._emit("ATTENDANCE_RECORDED", org_id, {
            "org_id": org_id, "child_id": child_id, "date": date,
            "age_band": age_band, "material_id": material_id,
        }, event_id)

    def record_closure(self, actor: dict, *, org_id: str, dates: list[str],
                       reason: str = "", event_id: str | None = None) -> list[dict]:
        """登记临时停园。已冻结月份自动生成待审核调整（归属停园日所在月）。"""
        self._require(actor, "ORG_OPERATOR")
        events = []
        for day in sorted(set(dates)):
            closure_event = self._emit("CLOSURE_RECORDED", org_id, {
                "org_id": org_id, "date": day, "reason": reason,
            }, event_id if len(dates) == 1 else None)
            events.append(closure_event)
            events.extend(self._auto_adjust(org_id, day[:7], "closure",
                                            {"type": "closure", "date": day},
                                            source_id=closure_event["event_id"]))
        return events

    def record_withdrawal(self, actor: dict, *, org_id: str, child_id: str,
                          effective_date: str, refund_cents: int = 0,
                          event_id: str | None = None) -> list[dict]:
        """登记退托。跨月退托对生效月及之后已冻结月份自动生成调整。"""
        self._require(actor, "ORG_OPERATOR")
        event = self._emit("WITHDRAWAL_RECORDED", org_id, {
            "org_id": org_id, "child_id": child_id,
            "effective_date": effective_date, "refund_cents": refund_cents,
        }, event_id)
        events = [event]
        for (org, month), claim_id in sorted(self.claim_by_org_month.items()):
            if org != org_id or month < effective_date[:7]:
                continue
            claim = self.claims[claim_id]
            if child_id not in claim["lines"]:
                continue
            events.extend(self._auto_adjust(
                org_id, month, "withdrawal",
                {"type": "withdrawal", "child_id": child_id,
                 "effective_date": effective_date},
                source_id=event["event_id"]))
        return events

    def _auto_adjust(self, org_id: str, month: str, reason: str,
                     update: dict, source_id: str) -> list[dict]:
        """对已冻结申报生成系统调整；零差额则不产生调整。"""
        claim = self._find_claim(org_id, month)
        if claim is None:
            return []
        adjustment_id = f"adj-{source_id}-{claim['claim_id']}"
        if self.adjustments.get(adjustment_id) or self.store.get(adjustment_id):
            return []  # 崩溃重入：该来源的调整已存在
        deltas = self._preview_deltas(claim, [update])
        if not deltas:
            return []
        return [self._emit("ADJUSTMENT_APPENDED", org_id, {
            "adjustment_id": adjustment_id,
            "claim_id": claim["claim_id"],
            "attributed_month": month,
            "reason": reason,
            "updates": [update],
            "material_id": None,
            "version": claim["version"] + 1,
        }, adjustment_id)]

    # ------------------------------------------------------------------
    # 月度申报 / 审核 / 申诉
    # ------------------------------------------------------------------

    def submit_claim(self, actor: dict, *, org_id: str, month: str,
                     expected_version: int = 0,
                     event_id: str | None = None) -> dict:
        """提交月度申报（提交即冻结，快照当时计算输入）。

        并发申报：已存在申报时 expected_version 必须等于当前版本，
        否则报 VersionConflict；相等则视为重放，返回原事件。
        """
        self._require(actor, "ORG_OPERATOR")
        existing = self._find_claim(org_id, month)
        if existing is not None:
            if expected_version != existing["version"]:
                raise VersionConflict(
                    f"申报版本冲突：期望 {expected_version}，当前 {existing['version']}")
            return self.store.get(existing["claim_event_id"]) or existing
        if expected_version != 0:
            raise VersionConflict(f"申报版本冲突：期望 {expected_version}，当前 0")
        inputs = self._live_inputs(org_id, month)
        lines = compute_month(month, inputs, self._all_policies())
        claim_id = f"claim-{org_id}-{month}"
        return self._emit("MONTH_CLAIMED", org_id, {
            "claim_id": claim_id, "org_id": org_id, "month": month,
            "version": 1, "lines": lines, "inputs": inputs,
        }, event_id)

    def review_claim(self, actor: dict, *, claim_id: str,
                     decisions: list[dict], event_id: str | None = None) -> dict:
        """审核员逐行核定：decisions=[{child_id, approved, amount_cents?}]。"""
        self._require(actor, "REVIEWER")
        claim = self._claim_or_raise(claim_id)
        payload_decisions = []
        for decision in decisions:
            child_id = decision["child_id"]
            line = claim["lines"].get(child_id)
            if line is None:
                raise StateError(f"申报 {claim_id} 没有儿童 {child_id} 的行")
            if line["status"] == "appealed":
                raise StateError(f"儿童 {child_id} 的行在申诉中，已冻结")
            if line["status"] != "pending":
                raise StateError(f"儿童 {child_id} 的行已 {line['status']}，不能重复审核")
            approved = bool(decision["approved"])
            payload_decisions.append({
                "child_id": child_id, "approved": approved,
                "amount_cents": line["amount_cents"] if approved else 0,
            })
        return self._emit("CLAIM_REVIEWED", claim["org_id"], {
            "claim_id": claim_id, "line_decisions": payload_decisions}, event_id)

    def open_appeal(self, actor: dict, *, claim_id: str, line_keys: list[str],
                    appeal_id: str | None = None,
                    event_id: str | None = None) -> dict:
        """机构申诉：仅冻结争议行，其他行照常结算。"""
        self._require(actor, "ORG_OPERATOR")
        claim = self._claim_or_raise(claim_id)
        for child_id in line_keys:
            line = claim["lines"].get(child_id)
            if line is None:
                raise StateError(f"申报 {claim_id} 没有儿童 {child_id} 的行")
            if line["status"] == "pending":
                raise StateError(f"儿童 {child_id} 的行尚未审核，不能申诉")
        appeal_id = appeal_id or self._next_id("appeal")
        return self._emit("APPEAL_OPENED", claim["org_id"], {
            "appeal_id": appeal_id, "claim_id": claim_id,
            "line_keys": list(line_keys)}, event_id)

    def resolve_appeal(self, actor: dict, *, appeal_id: str,
                       decisions: list[dict], event_id: str | None = None) -> dict:
        """申诉结案：按裁定恢复争议行状态。"""
        self._require(actor, "REVIEWER")
        appeal = self.appeals.get(appeal_id)
        if appeal is None or appeal["status"] != "open":
            raise StateError(f"申诉 {appeal_id} 不存在或已结案")
        claim = self.claims[appeal["claim_id"]]
        payload_decisions = []
        for decision in decisions:
            child_id = decision["child_id"]
            if child_id not in appeal["lines"]:
                raise StateError(f"儿童 {child_id} 不在申诉 {appeal_id} 范围内")
            line = claim["lines"][child_id]
            approved = bool(decision["approved"])
            payload_decisions.append({
                "child_id": child_id, "approved": approved,
                "amount_cents": line["amount_cents"] if approved else 0,
            })
        return self._emit("APPEAL_RESOLVED", claim["org_id"], {
            "appeal_id": appeal_id, "claim_id": appeal["claim_id"],
            "line_decisions": payload_decisions}, event_id)

    # ------------------------------------------------------------------
    # 冻结后的追加调整
    # ------------------------------------------------------------------

    def append_adjustment(self, actor: dict, *, claim_id: str,
                          attributed_month: str, reason: str, updates: list[dict],
                          expected_version: int, material_id: str | None = None,
                          event_id: str | None = None) -> dict:
        """冻结后追加调整（后补证明/停园/退托/人工差额），归属原月份。

        updates 支持：
        - {"type": "attendance", "child_id", "date", "age_band"}  后补出勤证明
        - {"type": "closure", "date"}                             补登停园
        - {"type": "withdrawal", "child_id", "effective_date"}    补登退托
        - {"type": "manual_delta", "child_id", "delta_cents"}     人工差额
        """
        self._require(actor, "ORG_OPERATOR")
        claim = self._claim_or_raise(claim_id)
        if expected_version != claim["version"]:
            raise VersionConflict(
                f"调整版本冲突：期望 {expected_version}，当前 {claim['version']}")
        if material_id:
            key = f"adj:{material_id}"
            known = self.materials.get(key)
            if known is not None:
                existing = self.adjustments[known]
                if existing["updates"] != updates:
                    raise MaterialConflict(f"材料 {material_id} 已用于其他调整")
                return self.store.get(known) or existing
        adjustment_id = self._next_id("adj")
        return self._emit("ADJUSTMENT_APPENDED", claim["org_id"], {
            "adjustment_id": adjustment_id, "claim_id": claim_id,
            "attributed_month": attributed_month, "reason": reason,
            "updates": copy.deepcopy(updates), "material_id": material_id,
            "version": claim["version"] + 1,
        }, event_id or adjustment_id)

    def review_adjustment(self, actor: dict, *, adjustment_id: str,
                          approve: bool, event_id: str | None = None) -> dict:
        """审核调整：通过时基于已批准基线重算差额并入账。"""
        self._require(actor, "REVIEWER")
        adj = self.adjustments.get(adjustment_id)
        if adj is None or adj["status"] != "pending":
            raise StateError(f"调整 {adjustment_id} 不存在或已审核")
        claim = self.claims[adj["claim_id"]]
        deltas = self._preview_deltas(claim, adj["updates"]) if approve else {}
        return self._emit("ADJUSTMENT_REVIEWED", claim["org_id"], {
            "adjustment_id": adjustment_id, "claim_id": claim["claim_id"],
            "approved": approve, "deltas": deltas,
            "version": claim["version"] + 1,
        }, event_id)

    def _preview_deltas(self, claim: dict, extra_updates: list[dict]) -> dict:
        """在已批准调整基线上追加 extra_updates 重算，返回逐儿童差额。"""
        inputs, manual, base_lines = self._approved_base(claim)
        inputs = copy.deepcopy(inputs)
        manual = dict(manual)
        self._apply_updates(inputs, manual, extra_updates)
        new_lines = self._compute_with_manual(claim["month"], inputs, manual)
        return diff_lines(base_lines, new_lines)

    def _approved_base(self, claim: dict) -> tuple[dict, dict, list]:
        """冻结快照 + 已批准调整（按批准顺序）重放的基线。"""
        inputs = copy.deepcopy(claim["inputs"])
        manual: dict[str, int] = {}
        approved = [self.adjustments[aid] for aid in claim["adjustments"]]
        approved = [a for a in approved if a["status"] == "approved"]
        approved.sort(key=lambda a: a["approved_seq"])
        lines = None
        for adj in approved:
            self._apply_updates(inputs, manual, adj["updates"])
        if approved or manual:
            lines = self._compute_with_manual(claim["month"], inputs, manual)
        else:
            lines = [self._public_line(line) for line in claim["lines"].values()]
            lines.sort(key=lambda l: l["child_id"])
        return inputs, manual, lines

    @staticmethod
    def _public_line(line: dict) -> dict:
        return {k: line[k] for k in
                ("child_id", "age_band", "attendance_days", "relief_days",
                 "capped_days", "amount_cents", "policy_ids")}

    @staticmethod
    def _apply_updates(inputs: dict, manual: dict, updates: list[dict]) -> None:
        for upd in updates:
            kind = upd["type"]
            if kind == "attendance":
                inputs.setdefault("attendance", {}).setdefault(
                    upd["child_id"], {})[upd["date"]] = {"age_band": upd["age_band"]}
            elif kind == "closure":
                closures = set(inputs.get("closures", []))
                closures.add(upd["date"])
                inputs["closures"] = sorted(closures)
            elif kind == "withdrawal":
                withdrawals = inputs.setdefault("withdrawals", {})
                known = withdrawals.get(upd["child_id"])
                if known is None or upd["effective_date"] < known:
                    withdrawals[upd["child_id"]] = upd["effective_date"]
            elif kind == "manual_delta":
                manual[upd["child_id"]] = manual.get(upd["child_id"], 0) + int(
                    upd["delta_cents"])
            else:
                raise StateError(f"未知调整类型 {kind}")

    def _compute_with_manual(self, month: str, inputs: dict,
                             manual: dict) -> list[dict]:
        lines = compute_month(month, inputs, self._all_policies())
        by_child = {line["child_id"]: line for line in lines}
        for child_id, delta in manual.items():
            line = by_child.get(child_id)
            if line is None:
                line = {"child_id": child_id, "age_band": "",
                        "attendance_days": 0, "relief_days": 0, "capped_days": 0,
                        "amount_cents": 0, "policy_ids": []}
                lines.append(line)
                by_child[child_id] = line
            line["amount_cents"] += delta
        lines.sort(key=lambda l: l["child_id"])
        return lines

    # ------------------------------------------------------------------
    # 支付批次 / 抵扣 / 追缴
    # ------------------------------------------------------------------

    def _claim_due(self, claim: dict) -> int:
        approved = sum(line["reviewed_amount_cents"] or 0
                       for line in claim["lines"].values()
                       if line["status"] == "approved")
        return approved + sum(claim["approved_delta"].values())

    def create_payment_batch(self, actor: dict, *, org_id: str,
                             batch_id: str | None = None,
                             event_id: str | None = None) -> dict:
        """开启支付批次：汇总已审定申报的应付差额，并计划抵扣与追缴。

        应付净额 = 审定应付 - 已结算；净额为负的申报转入追缴计划（追缴落地
        时冲减该申报的已结算额），未结追缴按编号顺序抵扣本批应付，余额作为
        实际拨付。已支付事件永不改写，差异只通过抵扣/追缴平衡。
        """
        self._require(actor, "PAYER")
        if any(b["org_id"] == org_id and b["status"] == "open"
               for b in self.batches.values()):
            raise StateError("存在未完成的支付批次，请先拨付或重启后续跑")

        lines = []
        planned_recoveries = []
        batch_id = batch_id or self._next_id("batch")
        for claim in sorted(self.claims.values(),
                            key=lambda c: (c["month"], c["claim_id"])):
            if claim["org_id"] != org_id or claim["status"] == "frozen":
                continue
            due = self._claim_due(claim)
            net = due - claim["settled_cents"]
            if net > 0:
                lines.append({
                    "claim_id": claim["claim_id"], "month": claim["month"],
                    "due_cents": due, "settled_cents": claim["settled_cents"],
                    "net_cents": net,
                })
            elif net < 0:
                planned_recoveries.append({
                    "recovery_id": f"rec-{batch_id}-{claim['claim_id']}",
                    "claim_id": claim["claim_id"],
                    "amount_cents": -net,
                    "reason": f"{claim['month']} 清算差额为负，转入追缴",
                })
        if not lines and not planned_recoveries:
            raise StateError("没有待结算的申报")

        gross = sum(line["net_cents"] for line in lines)
        planned_offsets = []
        remaining = gross
        for recovery_id in sorted(self.recoveries):
            recovery = self.recoveries[recovery_id]
            if recovery["org_id"] != org_id or recovery["balance_cents"] <= 0:
                continue
            amount = min(remaining, recovery["balance_cents"])
            if amount > 0:
                planned_offsets.append({"recovery_id": recovery_id,
                                        "amount_cents": amount})
                remaining -= amount

        return self._emit("PAYMENT_BATCH_OPENED", org_id, {
            "batch_id": batch_id, "org_id": org_id, "lines": lines,
            "recoveries": planned_recoveries, "offsets": planned_offsets,
            "gross_cents": gross, "cash_cents": remaining,
        }, event_id or batch_id)

    def post_payment(self, actor: dict, *, batch_id: str) -> list[dict]:
        """拨付批次：逐行付款、登记追缴、执行抵扣、开具回执。

        每一步都是独立事件且幂等，崩溃后重入可从断点继续。
        """
        self._require(actor, "PAYER")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise StateError(f"批次 {batch_id} 不存在")
        events = []
        for line in batch["lines"]:
            if line["claim_id"] in batch["posted_lines"]:
                continue
            events.append(self._emit("PAYMENT_POSTED", batch["org_id"], {
                "batch_id": batch_id, "line_key": line["claim_id"],
                "amount_cents": line["net_cents"],
            }, f"pay-{batch_id}-{line['claim_id']}"))
        for planned in batch.get("recoveries", []):
            if planned["recovery_id"] in batch["posted_recoveries"]:
                continue
            events.append(self._emit("RECOVERY_POSTED", batch["org_id"], {
                **planned, "org_id": batch["org_id"], "batch_id": batch_id,
            }, planned["recovery_id"]))
        for planned in batch.get("offsets", []):
            if planned["recovery_id"] in batch["posted_offsets"]:
                continue
            events.append(self._emit("OFFSET_APPLIED", batch["org_id"], {
                "batch_id": batch_id, "recovery_id": planned["recovery_id"],
                "amount_cents": planned["amount_cents"],
            }, f"off-{batch_id}-{planned['recovery_id']}"))
        if not batch["receipt_issued"]:
            events.append(self._emit("RECEIPT_ISSUED", batch["org_id"], {
                "receipt_id": f"rcpt-{batch_id}", "org_id": batch["org_id"],
                "batch_id": batch_id, "receipt_kind": "payment",
                "amount_cents": batch["cash_cents"],
            }, f"rcpt-{batch_id}"))
        return events

    def post_recovery(self, actor: dict, *, org_id: str, amount_cents: int,
                      reason: str, claim_id: str | None = None,
                      recovery_id: str | None = None,
                      event_id: str | None = None) -> dict:
        """追缴登记（独立追缴，不依赖批次）。"""
        self._require(actor, "RECOVERER")
        recovery_id = recovery_id or self._next_id("rec")
        return self._emit("RECOVERY_POSTED", org_id, {
            "recovery_id": recovery_id, "org_id": org_id,
            "amount_cents": amount_cents, "reason": reason,
            "claim_id": claim_id, "batch_id": None,
        }, event_id or recovery_id)

    def mark_recovery_paid(self, actor: dict, *, recovery_id: str,
                           event_id: str | None = None) -> dict:
        """追缴到账：开具追缴回执并销账。"""
        self._require(actor, "RECOVERER")
        recovery = self.recoveries.get(recovery_id)
        if recovery is None:
            raise StateError(f"追缴 {recovery_id} 不存在")
        if recovery["balance_cents"] <= 0:
            raise StateError(f"追缴 {recovery_id} 已结清")
        return self._emit("RECEIPT_ISSUED", recovery["org_id"], {
            "receipt_id": f"rcpt-{recovery_id}", "org_id": recovery["org_id"],
            "recovery_id": recovery_id, "receipt_kind": "recovery",
            "amount_cents": recovery["balance_cents"],
        }, event_id or f"rcpt-{recovery_id}")

    # ------------------------------------------------------------------
    # 查询：季度对账 / 金额追溯 / 家长视图 / 待办
    # ------------------------------------------------------------------

    def _require_staff_or_org(self, actor: dict, org_id: str) -> None:
        if actor.get("role") in STAFF_ROLES:
            return
        if actor.get("role") == "ORG_OPERATOR" and actor.get("org_id") == org_id:
            return
        raise PermissionDenied("无权查看该机构的对账数据")

    def quarterly_report(self, actor: dict, *, org_id: str, quarter: str) -> dict:
        """季度对账：每月申报、调整、支付、追缴、回执的汇总与事件引用。"""
        self._require_staff_or_org(actor, org_id)
        months = []
        for month in months_of_quarter(quarter):
            claim = self._find_claim(org_id, month)
            if claim is None:
                months.append({"month": month, "claim_id": None})
                continue
            adjustments = [self.adjustments[aid] for aid in claim["adjustments"]]
            months.append({
                "month": month,
                "claim_id": claim["claim_id"],
                "status": claim["status"],
                "claimed_cents": sum(l["amount_cents"]
                                     for l in claim["lines"].values()),
                "reviewed_cents": sum(l["reviewed_amount_cents"] or 0
                                      for l in claim["lines"].values()
                                      if l["status"] == "approved"),
                "adjustment_cents": sum(claim["approved_delta"].values()),
                "settled_cents": claim["settled_cents"],
                "outstanding_cents": self._claim_due(claim) - claim["settled_cents"],
                "adjustments": [{
                    "adjustment_id": a["adjustment_id"], "reason": a["reason"],
                    "status": a["status"], "deltas": a["deltas"],
                } for a in adjustments],
            })
        batches = [{
            "batch_id": b["batch_id"], "status": b["status"],
            "gross_cents": b["gross_cents"], "cash_cents": b["cash_cents"],
            "lines": b["lines"], "offsets": b.get("offsets", []),
        } for b in sorted(self.batches.values(), key=lambda b: b["batch_id"])
            if b["org_id"] == org_id
            and any(l["month"] in months_of_quarter(quarter) for l in b["lines"])]
        recoveries = [{
            "recovery_id": r["recovery_id"], "amount_cents": r["amount_cents"],
            "balance_cents": r["balance_cents"], "status": r["status"],
            "reason": r["reason"],
        } for r in sorted(self.recoveries.values(),
                          key=lambda r: r["recovery_id"])
            if r["org_id"] == org_id]
        receipts = [dict(r) for r in self.receipts.values()
                    if r["org_id"] == org_id]
        return {
            "org_id": org_id, "quarter": quarter, "months": months,
            "batches": batches, "recoveries": recoveries, "receipts": receipts,
            "totals": {
                "claimed_cents": sum(m.get("claimed_cents", 0) for m in months),
                "settled_cents": sum(m.get("settled_cents", 0) for m in months),
                "outstanding_cents": sum(m.get("outstanding_cents", 0)
                                         for m in months),
                "recovery_balance_cents": sum(r["balance_cents"]
                                              for r in recoveries),
            },
        }

    def trace_amount(self, actor: dict, *, claim_id: str, child_id: str) -> dict:
        """从一笔金额追溯到政策、容量、出勤、调整与回执。"""
        claim = self._claim_or_raise(claim_id)
        self._require_staff_or_org(actor, claim["org_id"])
        line = claim["lines"].get(child_id)
        if line is None:
            raise StateError(f"申报 {claim_id} 没有儿童 {child_id} 的行")
        inputs = claim["inputs"]
        attendance = sorted(inputs.get("attendance", {}).get(child_id, {}))
        adjustments = [{
            "adjustment_id": a["adjustment_id"], "reason": a["reason"],
            "status": a["status"],
            "delta_cents": a["deltas"].get(child_id, 0),
        } for a in (self.adjustments[aid] for aid in claim["adjustments"])
            if child_id in a["deltas"] or any(
                u.get("child_id") == child_id for u in a["updates"])]
        payments = []
        for batch in self.batches.values():
            for batch_line in batch["lines"]:
                if batch_line["claim_id"] == claim_id:
                    payments.append({
                        "batch_id": batch["batch_id"],
                        "net_cents": batch_line["net_cents"],
                        "posted": claim_id in batch["posted_lines"],
                        "receipt_id": f"rcpt-{batch['batch_id']}"
                        if batch["receipt_issued"] else None,
                    })
        return {
            "claim_id": claim_id, "child_id": child_id,
            "month": claim["month"], "org_id": claim["org_id"],
            "line": self._public_line(line),
            "review_status": line["status"],
            "reviewed_amount_cents": line["reviewed_amount_cents"],
            "policy_ids": line["policy_ids"],
            "capacity": {"institution_type": inputs.get("institution_type"),
                         "slots": inputs.get("capacity", {})},
            "attendance_dates": attendance,
            "closure_dates": sorted(inputs.get("closures", [])),
            "withdrawal": inputs.get("withdrawals", {}).get(child_id),
            "adjustments": adjustments,
            "payments": payments,
        }

    def parent_view(self, actor: dict, *, parent_id: str) -> dict:
        """家长视图：仅可见自己孩子的月份金额与退费影响。"""
        if actor.get("role") == "PARENT":
            if actor.get("id") != parent_id:
                raise PermissionDenied("家长只能查看自己的关联儿童")
        elif actor.get("role") not in STAFF_ROLES:
            raise PermissionDenied("无权查看家长视图")
        children = sorted(self.parent_links.get(parent_id, set()))
        result = []
        for child_id in children:
            months = []
            for claim in sorted(self.claims.values(),
                                key=lambda c: (c["month"], c["claim_id"])):
                line = claim["lines"].get(child_id)
                if line is None:
                    continue
                if line["status"] == "approved":
                    amount = line["reviewed_amount_cents"]
                elif line["status"] == "rejected":
                    amount = 0
                else:
                    amount = line["amount_cents"]
                months.append({
                    "month": claim["month"], "org_id": claim["org_id"],
                    "status": line["status"],
                    "amount_cents": amount,
                    "adjustment_cents": claim["approved_delta"].get(child_id, 0),
                })
            result.append({
                "child_id": child_id, "months": months,
                "refunds": list(self.refunds.get(child_id, [])),
            })
        return {"parent_id": parent_id, "children": result}

    def pending_work(self) -> dict:
        """重启后的待办：未审完的申报、未拨完的批次、未结追缴、未结申诉。"""
        return {
            "claims_pending_review": sorted(
                c["claim_id"] for c in self.claims.values()
                if c["status"] == "frozen"),
            "open_batches": sorted(
                b["batch_id"] for b in self.batches.values()
                if b["status"] == "open"),
            "open_recoveries": sorted(
                r["recovery_id"] for r in self.recoveries.values()
                if r["balance_cents"] > 0),
            "open_appeals": sorted(
                a["appeal_id"] for a in self.appeals.values()
                if a["status"] == "open"),
            "pending_adjustments": sorted(
                a["adjustment_id"] for a in self.adjustments.values()
                if a["status"] == "pending"),
        }

    def _claim_or_raise(self, claim_id: str) -> dict:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise StateError(f"申报 {claim_id} 不存在")
        return claim
