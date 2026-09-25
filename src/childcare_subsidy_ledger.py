"""childcare_subsidy_ledger 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = [
    # 既有基础事件
    'POLICY_PUBLISHED', 'CAPACITY_DECLARED', 'MONTH_CLAIMED', 'PAYMENT_POSTED', 'RECOVERY_POSTED',
    # 跨月账本扩展事件
    'ATTENDANCE_RECORDED', 'CLOSURE_REPORTED', 'WITHDRAWAL_RECORDED',
    'CLAIM_FROZEN', 'CLAIM_ADJUSTED', 'CLAIM_AUDITED',
    'APPEAL_OPENED', 'APPEAL_RESOLVED',
    'PAYMENT_BATCH_OPENED', 'OFFSET_APPLIED', 'RECOVERY_COLLECTED',
]
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
