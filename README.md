# 普惠托育补助清算

本项目用于整理普惠托育补助清算领域中的事件名称、交换字段与脱敏样例，方便业务、运营和研发人员在同一套术语下讨论后续服务。资料只包含领域约定，不包含真实个人信息、生产连接或外部账号。

## 目录

- `src/childcare_subsidy_ledger.py`：事件种类与最小字段校验。
- `src/ledger.py`：跨月清算账本，在事件日志之上维护可重放的清算状态。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `tests/`：保证样例与领域约定一致，并覆盖账本行为。

## 跨月账本（src/ledger.py）

账本只可追加，全部状态由事件重放得到；金额一律以“分”为单位的整数表示。

- **政策版本化**：按机构类型、年龄段与生效日维护政策版本，逐日按生效版本计价。
- **名额与出勤双上限**：名额声明限定各年龄段可补助人数（超出按儿童编号排序截断），出勤证据限定可补助天数，重复签到按日期去重。
- **跨月归属**：临时停园、跨月退托、后补证明均为独立事件，归属到对应月份再参与计价。
- **冻结与追加调整**：月度申报冻结后仅接受追加调整（`append_adjustment`），不再整体重报。
- **抵扣与追缴**：已支付差异形成下一期抵扣（`OFFSET_APPLIED`）或追缴（`RECOVERY_POSTED`），历史付款记录永不改写。
- **申诉按行冻结**：申诉期间只冻结争议行，其余家庭照常结算；办结后差额追加付款。
- **角色分离**：审核（`AUDITOR`）、付款（`PAYER`）、追缴（`RECOVERER`）由不同角色执行；家长（`PARENT`）仅可见本人子女的月份与退费影响。
- **幂等与并发**：相同材料凭幂等键重放不重复计费；并发申报以版本号冲突（`VersionConflict`）。
- **重启续办**：事件落盘（JSONL）后重启，`pending_work()` 列出待审核申报、未完成支付批次与未结追缴。
- **季度对账**：`reconcile_quarter(year, quarter)` 让每笔金额都可追到政策版本、名额声明、出勤证据、追加调整与回执。

### 最小示例

```python
from src.ledger import Ledger, Role

ledger = Ledger("ledger.jsonl")  # 传入路径即落盘，重启后可续办
ledger.publish_policy(Role.POLICY_ADMIN, "admin", org_type="民办普惠园", age_band="托小班",
                      daily_rate_cents=8000, effective_from="2026-07-01")
ledger.declare_capacity(Role.ORG, "staff-1", org_id="org-1", org_type="民办普惠园",
                        month="2026-07", age_band="托小班", slots=20)
ledger.record_attendance(Role.ORG, "staff-1", org_id="org-1", child_id="c1",
                         dates=["2026-07-08", "2026-07-09"], evidence_id="ev-1")
ledger.submit_claim(Role.ORG, "staff-1", org_id="org-1", org_type="民办普惠园",
                    month="2026-07", expected_version=0,
                    lines=[{"child_id": "c1", "age_band": "托小班", "days": 2, "guardian_id": "g1"}])
ledger.audit_claim(Role.AUDITOR, "aud-1", org_id="org-1", month="2026-07")
ledger.open_batch(Role.PAYER, "pay-1", batch_id="b-1", items=[{"org_id": "org-1", "month": "2026-07"}])
ledger.post_payment(Role.PAYER, "pay-1", batch_id="b-1", org_id="org-1", month="2026-07")
```

## 测试与构建

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
