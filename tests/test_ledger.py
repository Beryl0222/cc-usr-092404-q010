"""跨月清算账本的行为测试：资格、冻结、调整、支付、追缴、申诉、
角色、幂等、并发、重启恢复与季度对账。"""

import tempfile
import unittest
from pathlib import Path

from src.childcare_subsidy_ledger import (
    EVENT_KINDS,
    EventStore,
    Ledger,
    MaterialConflict,
    PermissionDenied,
    StateError,
    VersionConflict,
)
from src.childcare_subsidy_ledger.events import PAYLOAD_REQUIRED_FIELDS

ADMIN = {"id": "admin-1", "role": "ADMIN"}
OPERATOR = {"id": "op-1", "role": "ORG_OPERATOR", "org_id": "org-1"}
REVIEWER = {"id": "rev-1", "role": "REVIEWER"}
PAYER = {"id": "pay-1", "role": "PAYER"}
RECOVERER = {"id": "rec-1", "role": "RECOVERER"}
PARENT = {"id": "parent-1", "role": "PARENT"}

CLOCK = lambda: "2026-09-24T09:00:00+08:00"  # noqa: E731


def day_range(month, start, end):
    return [f"{month}-{day:02d}" for day in range(start, end + 1)]


def make_ledger(store=None):
    return Ledger(store or EventStore(), clock=CLOCK)


def publish_base_policy(ledger, rate=1000, relief=50):
    ledger.publish_policy(
        ADMIN, policy_id="pol-base", version=1,
        institution_type="community", age_band="toddler",
        effective_from="2026-01-01", daily_rate_cents=rate,
        closure_relief_rate=relief)


def declare_capacity(ledger, month, slots=30):
    ledger.declare_capacity(
        OPERATOR, org_id="org-1", month=month,
        institution_type="community", slots={"toddler": slots})


def attend(ledger, child_id, dates, org_id="org-1"):
    for date in dates:
        ledger.record_attendance(
            OPERATOR, org_id=org_id, child_id=child_id, date=date,
            age_band="toddler", material_id=f"mat-{child_id}-{date}")


def review_all(ledger, claim_id):
    claim = ledger.claims[claim_id]
    ledger.review_claim(REVIEWER, claim_id=claim_id, decisions=[
        {"child_id": cid, "approved": True} for cid in sorted(claim["lines"])])


def settle_month(ledger, month, children_days):
    """登记出勤、申报、审核并拨付一个月份，返回批次。"""
    declare_capacity(ledger, month)
    for child_id, dates in children_days.items():
        attend(ledger, child_id, dates)
    ledger.submit_claim(OPERATOR, org_id="org-1", month=month,
                        expected_version=0)
    review_all(ledger, f"claim-org-1-{month}")
    batch = ledger.create_payment_batch(PAYER, org_id="org-1")
    ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])
    return batch


class ContractTest(unittest.TestCase):
    def test_every_kind_has_payload_contract(self):
        self.assertEqual(sorted(EVENT_KINDS), sorted(PAYLOAD_REQUIRED_FIELDS))


