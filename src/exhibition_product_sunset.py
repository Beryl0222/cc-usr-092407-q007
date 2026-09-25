"""exhibition_product_sunset 领域资料的基础结构。

事件种类
--------
EXHIBITION_CLOSING          闭展决定：固定商品版本、渠道清单、库存所有权与合同版本
CONTRACT_LOCKED             合同快照（费用/寄售分成条款按闭展时版本锁定）
CHANNEL_REPORTED            渠道/仓库回报：迟到、重复均按内容识别同一事实
DISCREPANCY_FOUND           同标识异内容：仅冻结对应渠道的对应货品，其他继续推进
RETURN_TO_SUPPLIER_PLANNED  退回供应商计划（待批）
DONATION_PLANNED            捐赠计划（待批）
PERMANENT_SALE_PLANNED      转常设销售计划（待批）
DESTRUCTION_PLANNED         销毁计划（待批，含仓库误列的售后预留须先剔除）
DISPOSAL_APPROVED           处置批准（明细行级）
DISPOSAL_REJECTED           处置驳回
ORDER_PROTECTED             已付款订单预留（任何时点不得侵占）
AFTERSALES_RESERVED         售后换货预留（任何时点不得侵占，含在途/待判退）
QUALITY_QUARANTINED         质量隔离
RETURN_WINDOW_OPEN         退货期内暂记占用
RETAINED_SAMPLE             保留样品
IN_TRANSIT_ORDER            在途订单事实（可能晚于账面下架到达）
STOCK_POSITION              库存头寸：供应商寄售 / 自营库存 / 售后预留所有权

STATUS 枚举
-----------
pending     计划待批
frozen      对应渠道冻结（同标识异内容），该渠道该 sku 暂停一切变动
approved    已批准，待执行
rejected    已驳回
executed    已出库/已完成执行
settled     已结算（分成/费用按锁定合同版本）

渠道
----
flagship_ec   直营网店
consignment   寄售门店
warehouse     仓库
supplier      供应商侧
"""

from __future__ import annotations

EVENT_KINDS = [
    "EXHIBITION_CLOSING",
    "CONTRACT_LOCKED",
    "CHANNEL_REPORTED",
    "DISCREPANCY_FOUND",
    "STOCK_POSITION",
    "IN_TRANSIT_ORDER",
    "ORDER_PROTECTED",
    "AFTERSALES_RESERVED",
    "QUALITY_QUARANTINED",
    "RETURN_WINDOW_OPEN",
    "RETAINED_SAMPLE",
    "RETURN_TO_SUPPLIER_PLANNED",
    "DONATION_PLANNED",
    "PERMANENT_SALE_PLANNED",
    "DESTRUCTION_PLANNED",
    "DISPOSAL_APPROVED",
    "DISPOSAL_REJECTED",
    "ITEM_DISPOSED",
    "SETTLEMENT_POSTED",
]

# 四种互斥去向
DESTINATIONS = ("RETURN_TO_SUPPLIER", "PERMANENT_SALE", "DONATION", "DESTRUCTION")
DESTINATION_EVENT = {
    "RETURN_TO_SUPPLIER": "RETURN_TO_SUPPLIER_PLANNED",
    "PERMANENT_SALE": "PERMANENT_SALE_PLANNED",
    "DONATION": "DONATION_PLANNED",
    "DESTRUCTION": "DESTRUCTION_PLANNED",
}

CHANNELS = ("flagship_ec", "consignment", "warehouse", "supplier")
STATUSES = ("pending", "frozen", "approved", "rejected", "executed", "settled")
OWNERSHIP = ("consigned", "owned", "aftersales_reserve", "sample", "in_transit")

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

# 各种事件必须携带的载荷字段（subject_id 统一取闭展主体，例如某个SKU或批次）
PAYLOAD_REQUIRED = {
    "EXHIBITION_CLOSING": ["product_version", "channels", "ownership", "contract_version"],
    "CONTRACT_LOCKED": ["contract_version", "consignment_rate", "destruction_fee_rule"],
    "CHANNEL_REPORTED": ["channel", "facts"],
    "DISCREPANCY_FOUND": ["channel", "subject_id", "first_content_hash", "second_content_hash"],
    "STOCK_POSITION": ["channel", "ownership", "quantity"],
    "IN_TRANSIT_ORDER": ["channel", "quantity", "order_ref"],
    "ORDER_PROTECTED": ["channel", "quantity", "order_ref"],
    "AFTERSALES_RESERVED": ["channel", "quantity", "reservation_id"],
    "QUALITY_QUARANTINED": ["channel", "quantity", "batch_id"],
    "RETURN_WINDOW_OPEN": ["channel", "quantity"],
    "RETAINED_SAMPLE": ["channel", "quantity", "sample_id"],
    "RETURN_TO_SUPPLIER_PLANNED": ["plan_id", "channel", "quantity"],
    "DONATION_PLANNED": ["plan_id", "channel", "quantity"],
    "PERMANENT_SALE_PLANNED": ["plan_id", "channel", "theoretical_qty", "available_qty"],
    "DESTRUCTION_PLANNED": ["plan_id", "channel", "theoretical_qty", "available_qty"],
    "DISPOSAL_APPROVED": ["plan_id", "destination", "channel", "quantity", "approver_id", "executor_id_hint"],
    "DISPOSAL_REJECTED": ["plan_id", "reason"],
    "ITEM_DISPOSED": ["plan_id", "destination", "channel", "step", "quantity", "executor_id"],
    "SETTLEMENT_POSTED": ["plan_id", "contract_version", "consignment_rate", "amount", "voucher_id"],
}

# 执行步骤（ITEM_DISPOSED 分步落账；停机重放只补齐未落账步骤）
DISPOSAL_STEPS = ("pick", "outbound", "confirm")


def content_hash(payload: dict) -> str:
    """事实内容指纹：同一内容的迟到/重复回报识别为同一事实。

    与来源渠道、上报时间、上报人无关，只对业务事实内容（数量、单据号、
    渠道、状态等）计算，方便幂等收单。
    """
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段与载荷字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in EVENT_KINDS:
        problems.append("kind")
        return problems
    payload = record.get("payload") or {}
    for name in PAYLOAD_REQUIRED.get(kind, []):
        if name not in payload:
            problems.append(f"payload.{name}")
    return problems
