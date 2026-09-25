"""退场事件统一清算台账。

设计要点（与业务陈述一一对应）
--------------------------------
1. 闭展快照先行：EXHIBITION_CLOSING 固定商品版本、渠道、库存所有权、合同版本；
   闭展之后到达的事实只能进入清算视图，不能改写快照本身。合同条款另由
   CONTRACT_LOCKED 固化，费用与寄售分成一律按锁定版本计算。
2. 保护性占用优先：已付款订单（含在途/晚到订单）、售后换货预留、质量隔离、
   退货期暂记、保留样品先从可分配池中扣除；销毁计划永远触碰不到售后预留。
3. 去向互斥且总量守恒：每个（主体, 渠道）头寸恒满足
       总头寸 = 保护性占用 + 四去向(待执行+已执行) + 冻结 + 未决 + 可分配
   退回供应商 / 转常设销售 / 捐赠 / 销毁 四份计划数量互不重叠。
4. 按内容识别事实：回报以业务内容指纹去重，迟到或重复提交不再产生影响；
   同一事实标识出现两种内容（同标识异内容）时，只冻结对应渠道的对应货品，
   其他渠道、其他货品照常推进。
5. 职责分离：批准人(approver)与出库执行人(executor)必须不同；未批准不出库，
   不按 pick→outbound→confirm 顺序不落账。
6. 断点补齐：出库/退款/下架步骤全部幂等，停机后重放只补齐未落账步骤，
   已完成的步骤不会重复扣减。
7. 可追溯：从一件商品(sku)或一个批次(batch)即可汇总订单保护、库存去向、
   审批责任与结算凭证；未解决差异保持明确占用（frozen/unresolved），
   绝不被任何去向吞掉。

冻结/未决的记账原则：它们是总头寸内部对可分配池的"重分类占用"，
凭空记账会破坏守恒；只有新申报的实物（如晚到在途量）才增加总头寸。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .exhibition_product_sunset import (
    DESTINATIONS,
    DISPOSAL_STEPS,
    content_hash,
    validate_event,
)

PROTECTED_KINDS = (
    "ORDER_PROTECTED",
    "AFTERSALES_RESERVED",
    "QUALITY_QUARANTINED",
    "RETURN_WINDOW_OPEN",
    "RETAINED_SAMPLE",
)

_FACT_HASH_KEYS = (
    "kind", "quantity", "order_ref", "reservation_id", "batch_id", "sample_id", "status",
)


class LedgerError(ValueError):
    """清算规则被违反（守恒、越权、顺序、合同版本等）。"""


@dataclass
class ChannelState:
    """单个（商品主体, 渠道）的头寸与占用。"""

    on_hand: int = 0                 # 闭展账面临展库存
    late_recorded: int = 0           # 闭展后晚到/在途补录（有实物依据，增加总头寸）
    protected: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    allocated: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    executed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    frozen: int = 0                  # 同标识异内容：从可分配池划出的冻结占用
    unresolved: int = 0              # 其他未决差异：从可分配池划出的明确占用
    blocked: bool = False            # 该（主体, 渠道）是否因差异被停止推进

    @property
    def total(self) -> int:
        return self.on_hand + self.late_recorded

    @property
    def protected_qty(self) -> int:
        return sum(self.protected.values())

    @property
    def allocated_qty(self) -> int:
        return sum(self.allocated.values())

    @property
    def executed_qty(self) -> int:
        return sum(self.executed.values())

    @property
    def used(self) -> int:
        return (self.protected_qty + self.allocated_qty
                + self.executed_qty + self.frozen + self.unresolved)

    @property
    def available(self) -> int:
        """可分配池：总头寸扣除保护、待执行/已执行去向、冻结与未决。"""
        return self.total - self.used

    def carve_frozen(self, qty: int) -> int:
        """从可分配池划出冻结占用，返回实际划出量（不侵占保护/已批份额）。"""
        carve = max(0, min(qty, self.available))
        self.frozen += carve
        return carve


@dataclass
class Plan:
    plan_id: str
    subject_id: str
    channel: str
    destination: str
    quantity: int
    status: str = "pending"          # pending/approved/rejected/executed/settled
    approver_id: str | None = None
    executor_id: str | None = None
    steps_done: list[str] = field(default_factory=list)
    batch_id: str | None = None
    settlement_voucher: str | None = None
    settlement_amount: int | None = None
    contract_version: str | None = None
    reject_reason: str | None = None


class LiquidationLedger:
    """事件溯源式清算台账：append 事件，派生头寸、计划、凭证。"""

    def __init__(self) -> None:
        self.closing: dict | None = None
        self.contract: dict | None = None
        self.states: dict[tuple[str, str], ChannelState] = {}
        self.plans: dict[str, Plan] = {}
        # 批次维度的质量隔离量（批次视角追溯用）
        self.batch_quarantine: dict[str, int] = defaultdict(int)
        # 事实去重：(subject, channel, 事实标识) -> (内容哈希, 数量)
        self.facts: dict[tuple[str, str, str], tuple[str, int]] = {}
        self.seen_events: set[str] = set()
        self.audit: list[str] = []

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #
    def apply(self, event: dict) -> list[str]:
        """应用一个事件，返回提示信息（重复回报、幂等空操作等）。

        违反硬性清算规则时抛 :class:`LedgerError`。
        """
        problems = validate_event(event)
        if problems:
            raise LedgerError(f"事件字段不完整 {event.get('event_id')}: {problems}")
        eid = event["event_id"]
        if eid in self.seen_events:
            return [f"duplicate event ignored: {eid}"]
        kind = event["kind"]
        handler = getattr(self, f"_on_{kind.lower()}")
        notes = handler(event["subject_id"], event["payload"]) or []
        self.seen_events.add(eid)
        return notes

    def apply_all(self, events: list[dict]) -> list[str]:
        notes: list[str] = []
        for ev in events:
            notes.extend(self.apply(ev))
        return notes

    def _state(self, subject: str, channel: str) -> ChannelState:
        key = (subject, channel)
        if key not in self.states:
            self.states[key] = ChannelState()
        return self.states[key]

    def _require_closing(self) -> None:
        if self.closing is None:
            raise LedgerError("闭展快照尚未固定，不能开始清算")

    # ------------------------------------------------------------------ #
    # 闭展锁定
    # ------------------------------------------------------------------ #
    def _on_exhibition_closing(self, subject: str, p: dict) -> list[str]:
        if self.closing is not None:
            raise LedgerError("闭展快照已固定，不得重开")
        if not p.get("product_version") or not p.get("channels"):
            raise LedgerError("闭展必须固定商品版本与渠道清单")
        self.closing = {
            "subject_id": subject,
            "product_version": p["product_version"],
            "channels": tuple(p["channels"]),
            "ownership": dict(p["ownership"]),
            "contract_version": p["contract_version"],
        }
        for ch, qty in (p.get("positions") or {}).items():
            self._state(subject, ch).on_hand = int(qty)
        self.audit.append(f"closing locked subject={subject} cv={p['contract_version']}")
        return []

    def _on_contract_locked(self, subject: str, p: dict) -> list[str]:
        self._require_closing()
        if p["contract_version"] != self.closing["contract_version"]:
            raise LedgerError("合同版本必须与闭展锁定版本一致")
        if self.contract is not None:
            raise LedgerError("合同条款已锁定")
        self.contract = dict(p)
        return []

    # ------------------------------------------------------------------ #
    # 渠道回报：按内容去重；同标识异内容只冻结（主体, 渠道）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fact_id(fact: dict) -> str:
        for k in ("fact_id", "order_ref", "reservation_id", "batch_id", "sample_id"):
            if fact.get(k):
                return str(fact[k])
        return content_hash(fact.get("facts", fact))

    @staticmethod
    def _fact_hash(fact: dict) -> str:
        return content_hash({k: fact[k] for k in _FACT_HASH_KEYS if k in fact})

    def _on_channel_reported(self, subject: str, p: dict) -> list[str]:
        self._require_closing()
        channel = p["channel"]
        notes: list[str] = []
        for fact in p["facts"]:
            key = (subject, channel, self._fact_id(fact))
            h = self._fact_hash(fact)
            qty = int(fact.get("quantity", 0))
            if key in self.facts:
                prev_h, prev_qty = self.facts[key]
                if prev_h == h:
                    notes.append(f"same content redelivery ignored: {key[2]}@{channel}")
                    continue
                notes.extend(self._freeze_conflict(subject, channel, key, prev_qty, qty))
                continue
            self.facts[key] = (h, qty)
            self._accept_fact(subject, channel, fact)
        return notes

    def _freeze_conflict(self, subject, channel, key, prev_qty, qty) -> list[str]:
        """同标识异内容：只冻结该（主体, 渠道），差异量配平挂冻结。

        不回滚、不削减第一笔已入账事实（已付款/售后保护永不被冲销）。
        新内容多报的实物补录总头寸并同额冻结；少于首报时首报维持有效。
        然后 blocked=True，该渠道该货品的处置计划一律停止；
        其他渠道、其他货品不受影响。
        """
        st = self._state(subject, channel)
        if qty > prev_qty:
            delta = qty - prev_qty
            st.late_recorded += delta
            st.frozen += delta
        st.blocked = True
        self.audit.append(
            f"FROZEN {key} channel={channel} disputed={max(prev_qty, qty)}"
        )
        return [f"conflicting fact frozen channel only: {key[2]}@{channel}"]

    def _accept_fact(self, subject: str, channel: str, fact: dict) -> None:
        """按回报事实内容派生占用/补录，与「谁上报、何时上报」无关。"""
        kind = fact["kind"]
        qty = int(fact.get("quantity", 0))
        st = self._state(subject, channel)
        if kind == "STOCK_POSITION":
            if fact.get("ownership") == "in_transit" or fact.get("late"):
                # 晚到实物补录总头寸，先挂未决占用，核对后再释放
                st.late_recorded += qty
                st.unresolved += qty
            # 同口径常规盘点仅留痕，不改写期初、不新增占用
        elif kind == "IN_TRANSIT_ORDER":
            # 寄售门店/网店晚到的新订单：补录头寸并立即被订单保护占用
            st.late_recorded += qty
            st.protected["ORDER_PROTECTED"] += qty
        elif kind in PROTECTED_KINDS:
            if st.available < qty:
                raise LedgerError(
                    f"保护性占用 {kind} 数量 {qty} 超过可分配头寸 {st.available}"
                )
            st.protected[kind] += qty
            if kind == "QUALITY_QUARANTINED" and fact.get("batch_id"):
                self.batch_quarantine[str(fact["batch_id"])] += qty
        else:
            raise LedgerError(f"未知回报事实类型: {kind}")

    def _on_discrepancy_found(self, subject: str, p: dict) -> list[str]:
        channel = p["channel"]
        st = self._state(subject, channel)
        qty = int(p.get("quantity", 0))
        carved = st.carve_frozen(qty) if qty else 0
        st.blocked = True
        self.audit.append(
            f"discrepancy frozen subject={subject} channel={channel} carved={carved}"
        )
        return [f"channel frozen only: {subject}@{channel} (carved={carved})"]

    # 保护性占用的直接事件（与 CHANNEL_REPORTED 派生等价，按事实号各自去重）
    def _protect(self, subject: str, p: dict, kind: str) -> None:
        self._require_closing()
        st = self._state(subject, p["channel"])
        qty = int(p["quantity"])
        if st.available < qty:
            raise LedgerError(f"保护性占用 {kind} 数量 {qty} 超过可分配头寸 {st.available}")
        st.protected[kind] += qty

    def _on_order_protected(self, s, p): self._protect(s, p, "ORDER_PROTECTED")
    def _on_aftersales_reserved(self, s, p): self._protect(s, p, "AFTERSALES_RESERVED")
    def _on_return_window_open(self, s, p): self._protect(s, p, "RETURN_WINDOW_OPEN")
    def _on_retained_sample(self, s, p): self._protect(s, p, "RETAINED_SAMPLE")

    def _on_quality_quarantined(self, subject: str, p: dict) -> None:
        self._protect(subject, p, "QUALITY_QUARANTINED")
        if p.get("batch_id"):
            self.batch_quarantine[str(p["batch_id"])] += int(p["quantity"])

    def _on_in_transit_order(self, subject: str, p: dict) -> None:
        self._require_closing()
        st = self._state(subject, p["channel"])
        qty = int(p["quantity"])
        st.late_recorded += qty
        st.protected["ORDER_PROTECTED"] += qty

    def _on_stock_position(self, subject: str, p: dict) -> None:
        if self.closing is not None:
            raise LedgerError("闭展后头寸只能通过回报事件补录/挂差异，不得改写期初")
        self._state(subject, p["channel"]).on_hand = int(p["quantity"])

    # ------------------------------------------------------------------ #
    # 处置计划：互斥、守恒；售后预留永不进销毁
    # ------------------------------------------------------------------ #
    def _plan(self, subject: str, p: dict, destination: str) -> None:
        self._require_closing()
        channel, qty = p["channel"], int(p["quantity"])
        plan_id = p["plan_id"]
        if plan_id in self.plans:
            raise LedgerError(f"计划号重复: {plan_id}")
        st = self._state(subject, channel)
        if st.blocked:
            raise LedgerError(f"{subject}@{channel} 已因差异冻结，停止处置")
        if destination == "DESTRUCTION":
            theoretical = int(p.get("theoretical_qty", qty))
            aftersales = st.protected.get("AFTERSALES_RESERVED", 0)
            # 仓库若把售后换货预留列入销毁口径，必须在此被挡住
            if theoretical > qty and theoretical - aftersales < qty:
                raise LedgerError("销毁计划不得包含售后换货预留")
        if st.available < qty:
            raise LedgerError(
                f"可分配量不足: {destination} 申请 {qty}，可分配 {st.available}"
                f"（保护 {st.protected_qty}/冻结 {st.frozen}/未决 {st.unresolved}）"
            )
        self.plans[plan_id] = Plan(
            plan_id=plan_id, subject_id=subject, channel=channel,
            destination=destination, quantity=qty, batch_id=p.get("batch_id"),
        )
        st.allocated[destination] += qty

    def _on_return_to_supplier_planned(self, s, p): self._plan(s, p, "RETURN_TO_SUPPLIER")
    def _on_donation_planned(self, s, p): self._plan(s, p, "DONATION")
    def _on_permanent_sale_planned(self, s, p): self._plan(s, p, "PERMANENT_SALE")
    def _on_destruction_planned(self, s, p): self._plan(s, p, "DESTRUCTION")

    # ------------------------------------------------------------------ #
    # 审批 / 驳回（批准人不能是执行人）
    # ------------------------------------------------------------------ #
    def _get_plan(self, plan_id: str) -> Plan:
        if plan_id not in self.plans:
            raise LedgerError(f"计划不存在: {plan_id}")
        return self.plans[plan_id]

    def _on_disposal_approved(self, subject: str, p: dict) -> None:
        plan = self._get_plan(p["plan_id"])
        if plan.status != "pending":
            raise LedgerError(f"计划 {plan.plan_id} 状态为 {plan.status}，不能批准")
        if not p.get("approver_id"):
            raise LedgerError("缺少批准人")
        if p["approver_id"] == p.get("executor_id_hint"):
            raise LedgerError("批准处置的人不能同时执行出库（职责分离）")
        plan.status = "approved"
        plan.approver_id = p["approver_id"]

    def _on_disposal_rejected(self, subject: str, p: dict) -> None:
        plan = self._get_plan(p["plan_id"])
        if plan.status != "pending":
            raise LedgerError("仅待批计划可驳回")
        plan.status = "rejected"
        plan.reject_reason = p.get("reason")
        self._state(plan.subject_id, plan.channel).allocated[plan.destination] -= plan.quantity

    # ------------------------------------------------------------------ #
    # 分步出库：幂等落账，停机重放只补齐未落账步骤
    # ------------------------------------------------------------------ #
    def _on_item_disposed(self, subject: str, p: dict) -> list[str]:
        plan = self._get_plan(p["plan_id"])
        step, qty = p["step"], int(p["quantity"])
        if step not in DISPOSAL_STEPS:
            raise LedgerError(f"非法出库步骤: {step}")
        if plan.status not in ("approved", "executed"):
            raise LedgerError(f"计划未批准，禁止出库: {plan.status}")
        if p["executor_id"] == plan.approver_id:
            raise LedgerError("执行人不得与批准人为同一人")
        if qty != plan.quantity:
            raise LedgerError("出库数量必须与批准数量一致（互斥份额不得拆占）")
        idx = DISPOSAL_STEPS.index(step)
        missing = [s for s in DISPOSAL_STEPS[:idx] if s not in plan.steps_done]
        if missing:
            raise LedgerError(f"出库步骤乱序：{step} 前缺少 {missing}")
        if step in plan.steps_done:
            return [f"step already posted, no-op: {plan.plan_id}/{step}"]
        if plan.executor_id is None:
            plan.executor_id = p["executor_id"]
        elif plan.executor_id != p["executor_id"]:
            raise LedgerError("同一计划不得更换执行人")
        if step == "confirm":
            st = self._state(plan.subject_id, plan.channel)
            st.allocated[plan.destination] -= plan.quantity
            st.executed[plan.destination] += plan.quantity
            plan.status = "executed"
        plan.steps_done.append(step)
        return []

    # ------------------------------------------------------------------ #
    # 结算：必须使用闭展锁定的合同版本
    # ------------------------------------------------------------------ #
    def _on_settlement_posted(self, subject: str, p: dict) -> None:
        self._require_closing()
        if self.contract is None:
            raise LedgerError("合同尚未锁定，无法结算")
        if p["contract_version"] != self.closing["contract_version"]:
            raise LedgerError("结算必须使用闭展时锁定的合同版本")
        plan = self._get_plan(p["plan_id"])
        if plan.status != "executed":
            raise LedgerError("只有执行完成的处置才能结算")
        expected = self._settlement_amount(plan)
        if int(p["amount"]) != expected:
            raise LedgerError(f"结算金额 {p['amount']} 与锁定合同计算结果 {expected} 不符")
        plan.status = "settled"
        plan.settlement_voucher = p["voucher_id"]
        plan.settlement_amount = int(p["amount"])
        plan.contract_version = p["contract_version"]

    def _settlement_amount(self, plan: Plan) -> int:
        """按锁定合同计算寄售渠道分成/费用（最小口径，金额单位：分）。"""
        if plan.channel != "consignment":
            return 0
        rate = float(self.contract["consignment_rate"])
        if plan.destination == "PERMANENT_SALE":
            return round(plan.quantity * rate)
        if plan.destination in ("DESTRUCTION", "DONATION"):
            return round(plan.quantity * float(self.contract.get("destruction_fee_rule", 0)))
        return 0  # 退回供应商不产生分成

    # ------------------------------------------------------------------ #
    # 查询 / 核对
    # ------------------------------------------------------------------ #
    def conservation_check(self) -> list[str]:
        """总量守恒核对：总头寸 = 保护+待执行+已执行+冻结+未决+可分配。"""
        problems: list[str] = []
        for (subject, ch), st in self.states.items():
            if st.available < 0:
                problems.append(
                    f"{subject}@{ch}: 占用超出头寸 {st.total}（保护{st.protected_qty} "
                    f"待执{st.allocated_qty} 已执行{st.executed_qty} "
                    f"冻结{st.frozen} 未决{st.unresolved}）"
                )
        return problems

    def subject_view(self, subject: str) -> dict:
        """从一件商品（sku）汇总：订单保护、库存去向、审批责任、结算凭证。"""
        protected: dict[str, int] = defaultdict(int)
        destinations = {d: 0 for d in DESTINATIONS}
        approvals: list[dict] = []
        vouchers: list[dict] = []
        frozen = unresolved = total = 0
        for plan in self.plans.values():
            if plan.subject_id != subject:
                continue
            if plan.status in ("executed", "settled"):
                destinations[plan.destination] += plan.quantity
            if plan.approver_id:
                approvals.append({
                    "plan_id": plan.plan_id, "approver": plan.approver_id,
                    "executor": plan.executor_id, "status": plan.status,
                    "channel": plan.channel, "qty": plan.quantity,
                })
            if plan.settlement_voucher:
                vouchers.append({
                    "plan_id": plan.plan_id, "voucher": plan.settlement_voucher,
                    "amount": plan.settlement_amount,
                    "contract_version": plan.contract_version,
                })
        for (sub, ch), st in self.states.items():
            if sub != subject:
                continue
            for k, v in st.protected.items():
                protected[k] += v
            frozen += st.frozen
            unresolved += st.unresolved
            total += st.total
        return {
            "key": subject,
            "contract_version": self.closing["contract_version"] if self.closing else None,
            "product_version": self.closing["product_version"] if self.closing else None,
            "total": total,
            "protected": dict(protected),
            "destinations": destinations,
            "approvals": approvals,
            "settlement_vouchers": vouchers,
            "frozen": frozen,
            "unresolved": unresolved,
            "conserved": not self.conservation_check(),
        }

    def batch_view(self, batch_id: str) -> dict:
        """从一个批次反向汇总：批次内去向、质量隔离、审批责任、结算凭证。"""
        plans = [p for p in self.plans.values() if p.batch_id == batch_id]
        destinations = {d: 0 for d in DESTINATIONS}
        approvals: list[dict] = []
        vouchers: list[dict] = []
        for plan in plans:
            if plan.status in ("executed", "settled"):
                destinations[plan.destination] += plan.quantity
            if plan.approver_id:
                approvals.append({
                    "plan_id": plan.plan_id, "approver": plan.approver_id,
                    "executor": plan.executor_id, "status": plan.status,
                    "channel": plan.channel, "qty": plan.quantity,
                })
            if plan.settlement_voucher:
                vouchers.append({
                    "plan_id": plan.plan_id, "voucher": plan.settlement_voucher,
                    "amount": plan.settlement_amount,
                    "contract_version": plan.contract_version,
                })
        quarantine = self.batch_quarantine.get(batch_id, 0)
        return {
            "key": batch_id,
            "contract_version": self.closing["contract_version"] if self.closing else None,
            "product_version": self.closing["product_version"] if self.closing else None,
            "total": quarantine + sum(p.quantity for p in plans),
            "protected": {"QUALITY_QUARANTINED": quarantine} if quarantine else {},
            "destinations": destinations,
            "approvals": approvals,
            "settlement_vouchers": vouchers,
            "frozen": 0,
            "unresolved": 0,
            "conserved": not self.conservation_check(),
        }

    def open_steps(self) -> list[str]:
        """停机恢复用：列出已批准但尚未完成的落账步骤，供补齐重放。"""
        return [
            f"{plan.plan_id}:{step}"
            for plan in self.plans.values()
            if plan.status in ("approved", "executed")
            for step in DISPOSAL_STEPS
            if step not in plan.steps_done
        ]