class EligibilityTest(unittest.TestCase):
    def test_policy_versioned_by_effective_date(self):
        ledger = make_ledger()
        publish_base_policy(ledger, rate=1000)
        ledger.publish_policy(
            ADMIN, policy_id="pol-base", version=2,
            institution_type="community", age_band="toddler",
            effective_from="2026-07-16", daily_rate_cents=1500,
            closure_relief_rate=50)
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-15", "2026-07-16", "2026-07-17"])
        event = ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                    expected_version=0)
        (line,) = event["payload"]["lines"]
        # 07-15 走 v1（1000），07-16/17 走 v2（1500）
        self.assertEqual(line["amount_cents"], 1000 + 1500 + 1500)
        self.assertEqual(line["policy_ids"], ["pol-base@v1", "pol-base@v2"])

    def test_capacity_and_attendance_jointly_limit_person_times(self):
        ledger = make_ledger()
        publish_base_policy(ledger)
        declare_capacity(ledger, "2026-07", slots=2)
        attend(ledger, "c1", day_range("2026-07", 1, 2))
        attend(ledger, "c2", day_range("2026-07", 1, 2))
        event = ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                    expected_version=0)
        lines = {l["child_id"]: l for l in event["payload"]["lines"]}
        # 名额 2 人次：c1（id 排序优先）占满，c2 被截断
        self.assertEqual(lines["c1"]["amount_cents"], 2000)
        self.assertEqual(lines["c1"]["capped_days"], 0)
        self.assertEqual(lines["c2"]["amount_cents"], 0)
        self.assertEqual(lines["c2"]["capped_days"], 2)

    def test_duplicate_checkin_counts_once(self):
        ledger = make_ledger()
        publish_base_policy(ledger)
        declare_capacity(ledger, "2026-07")
        first = ledger.record_attendance(
            OPERATOR, org_id="org-1", child_id="c1", date="2026-07-01",
            age_band="toddler", material_id="mat-c1-2026-07-01")
        again = ledger.record_attendance(
            OPERATOR, org_id="org-1", child_id="c1", date="2026-07-01",
            age_band="toddler", material_id="mat-c1-2026-07-01")
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(
            len([e for e in ledger.store.all()
                 if e["kind"] == "ATTENDANCE_RECORDED"]), 1)

    def test_same_material_different_content_conflicts(self):
        ledger = make_ledger()
        publish_base_policy(ledger)
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-01"])
        with self.assertRaises(MaterialConflict):
            ledger.record_attendance(
                OPERATOR, org_id="org-1", child_id="c1", date="2026-07-01",
                age_band="infant", material_id="mat-c1-2026-07-01")

    def test_closure_relief_attributed_to_closure_month(self):
        ledger = make_ledger()
        publish_base_policy(ledger, rate=1000, relief=50)
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", day_range("2026-07", 1, 3))
        ledger.record_closure(OPERATOR, org_id="org-1",
                              dates=["2026-07-10"], reason="台风")
        event = ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                    expected_version=0)
        (line,) = event["payload"]["lines"]
        self.assertEqual(line["attendance_days"], 3)
        self.assertEqual(line["relief_days"], 1)
        self.assertEqual(line["amount_cents"], 3000 + 500)

    def test_withdrawal_cuts_subsidy_from_effective_date(self):
        ledger = make_ledger()
        publish_base_policy(ledger)
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", day_range("2026-07", 1, 20))
        ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                 effective_date="2026-07-15")
        event = ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                    expected_version=0)
        (line,) = event["payload"]["lines"]
        self.assertEqual(line["attendance_days"], 14)
        self.assertEqual(line["amount_cents"], 14000)


class FreezeAndAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        publish_base_policy(self.ledger)
        declare_capacity(self.ledger, "2026-07")
        attend(self.ledger, "c1", day_range("2026-07", 1, 3))
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                 expected_version=0)
        self.claim_id = "claim-org-1-2026-07"

    def test_frozen_month_rejects_direct_attendance(self):
        with self.assertRaises(StateError):
            attend(self.ledger, "c1", ["2026-07-10"])

    def test_late_proof_settles_as_adjustment_attributed_to_source_month(self):
        adj = self.ledger.append_adjustment(
            OPERATOR, claim_id=self.claim_id, attributed_month="2026-07",
            reason="后补证明", expected_version=1, material_id="proof-1",
            updates=[{"type": "attendance", "child_id": "c1",
                      "date": "2026-07-10", "age_band": "toddler"}])
        review_all(self.ledger, self.claim_id)
        self.ledger.review_adjustment(
            REVIEWER, adjustment_id=adj["payload"]["adjustment_id"],
            approve=True)
        claim = self.ledger.claims[self.claim_id]
        self.assertEqual(claim["approved_delta"], {"c1": 1000})
        # 归属原月份：差额计入 7 月申报，而不是修改历史月报
        self.assertEqual(adj["payload"]["attributed_month"], "2026-07")
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        (line,) = batch["payload"]["lines"]
        self.assertEqual(line["net_cents"], 3000 + 1000)

    def test_adjustment_requires_reviewer_role(self):
        adj = self.ledger.append_adjustment(
            OPERATOR, claim_id=self.claim_id, attributed_month="2026-07",
            reason="后补证明", expected_version=1,
            updates=[{"type": "manual_delta", "child_id": "c1",
                      "delta_cents": 100}])
        with self.assertRaises(PermissionDenied):
            self.ledger.review_adjustment(
                OPERATOR, adjustment_id=adj["payload"]["adjustment_id"],
                approve=True)

    def test_rejected_adjustment_leaves_no_trace_in_settlement(self):
        adj = self.ledger.append_adjustment(
            OPERATOR, claim_id=self.claim_id, attributed_month="2026-07",
            reason="后补证明", expected_version=1,
            updates=[{"type": "manual_delta", "child_id": "c1",
                      "delta_cents": 100}])
        self.ledger.review_adjustment(
            REVIEWER, adjustment_id=adj["payload"]["adjustment_id"],
            approve=False)
        self.assertEqual(self.ledger.claims[self.claim_id]["approved_delta"],
                         {})

    def test_adjustment_material_replay_is_idempotent(self):
        updates = [{"type": "manual_delta", "child_id": "c1",
                    "delta_cents": 100}]
        first = self.ledger.append_adjustment(
            OPERATOR, claim_id=self.claim_id, attributed_month="2026-07",
            reason="补录", expected_version=1, material_id="proof-9",
            updates=updates)
        claim = self.ledger.claims[self.claim_id]
        again = self.ledger.append_adjustment(
            OPERATOR, claim_id=self.claim_id, attributed_month="2026-07",
            reason="补录", expected_version=claim["version"],
            material_id="proof-9", updates=updates)
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(len(claim["adjustments"]), 1)

    def test_closure_after_freeze_generates_attributed_adjustment(self):
        self.ledger.record_closure(OPERATOR, org_id="org-1",
                                   dates=["2026-07-10"], reason="台风")
        claim = self.ledger.claims[self.claim_id]
        self.assertEqual(len(claim["adjustments"]), 1)
        adj = self.ledger.adjustments[claim["adjustments"][0]]
        self.assertEqual(adj["attributed_month"], "2026-07")
        self.assertEqual(adj["reason"], "closure")
        self.ledger.review_adjustment(REVIEWER,
                                      adjustment_id=adj["adjustment_id"],
                                      approve=True)
        self.assertEqual(claim["approved_delta"], {"c1": 500})

    def test_cross_month_withdrawal_adjusts_frozen_source_month(self):
        # c1 在 7、8 两月都有出勤；8 月申报后，退托回溯生效到 7-15
        declare_capacity(self.ledger, "2026-08")
        attend(self.ledger, "c1", day_range("2026-08", 1, 5))
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-08",
                                 expected_version=0)
        july_claim = self.ledger.claims[self.claim_id]
        july_version = july_claim["version"]
        self.ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                      effective_date="2026-07-15",
                                      refund_cents=2000)
        # 7 月出勤都在 15 日前 → 零差额不产生调整
        self.assertEqual(july_claim["version"], july_version)
        # 8 月出勤全部在生效日之后 → 自动生成负向调整，归属 8 月
        august_claim = self.ledger.claims["claim-org-1-2026-08"]
        (adj_id,) = august_claim["adjustments"]
        adj = self.ledger.adjustments[adj_id]
        self.assertEqual(adj["attributed_month"], "2026-08")
        self.assertEqual(adj["reason"], "withdrawal")
        self.ledger.review_adjustment(REVIEWER, adjustment_id=adj_id,
                                      approve=True)
        self.assertEqual(august_claim["approved_delta"], {"c1": -5000})
        # 退托事实已入账，家长可见退费
        self.assertEqual(self.ledger.refunds["c1"],
                         [{"month": "2026-07", "refund_cents": 2000}])

    def test_cross_month_withdrawal_creates_negative_delta(self):
        attend_later = day_range("2026-07", 16, 20)
        # 先补登 7 月下旬出勤再冻结申报
        ledger = make_ledger()
        publish_base_policy(ledger)
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", day_range("2026-07", 1, 20))
        ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                            expected_version=0)
        claim = ledger.claims["claim-org-1-2026-07"]
        ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                 effective_date="2026-07-15")
        (adj_id,) = claim["adjustments"]
        ledger.review_adjustment(REVIEWER, adjustment_id=adj_id, approve=True)
        # 7-15 起退托：20 天 → 14 天，差额 -6000
        self.assertEqual(claim["approved_delta"], {"c1": -6000})
        self.assertEqual(attend_later[0], "2026-07-16")


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        publish_base_policy(self.ledger)

    def test_duplicate_claim_submission_conflicts_on_version(self):
        declare_capacity(self.ledger, "2026-07")
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                 expected_version=0)
        with self.assertRaises(VersionConflict):
            self.ledger.submit_claim(OPERATOR, org_id="org-1",
                                     month="2026-07", expected_version=0)
        # 携带当前版本号的重放是幂等的
        replay = self.ledger.submit_claim(OPERATOR, org_id="org-1",
                                          month="2026-07", expected_version=1)
        self.assertEqual(replay["payload"]["claim_id"], "claim-org-1-2026-07")

    def test_capacity_declaration_uses_optimistic_version(self):
        declare_capacity(self.ledger, "2026-07")
        with self.assertRaises(VersionConflict):
            self.ledger.declare_capacity(
                OPERATOR, org_id="org-1", month="2026-07",
                institution_type="community", slots={"toddler": 5},
                expected_version=0)
        self.ledger.declare_capacity(
            OPERATOR, org_id="org-1", month="2026-07",
            institution_type="community", slots={"toddler": 5},
            expected_version=1)

    def test_adjustment_uses_optimistic_version(self):
        declare_capacity(self.ledger, "2026-07")
        attend(self.ledger, "c1", ["2026-07-01"])
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                 expected_version=0)
        kwargs = dict(claim_id="claim-org-1-2026-07",
                      attributed_month="2026-07", reason="补录",
                      updates=[{"type": "manual_delta", "child_id": "c1",
                                "delta_cents": 1}])
        self.ledger.append_adjustment(OPERATOR, expected_version=1, **kwargs)
        with self.assertRaises(VersionConflict):
            self.ledger.append_adjustment(OPERATOR, expected_version=1,
                                          **kwargs)


class PaymentRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        publish_base_policy(self.ledger)

    def test_payment_flow_and_receipt(self):
        batch = settle_month(self.ledger, "2026-07",
                             {"c1": day_range("2026-07", 1, 3)})
        claim = self.ledger.claims["claim-org-1-2026-07"]
        self.assertEqual(claim["settled_cents"], 3000)
        receipt = self.ledger.receipts[f"rcpt-{batch['payload']['batch_id']}"]
        self.assertEqual(receipt["amount_cents"], 3000)
        self.assertEqual(receipt["receipt_kind"], "payment")

    def test_paid_difference_becomes_recovery_not_rewrite(self):
        # 7 月按 20 天拨付；随后退托回溯，差额形成追缴而非改写原付款
        settle_month(self.ledger, "2026-07",
                     {"c1": day_range("2026-07", 1, 20)})
        paid_events_before = [e["event_id"] for e in self.ledger.store.all()
                              if e["kind"] == "PAYMENT_POSTED"]
        self.ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                      effective_date="2026-07-15")
        claim = self.ledger.claims["claim-org-1-2026-07"]
        (adj_id,) = claim["adjustments"]
        self.ledger.review_adjustment(REVIEWER, adjustment_id=adj_id,
                                      approve=True)
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])
        # 原付款事件未被改写
        paid_events_after = [e["event_id"] for e in self.ledger.store.all()
                             if e["kind"] == "PAYMENT_POSTED"]
        self.assertTrue(set(paid_events_before) <= set(paid_events_after))
        original = self.ledger.store.get(paid_events_before[0])
        self.assertEqual(original["payload"]["amount_cents"], 20000)
        # 差额 6000 形成追缴
        (recovery,) = self.ledger.recoveries.values()
        self.assertEqual(recovery["amount_cents"], 6000)
        self.assertEqual(recovery["balance_cents"], 6000)

    def test_recovery_offsets_next_period_payment(self):
        settle_month(self.ledger, "2026-07",
                     {"c1": day_range("2026-07", 1, 20)})
        self.ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                      effective_date="2026-07-15")
        claim = self.ledger.claims["claim-org-1-2026-07"]
        (adj_id,) = claim["adjustments"]
        self.ledger.review_adjustment(REVIEWER, adjustment_id=adj_id,
                                      approve=True)
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])
        # 8 月另一名儿童应付 5000，先抵扣追缴 6000 中的 5000，实付 0
        settle_month(self.ledger, "2026-08",
                     {"c2": day_range("2026-08", 1, 5)})
        recovery = next(iter(self.ledger.recoveries.values()))
        self.assertEqual(recovery["balance_cents"], 1000)
        offsets = [e for e in self.ledger.store.all()
                   if e["kind"] == "OFFSET_APPLIED"]
        self.assertEqual(offsets[-1]["payload"]["amount_cents"], 5000)
        august_receipt = self.ledger.receipts[
            f"rcpt-{self._last_batch_id()}"]
        self.assertEqual(august_receipt["amount_cents"], 0)

    def test_recovery_collected_by_recoverer_role(self):
        settle_month(self.ledger, "2026-07", {"c1": day_range("2026-07", 1, 3)})
        self.ledger.post_recovery(RECOVERER, org_id="org-1",
                                  amount_cents=1200, reason="现场核查",
                                  recovery_id="rec-manual-1")
        with self.assertRaises(PermissionDenied):
            self.ledger.mark_recovery_paid(PAYER, recovery_id="rec-manual-1")
        self.ledger.mark_recovery_paid(RECOVERER, recovery_id="rec-manual-1")
        recovery = self.ledger.recoveries["rec-manual-1"]
        self.assertEqual(recovery["balance_cents"], 0)
        self.assertEqual(recovery["status"], "paid")
        self.assertEqual(self.ledger.receipts["rcpt-rec-manual-1"]
                         ["receipt_kind"], "recovery")

    def test_batch_creation_requires_payer(self):
        settle_month(self.ledger, "2026-07", {"c1": day_range("2026-07", 1, 1)})
        with self.assertRaises(PermissionDenied):
            self.ledger.create_payment_batch(REVIEWER, org_id="org-1")

    def _last_batch_id(self):
        return sorted(self.ledger.batches)[-1]


class AppealTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        publish_base_policy(self.ledger)
        declare_capacity(self.ledger, "2026-07")
        attend(self.ledger, "c1", day_range("2026-07", 1, 3))
        attend(self.ledger, "c2", day_range("2026-07", 1, 2))
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                 expected_version=0)
        review_all(self.ledger, "claim-org-1-2026-07")

    def test_appeal_freezes_only_disputed_lines(self):
        self.ledger.open_appeal(OPERATOR, claim_id="claim-org-1-2026-07",
                                line_keys=["c1"], appeal_id="appeal-1")
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])
        claim = self.ledger.claims["claim-org-1-2026-07"]
        # 争议行 c1 冻结，c2 照常结算
        self.assertEqual(claim["settled_cents"], 2000)
        self.assertEqual(claim["lines"]["c1"]["status"], "appealed")
        # 结案后争议行进入下一批结算
        self.ledger.resolve_appeal(REVIEWER, appeal_id="appeal-1", decisions=[
            {"child_id": "c1", "approved": True}])
        batch2 = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch2["payload"]["batch_id"])
        self.assertEqual(claim["settled_cents"], 5000)

    def test_review_of_appealed_line_is_blocked(self):
        self.ledger.open_appeal(OPERATOR, claim_id="claim-org-1-2026-07",
                                line_keys=["c1"], appeal_id="appeal-1")
        with self.assertRaises(StateError):
            self.ledger.review_claim(REVIEWER,
                                     claim_id="claim-org-1-2026-07",
                                     decisions=[{"child_id": "c1",
                                                 "approved": True}])

    def test_appeal_rejection_zeroes_line(self):
        self.ledger.open_appeal(OPERATOR, claim_id="claim-org-1-2026-07",
                                line_keys=["c1"], appeal_id="appeal-1")
        self.ledger.resolve_appeal(REVIEWER, appeal_id="appeal-1", decisions=[
            {"child_id": "c1", "approved": False}])
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])
        claim = self.ledger.claims["claim-org-1-2026-07"]
        self.assertEqual(claim["lines"]["c1"]["reviewed_amount_cents"], 0)
        self.assertEqual(claim["settled_cents"], 2000)


