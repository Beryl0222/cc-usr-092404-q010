"""资格计算引擎：政策 × 容量 × 出勤 → 每个儿童当月的可补助金额。

规则要点：
- 政策按（机构类型, 年龄段, 生效日）版本化，逐日匹配生效版本；
- 可补助人次由名额声明与出勤证据共同限定：先按出勤/停园减免逐日累计，
  再按机构当月该年龄段的名额上限截断（儿童 id 排序，确定性分配）；
- 同一儿童同一天多次签到只计一次（重复签到去重）；
- 停园日按政策 closure_relief_rate 计减免，且与出勤互斥；
- 退托生效日之后不再产生可补助天数（跨月退托自然归属到对应月份）。
"""

from __future__ import annotations

from datetime import date, timedelta

# 计算所需的输入集合；冻结时整体快照，调整时在其副本上重算，保证归属清晰。
INPUT_KEYS = ("attendance", "closures", "withdrawals", "capacity", "institution_type")


def month_days(month: str) -> list[str]:
    """返回 'YYYY-MM' 当月全部日期（ISO 字符串，升序）。"""
    year, mon = (int(part) for part in month.split("-"))
    first = date(year, mon, 1)
    last = date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)
    days, cur = [], first
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def quarter_of(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    return f"{year}-Q{(mon - 1) // 3 + 1}"


def months_of_quarter(quarter: str) -> list[str]:
    year, q = quarter.split("-Q")
    start = (int(q) - 1) * 3 + 1
    return [f"{year}-{m:02d}" for m in range(start, start + 3)]


def find_policy(policies: list[dict], institution_type: str, age_band: str,
                on_date: str) -> dict | None:
    """在生效日 <= on_date 的版本中取最新一版。"""
    best = None
    for pol in policies:
        if pol["institution_type"] != institution_type or pol["age_band"] != age_band:
            continue
        if pol["effective_from"] <= on_date and (
                best is None
                or pol["effective_from"] > best["effective_from"]
                or (pol["effective_from"] == best["effective_from"]
                    and pol["version"] > best["version"])):
            best = pol
    return best


def _sorted_children(attendance: dict, withdrawals: dict, month: str) -> list[str]:
    days = set(month_days(month))
    children = {cid for cid, dates in attendance.items() if set(dates) & days}
    children |= {cid for cid, eff in withdrawals.items() if eff[:7] == month}
    return sorted(children)


def compute_month(month: str, inputs: dict, policies: list[dict]) -> list[dict]:
    """对单个机构单月重算全部儿童的补助资格，返回确定排序的行列表。"""
    attendance = inputs.get("attendance", {})          # child_id -> set(date)
    closures = set(inputs.get("closures", ()))         # 停园日期
    withdrawals = inputs.get("withdrawals", {})        # child_id -> 生效日
    capacity = inputs.get("capacity", {})              # age_band -> 名额
    institution_type = inputs.get("institution_type")
    days = month_days(month)

    used_by_band: dict[str, int] = {}
    lines: list[dict] = []
    for child_id in _sorted_children(attendance, withdrawals, month):
        eff = withdrawals.get(child_id)
        per_day: list[tuple[str, str, int, str | None]] = []
        band_of_month: str | None = None
        for day in days:
            if eff is not None and day >= eff:
                continue  # 退托生效日起不再补助
            record = attendance.get(child_id, {}).get(day)
            on_closure = day in closures
            if record is None and not on_closure:
                continue
            band = record["age_band"] if record else band_of_month
            if band is None:
                continue  # 停园日无法确定年龄段（当月无任何出勤），不计
            band_of_month = band
            policy = find_policy(policies, institution_type, band, day)
            if policy is None:
                continue
            if record is not None:
                kind, rate = "attendance", policy["daily_rate_cents"]
            else:
                kind = "closure_relief"
                rate = policy["daily_rate_cents"] * policy.get("closure_relief_rate", 0) // 100
            per_day.append((day, kind, rate,
                            f"{policy['policy_id']}@v{policy['version']}"))

        if not per_day or band_of_month is None:
            continue
        # 名额截断：该年龄段当月剩余名额
        remaining = capacity.get(band_of_month, 0) - used_by_band.get(band_of_month, 0)
        counted = per_day[:max(remaining, 0)]
        used_by_band[band_of_month] = used_by_band.get(band_of_month, 0) + len(counted)

        attendance_days = sum(1 for _, kind, _, _ in counted if kind == "attendance")
        relief_days = sum(1 for _, kind, _, _ in counted if kind == "closure_relief")
        amount = sum(rate for _, _, rate, _ in counted)
        lines.append({
            "child_id": child_id,
            "age_band": band_of_month,
            "attendance_days": attendance_days,
            "relief_days": relief_days,
            "capped_days": len(per_day) - len(counted),
            "amount_cents": amount,
            "policy_ids": sorted({pid for _, _, _, pid in counted if pid}),
        })
    return lines


def diff_lines(old: list[dict], new: list[dict]) -> dict[str, int]:
    """逐儿童比较两次计算结果，返回 child_id -> 差额（分）。"""
    old_amounts = {line["child_id"]: line["amount_cents"] for line in old}
    new_amounts = {line["child_id"]: line["amount_cents"] for line in new}
    deltas: dict[str, int] = {}
    for child_id in sorted(set(old_amounts) | set(new_amounts)):
        delta = new_amounts.get(child_id, 0) - old_amounts.get(child_id, 0)
        if delta:
            deltas[child_id] = delta
    return deltas
