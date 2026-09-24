# 普惠托育补助清算

本项目实现普惠托育补助的**跨月清算账本**：机构申报容量、儿童出勤、停园减免、
退托退款分别在不同月份修订时，所有事实以事件形式追加入账，已拨付金额始终
可以沿事件链回溯到来由，不会被后续修订覆盖。

## 领域模型

### 事件溯源

一切事实都是仅追加的事件（`src/childcare_subsidy_ledger/events.py`）：
政策发布、容量声明、出勤登记、停园、退托、月度申报、逐行审核、申诉、
追加调整、支付批次、付款、抵扣、追缴、回执。账本状态由事件重放得到，
系统重启后自动恢复未完成的审核与支付批次（`Ledger.pending_work()`）。

### 政策版本化

政策按（机构类型 × 年龄段 × 生效日）版本化，资格计算逐日匹配生效版本；
同一政策号可发布多个版本，对账时以 `政策号@版本` 追溯。

### 可补助人次

名额声明与出勤证据共同限定可补助人次：先按出勤/停园减免逐日累计
（同一儿童同一天多次签到只计一次），再按机构当月该年龄段名额上限截断，
儿童按 id 排序确定性分配。停园日按政策减免比例计入，退托生效日起不再补助。

### 冻结与追加调整

月度申报提交即冻结，快照当时的计算输入。冻结后不再接受直接出勤登记，
只接受追加调整（后补证明、停园、退托、人工差额），调整归属原申报月份，
经审核员批准后才产生差额并入账。跨月退托、冻结后补登停园会自动生成
对应月份的待审核调整；零差额不产生调整。

### 支付、抵扣与追缴

付款只增不改。已支付差异通过下一批次抵扣（OFFSET_APPLIED）或追缴
（RECOVERY_POSTED）平衡：申报净额为负时转入追缴并冲减该申报的已结算额；
未结追缴按编号顺序抵扣后续应付，余额作为实际拨付并开具回执。
机构申诉期间只冻结争议行，其他家庭照常结算。

### 角色与可见性

| 角色 | 职责 |
| --- | --- |
| `ADMIN` | 发布政策、绑定儿童与家长 |
| `ORG_OPERATOR` | 容量声明、出勤/停园/退托登记、提交申报、追加调整、申诉 |
| `REVIEWER` | 逐行审核申报、审核调整、申诉结案 |
| `PAYER` | 开启并拨付支付批次 |
| `RECOVERER` | 登记追缴、追缴销账 |
| `PARENT` | 仅可查看自己关联儿童的月份金额与退费影响 |

### 幂等与并发

- 相同 `event_id` 重放不重复计费；相同材料号重复提交返回原事件，
  材料号相同而内容不同报 `MaterialConflict`；
- 申报、容量声明、追加调整采用 `expected_version` 乐观并发，
  版本不匹配报 `VersionConflict`；
- 支付批次的每一步（付款、追缴、抵扣、回执）都是独立的幂等事件，
  崩溃后重入可从断点继续。

### 季度对账

`Ledger.quarterly_report()` 汇总季度内每月申报、调整、支付、追缴与回执；
`Ledger.trace_amount()` 可从任意一笔金额追溯到政策版本、容量声明、
出勤日期、停园/退托事实、调整与回执。

## 目录

- `src/childcare_subsidy_ledger/events.py`：事件契约与仅追加 JSONL 存储。
- `src/childcare_subsidy_ledger/compute.py`：政策 × 容量 × 出勤的资格计算。
- `src/childcare_subsidy_ledger/ledger.py`：命令、角色、并发控制与状态机。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `tests/`：行为测试（资格、冻结、调整、支付、追缴、申诉、角色、
  幂等、并发、重启恢复、季度对账）。

## 测试与构建

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 最小示例

```python
from src.childcare_subsidy_ledger import EventStore, Ledger

admin = {"id": "a", "role": "ADMIN"}
operator = {"id": "o", "role": "ORG_OPERATOR", "org_id": "org-1"}
reviewer = {"id": "r", "role": "REVIEWER"}
payer = {"id": "p", "role": "PAYER"}

ledger = Ledger(EventStore("events.jsonl"))  # 重启后重放即可恢复
ledger.publish_policy(admin, policy_id="pol", version=1,
                      institution_type="community", age_band="toddler",
                      effective_from="2026-07-01", daily_rate_cents=1000,
                      closure_relief_rate=50)
ledger.declare_capacity(operator, org_id="org-1", month="2026-07",
                        institution_type="community", slots={"toddler": 30})
ledger.record_attendance(operator, org_id="org-1", child_id="c1",
                         date="2026-07-01", age_band="toddler",
                         material_id="mat-c1-2026-07-01")
ledger.submit_claim(operator, org_id="org-1", month="2026-07",
                    expected_version=0)
ledger.review_claim(reviewer, claim_id="claim-org-1-2026-07",
                    decisions=[{"child_id": "c1", "approved": True}])
batch = ledger.create_payment_batch(payer, org_id="org-1")
ledger.post_payment(payer, batch_id=batch["payload"]["batch_id"])
```
