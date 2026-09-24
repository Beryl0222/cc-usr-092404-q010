"""普惠托育补助清算账本的事件契约与仅追加存储。

账本采用事件溯源：所有事实（政策、容量、出勤、停园、退托、申报、审核、
支付、抵扣、追缴、回执）都以事件形式追加，任何金额都能沿事件链回溯。
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# 事件种类
# ---------------------------------------------------------------------------

EVENT_KINDS = [
    "POLICY_PUBLISHED",      # 政策版本发布（机构类型 × 年龄段 × 生效日）
    "CAPACITY_DECLARED",     # 机构月度名额声明
    "ATTENDANCE_RECORDED",   # 儿童出勤证据（签到）
    "CLOSURE_RECORDED",      # 临时停园（按日减免）
    "WITHDRAWAL_RECORDED",   # 退托（可跨月生效）
    "CHILD_PARENT_LINKED",   # 儿童与家长的可见性绑定
    "MONTH_CLAIMED",         # 月度申报提交（提交即冻结，含快照）
    "CLAIM_REVIEWED",        # 审核员逐行核定
    "APPEAL_OPENED",         # 机构申诉（仅冻结争议行）
    "APPEAL_RESOLVED",       # 申诉结案
    "ADJUSTMENT_APPENDED",   # 冻结后追加调整（归属原月份）
    "ADJUSTMENT_REVIEWED",   # 调整审核（通过时计算差额，入账待结算）
    "PAYMENT_BATCH_OPENED",  # 支付批次开启（含各行结算明细）
    "PAYMENT_POSTED",        # 批次内单行付款完成（不可改写）
    "OFFSET_APPLIED",        # 应付款抵扣应追回款
    "RECOVERY_POSTED",       # 追缴登记
    "RECEIPT_ISSUED",        # 回执（付款 / 追缴）
]

# 每种事件 payload 必须具备的字段（用于样例与交换格式核对）
PAYLOAD_REQUIRED_FIELDS = {
    "POLICY_PUBLISHED": ("policy_id", "version", "institution_type", "age_band",
                         "effective_from", "daily_rate_cents"),
    "CAPACITY_DECLARED": ("org_id", "month", "institution_type", "slots"),
    "ATTENDANCE_RECORDED": ("org_id", "child_id", "date", "age_band"),
    "CLOSURE_RECORDED": ("org_id", "date"),
    "WITHDRAWAL_RECORDED": ("org_id", "child_id", "effective_date"),
    "CHILD_PARENT_LINKED": ("child_id", "parent_id"),
    "MONTH_CLAIMED": ("claim_id", "org_id", "month", "version", "lines"),
    "CLAIM_REVIEWED": ("claim_id", "line_decisions"),
    "APPEAL_OPENED": ("appeal_id", "claim_id", "line_keys"),
    "APPEAL_RESOLVED": ("appeal_id", "claim_id", "line_decisions"),
    "ADJUSTMENT_APPENDED": ("adjustment_id", "claim_id", "attributed_month",
                            "reason", "updates"),
    "ADJUSTMENT_REVIEWED": ("adjustment_id", "claim_id", "approved", "deltas"),
    "PAYMENT_BATCH_OPENED": ("batch_id", "org_id", "lines"),
    "PAYMENT_POSTED": ("batch_id", "line_key", "amount_cents"),
    "OFFSET_APPLIED": ("batch_id", "recovery_id", "amount_cents"),
    "RECOVERY_POSTED": ("recovery_id", "org_id", "amount_cents", "reason"),
    "RECEIPT_ISSUED": ("receipt_id", "org_id", "amount_cents", "receipt_kind"),
}

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段，返回缺失/非法字段名列表。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in EVENT_KINDS:
        problems.append("kind")
        return problems
    payload = record.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload")
        return problems
    for name in PAYLOAD_REQUIRED_FIELDS[kind]:
        if name not in payload:
            problems.append(f"payload.{name}")
    return problems


# ---------------------------------------------------------------------------
# 仅追加事件存储（JSONL 持久化 + event_id 幂等）
# ---------------------------------------------------------------------------


class EventStore:
    """仅追加的事件存储。

    - 持久化为 JSONL，重启后 ``load`` 重放全部事件即可恢复状态；
    - 相同 ``event_id`` 重复追加是幂等的：返回已存在的事件，不重复计费。
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._events: list[dict] = []
        self._by_id: dict[str, dict] = {}
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._remember(json.loads(line))

    def _remember(self, event: dict) -> None:
        self._events.append(event)
        self._by_id[event["event_id"]] = event

    def append(self, event: dict) -> tuple[dict, bool]:
        """追加事件；返回 (事件, 是否为新事件)。重复 event_id 直接返回原事件。"""
        existing = self._by_id.get(event["event_id"])
        if existing is not None:
            return existing, False
        self._remember(event)
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event, True

    def get(self, event_id: str) -> dict | None:
        return self._by_id.get(event_id)

    def all(self) -> list[dict]:
        return list(self._events)