class RoleTest(unittest.TestCase):
    def test_role_separation(self):
        ledger = make_ledger()
        with self.assertRaises(PermissionDenied):
            ledger.publish_policy(OPERATOR, policy_id="p", version=1,
                                  institution_type="community",
                                  age_band="toddler",
                                  effective_from="2026-01-01",
                                  daily_rate_cents=100)
        publish_base_policy(ledger)
        with self.assertRaises(PermissionDenied):
            ledger.declare_capacity(REVIEWER, org_id="org-1", month="2026-07",
                                    institution_type="community",
                                    slots={"toddler": 1})
        declare_capacity(ledger, "2026-07")
        attend(ledger, "c1", ["2026-07-01"])
        ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                            expected_version=0)
        with self.assertRaises(PermissionDenied):
            ledger.review_claim(PAYER, claim_id="claim-org-1-2026-07",
                                decisions=[{"child_id": "c1",
                                            "approved": True}])
        with self.assertRaises(PermissionDenied):
            ledger.post_recovery(PAYER, org_id="org-1", amount_cents=1,
                                 reason="越权")
        with self.assertRaises(PermissionDenied):
            ledger.quarterly_report(OPERATOR, org_id="org-2",
                                    quarter="2026-Q3")


class RestartRecoveryTest(unittest.TestCase):
    def test_restart_resumes_pending_review_and_open_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            ledger = Ledger(EventStore(path), clock=CLOCK)
            publish_base_policy(ledger)
            declare_capacity(ledger, "2026-07")
            attend(ledger, "c1", day_range("2026-07", 1, 3))
            ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-07",
                                expected_version=0)

            # 模拟重启：重放事件后待审核申报仍在
            ledger = Ledger(EventStore(path), clock=CLOCK)
            self.assertEqual(ledger.pending_work()["claims_pending_review"],
                             ["claim-org-1-2026-07"])
            review_all(ledger, "claim-org-1-2026-07")
            batch = ledger.create_payment_batch(PAYER, org_id="org-1")
            batch_id = batch["payload"]["batch_id"]

            # 再次重启：未拨付批次可继续
            ledger = Ledger(EventStore(path), clock=CLOCK)
            self.assertEqual(ledger.pending_work()["open_batches"],
                             [batch_id])
            ledger.post_payment(PAYER, batch_id=batch_id)
            self.assertEqual(ledger.pending_work()["open_batches"], [])
            self.assertEqual(
                ledger.claims["claim-org-1-2026-07"]["settled_cents"], 3000)

    def test_partially_posted_batch_resumes_from_breakpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            ledger = Ledger(EventStore(path), clock=CLOCK)
            publish_base_policy(ledger)
            settle_month(ledger, "2026-07", {"c1": day_range("2026-07", 1, 2)})
            # 制造一笔新应付并开启批次，但只拨付其中一行就"崩溃"
            declare_capacity(ledger, "2026-08")
            attend(ledger, "c1", day_range("2026-08", 1, 2))
            ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-08",
                                expected_version=0)
            review_all(ledger, "claim-org-1-2026-08")
            batch = ledger.create_payment_batch(PAYER, org_id="org-1")
            batch_id = batch["payload"]["batch_id"]
            ledger.store.append({
                "event_id": f"pay-{batch_id}-claim-org-1-2026-08",
                "kind": "PAYMENT_POSTED", "occurred_at": CLOCK(),
                "subject_id": "org-1",
                "payload": {"batch_id": batch_id,
                            "line_key": "claim-org-1-2026-08",
                            "amount_cents": 2000}})

            ledger = Ledger(EventStore(path), clock=CLOCK)
            self.assertEqual(ledger.pending_work()["open_batches"], [batch_id])
            events = ledger.post_payment(PAYER, batch_id=batch_id)
            kinds = [e["kind"] for e in events]
            # 已拨付的行不重复，只补回执
            self.assertNotIn("PAYMENT_POSTED", kinds)
            self.assertIn("RECEIPT_ISSUED", kinds)
            self.assertEqual(ledger.pending_work()["open_batches"], [])

    def test_replay_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            ledger = Ledger(EventStore(path), clock=CLOCK)
            publish_base_policy = None  # noqa: F841
            ledger.publish_policy(
                ADMIN, policy_id="pol-base", version=1,
                institution_type="community", age_band="toddler",
                effective_from="2026-01-01", daily_rate_cents=1000,
                closure_relief_rate=50)
            settle_month(ledger, "2026-07", {"c1": day_range("2026-07", 1, 3)})
            event_count = len(ledger.store.all())
            reloaded = Ledger(EventStore(path), clock=CLOCK)
            self.assertEqual(
                reloaded.claims["claim-org-1-2026-07"]["settled_cents"], 3000)
            # 相同 event_id 重放不重复计费
            for event in ledger.store.all():
                reloaded.store.append(event)
            self.assertEqual(len(reloaded.store.all()), event_count)
            self.assertEqual(
                reloaded.claims["claim-org-1-2026-07"]["settled_cents"], 3000)


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        publish_base_policy(self.ledger)
        self.ledger.link_parent(ADMIN, child_id="c1", parent_id="parent-1")
        settle_month(self.ledger, "2026-07",
                     {"c1": day_range("2026-07", 1, 3)})
        self.ledger.record_withdrawal(OPERATOR, org_id="org-1", child_id="c1",
                                      effective_date="2026-08-02",
                                      refund_cents=800)
        declare_capacity(self.ledger, "2026-08")
        attend(self.ledger, "c1", ["2026-08-01"])
        self.ledger.submit_claim(OPERATOR, org_id="org-1", month="2026-08",
                                 expected_version=0)
        review_all(self.ledger, "claim-org-1-2026-08")
        batch = self.ledger.create_payment_batch(PAYER, org_id="org-1")
        self.ledger.post_payment(PAYER, batch_id=batch["payload"]["batch_id"])

    def test_quarterly_report_traces_every_amount(self):
        report = self.ledger.quarterly_report(REVIEWER, org_id="org-1",
                                              quarter="2026-Q3")
        july = next(m for m in report["months"] if m["month"] == "2026-07")
        august = next(m for m in report["months"] if m["month"] == "2026-08")
        self.assertEqual(july["settled_cents"], 3000)
        # 8 月退托 8-02 生效：仅 8-01 一天可补助
        self.assertEqual(august["settled_cents"], 1000)
        self.assertEqual(report["totals"]["settled_cents"], 4000)
        self.assertEqual(len(report["receipts"]), 2)

    def test_trace_amount_reaches_policy_capacity_attendance_receipt(self):
        trace = self.ledger.trace_amount(
            REVIEWER, claim_id="claim-org-1-2026-07", child_id="c1")
        self.assertEqual(trace["policy_ids"], ["pol-base@v1"])
        self.assertEqual(trace["capacity"]["slots"], {"toddler": 30})
        self.assertEqual(trace["attendance_dates"],
                         day_range("2026-07", 1, 3))
        self.assertEqual(trace["payments"][0]["receipt_id"],
                         f"rcpt-{trace['payments'][0]['batch_id']}")
        self.assertIsNone(trace["withdrawal"])

    def test_parent_sees_only_own_children_and_refunds(self):
        view = self.ledger.parent_view(PARENT, parent_id="parent-1")
        (child,) = view["children"]
        self.assertEqual(child["child_id"], "c1")
        self.assertEqual(child["refunds"],
                         [{"month": "2026-08", "refund_cents": 800}])
        self.assertEqual([m["month"] for m in child["months"]],
                         ["2026-07", "2026-08"])
        with self.assertRaises(PermissionDenied):
            self.ledger.parent_view(PARENT, parent_id="parent-2")
        with self.assertRaises(PermissionDenied):
            self.ledger.parent_view(
                {"id": "x", "role": "ORG_OPERATOR", "org_id": "org-1"},
                parent_id="parent-1")

    def test_quarterly_report_rejects_parent_role(self):
        with self.assertRaises(PermissionDenied):
            self.ledger.quarterly_report(PARENT, org_id="org-1",
                                         quarter="2026-Q3")


if __name__ == "__main__":
    unittest.main()
