"""跨月清算账本。

在既有政策、容量、月度申报、支付与追缴事件之上，维护一个只可追加的事件账本：

- 政策按机构类型、年龄段与生效日版本化，逐日按生效版本计价；
- 名额声明与出勤证据共同限定可补助人次，重复签到按日期去重；
- 临时停园、跨月退托、后补证明分别作为独立事件归属到对应月份；
- 月度申报冻结后仅接受追加调整，已支付差异形成下一期抵扣或追缴，不改写原付款；
- 机构申诉期间只冻结争议行，其余家庭照常结算；
- 审核、付款、追缴由不同角色执行，家长仅可见自身子女的月份与退费影响；
- 相同材料凭幂等键重放不重复计费，并发申报以版本号冲突；
- 事件落盘后重启可续办未完成的审核与支付批次；
- 季度对账可从每笔金额追到政策版本、名额声明、出勤证据、调整与回执。

金额一律以“分”为单位的整数表示，避免浮点误差。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .childcare_subsidy_ledger import EVENT_KINDS


class Role:
    """清算平台参与角色；审核、付款与追缴必须分离。"""

    POLICY_ADMIN = "POLICY_ADMIN"
    ORG = "ORG"
    AUDITOR = "AUDITOR"
    PAYER = "PAYER"
    RECOVERER = "RECOVERER"
    PARENT = "PARENT"


class LedgerError(Exception):
    """账本领域错误基类。"""


class PermissionDenied(LedgerError):
    """角色不具备执行该操作的权限。"""


class VersionConflict(LedgerError):
    """申报版本与预期不一致，存在并发修改。"""


class StateError(LedgerError):
    """当前状态不允许该操作。"""


def _month_of(day: str) -> str:
    return day[:7]


def _expand_dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Ledger:
    """只可追加的跨月清算账本；全部状态均可由事件日志重放得到。"""

    def __init__(self, log_path: str | Path | None = None):
        self.log_path = Path(log_path) if log_path is not None else None
        self.events: list[dict] = []
        self._event_ids: set[str] = set()
        self._idempotency: dict[str, str] = {}

        self.policies: dict[tuple[str, str], list[dict]] = {}
        self.orgs: dict[str, str] = {}  # org_id -> org_type
        self.capacity: dict[tuple[str, str, str], list[dict]] = {}
        self.attendance: dict[tuple[str, str], dict[str, dict]] = {}
        self.closures: dict[str, list[dict]] = {}
        self.withdrawals: dict[str, str] = {}  # child_id -> 退托生效日
        self.claims: dict[tuple[str, str], dict] = {}
        self.claim_versions: dict[tuple[str, str], int] = {}
        self.appeals: dict[str, dict] = {}
        self.frozen_lines: set[tuple[str, str, str]] = set()
        self.batches: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.recoveries: dict[str, dict] = {}
        self.offsets: list[dict] = []

        if self.log_path and self.log_path.exists():
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._append(json.loads(line), persist=False)

    # ---------- 事件基座 ----------

    def _append(self, event: dict, persist: bool = True) -> dict:
        event_id = event["event_id"]
        if event_id in self._event_ids:  # 相同材料重放不重复计费
            return event
        if event["kind"] not in EVENT_KINDS:
            raise LedgerError(f"未知事件种类: {event['kind']}")
        self._event_ids.add(event_id)
        self.events.append(event)
        key = event.get("idempotency_key")
        if key:
            self._idempotency[key] = event_id
        self._apply(event)
        if persist and self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def _emit(self, kind: str, *, actor_role: str, actor_id: str, subject_id: str,
              payload: dict, idempotency_key: str | None = None,
              occurred_at: str | None = None) -> dict:
        event = {
            "event_id": _new_id(),
            "kind": kind,
            "occurred_at": occurred_at or _now(),
            "subject_id": subject_id,
            "actor": {"role": actor_role, "id": actor_id},
            "payload": payload,
        }
        if idempotency_key is not None:
            event["idempotency_key"] = idempotency_key
        return self._append(event)

    @staticmethod
    def _require(role: str, allowed: tuple[str, ...], action: str) -> None:
        if role not in allowed:
            raise PermissionDenied(f"{action}需要角色 {'/'.join(allowed)}，实际为 {role}")

    def _replayed(self, idempotency_key: str | None) -> dict | None:
        if idempotency_key is not None and idempotency_key in self._idempotency:
            return {"event_id": self._idempotency[idempotency_key], "replayed": True}
        return None

    def _apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['kind'].lower()}", None)
        if handler is None:
            raise LedgerError(f"缺少事件处理器: {event['kind']}")
        handler(event)

    # ---------- 事件处理器（只修改状态，不做权限判断） ----------

    def _on_policy_published(self, event: dict) -> None:
        p = event["payload"]
        versions = self.policies.setdefault((p["org_type"], p["age_band"]), [])
        versions.append({**p, "event_id": event["event_id"]})
        versions.sort(key=lambda v: v["effective_from"])

    def _on_capacity_declared(self, event: dict) -> None:
        p = event["payload"]
        self.orgs.setdefault(p["org_id"], p["org_type"])
        self.capacity.setdefault((p["org_id"], p["month"], p["age_band"]), []).append({
            "event_id": event["event_id"],
            "slots": p["slots"],
            "occurred_at": event["occurred_at"],
        })

    def _record_attendance(self, org_id: str, child_id: str, dates: list[str], evidence_id: str) -> None:
        for day in dates:
            per_child = self.attendance.setdefault((org_id, _month_of(day)), {}).setdefault(
                child_id, {"dates": set(), "evidence": set()})
            per_child["dates"].add(day)  # 重复签到按日期自然去重
            per_child["evidence"].add(evidence_id)

    def _on_attendance_recorded(self, event: dict) -> None:
        p = event["payload"]
        self._record_attendance(p["org_id"], p["child_id"], p["dates"], p["evidence_id"])

    def _on_closure_reported(self, event: dict) -> None:
        p = event["payload"]
        self.closures.setdefault(p["org_id"], []).append({
            "event_id": event["event_id"],
            "start": p["start_date"],
            "end": p["end_date"],
            "reason": p.get("reason", ""),
        })

    def _on_withdrawal_recorded(self, event: dict) -> None:
        p = event["payload"]
        self.withdrawals[p["child_id"]] = p["effective_date"]

    def _on_month_claimed(self, event: dict) -> None:
        p = event["payload"]
        key = (p["org_id"], p["month"])
        self.orgs.setdefault(p["org_id"], p["org_type"])
        claim = self.claims.setdefault(key, {"adjustments": [], "audits": []})
        claim.update({
            "org_id": p["org_id"],
            "month": p["month"],
            "org_type": p["org_type"],
            "lines": {line["child_id"]: dict(line) for line in p["lines"]},
            "status": "SUBMITTED",
            "audit": None,  # 重新申报后须重新审核
        })
        self.claim_versions[key] = self.claim_versions.get(key, 0) + 1

    def _on_claim_frozen(self, event: dict) -> None:
        p = event["payload"]
        key = (p["org_id"], p["month"])
        self.claims[key]["status"] = "FROZEN"
        self.claim_versions[key] += 1

    def _on_claim_adjusted(self, event: dict) -> None:
        p = event["payload"]
        key = (p["org_id"], p["month"])
        claim = self.claims[key]
        for line in p.get("lines", []):
            claim["lines"][line["child_id"]] = dict(line)
        for proof in p.get("evidence", []):  # 后补证明归入对应月份的出勤证据
            self._record_attendance(p["org_id"], proof["child_id"], proof["dates"], proof["evidence_id"])
        claim["adjustments"].append({"event_id": event["event_id"], "reason": p.get("reason", "")})
        claim["audit"] = None  # 调整后须重新审核
        self.claim_versions[key] += 1

    def _on_claim_audited(self, event: dict) -> None:
        p = event["payload"]
        key = (p["org_id"], p["month"])
        snapshot = {**p, "event_id": event["event_id"]}
        self.claims[key]["audit"] = snapshot
        self.claims[key]["audits"].append(snapshot)

    def _on_appeal_opened(self, event: dict) -> None:
        p = event["payload"]
        self.appeals[p["appeal_id"]] = {**p, "status": "OPEN", "event_id": event["event_id"]}
        self.frozen_lines.add((p["org_id"], p["month"], p["child_id"]))

    def _on_appeal_resolved(self, event: dict) -> None:
        p = event["payload"]
        appeal = self.appeals[p["appeal_id"]]
        appeal["status"] = "RESOLVED"
        appeal["resolution"] = p.get("resolution", "")
        self.frozen_lines.discard((appeal["org_id"], appeal["month"], appeal["child_id"]))

    def _on_payment_batch_opened(self, event: dict) -> None:
        p = event["payload"]
        self.batches[p["batch_id"]] = {
            "items": [(item["org_id"], item["month"]) for item in p["items"]],
            "posted": set(),
            "event_id": event["event_id"],
        }

    def _on_offset_applied(self, event: dict) -> None:
        p = event["payload"]
        self.offsets.append({**p, "event_id": event["event_id"]})
        self.recoveries[p["recovery_id"]]["remaining"] -= p["amount"]

    def _on_payment_posted(self, event: dict) -> None:
        p = event["payload"]
        self.payments[p["payment_id"]] = {**p, "event_id": event["event_id"]}
        batch = self.batches.get(p["batch_id"])
        if batch is not None:
            batch["posted"].add((p["org_id"], p["month"]))

    def _on_recovery_posted(self, event: dict) -> None:
        p = event["payload"]
        self.recoveries[p["recovery_id"]] = {
            **p, "remaining": p["amount"], "status": "OPEN", "event_id": event["event_id"],
        }

    def _on_recovery_collected(self, event: dict) -> None:
        recovery = self.recoveries[event["payload"]["recovery_id"]]
        recovery["remaining"] = 0
        recovery["status"] = "COLLECTED"

    # ---------- 内部查询 ----------

    def _closure_dates(self, org_id: str) -> set[str]:
        dates: set[str] = set()
        for closure in self.closures.get(org_id, []):
            dates.update(_expand_dates(closure["start"], closure["end"]))
        return dates

    def _capacity_slots(self, org_id: str, month: str, age_band: str) -> tuple[int, str | None]:
        declarations = self.capacity.get((org_id, month, age_band), [])
        if not declarations:
            return 0, None  # 未声明名额则当月该年龄段不可补助
        latest = declarations[-1]
        return latest["slots"], latest["event_id"]

    def _policy_on(self, org_type: str, age_band: str, day: str) -> dict:
        versions = [v for v in self.policies.get((org_type, age_band), [])
                    if v["effective_from"] <= day and (not v.get("effective_to") or day <= v["effective_to"])]
        if not versions:
            raise StateError(f"{org_type}/{age_band} 在 {day} 缺少生效政策")
        return versions[-1]

    def _settled_total(self, org_id: str, month: str) -> int:
        return sum(p["gross"] for p in self.payments.values()
                   if p["org_id"] == org_id and p["month"] == month)

    def _payable_now(self, org_id: str, month: str) -> int:
        """最新审核口径下的当前可付金额（动态扣除申诉冻结行）。"""
        audit = self.claims[(org_id, month)]["audit"]
        held = sum(info["amount"] for child, info in audit["lines"].items()
                   if (org_id, month, child) in self.frozen_lines)
        return audit["amount"] - held

    def _fresh_audit(self, org_id: str, month: str) -> dict:
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is None:
            raise StateError(f"{org_id}/{month} 无申报")
        audit = claim["audit"]
        if audit is None or audit["claim_version"] != self.claim_versions[key]:
            raise StateError(f"{org_id}/{month} 审核缺失或已过期，需重新审核")
        return audit

    def _check_org_type(self, org_id: str, org_type: str) -> None:
        known = self.orgs.get(org_id)
        if known is not None and known != org_type:
            raise StateError(f"机构 {org_id} 类型已登记为 {known}，与 {org_type} 不一致")

    # ---------- 政策与名额 ----------

    def publish_policy(self, actor_role: str, actor_id: str, *, org_type: str, age_band: str,
                       daily_rate_cents: int, effective_from: str, effective_to: str | None = None,
                       idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.POLICY_ADMIN,), "发布政策")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        if daily_rate_cents <= 0:
            raise LedgerError("补助标准必须为正数（分）")
        return self._emit("POLICY_PUBLISHED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_type}/{age_band}",
                          payload={"policy_id": _new_id(), "org_type": org_type, "age_band": age_band,
                                   "daily_rate_cents": daily_rate_cents,
                                   "effective_from": effective_from, "effective_to": effective_to},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def declare_capacity(self, actor_role: str, actor_id: str, *, org_id: str, org_type: str,
                         month: str, age_band: str, slots: int,
                         idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "声明名额")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        self._check_org_type(org_id, org_type)
        if slots < 0:
            raise LedgerError("名额必须为非负整数")
        return self._emit("CAPACITY_DECLARED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}",
                          payload={"org_id": org_id, "org_type": org_type, "month": month,
                                   "age_band": age_band, "slots": slots},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    # ---------- 出勤、停园与退托 ----------

    def record_attendance(self, actor_role: str, actor_id: str, *, org_id: str, child_id: str,
                          dates: list[str], evidence_id: str,
                          idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "登记出勤")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        if not dates:
            raise LedgerError("出勤日期不能为空")
        return self._emit("ATTENDANCE_RECORDED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{child_id}",
                          payload={"org_id": org_id, "child_id": child_id,
                                   "dates": sorted(dates), "evidence_id": evidence_id},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def report_closure(self, actor_role: str, actor_id: str, *, org_id: str,
                       start_date: str, end_date: str, reason: str = "",
                       idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "报备停园")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        if start_date > end_date:
            raise LedgerError("停园起日不能晚于止日")
        return self._emit("CLOSURE_REPORTED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=org_id,
                          payload={"org_id": org_id, "start_date": start_date,
                                   "end_date": end_date, "reason": reason},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def record_withdrawal(self, actor_role: str, actor_id: str, *, org_id: str, child_id: str,
                          effective_date: str,
                          idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "登记退托")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        return self._emit("WITHDRAWAL_RECORDED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{child_id}",
                          payload={"org_id": org_id, "child_id": child_id,
                                   "effective_date": effective_date},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    # ---------- 月度申报 ----------

    @staticmethod
    def _validate_lines(lines: list[dict]) -> None:
        seen: set[str] = set()
        for line in lines:
            for field in ("child_id", "age_band", "days", "guardian_id"):
                if field not in line:
                    raise LedgerError(f"申报行缺少字段 {field}")
            if line["child_id"] in seen:
                raise LedgerError(f"申报行儿童重复: {line['child_id']}")
            seen.add(line["child_id"])
            if not isinstance(line["days"], int) or line["days"] < 0:
                raise LedgerError("申报天数必须为非负整数")

    def _check_version(self, key: tuple[str, str], expected_version: int) -> None:
        current = self.claim_versions.get(key, 0)
        if expected_version != current:
            raise VersionConflict(f"申报 {key[0]}/{key[1]} 版本冲突：期望 {expected_version}，当前 {current}")

    def submit_claim(self, actor_role: str, actor_id: str, *, org_id: str, org_type: str,
                     month: str, lines: list[dict], expected_version: int = 0,
                     idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "提交申报")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is not None and claim["status"] == "FROZEN":
            raise StateError("申报已冻结，仅接受追加调整")
        self._check_version(key, expected_version)
        self._check_org_type(org_id, org_type)
        self._validate_lines(lines)
        return self._emit("MONTH_CLAIMED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}",
                          payload={"org_id": org_id, "org_type": org_type, "month": month,
                                   "lines": [dict(line) for line in lines]},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def freeze_claim(self, actor_role: str, actor_id: str, *, org_id: str, month: str,
                     idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "冻结申报")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is None:
            raise StateError(f"{org_id}/{month} 无申报")
        if claim["status"] == "FROZEN":
            raise StateError("申报已冻结")
        return self._emit("CLAIM_FROZEN", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}",
                          payload={"org_id": org_id, "month": month},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def append_adjustment(self, actor_role: str, actor_id: str, *, org_id: str, month: str,
                          reason: str, expected_version: int,
                          lines: list[dict] | None = None, evidence: list[dict] | None = None,
                          idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        """冻结后唯一的变更入口：追加调整行或后补出勤证明。"""
        self._require(actor_role, (Role.ORG,), "追加调整")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is None:
            raise StateError(f"{org_id}/{month} 无申报")
        if claim["status"] != "FROZEN":
            raise StateError("未冻结的申报可直接重新提交，无需追加调整")
        self._check_version(key, expected_version)
        if lines:
            self._validate_lines(lines)
        if not lines and not evidence:
            raise LedgerError("调整内容不能为空")
        return self._emit("CLAIM_ADJUSTED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}",
                          payload={"org_id": org_id, "month": month, "reason": reason,
                                   "lines": [dict(line) for line in lines or []],
                                   "evidence": [dict(item) for item in evidence or []]},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    # ---------- 审核 ----------

    def audit_claim(self, actor_role: str, actor_id: str, *, org_id: str, month: str,
                    idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.AUDITOR,), "审核申报")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is None:
            raise StateError(f"{org_id}/{month} 无申报")
        if not claim["lines"]:
            raise StateError("申报没有可审核的行")
        snapshot = self._build_snapshot(key)
        snapshot["auditor"] = actor_id
        return self._emit("CLAIM_AUDITED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}", payload=snapshot,
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def _build_snapshot(self, key: tuple[str, str]) -> dict:
        org_id, month = key
        claim = self.claims[key]
        org_type = claim["org_type"]
        closure_dates = self._closure_dates(org_id)
        lines: dict[str, dict] = {}
        for child_id, line in sorted(claim["lines"].items()):
            attendance = self.attendance.get(key, {}).get(child_id, {"dates": set(), "evidence": set()})
            withdrawal = self.withdrawals.get(child_id)
            eligible = [day for day in sorted(attendance["dates"])
                        if day not in closure_dates and (withdrawal is None or day < withdrawal)]
            eligible = eligible[: line["days"]]  # 出勤证据限定可补助天数
            lines[child_id] = {
                "age_band": line["age_band"],
                "guardian_id": line["guardian_id"],
                "claimed_days": line["days"],
                "eligible_dates": eligible,
                "evidence": sorted(attendance["evidence"]),
                "held": (org_id, month, child_id) in self.frozen_lines,
                "capacity_event": None,
            }
        # 名额声明按年龄段限定可补助人数，超出部分按儿童编号排序截断，保证结果确定
        by_band: dict[str, list[str]] = {}
        for child_id, info in lines.items():
            if info["eligible_dates"]:
                by_band.setdefault(info["age_band"], []).append(child_id)
        for band, children in by_band.items():
            slots, capacity_event = self._capacity_slots(org_id, month, band)
            for child_id in children:
                lines[child_id]["capacity_event"] = capacity_event
            for child_id in sorted(children)[slots:]:
                lines[child_id]["eligible_dates"] = []
                lines[child_id]["capacity_excluded"] = True
        # 逐日按生效政策版本计价
        total = 0
        for info in lines.values():
            amount = 0
            policy_events: set[str] = set()
            for day in info["eligible_dates"]:
                version = self._policy_on(org_type, info["age_band"], day)
                amount += version["daily_rate_cents"]
                policy_events.add(version["event_id"])
            info["amount"] = amount
            info["eligible_days"] = len(info["eligible_dates"])
            info["policy_events"] = sorted(policy_events)
            total += amount
        held = sum(info["amount"] for info in lines.values() if info["held"])
        return {
            "org_id": org_id,
            "month": month,
            "claim_version": self.claim_versions[key],
            "lines": lines,
            "amount": total,
            "held_amount": held,
            "payable": total - held,
        }

    # ---------- 申诉 ----------

    def open_appeal(self, actor_role: str, actor_id: str, *, org_id: str, month: str,
                    child_id: str, reason: str,
                    idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.ORG,), "发起申诉")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        claim = self.claims.get((org_id, month))
        if claim is None or child_id not in claim["lines"]:
            raise StateError(f"{org_id}/{month} 无儿童 {child_id} 的申报行")
        for appeal in self.appeals.values():
            if (appeal["org_id"], appeal["month"], appeal["child_id"]) == (org_id, month, child_id) \
                    and appeal["status"] == "OPEN":
                raise StateError("该行已有进行中的申诉")
        return self._emit("APPEAL_OPENED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}/{child_id}",
                          payload={"appeal_id": _new_id(), "org_id": org_id, "month": month,
                                   "child_id": child_id, "reason": reason},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def resolve_appeal(self, actor_role: str, actor_id: str, *, appeal_id: str, resolution: str = "",
                       idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.AUDITOR,), "办结申诉")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        appeal = self.appeals.get(appeal_id)
        if appeal is None:
            raise StateError(f"申诉不存在: {appeal_id}")
        if appeal["status"] != "OPEN":
            raise StateError("申诉已办结")
        return self._emit("APPEAL_RESOLVED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=appeal_id,
                          payload={"appeal_id": appeal_id, "resolution": resolution},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    # ---------- 支付、抵扣与追缴 ----------

    def open_batch(self, actor_role: str, actor_id: str, *, items: list[dict],
                   batch_id: str | None = None,
                   idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.PAYER,), "开立支付批次")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        if not items:
            raise LedgerError("支付批次不能为空")
        for item in items:
            if (item["org_id"], item["month"]) not in self.claims:
                raise StateError(f"{item['org_id']}/{item['month']} 无申报")
        return self._emit("PAYMENT_BATCH_OPENED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=batch_id or "batch",
                          payload={"batch_id": batch_id or _new_id(),
                                   "items": [dict(item) for item in items]},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def post_payment(self, actor_role: str, actor_id: str, *, batch_id: str,
                     org_id: str, month: str, receipt_id: str | None = None,
                     idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        """按最新审核结果付款；先以未结追缴抵扣，差额形成新的付款，不改写历史付款。"""
        self._require(actor_role, (Role.PAYER,), "支付付款")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        batch = self.batches.get(batch_id)
        if batch is None:
            raise StateError(f"支付批次不存在: {batch_id}")
        if (org_id, month) not in batch["items"]:
            raise StateError("批次不包含该申报")
        if (org_id, month) in batch["posted"]:
            raise StateError("该申报已在此批次支付")
        self._fresh_audit(org_id, month)
        payable = self._payable_now(org_id, month) - self._settled_total(org_id, month)
        if payable < 0:
            raise StateError("已超额支付，差额须通过追缴处理")
        if payable == 0:
            raise StateError("无可支付金额")
        payment_id = _new_id()
        offsets = []
        cash = payable
        for recovery in self.recoveries.values():  # 按入账顺序抵扣未结追缴
            if recovery["org_id"] != org_id or recovery["remaining"] <= 0:
                continue
            take = min(cash, recovery["remaining"])
            offsets.append(self._emit(
                "OFFSET_APPLIED", actor_role=actor_role, actor_id=actor_id,
                subject_id=f"{org_id}/{month}",
                payload={"offset_id": _new_id(), "payment_id": payment_id,
                         "recovery_id": recovery["recovery_id"], "org_id": org_id, "amount": take},
                occurred_at=occurred_at))
            cash -= take
            if cash == 0:
                break
        payment = self._emit("PAYMENT_POSTED", actor_role=actor_role, actor_id=actor_id,
                             subject_id=f"{org_id}/{month}",
                             payload={"payment_id": payment_id, "batch_id": batch_id,
                                      "org_id": org_id, "month": month,
                                      "gross": payable, "offset_total": payable - cash,
                                      "amount": cash, "receipt_id": receipt_id or _new_id(),
                                      "audit_event": self.claims[(org_id, month)]["audit"]["event_id"],
                                      "claim_version": self.claim_versions[(org_id, month)]},
                             idempotency_key=idempotency_key, occurred_at=occurred_at)
        return {"payment": payment, "offsets": offsets}

    def post_recovery(self, actor_role: str, actor_id: str, *, org_id: str, month: str,
                      reason: str = "",
                      idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        """已支付大于应支付时形成追缴；追缴只追加事件，不改写原付款。"""
        self._require(actor_role, (Role.RECOVERER,), "登记追缴")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        self._fresh_audit(org_id, month)
        overpaid = self._settled_total(org_id, month) - self._payable_now(org_id, month)
        posted = sum(r["amount"] for r in self.recoveries.values()
                     if r["org_id"] == org_id and r["month"] == month)
        amount = overpaid - posted
        if amount <= 0:
            raise StateError("无待追缴差额")
        return self._emit("RECOVERY_POSTED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=f"{org_id}/{month}",
                          payload={"recovery_id": _new_id(), "org_id": org_id, "month": month,
                                   "amount": amount, "reason": reason,
                                   "audit_event": self.claims[(org_id, month)]["audit"]["event_id"]},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    def collect_recovery(self, actor_role: str, actor_id: str, *, recovery_id: str,
                         idempotency_key: str | None = None, occurred_at: str | None = None) -> dict:
        self._require(actor_role, (Role.RECOVERER,), "核销追缴")
        hit = self._replayed(idempotency_key)
        if hit:
            return hit
        recovery = self.recoveries.get(recovery_id)
        if recovery is None:
            raise StateError(f"追缴不存在: {recovery_id}")
        if recovery["remaining"] <= 0:
            raise StateError("追缴已结清")
        return self._emit("RECOVERY_COLLECTED", actor_role=actor_role, actor_id=actor_id,
                          subject_id=recovery_id,
                          payload={"recovery_id": recovery_id},
                          idempotency_key=idempotency_key, occurred_at=occurred_at)

    # ---------- 只读视图 ----------

    def claim_summary(self, org_id: str, month: str) -> dict:
        key = (org_id, month)
        claim = self.claims.get(key)
        if claim is None:
            raise StateError(f"{org_id}/{month} 无申报")
        audit = claim["audit"]
        settled = self._settled_total(org_id, month)
        return {
            "status": claim["status"],
            "version": self.claim_versions[key],
            "entitlement": self._payable_now(org_id, month) if audit else None,
            "settled": settled,
            "balance": (self._payable_now(org_id, month) - settled) if audit else None,
            "payments": [p for p in self.payments.values()
                         if p["org_id"] == org_id and p["month"] == month],
        }

    def parent_statement(self, actor_role: str, actor_id: str, *, child_id: str) -> list[dict]:
        """家长视图：仅返回本人子女的月份、可补助天数与退费影响。"""
        self._require(actor_role, (Role.PARENT,), "家长查询")
        entries = []
        for (org_id, month), claim in sorted(self.claims.items()):
            line = claim["lines"].get(child_id)
            if line is None:
                continue
            if line["guardian_id"] != actor_id:
                raise PermissionDenied("仅可查看本人子女的月份与退费影响")
            entry = {
                "org_id": org_id,
                "month": month,
                "status": claim["status"],
                "claimed_days": line["days"],
                "eligible_days": None,
                "amount": None,
                "held": (org_id, month, child_id) in self.frozen_lines,
                "recoveries": [],
            }
            audit = claim["audit"]
            if audit is not None:
                snapshot_line = audit["lines"].get(child_id, {})
                entry["eligible_days"] = snapshot_line.get("eligible_days")
                entry["amount"] = snapshot_line.get("amount")
                for recovery in self.recoveries.values():
                    if recovery["org_id"] == org_id and recovery["month"] == month:
                        entry["recoveries"].append({
                            "recovery_id": recovery["recovery_id"],
                            "amount": self._child_recovery_share(org_id, month, child_id, recovery),
                            "status": recovery["status"],
                        })
            entries.append(entry)
        return entries

    def _child_recovery_share(self, org_id: str, month: str, child_id: str, recovery: dict) -> int:
        """把追缴金额归属到具体儿童：按“已付份额 − 当前应补份额”在儿童间分摊。"""
        claim = self.claims[(org_id, month)]
        audit = claim["audit"]
        if audit is None:
            return 0
        paid_events = [p["audit_event"] for p in self.payments.values()
                       if p["org_id"] == org_id and p["month"] == month]
        if not paid_events:
            return 0
        paid_audit = next((a for a in claim["audits"] if a["event_id"] == paid_events[-1]), None)
        if paid_audit is None or not paid_audit["amount"]:
            return 0
        settled = self._settled_total(org_id, month)

        def overpaid(child: str) -> int:
            paid_line = paid_audit["lines"].get(child, {}).get("amount", 0)
            paid_share = settled * paid_line // paid_audit["amount"]
            entitled = audit["lines"].get(child, {}).get("amount", 0)
            return max(0, paid_share - entitled)

        total = sum(overpaid(child) for child in paid_audit["lines"])
        if total <= 0:
            return 0
        return recovery["amount"] * overpaid(child_id) // total

    def pending_work(self) -> dict:
        """重启后续办清单：待审核申报、未完成支付批次、未结追缴。"""
        return {
            "claims_pending_audit": sorted(
                f"{org_id}/{month}"
                for (org_id, month), claim in self.claims.items()
                if claim["audit"] is None
                or claim["audit"]["claim_version"] != self.claim_versions[(org_id, month)]),
            "open_batches": [
                {"batch_id": batch_id,
                 "remaining": sorted(f"{org_id}/{month}" for (org_id, month) in batch["items"]
                                     if (org_id, month) not in batch["posted"])}
                for batch_id, batch in self.batches.items()
                if any(item not in batch["posted"] for item in batch["items"])],
            "open_recoveries": [
                {"recovery_id": recovery_id, "org_id": recovery["org_id"],
                 "remaining": recovery["remaining"]}
                for recovery_id, recovery in self.recoveries.items()
                if recovery["remaining"] > 0],
        }

    def reconcile_quarter(self, year: int, quarter: int) -> dict:
        """季度对账：每笔金额均可追到政策版本、名额声明、出勤证据、调整与回执。"""
        months = {f"{year}-{m:02d}" for m in range((quarter - 1) * 3 + 1, (quarter - 1) * 3 + 4)}
        payments = []
        for payment in self.payments.values():
            if payment["month"] not in months:
                continue
            claim = self.claims[(payment["org_id"], payment["month"])]
            audit = next((a for a in claim["audits"] if a["event_id"] == payment["audit_event"]),
                         claim["audit"])
            payments.append({
                "payment_id": payment["payment_id"],
                "batch_id": payment["batch_id"],
                "org_id": payment["org_id"],
                "month": payment["month"],
                "gross": payment["gross"],
                "offset_total": payment["offset_total"],
                "amount": payment["amount"],
                "receipt_id": payment["receipt_id"],
                "audit_event": payment["audit_event"],
                "lines": audit["lines"],
                "adjustments": [a["event_id"] for a in claim["adjustments"]],
                "offsets": [o for o in self.offsets if o["payment_id"] == payment["payment_id"]],
                "recoveries": [r for r in self.recoveries.values()
                               if r["org_id"] == payment["org_id"] and r["month"] == payment["month"]],
            })
        recoveries = [r for r in self.recoveries.values() if r["month"] in months]
        return {
            "year": year,
            "quarter": quarter,
            "months": sorted(months),
            "payments": payments,
            "recoveries": recoveries,
            "totals": {
                "gross": sum(p["gross"] for p in payments),
                "offset": sum(p["offset_total"] for p in payments),
                "cash": sum(p["amount"] for p in payments),
                "recovered": sum(r["amount"] for r in recoveries),
                "recovery_outstanding": sum(r["remaining"] for r in recoveries),
            },
        }
