"""exhibition_product_sunset 领域资料的基础结构。"""

from __future__ import annotations

# 事件种类，按清算链路排列：闭展决定 → 渠道回报 → 份额划分 → 批准 → 落账 → 凭证
EXHIBITION_CLOSING = "EXHIBITION_CLOSING"  # 闭展决定：锁定商品版本、渠道、库存所有权与合同版本
CHANNEL_EXPOSURE_REPORTED = "CHANNEL_EXPOSURE_REPORTED"  # 渠道库存回报入账
CHANNEL_REPORT_CONFLICT = "CHANNEL_REPORT_CONFLICT"  # 同标识异内容：冻结对应渠道的对应货品
DISPOSAL_PLAN_LOCKED = "DISPOSAL_PLAN_LOCKED"  # 互斥处置份额划分（退回供应商/转常设/捐赠/销毁）
ORDER_PROTECTED = "ORDER_PROTECTED"  # 已付款订单与售后预留保护
SUNSET_ACTION_APPROVED = "SUNSET_ACTION_APPROVED"  # 处置批准
DISPOSAL_STEP_RECORDED = "DISPOSAL_STEP_RECORDED"  # 退款/下架/出库步骤落账
ITEM_DISPOSED = "ITEM_DISPOSED"  # 出库完成
SETTLEMENT_STATEMENT_ISSUED = "SETTLEMENT_STATEMENT_ISSUED"  # 结算凭证出具

EVENT_KINDS = [
    EXHIBITION_CLOSING,
    CHANNEL_EXPOSURE_REPORTED,
    CHANNEL_REPORT_CONFLICT,
    DISPOSAL_PLAN_LOCKED,
    ORDER_PROTECTED,
    SUNSET_ACTION_APPROVED,
    DISPOSAL_STEP_RECORDED,
    ITEM_DISPOSED,
    SETTLEMENT_STATEMENT_ISSUED,
]
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
