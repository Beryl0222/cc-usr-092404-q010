import tempfile
import unittest
from pathlib import Path

from src.ledger import Ledger, PermissionDenied, Role, StateError, VersionConflict

ORG_TYPE = "民办普惠园"
BAND = "托小班"
RATE = 8000  # 每日 80 元，单位：分


def make_ledger(path=None):
    ledger = Ledger(path)
    ledger.publish_policy(Role.POLICY_ADMIN, "admin", org_type=ORG_TYPE, age_band=BAND,
                          daily_rate_cents=RATE, effective_from="2026-07-01")
    return ledger


def declare(ledger, month, slots=2, org="org-1"):
    return ledger.declare_capacity(Role.ORG, "staff-1", org_id=org, org_type=ORG_TYPE,
                                   month=month, age_band=BAND, slots=slots)


def attend(ledger, child, dates, evidence=None, org="org-1", **kwargs):
    return ledger.record_attendance(Role.ORG, "staff-1", org_id=org, child_id=child,
                                    dates=dates, evidence_id=evidence or f"ev-{child}-{min(dates)}",
                                    **kwargs)


def line(child, days, guardian=None):
    return {"child_id": child, "age_band": BAND, "days": days,
            "guardian_id": guardian or f"g-{child}"}


def submit(ledger, month, lines, org="org-1", expected_version=0, **kwargs):
    return ledger.submit_claim(Role.ORG, "staff-1", org_id=org, org_type=ORG_TYPE,
                               month=month, lines=lines,
                               expected_version=expected_version, **kwargs)


def audit(ledger, month, org="org-1"):
    return ledger.audit_claim(Role.AUDITOR, "aud-1", org_id=org, month=month)


def pay(ledger, batch, org, month, **kwargs):
    return ledger.post_payment(Role.PAYER, "pay-1", batch_id=batch, org_id=org, month=month, **kwargs)


def july_claim(ledger, children):
    """7 月申报、审核、批次的一条龙准备。"""
    declare(ledger, "2026-07")
    submit(ledger, "2026-07", [line(child, days) for child, days in children])
    audit(ledger, "2026-07")
    ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1",
                      items=[{"org_id": "org-1", "month": "2026-07"}])


class PolicyVersionTest(unittest.TestCase):
    def test_policy_versioned_by_org_type_band_and_effective_date(self):
        ledger = make_ledger()
        ledger.publish_policy(Role.POLICY_ADMIN, "admin", org_type=ORG_TYPE, age_band=BAND,
                              daily_rate_cents=9000, effective_from="2026-08-01")
        declare(ledger, "2026-07")
        declare(ledger, "2026-08")
        attend(ledger, "c1", ["2026-07-08", "2026-07-09"])
        attend(ledger, "c1", ["2026-08-03", "2026-08-04"])
        submit(ledger, "2026-07", [line("c1", 2)])
        submit(ledger, "2026-08", [line("c1", 2)])
        july = audit(ledger, "2026-07")["payload"]
        august = audit(ledger, "2026-08")["payload"]
        self.assertEqual(july["lines"]["c1"]["amount"], 2 * 8000)
        self.assertEqual(august["lines"]["c1"]["amount"], 2 * 9000)
        # 两个月份各自引用不同的政策版本事件
        self.assertNotEqual(july["lines"]["c1"]["policy_events"],
                            august["lines"]["c1"]["policy_events"])


