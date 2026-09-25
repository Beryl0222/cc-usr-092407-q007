# 临展文创退场处置

临展闭展后的**统一清算**领域资料与最小可运行台账：让退场事件承担财务月结的统一依据——
闭展先锁定商品版本、渠道、库存所有权与合同版本，再结合在途订单、退货期、质量隔离、
保留样品，把库存划成退回供应商 / 转常设销售 / 捐赠 / 销毁四个互斥份额，并保证
已付款订单与售后预留任何时点都不被侵占。

## 清算规则（领域不变量）

1. **闭展快照先行**：`EXHIBITION_CLOSING` 固定商品版本、渠道、所有权、合同版本与期初头寸；
   闭展后到达的事实只能补录/挂差异，不能改写快照，也不能重开闭展。
2. **保护性占用优先**：已付款订单（含闭展后晚到的在途订单）、售后换货预留、质量隔离、
   退货期暂记、保留样品先从可分配池扣除。销毁计划若口径含售后预留，直接被拒绝。
3. **去向互斥、总量守恒**：每个（商品 × 渠道）恒满足
   `总头寸 = 保护性占用 + 待执行 + 已执行 + 冻结 + 未决 + 可分配`，
   四个去向的计划数量互不重叠，`conservation_check()` 随时可核对。
4. **按内容识别事实**：渠道/门店/仓库回报以业务内容指纹去重，迟到或重复提交不重复入账；
   同标识异内容只冻结对应渠道的对应货品（`blocked`），其他渠道、其他货品照常推进。
5. **职责分离**：批准人（`approver_id`）与出库执行人（`executor_id`）必须不同；
   未批准不出库，不按 `pick → outbound → confirm` 顺序不落账。
6. **断点补齐**：事件按 `event_id` 与出库步骤双重幂等；退款、下架、出库途中停机后，
   重放只返回提示并补齐未落账步骤，`open_steps()` 列出待补清单，已落账步骤不重复扣减。
7. **锁定合同结算**：分成与费用只按闭展时 `CONTRACT_LOCKED` 的合同版本与费率计算，
   版本不符或金额不符的凭证被拒绝。
8. **单品/批次可追溯**：`subject_view(sku)` 与 `batch_view(batch_id)` 一次返回
   订单保护、四向去向、审批责任与结算凭证；未解决差异保留在 `frozen`/`unresolved`
   明确占用中，绝不被任何处置份额吞掉。

## 目录

- `src/exhibition_product_sunset.py`：事件种类、载荷字段契约与内容指纹。
- `src/liquidation.py`：事件溯源式统一清算台账（`LiquidationLedger`）。
- `data/sample.json`：最小闭展事件（格式核对用虚构资料）。
- `data/scenario.json`：完整虚构清算事件流——晚到门店新订单、重复回报、
  同标识异内容渠道冻结、售后预留剔除、四去向处置、分步出库、寄售分成结算。
- `tests/`：契约测试与全部不变量测试（守恒、幂等、冻结隔离、职责分离、断点恢复、
  合同版本、单品/批次追溯）。

## 本地核对

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
```

## 最小用法

```python
import json
from src.liquidation import LiquidationLedger

ledger = LiquidationLedger()
notes = ledger.apply_all(json.load(open("data/scenario.json", encoding="utf-8")))
assert ledger.conservation_check() == []        # 总量守恒
ledger.subject_view("SKU-LX-001")               # 从一件商品核对全部清算结果
ledger.batch_view("B202608")                    # 或从一个批次反向核对
ledger.open_steps()                             # 停机恢复：未落账步骤清单
```

所有数据均为虚构，不含真实个人信息、生产连接或外部账号。
