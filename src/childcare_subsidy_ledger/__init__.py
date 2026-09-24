"""普惠托育补助跨月清算账本。

- events：事件契约与仅追加存储（幂等、可重放）；
- compute：政策 × 容量 × 出勤的资格计算；
- ledger：命令、角色、并发控制与状态机。
"""

from .compute import compute_month, diff_lines, month_days, months_of_quarter, quarter_of
from .events import EVENT_KINDS, EventStore, validate_event
from .ledger import (
    Ledger,
    LedgerError,
    MaterialConflict,
    PermissionDenied,
    StateError,
    VersionConflict,
)

__all__ = [
    "EVENT_KINDS",
    "EventStore",
    "Ledger",
    "LedgerError",
    "MaterialConflict",
    "PermissionDenied",
    "StateError",
    "VersionConflict",
    "compute_month",
    "diff_lines",
    "month_days",
    "months_of_quarter",
    "quarter_of",
    "validate_event",
]