class EntitlementTest(unittest.TestCase):
    def test_capacity_and_attendance_jointly_cap_subsidy(self):
        ledger = make_ledger()
        declare(ledger, "2026-07", slots=1)  # 名额仅 1 人
        # 重复签到不重复计数：同一日期登记两次仍算一天
        attend(ledger, "c1", ["2026-07-08", "2026-07-09", "2026-07-10"], evidence="ev-a")
        attend(ledger, "c1", ["2026-07-08"], evidence="ev-a-dup")
        attend(ledger, "c2", ["2026-07-08", "2026-07-09", "2026-07-10"], evidence="ev-b")
        submit(ledger, "2026-07", [line("c1", 5), line("c2", 3)])
        snapshot = audit(ledger, "2026-07")["payload"]
        # 申报 5 天但出勤证据只有 3 天
        self.assertEqual(snapshot["lines"]["c1"]["eligible_days"], 3)
        # 名额只有 1 个，按儿童编号排序截断，c2 被名额排除
        self.assertTrue(snapshot["lines"]["c2"]["capacity_excluded"])
        self.assertEqual(snapshot["amount"], 3 * RATE)

    def test_closure_and_cross_month_withdrawal_attribution(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        ledger.report_closure(Role.ORG, "staff-1", org_id="org-1",
                              start_date="2026-07-10", end_date="2026-07-11", reason="台风停园")
        attend(ledger, "c1", ["2026-07-08", "2026-07-10", "2026-07-12"])
        attend(ledger, "c2", ["2026-07-14", "2026-07-16"])
        # 跨月退托：7 月 15 日起退托，之后日期不可补助
        ledger.record_withdrawal(Role.ORG, "staff-1", org_id="org-1", child_id="c2",
                                 effective_date="2026-07-15")
        submit(ledger, "2026-07", [line("c1", 3), line("c2", 2)])
        snapshot = audit(ledger, "2026-07")["payload"]
        # 停园日 07-10 被剔除
        self.assertEqual(snapshot["lines"]["c1"]["eligible_dates"], ["2026-07-08", "2026-07-12"])
        # 退托生效日及之后被剔除
        self.assertEqual(snapshot["lines"]["c2"]["eligible_dates"], ["2026-07-14"])
        self.assertEqual(snapshot["amount"], 3 * RATE)


class FreezeAndAdjustmentTest(unittest.TestCase):
    def test_frozen_claim_only_accepts_appended_adjustment(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-08"], evidence="ev-1")
        submit(ledger, "2026-07", [line("c1", 2)])
        ledger.freeze_claim(Role.ORG, "staff-1", org_id="org-1", month="2026-07")
        # 冻结后不允许整体重报
        with self.assertRaises(StateError):
            submit(ledger, "2026-07", [line("c1", 2)], expected_version=2)
        # 后补证明通过追加调整入账
        ledger.append_adjustment(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                 reason="后补出勤证明", expected_version=2,
                                 evidence=[{"child_id": "c1", "dates": ["2026-07-09"],
                                            "evidence_id": "late-1"}])
        snapshot = audit(ledger, "2026-07")["payload"]
        self.assertEqual(snapshot["lines"]["c1"]["eligible_days"], 2)
        self.assertIn("late-1", snapshot["lines"]["c1"]["evidence"])

    def test_adjustment_requires_frozen_claim(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        submit(ledger, "2026-07", [line("c1", 1)])
        with self.assertRaises(StateError):
            ledger.append_adjustment(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                     reason="未冻结", expected_version=1,
                                     lines=[line("c1", 2)])


class PaymentRecoveryTest(unittest.TestCase):
    def test_paid_difference_becomes_recovery_and_next_period_offset(self):
        ledger = make_ledger()
        attend(ledger, "c1", ["2026-07-08", "2026-07-09"])
        july_claim(ledger, [("c1", 2)])
        result = pay(ledger, "b-1", "org-1", "2026-07", receipt_id="rcpt-1")
        payment = result["payment"]["payload"]
        self.assertEqual((payment["gross"], payment["amount"]), (16000, 16000))
        # 跨月退托追溯：c1 自 07-09 起退托，7 月只应补助 1 天
        ledger.record_withdrawal(Role.ORG, "staff-1", org_id="org-1", child_id="c1",
                                 effective_date="2026-07-09")
        audit(ledger, "2026-07")
        recovery = ledger.post_recovery(Role.RECOVERER, "rec-1", org_id="org-1",
                                        month="2026-07", reason="退托追溯")["payload"]
        self.assertEqual(recovery["amount"], 8000)
        # 原付款不被改写
        self.assertEqual(ledger.payments[payment["payment_id"]]["gross"], 16000)
        self.assertEqual(ledger.payments[payment["payment_id"]]["receipt_id"], "rcpt-1")
        # 下一期付款自动抵扣未结追缴
        declare(ledger, "2026-08")
        attend(ledger, "c2", ["2026-08-03", "2026-08-04"])
        submit(ledger, "2026-08", [line("c2", 2)])
        audit(ledger, "2026-08")
        ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-2",
                          items=[{"org_id": "org-1", "month": "2026-08"}])
        result = pay(ledger, "b-2", "org-1", "2026-08")
        august = result["payment"]["payload"]
        self.assertEqual((august["gross"], august["offset_total"], august["amount"]),
                         (16000, 8000, 8000))
        self.assertEqual(len(result["offsets"]), 1)
        self.assertEqual(ledger.recoveries[recovery["recovery_id"]]["remaining"], 0)
        self.assertEqual(ledger.pending_work()["open_recoveries"], [])

    def test_recovery_can_be_collected_instead_of_offset(self):
        ledger = make_ledger()
        attend(ledger, "c1", ["2026-07-08", "2026-07-09"])
        july_claim(ledger, [("c1", 2)])
        pay(ledger, "b-1", "org-1", "2026-07")
        ledger.record_withdrawal(Role.ORG, "staff-1", org_id="org-1", child_id="c1",
                                 effective_date="2026-07-09")
        audit(ledger, "2026-07")
        recovery = ledger.post_recovery(Role.RECOVERER, "rec-1", org_id="org-1",
                                        month="2026-07")["payload"]
        ledger.collect_recovery(Role.RECOVERER, "rec-1", recovery_id=recovery["recovery_id"])
        self.assertEqual(ledger.recoveries[recovery["recovery_id"]]["status"], "COLLECTED")
        self.assertEqual(ledger.pending_work()["open_recoveries"], [])


class AppealTest(unittest.TestCase):
    def test_appeal_freezes_only_disputed_line(self):
        ledger = make_ledger()
        attend(ledger, "c1", ["2026-07-08", "2026-07-09"])
        attend(ledger, "c2", ["2026-07-08", "2026-07-09"])
        july_claim(ledger, [("c1", 2), ("c2", 2)])
        appeal = ledger.open_appeal(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                    child_id="c1", reason="出勤争议")["payload"]
        # 申诉期间只冻结争议行，c2 照常结算
        result = pay(ledger, "b-1", "org-1", "2026-07")
        self.assertEqual(result["payment"]["payload"]["gross"], 16000)
        # 申诉办结后，争议行差额通过追加付款补齐，不改写原付款
        ledger.resolve_appeal(Role.AUDITOR, "aud-1", appeal_id=appeal["appeal_id"],
                              resolution="出勤有效")
        ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-2",
                          items=[{"org_id": "org-1", "month": "2026-07"}])
        result = pay(ledger, "b-2", "org-1", "2026-07")
        self.assertEqual(result["payment"]["payload"]["gross"], 16000)
        summary = ledger.claim_summary("org-1", "2026-07")
        self.assertEqual((summary["entitlement"], summary["settled"]), (32000, 32000))


class RoleAndVisibilityTest(unittest.TestCase):
    def test_audit_payment_recovery_require_distinct_roles(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-08"])
        submit(ledger, "2026-07", [line("c1", 1)])
        with self.assertRaises(PermissionDenied):
            ledger.audit_claim(Role.PAYER, "pay-1", org_id="org-1", month="2026-07")
        audit(ledger, "2026-07")
        ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1",
                          items=[{"org_id": "org-1", "month": "2026-07"}])
        with self.assertRaises(PermissionDenied):
            ledger.post_payment(Role.AUDITOR, "aud-1", batch_id="b-1",
                                org_id="org-1", month="2026-07")
        with self.assertRaises(PermissionDenied):
            ledger.post_recovery(Role.PAYER, "pay-1", org_id="org-1", month="2026-07")
        with self.assertRaises(PermissionDenied):
            ledger.publish_policy(Role.ORG, "staff-1", org_type=ORG_TYPE, age_band=BAND,
                                  daily_rate_cents=100, effective_from="2026-07-01")

    def test_parent_sees_only_own_child_months_and_refund_impact(self):
        ledger = make_ledger()
        attend(ledger, "c1", ["2026-07-08", "2026-07-09"])
        attend(ledger, "c2", ["2026-07-08"])
        july_claim(ledger, [("c1", 2), ("c2", 1)])
        pay(ledger, "b-1", "org-1", "2026-07")
        ledger.record_withdrawal(Role.ORG, "staff-1", org_id="org-1", child_id="c1",
                                 effective_date="2026-07-09")
        audit(ledger, "2026-07")
        ledger.post_recovery(Role.RECOVERER, "rec-1", org_id="org-1", month="2026-07")
        # c1 家长可见自身月份与按份额折算的退费影响
        entries = ledger.parent_statement(Role.PARENT, "g-c1", child_id="c1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["month"], "2026-07")
        self.assertEqual(entries[0]["eligible_days"], 1)
        self.assertEqual(entries[0]["recoveries"][0]["amount"], 8000)
        # c2 的申报行不会出现在 c1 家长的视图里
        self.assertNotIn("c2", [line_ for entry in entries for line_ in entry])
        # 他人家长查看 c1 被拒绝
        with self.assertRaises(PermissionDenied):
            ledger.parent_statement(Role.PARENT, "g-c2", child_id="c1")


class IdempotencyAndConcurrencyTest(unittest.TestCase):
    def test_replayed_material_is_not_charged_twice(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-08"], evidence="ev-1", idempotency_key="att-1")
        attend(ledger, "c1", ["2026-07-08"], evidence="ev-1", idempotency_key="att-1")
        first = submit(ledger, "2026-07", [line("c1", 1)], idempotency_key="claim-1")
        second = submit(ledger, "2026-07", [line("c1", 1)], idempotency_key="claim-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(ledger.claim_versions[("org-1", "2026-07")], 1)
        audit(ledger, "2026-07")
        ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1",
                          items=[{"org_id": "org-1", "month": "2026-07"}])
        pay(ledger, "b-1", "org-1", "2026-07", idempotency_key="pay-1")
        replayed = ledger.post_payment(Role.PAYER, "pay-1", batch_id="b-1",
                                       org_id="org-1", month="2026-07",
                                       idempotency_key="pay-1")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(ledger.payments), 1)
        self.assertEqual(ledger.claim_summary("org-1", "2026-07")["settled"], 8000)

    def test_concurrent_claim_uses_version_conflict(self):
        ledger = make_ledger()
        declare(ledger, "2026-07")
        submit(ledger, "2026-07", [line("c1", 1)])
        # 两个并发重报都基于版本 1，只有一个能成功
        submit(ledger, "2026-07", [line("c1", 2)], expected_version=1)
        with self.assertRaises(VersionConflict):
            submit(ledger, "2026-07", [line("c1", 3)], expected_version=1)
        # 冻结后的追加调整同样按版本互斥
        ledger.freeze_claim(Role.ORG, "staff-1", org_id="org-1", month="2026-07")
        with self.assertRaises(VersionConflict):
            ledger.append_adjustment(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                     reason="过期版本", expected_version=2,
                                     lines=[line("c1", 4)])
        ledger.append_adjustment(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                 reason="当前版本", expected_version=3,
                                 lines=[line("c1", 4)])


class RestartAndReconcileTest(unittest.TestCase):
    def test_restart_resumes_unfinished_audit_and_payment_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = make_ledger(path)
            declare(ledger, "2026-07")
            declare(ledger, "2026-08")
            attend(ledger, "c1", ["2026-07-08"])
            attend(ledger, "c1", ["2026-08-03"])
            submit(ledger, "2026-07", [line("c1", 1)])
            submit(ledger, "2026-08", [line("c1", 1)])
            audit(ledger, "2026-07")
            audit(ledger, "2026-08")
            ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1",
                              items=[{"org_id": "org-1", "month": "2026-07"},
                                     {"org_id": "org-1", "month": "2026-08"}])
            pay(ledger, "b-1", "org-1", "2026-07")
            # 系统重启：从事件日志恢复，继续未完成的支付批次
            reopened = Ledger(path)
            pending = reopened.pending_work()
            self.assertEqual(pending["open_batches"],
                             [{"batch_id": "b-1", "remaining": ["org-1/2026-08"]}])
            self.assertEqual(pending["claims_pending_audit"], [])
            result = pay(reopened, "b-1", "org-1", "2026-08")
            self.assertEqual(result["payment"]["payload"]["amount"], 8000)
            self.assertEqual(reopened.pending_work()["open_batches"], [])
            # 冻结后追加调整但未审核，重启后仍出现在待办中
            reopened.freeze_claim(Role.ORG, "staff-1", org_id="org-1", month="2026-07")
            reopened.append_adjustment(Role.ORG, "staff-1", org_id="org-1", month="2026-07",
                                       reason="后补证明", expected_version=2,
                                       evidence=[{"child_id": "c1", "dates": ["2026-07-09"],
                                                  "evidence_id": "late-1"}])
            restarted = Ledger(path)
            self.assertEqual(restarted.pending_work()["claims_pending_audit"],
                             ["org-1/2026-07"])
            self.assertEqual(len(restarted.events), len(restarted._event_ids))

    def test_quarterly_reconciliation_traces_every_amount(self):
        ledger = make_ledger()
        policy_event = ledger.events[0]["event_id"]
        capacity_event = declare(ledger, "2026-07", slots=1)["event_id"]
        attend(ledger, "c1", ["2026-07-08"], evidence="ev-1")
        submit(ledger, "2026-07", [line("c1", 2)])
        ledger.freeze_claim(Role.ORG, "staff-1", org_id="org-1", month="2026-07")
        adjustment = ledger.append_adjustment(
            Role.ORG, "staff-1", org_id="org-1", month="2026-07",
            reason="后补出勤证明", expected_version=2,
            evidence=[{"child_id": "c1", "dates": ["2026-07-09"], "evidence_id": "late-1"}])
        audit(ledger, "2026-07")
        ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1",
                          items=[{"org_id": "org-1", "month": "2026-07"}])
        pay(ledger, "b-1", "org-1", "2026-07", receipt_id="rcpt-1")
        report = ledger.reconcile_quarter(2026, 3)
        self.assertEqual(report["months"], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(len(report["payments"]), 1)
        entry = report["payments"][0]
        # 金额 → 回执
        self.assertEqual(entry["receipt_id"], "rcpt-1")
        self.assertEqual(entry["gross"], 16000)
        # 金额 → 政策版本、名额声明、出勤证据
        c1 = entry["lines"]["c1"]
        self.assertEqual(c1["policy_events"], [policy_event])
        self.assertEqual(c1["capacity_event"], capacity_event)
        self.assertEqual(c1["evidence"], ["ev-1", "late-1"])
        # 金额 → 追加调整
        self.assertEqual(entry["adjustments"], [adjustment["event_id"]])
        self.assertEqual(report["totals"],
                         {"gross": 16000, "offset": 0, "cash": 16000,
                          "recovered": 0, "recovery_outstanding": 0})


if __name__ == "__main__":
    unittest.main()
