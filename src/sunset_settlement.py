"""临展退场统一清算领域服务。

在既有退场事件资料之上承担统一清算：

* 闭展决定先锁定商品版本、渠道、库存所有权与合同版本；
* 直营网店、门店与仓库的回报按内容识别同一事实，迟到或重复不重复入账；
  同标识异内容仅冻结对应渠道的对应货品，其余货品继续推进，总量始终守恒；
* 结合在途订单、退货期、质量隔离与保留样品，把可处置量划分为
  退回供应商、转常设销售、捐赠、销毁的互斥份额，
  任何时点都不得侵占已付款订单与售后预留；
* 批准处置的人不能执行出库；
* 退款、下架、出库步骤幂等落账，途中停机后重新处理只补齐未落账步骤；
* 费用与寄售分成按闭展时锁定的合同版本结算；清算完成后财务可按
  单件商品或整个批次核对订单保护、库存去向、审批责任与结算凭证，
  未解决差异保持明确占用。

服务内维护一份只增不改的事件流水（``events()``），每个事件都满足
``exhibition_product_sunset.validate_event`` 的交换契约，可作为审计依据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .exhibition_product_sunset import (
    CHANNEL_EXPOSURE_REPORTED,
    CHANNEL_REPORT_CONFLICT,
    DISPOSAL_PLAN_LOCKED,
    DISPOSAL_STEP_RECORDED,
    EXHIBITION_CLOSING,
    ITEM_DISPOSED,
    ORDER_PROTECTED,
    SETTLEMENT_STATEMENT_ISSUED,
    SUNSET_ACTION_APPROVED,
)


# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------

class SunsetError(Exception):
    """清算领域错误基类。"""


class UnknownExhibitionError(SunsetError):
    """闭展决定尚未锁定。"""


class UnknownProductError(SunsetError):
    """商品不在闭展基线内。"""


class UnknownPlanError(SunsetError):
    """处置计划不存在或已被新版本取代。"""


class DuplicateClosureError(SunsetError):
    """同一展览的闭展决定只能锁定一次，合同版本不可替换。"""


class ConservationError(SunsetError):
    """总量守恒被破坏。"""


class DutySeparationError(SunsetError):
    """批准处置的人不能执行出库。"""


class PlanNotApprovedError(SunsetError):
    """处置计划尚未批准。"""


class PlanStaleError(SunsetError):
    """计划依据的回报事实已变化，需重新划分份额后再执行。"""


class ChannelFrozenError(SunsetError):
    """渠道因同标识异内容被冻结，其回报不再受理。"""


class SimulatedOutage(SunsetError):
    """演练用：模拟退款、下架或出库途中停机。"""


# ---------------------------------------------------------------------------
# 枚举与值对象
# ---------------------------------------------------------------------------

class Ownership(str, Enum):
    """库存所有权。"""

    OWN = "OWN"  # 自有
    CONSIGNMENT = "CONSIGNMENT"  # 寄售


class ShareKind(str, Enum):
    """互斥处置份额。"""

    RETURN_TO_SUPPLIER = "RETURN_TO_SUPPLIER"  # 退回供应商
    TRANSFER_TO_PERMANENT = "TRANSFER_TO_PERMANENT"  # 转常设销售
    DONATION = "DONATION"  # 捐赠
    DESTRUCTION = "DESTRUCTION"  # 销毁


class StepKind(str, Enum):
    """处置落账步骤。"""

    REFUND = "REFUND"  # 退款
    DELIST = "DELIST"  # 下架
    OUTBOUND = "OUTBOUND"  # 出库


class ReportStatus(str, Enum):
    """渠道回报受理结果。"""

    ACCEPTED = "ACCEPTED"  # 新事实，已入账
    DUPLICATE = "DUPLICATE"  # 内容相同的迟到或重复回报，已识别为同一事实
    CONFLICT = "CONFLICT"  # 同标识异内容，对应渠道货品已冻结


# 份额的确定性与展示顺序
_SHARE_ORDER = {
    ShareKind.RETURN_TO_SUPPLIER: 0,
    ShareKind.TRANSFER_TO_PERMANENT: 1,
    ShareKind.DONATION: 2,
    ShareKind.DESTRUCTION: 3,
}


@dataclass(frozen=True)
class ContractTerms:
    """闭展时锁定的合同条件。"""

    version: str
    disposal_fee_rate: float  # 销毁处置费用率（按销毁数量 × 单价计）
    consignment_share_rate: float  # 寄售合作方分成比例（按已售寄售数量 × 单价计）


@dataclass(frozen=True)
class BaselineItem:
    """闭展决定锁定的一件商品。"""

    product_id: str
    version: str  # 商品版本
    channels: tuple[str, ...]  # 期望回报的渠道
    ownership: Ownership  # 库存所有权
    quantity: int  # 闭展时点账面数量
    unit_price: float


@dataclass(frozen=True)
class ChannelReport:
    """渠道回报的库存事实。

    六个数量字段把该渠道负责的数量瓜分完毕：在架实盘、已付款在途订单、
    售后换货预留、退货期暂扣、质量隔离、保留样品。
    """

    report_id: str
    exhibition_id: str
    channel: str
    product_id: str
    on_hand: int
    paid_orders: int
    after_sales_reserved: int
    return_period_held: int
    quarantined: int
    samples: int

    def accounted(self) -> int:
        """该回报负责说明的总数量。"""
        return (
            self.on_hand
            + self.paid_orders
            + self.after_sales_reserved
            + self.return_period_held
            + self.quarantined
            + self.samples
        )

    def content_key(self) -> str:
        """按内容识别同一事实，与 report_id 无关。"""
        body = {
            "exhibition_id": self.exhibition_id,
            "channel": self.channel,
            "product_id": self.product_id,
            "on_hand": self.on_hand,
            "paid_orders": self.paid_orders,
            "after_sales_reserved": self.after_sales_reserved,
            "return_period_held": self.return_period_held,
            "quarantined": self.quarantined,
            "samples": self.samples,
        }
        blob = json.dumps(body, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class ReportOutcome:
    """渠道回报受理结果。"""

    status: ReportStatus
    report_id: str  # DUPLICATE 时为首次入账的 report_id
    detail: str


@dataclass(frozen=True)
class ShareAllocation:
    """一条互斥处置份额。"""

    kind: ShareKind
    quantity: int
    source: str  # "FREE" 在架自由量 | "QUARANTINE" 质量隔离


@dataclass(frozen=True)
class StepRecord:
    """已落账的处置步骤。"""

    key: str
    kind: StepKind
    actor: str
    recorded_at: str
    detail: dict


@dataclass
class PlanState:
    """处置计划当前状态。"""

    plan_id: str
    version: int
    shares: tuple[ShareAllocation, ...]
    refunds: dict[str, float]  # 无法履约需退款的订单：order_id -> 金额
    approved_by: str | None = None
    stale: bool = False
    steps: dict[str, StepRecord] = field(default_factory=dict)

    def outbounded(self) -> dict[ShareKind, int]:
        """各份额已出库数量（跨版本累计）。"""
        totals: dict[ShareKind, int] = {}
        for rec in self.steps.values():
            if rec.kind is StepKind.OUTBOUND:
                kind = ShareKind(rec.detail["share"])
                totals[kind] = totals.get(kind, 0) + rec.detail["quantity"]
        return totals


@dataclass(frozen=True)
class SettlementVoucher:
    """结算凭证：费用与寄售分成按闭展时锁定的合同版本计算。"""

    product_id: str
    contract_version: str
    disposal_fee: float  # 销毁处置费用
    consignment_payout: float  # 寄售合作方分成
    refund_total: float  # 退款合计


@dataclass(frozen=True)
class SettlementStatement:
    """单件商品的清算核对视图。"""

    exhibition_id: str
    product_id: str
    plan_id: str | None
    baseline_quantity: int
    order_protection: dict  # 订单保护：在途已付款、售后预留、退货期暂扣、已退款
    destinations: dict  # 库存去向：份额 -> 计划/已出库/待出库
    responsibility: dict  # 审批责任：批准人、执行人、职责分离校验
    voucher: SettlementVoucher | None
    samples_retained: int  # 保留样品
    unresolved_difference: int  # 未解决差异，保持明确占用
    missing_channels: tuple[str, ...]  # 尚未回报的渠道
    stale: bool
    conserved: bool  # 总量守恒校验
    completed: bool  # 全部步骤已落账


@dataclass(frozen=True)
class BatchStatement:
    """一个批次（整场闭展）的清算核对视图。"""

    exhibition_id: str
    statements: tuple[SettlementStatement, ...]
    conserved: bool
    unresolved_difference: int


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------

@dataclass
class _ProductLedger:
    """单件商品的清算台账。"""

    item: BaselineItem
    facts: dict[str, ChannelReport] = field(default_factory=dict)  # content_key -> 已入账事实
    report_keys: dict[str, str] = field(default_factory=dict)  # report_id -> content_key
    frozen_channels: set[str] = field(default_factory=set)
    plan: PlanState | None = None
    statement_issued: bool = False

    def totals(self) -> dict[str, int]:
        t = {
            "on_hand": 0,
            "paid_orders": 0,
            "after_sales_reserved": 0,
            "return_period_held": 0,
            "quarantined": 0,
            "samples": 0,
        }
        for rep in self.facts.values():
            t["on_hand"] += rep.on_hand
            t["paid_orders"] += rep.paid_orders
            t["after_sales_reserved"] += rep.after_sales_reserved
            t["return_period_held"] += rep.return_period_held
            t["quarantined"] += rep.quarantined
            t["samples"] += rep.samples
        return t


@dataclass
class _ExhibitionBaseline:
    """一场闭展锁定的基线。"""

    exhibition_id: str
    decided_by: str
    contract: ContractTerms
    items: dict[str, BaselineItem]
    ledgers: dict[str, _ProductLedger]


# ---------------------------------------------------------------------------
# 统一清算服务
# ---------------------------------------------------------------------------

class SunsetSettlement:
    """临展退场统一清算服务。"""

    def __init__(self, clock: Callable[[], str] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._events: list[dict] = []
        self._baselines: dict[str, _ExhibitionBaseline] = {}
        self._plan_index: dict[str, tuple[str, str]] = {}  # plan_id -> (exhibition_id, product_id)

    # -- 闭展决定 -----------------------------------------------------------

    def lock_closure(
        self,
        exhibition_id: str,
        items: list[BaselineItem],
        contract: ContractTerms,
        decided_by: str,
    ) -> None:
        """闭展决定：锁定商品版本、渠道、库存所有权与合同版本。

        同一场展览只能锁定一次；此后的费用与寄售分成一律以本次锁定的
        合同版本为准。
        """
        if exhibition_id in self._baselines:
            raise DuplicateClosureError(f"{exhibition_id} 的闭展决定已锁定，合同版本不可替换")
        if not decided_by:
            raise SunsetError("闭展决定必须记录决定人")
        if not (0.0 <= contract.disposal_fee_rate <= 1.0):
            raise SunsetError("处置费用率必须落在 [0, 1]")
        if not (0.0 <= contract.consignment_share_rate <= 1.0):
            raise SunsetError("寄售分成比例必须落在 [0, 1]")
        item_map: dict[str, BaselineItem] = {}
        for item in items:
            if item.product_id in item_map:
                raise SunsetError(f"商品 {item.product_id} 在闭展决定中重复")
            if item.quantity < 0 or item.unit_price < 0:
                raise SunsetError("数量与单价不得为负")
            if not item.channels:
                raise SunsetError("闭展决定必须固定商品的渠道")
            item_map[item.product_id] = item
        if not item_map:
            raise SunsetError("闭展决定至少包含一件商品")
        self._baselines[exhibition_id] = _ExhibitionBaseline(
            exhibition_id=exhibition_id,
            decided_by=decided_by,
            contract=contract,
            items=item_map,
            ledgers={pid: _ProductLedger(item=it) for pid, it in item_map.items()},
        )
        self._emit(
            EXHIBITION_CLOSING,
            exhibition_id,
            {
                "decided_by": decided_by,
                "contract_version": contract.version,
                "items": [
                    {
                        "product_id": it.product_id,
                        "version": it.version,
                        "channels": list(it.channels),
                        "ownership": it.ownership.value,
                        "quantity": it.quantity,
                    }
                    for it in item_map.values()
                ],
            },
        )

    # -- 渠道回报 -----------------------------------------------------------

    def ingest_report(self, report: ChannelReport) -> ReportOutcome:
        """受理渠道回报。

        * 内容相同的回报（无论 report_id 是否相同）识别为同一事实，不重复入账；
        * 同 report_id 异内容：撤回该渠道已入账事实并冻结该渠道的该货品，
          其他渠道与其他货品不受影响；
        * 总量始终守恒：被撤回或尚未回报的数量转入未解决差异，保持占用。
        """
        _, ledger = self._ledger(report.exhibition_id, report.product_id)
        if report.channel not in ledger.item.channels:
            raise SunsetError(f"渠道 {report.channel} 不在闭展决定固定的渠道内")
        quantities = (
            report.on_hand,
            report.paid_orders,
            report.after_sales_reserved,
            report.return_period_held,
            report.quarantined,
            report.samples,
        )
        if any(value < 0 for value in quantities):
            raise SunsetError("回报数量不得为负")
        if report.channel in ledger.frozen_channels:
            raise ChannelFrozenError(
                f"渠道 {report.channel} 的 {report.product_id} 已冻结，回报不再受理"
            )
        key = report.content_key()
        if key in ledger.facts:
            first = ledger.facts[key]
            return ReportOutcome(ReportStatus.DUPLICATE, first.report_id, "内容相同的回报，已识别为同一事实")
        if report.report_id in ledger.report_keys:
            # 同标识异内容：仅冻结对应渠道的对应货品
            for old_key in [k for k, r in ledger.facts.items() if r.channel == report.channel]:
                old = ledger.facts.pop(old_key)
                ledger.report_keys.pop(old.report_id, None)
            ledger.frozen_channels.add(report.channel)
            self._mark_stale(ledger)
            self._emit(
                CHANNEL_REPORT_CONFLICT,
                report.product_id,
                {
                    "exhibition_id": report.exhibition_id,
                    "channel": report.channel,
                    "report_id": report.report_id,
                    "reason": "同标识异内容",
                    "frozen": True,
                },
            )
            return ReportOutcome(ReportStatus.CONFLICT, report.report_id, "同标识异内容，对应渠道货品已冻结")
        ledger.facts[key] = report
        ledger.report_keys[report.report_id] = key
        self._mark_stale(ledger)
        self._emit(
            CHANNEL_EXPOSURE_REPORTED,
            report.product_id,
            {
                "exhibition_id": report.exhibition_id,
                "channel": report.channel,
                "report_id": report.report_id,
                "on_hand": report.on_hand,
                "paid_orders": report.paid_orders,
                "after_sales_reserved": report.after_sales_reserved,
                "return_period_held": report.return_period_held,
                "quarantined": report.quarantined,
                "samples": report.samples,
            },
        )
        return ReportOutcome(ReportStatus.ACCEPTED, report.report_id, "新事实已入账")

    # -- 份额划分 -----------------------------------------------------------

    def compute_plan(
        self,
        exhibition_id: str,
        product_id: str,
        own_disposal: dict[ShareKind, int] | None = None,
        refunds: dict[str, float] | None = None,
    ) -> PlanState:
        """划分互斥处置份额。

        可处置量只来自实盘在架，且以账面扣除保护与预留后的余量为上限，
        因此任何时点都不会侵占已付款订单与售后预留；质量隔离品一律就地
        销毁不回流，保留样品不进入处置份额；寄售商品的在架自由量只能
        退回供应商。未对清的数量保持为未解决差异。

        事实未变化时重复调用返回既有计划；事实变化后（计划已过期）调用
        生成新版本，已落账步骤保留，需重新批准。
        """
        baseline, ledger = self._ledger(exhibition_id, product_id)
        item = ledger.item
        if own_disposal and item.ownership is Ownership.CONSIGNMENT:
            raise SunsetError("寄售商品只能退回供应商，不接受自有划分口径")
        if ledger.plan is not None and not ledger.plan.stale:
            return ledger.plan
        totals = ledger.totals()
        protected = totals["paid_orders"] + totals["after_sales_reserved"] + totals["return_period_held"]
        headroom = item.quantity - protected - totals["quarantined"] - totals["samples"]
        free = min(totals["on_hand"], max(0, headroom))
        unresolved = item.quantity - protected - totals["quarantined"] - totals["samples"] - free
        shares: list[ShareAllocation] = []
        if totals["quarantined"]:
            shares.append(ShareAllocation(ShareKind.DESTRUCTION, totals["quarantined"], "QUARANTINE"))
        if free:
            if item.ownership is Ownership.CONSIGNMENT:
                shares.append(ShareAllocation(ShareKind.RETURN_TO_SUPPLIER, free, "FREE"))
            else:
                shares.extend(
                    ShareAllocation(kind, qty, "FREE")
                    for kind, qty in self._own_allocation(own_disposal, free)
                )
        refunds = dict(refunds or {})
        if any(amount < 0 for amount in refunds.values()):
            raise SunsetError("退款金额不得为负")
        # 总量守恒：保护 + 样品 + 份额 + 差异 == 账面
        accounted = protected + totals["samples"] + sum(s.quantity for s in shares) + unresolved
        if accounted != item.quantity:
            raise ConservationError(f"总量不守恒：{accounted} != 账面 {item.quantity}")
        previous = ledger.plan
        outbounded = previous.outbounded() if previous else {}
        for kind, qty in outbounded.items():
            planned = sum(s.quantity for s in shares if s.kind is kind)
            if planned < qty:
                raise ConservationError(f"{kind.value} 已出库 {qty}，新划分仅 {planned}，事实冲突")
        version = (previous.version + 1) if previous else 1
        plan = PlanState(
            plan_id=f"{exhibition_id}:{product_id}:v{version}",
            version=version,
            shares=tuple(sorted(shares, key=lambda s: _SHARE_ORDER[s.kind])),
            refunds=refunds,
            steps=previous.steps if previous else {},
        )
        if previous is not None:
            self._plan_index.pop(previous.plan_id, None)
        ledger.plan = plan
        self._plan_index[plan.plan_id] = (exhibition_id, product_id)
        self._emit(
            DISPOSAL_PLAN_LOCKED,
            product_id,
            {
                "plan_id": plan.plan_id,
                "version": version,
                "contract_version": baseline.contract.version,
                "shares": [
                    {"kind": s.kind.value, "quantity": s.quantity, "source": s.source}
                    for s in plan.shares
                ],
                "protected": protected,
                "samples_retained": totals["samples"],
                "unresolved": unresolved,
            },
        )
        self._emit(
            ORDER_PROTECTED,
            product_id,
            {
                "plan_id": plan.plan_id,
                "paid_orders": totals["paid_orders"],
                "after_sales_reserved": totals["after_sales_reserved"],
                "return_period_held": totals["return_period_held"],
                "refunds": dict(refunds),
            },
        )
        return plan

    @staticmethod
    def _own_allocation(
        own_disposal: dict[ShareKind, int] | None, free: int
    ) -> list[tuple[ShareKind, int]]:
        """自有商品在架自由量的划分口径，合计必须恰好等于可处置量。"""
        if own_disposal is None:
            return [(ShareKind.TRANSFER_TO_PERMANENT, free)] if free else []
        allocation: dict[ShareKind, int] = {}
        for raw_kind, qty in own_disposal.items():
            kind = ShareKind(raw_kind)
            if kind is ShareKind.RETURN_TO_SUPPLIER:
                raise SunsetError("自有商品不能退回供应商")
            if qty < 0:
                raise SunsetError("份额数量不得为负")
            allocation[kind] = allocation.get(kind, 0) + qty
        if sum(allocation.values()) != free:
            raise SunsetError(f"自有划分合计必须等于可处置量 {free}")
        return [(kind, qty) for kind, qty in allocation.items() if qty]

    # -- 批准与执行 -----------------------------------------------------------

    def approve_plan(self, plan_id: str, actor: str) -> PlanState:
        """批准处置计划；批准人此后不能执行该计划的出库步骤。"""
        plan, baseline, ledger = self._plan(plan_id)
        if plan.stale:
            raise PlanStaleError("回报事实已变化，需重新划分份额后再批准")
        if not actor:
            raise SunsetError("批准人不能为空")
        plan.approved_by = actor
        self._emit(
            SUNSET_ACTION_APPROVED,
            ledger.item.product_id,
            {
                "plan_id": plan_id,
                "approved_by": actor,
                "contract_version": baseline.contract.version,
            },
        )
        return plan

    def run_plan(self, plan_id: str, actor: str, fail_after: int | None = None) -> list[str]:
        """执行处置步骤（退款、下架、出库），幂等落账。

        已落账的步骤自动跳过，重新处理只补齐未落账步骤；``fail_after``
        用于演练：在落账指定步数后抛出 ``SimulatedOutage`` 模拟途中停机。
        批准人不能执行出库步骤（职责分离）。
        返回本次新落账的步骤标识。
        """
        plan, _, ledger = self._plan(plan_id)
        if plan.approved_by is None:
            raise PlanNotApprovedError("处置计划尚未批准")
        if plan.stale:
            raise PlanStaleError("回报事实已变化，需重新划分份额后再执行")
        if not actor:
            raise SunsetError("执行人不能为空")
        required = self._required_steps(ledger, plan)
        pending = [step for step in required if step[0] not in plan.steps]
        if actor == plan.approved_by and any(kind is StepKind.OUTBOUND for _, kind, _ in pending):
            raise DutySeparationError("批准处置的人不能执行出库")
        newly: list[str] = []
        for key, kind, detail in pending:
            plan.steps[key] = StepRecord(
                key=key, kind=kind, actor=actor, recorded_at=self._clock(), detail=detail
            )
            self._emit(
                DISPOSAL_STEP_RECORDED,
                ledger.item.product_id,
                {"plan_id": plan_id, "step": key, "actor": actor, **detail},
            )
            if kind is StepKind.OUTBOUND:
                self._emit(
                    ITEM_DISPOSED,
                    ledger.item.product_id,
                    {
                        "plan_id": plan_id,
                        "share": detail["share"],
                        "quantity": detail["quantity"],
                        "executor": actor,
                    },
                )
            newly.append(key)
            if fail_after is not None and len(newly) >= fail_after:
                raise SimulatedOutage(f"模拟停机：已落账 {len(newly)} 步，剩余步骤待重新处理补齐")
        return newly

    def _required_steps(self, ledger: _ProductLedger, plan: PlanState) -> list[tuple[str, StepKind, dict]]:
        """计划当前要求的全部步骤（含已落账），顺序确定。"""
        steps: list[tuple[str, StepKind, dict]] = [
            ("DELIST", StepKind.DELIST, {"channels": list(ledger.item.channels)})
        ]
        for order_id in sorted(plan.refunds):
            steps.append(
                (f"REFUND:{order_id}", StepKind.REFUND, {"order_id": order_id, "amount": plan.refunds[order_id]})
            )
        outbounded = plan.outbounded()
        planned: dict[ShareKind, int] = {}
        for share in plan.shares:
            planned[share.kind] = planned.get(share.kind, 0) + share.quantity
        for kind in sorted(planned, key=_SHARE_ORDER.__getitem__):
            remaining = planned[kind] - outbounded.get(kind, 0)
            if remaining <= 0:
                continue
            key = f"OUTBOUND:{kind.value}"
            if key in plan.steps:
                key = f"{key}#v{plan.version}"  # 重新划分后的补差出库
            steps.append((key, StepKind.OUTBOUND, {"share": kind.value, "quantity": remaining}))
        return steps

    # -- 清算核对 -----------------------------------------------------------

    def statement_for_product(self, exhibition_id: str, product_id: str) -> SettlementStatement:
        """单件商品的清算核对视图：订单保护、库存去向、审批责任、结算凭证。"""
        baseline, ledger = self._ledger(exhibition_id, product_id)
        item = ledger.item
        totals = ledger.totals()
        plan = ledger.plan
        refunds = plan.refunds if plan else {}
        required = self._required_steps(ledger, plan) if plan else []
        recorded = plan.steps if plan else {}
        completed = plan is not None and all(key in recorded for key, _, _ in required)
        outbounded = plan.outbounded() if plan else {}
        planned: dict[ShareKind, int] = {}
        if plan:
            for share in plan.shares:
                planned[share.kind] = planned.get(share.kind, 0) + share.quantity
        destinations = {
            kind.value: {
                "quantity": qty,
                "outbounded": outbounded.get(kind, 0),
                "pending": qty - outbounded.get(kind, 0),
            }
            for kind, qty in planned.items()
        }
        executors = sorted({rec.actor for rec in recorded.values()})
        approved_by = plan.approved_by if plan else None
        voucher = None
        if plan is not None:
            destruction = planned.get(ShareKind.DESTRUCTION, 0)
            payout = 0.0
            if item.ownership is Ownership.CONSIGNMENT:
                payout = totals["paid_orders"] * item.unit_price * baseline.contract.consignment_share_rate
            voucher = SettlementVoucher(
                product_id=product_id,
                contract_version=baseline.contract.version,
                disposal_fee=round(destruction * item.unit_price * baseline.contract.disposal_fee_rate, 2),
                consignment_payout=round(payout, 2),
                refund_total=round(sum(refunds.values()), 2),
            )
        protected = totals["paid_orders"] + totals["after_sales_reserved"] + totals["return_period_held"]
        shares_total = sum(planned.values())
        if plan is not None:
            unresolved = item.quantity - protected - totals["samples"] - shares_total
        else:
            unresolved = (
                item.quantity - protected - totals["samples"] - totals["quarantined"] - totals["on_hand"]
            )
        conserved = protected + totals["samples"] + shares_total + unresolved == item.quantity
        reported_channels = {rep.channel for rep in ledger.facts.values()}
        missing = tuple(ch for ch in item.channels if ch not in reported_channels)
        if completed and not ledger.statement_issued:
            ledger.statement_issued = True
            self._emit(
                SETTLEMENT_STATEMENT_ISSUED,
                product_id,
                {"plan_id": plan.plan_id, "exhibition_id": exhibition_id},
            )
        return SettlementStatement(
            exhibition_id=exhibition_id,
            product_id=product_id,
            plan_id=plan.plan_id if plan else None,
            baseline_quantity=item.quantity,
            order_protection={
                "paid_orders": totals["paid_orders"],
                "after_sales_reserved": totals["after_sales_reserved"],
                "return_period_held": totals["return_period_held"],
                "refunded": dict(refunds),
            },
            destinations=destinations,
            responsibility={
                "approved_by": approved_by,
                "executors": executors,
                "duty_separation_ok": approved_by is None or approved_by not in executors,
            },
            voucher=voucher,
            samples_retained=totals["samples"],
            unresolved_difference=unresolved,
            missing_channels=missing,
            stale=plan.stale if plan else False,
            conserved=conserved,
            completed=completed,
        )

    def statement_for_batch(self, exhibition_id: str) -> BatchStatement:
        """一个批次的清算核对视图：财务可整批核对，差异汇总保持占用。"""
        baseline = self._baselines.get(exhibition_id)
        if baseline is None:
            raise UnknownExhibitionError(f"闭展决定不存在：{exhibition_id}")
        statements = tuple(self.statement_for_product(exhibition_id, pid) for pid in baseline.items)
        return BatchStatement(
            exhibition_id=exhibition_id,
            statements=statements,
            conserved=all(s.conserved for s in statements),
            unresolved_difference=sum(s.unresolved_difference for s in statements),
        )

    # -- 查询 ---------------------------------------------------------------

    def current_plan(self, exhibition_id: str, product_id: str) -> PlanState | None:
        """商品当前的处置计划（可能已过期）。"""
        _, ledger = self._ledger(exhibition_id, product_id)
        return ledger.plan

    def events(self) -> list[dict]:
        """只增不改的事件流水，每个事件都满足领域交换契约。"""
        return [dict(event) for event in self._events]

    # -- 内部 ---------------------------------------------------------------

    def _ledger(self, exhibition_id: str, product_id: str) -> tuple[_ExhibitionBaseline, _ProductLedger]:
        baseline = self._baselines.get(exhibition_id)
        if baseline is None:
            raise UnknownExhibitionError(f"闭展决定不存在：{exhibition_id}")
        ledger = baseline.ledgers.get(product_id)
        if ledger is None:
            raise UnknownProductError(f"商品不在闭展基线内：{product_id}")
        return baseline, ledger

    def _plan(self, plan_id: str) -> tuple[PlanState, _ExhibitionBaseline, _ProductLedger]:
        location = self._plan_index.get(plan_id)
        if location is None:
            raise UnknownPlanError(f"处置计划不存在或已被新版本取代：{plan_id}")
        baseline, ledger = self._ledger(*location)
        return ledger.plan, baseline, ledger

    @staticmethod
    def _mark_stale(ledger: _ProductLedger) -> None:
        if ledger.plan is not None:
            ledger.plan.stale = True

    def _emit(self, kind: str, subject_id: str, payload: dict) -> None:
        self._events.append(
            {
                "event_id": f"evt-{len(self._events) + 1:04d}",
                "kind": kind,
                "occurred_at": self._clock(),
                "subject_id": subject_id,
                "payload": payload,
            }
        )
