import unittest

from src.exhibition_product_sunset import validate_event
from src.sunset_settlement import (
    BaselineItem,
    ChannelReport,
    ChannelFrozenError,
    ContractTerms,
    DuplicateClosureError,
    DutySeparationError,
    Ownership,
    PlanNotApprovedError,
    PlanStaleError,
    ReportStatus,
    ShareKind,
    SimulatedOutage,
    SunsetError,
    SunsetSettlement,
    UnknownExhibitionError,
    UnknownProductError,
)

NOW = "2026-09-25T09:00:00+08:00"
EX = "EX-009"

CONTRACT = ContractTerms(version="CT-2026-Q3", disposal_fee_rate=0.05, consignment_share_rate=0.4)
ITEMS = [
    BaselineItem("P-POSTCARD", "v3", ("DIRECT_ONLINE", "STORE"), Ownership.OWN, 100, 20.0),
    BaselineItem("P-FIGURE", "v1", ("STORE", "WAREHOUSE"), Ownership.CONSIGNMENT, 50, 300.0),
]


def make_service():
    svc = SunsetSettlement(clock=lambda: NOW)
    svc.lock_closure(EX, ITEMS, CONTRACT, decided_by="curator-01")
    return svc


def postcard_reports(svc):
    """明信片（自有，账面 100）：直营网店 46 + 门店 54，保护 25，样品 1，可处置 74。"""
    svc.ingest_report(ChannelReport("R-ONLINE-1", EX, "DIRECT_ONLINE", "P-POSTCARD", 30, 10, 2, 3, 0, 1))
    svc.ingest_report(ChannelReport("R-STORE-1", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))


def figure_reports(svc):
    """摆件（寄售，账面 50）：门店 40 + 仓库 10，保护 17，隔离 3，样品 2，可退回 28。"""
    svc.ingest_report(ChannelReport("R-STORE-9", EX, "STORE", "P-FIGURE", 20, 15, 2, 0, 3, 0))
    svc.ingest_report(ChannelReport("R-WH-9", EX, "WAREHOUSE", "P-FIGURE", 8, 0, 0, 0, 0, 2))


class ClosureLockTest(unittest.TestCase):
    def test_closure_locks_versions_channels_ownership_and_contract(self):
        svc = make_service()
        closing = [e for e in svc.events() if e["kind"] == "EXHIBITION_CLOSING"]
        self.assertEqual(len(closing), 1)
        payload = closing[0]["payload"]
        self.assertEqual(payload["decided_by"], "curator-01")
        self.assertEqual(payload["contract_version"], "CT-2026-Q3")
        items = {i["product_id"]: i for i in payload["items"]}
        self.assertEqual(items["P-POSTCARD"]["version"], "v3")
        self.assertEqual(items["P-POSTCARD"]["channels"], ["DIRECT_ONLINE", "STORE"])
        self.assertEqual(items["P-POSTCARD"]["ownership"], "OWN")
        self.assertEqual(items["P-FIGURE"]["ownership"], "CONSIGNMENT")

    def test_closure_cannot_be_locked_twice(self):
        svc = make_service()
        with self.assertRaises(DuplicateClosureError):
            svc.lock_closure(EX, ITEMS, ContractTerms("CT-2026-Q4", 0.9, 0.9), "curator-02")
        closing = [e for e in svc.events() if e["kind"] == "EXHIBITION_CLOSING"]
        self.assertEqual(len(closing), 1)
        self.assertEqual(closing[0]["payload"]["contract_version"], "CT-2026-Q3")


class ChannelReportTest(unittest.TestCase):
    def test_duplicate_report_same_content_is_one_fact(self):
        svc = make_service()
        first = svc.ingest_report(ChannelReport("R-1", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        self.assertEqual(first.status, ReportStatus.ACCEPTED)
        # 迟到或重复：标识不同但内容相同，识别为同一事实
        dup = svc.ingest_report(ChannelReport("R-1-RETRY", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        self.assertEqual(dup.status, ReportStatus.DUPLICATE)
        self.assertEqual(dup.report_id, "R-1")
        svc.ingest_report(ChannelReport("R-2", EX, "DIRECT_ONLINE", "P-POSTCARD", 30, 10, 2, 3, 0, 1))
        plan = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 74})
        self.assertEqual(sum(s.quantity for s in plan.shares), 74)  # 同一事实只入账一次
        reported = [e for e in svc.events() if e["kind"] == "CHANNEL_EXPOSURE_REPORTED"]
        self.assertEqual(len(reported), 2)

    def test_late_duplicate_does_not_disturb_approved_plan(self):
        svc = make_service()
        postcard_reports(svc)
        plan = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 74})
        svc.approve_plan(plan.plan_id, "manager-01")
        late = svc.ingest_report(ChannelReport("R-LATE", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        self.assertEqual(late.status, ReportStatus.DUPLICATE)
        self.assertFalse(svc.current_plan(EX, "P-POSTCARD").stale)
        newly = svc.run_plan(plan.plan_id, actor="ops-02")
        self.assertEqual(newly, ["DELIST", "OUTBOUND:TRANSFER_TO_PERMANENT"])

    def test_conflicting_report_freezes_only_that_channel(self):
        svc = make_service()
        svc.ingest_report(ChannelReport("R-ONLINE-1", EX, "DIRECT_ONLINE", "P-POSTCARD", 30, 10, 2, 3, 0, 1))
        svc.ingest_report(ChannelReport("R-STORE-1", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        # 同标识异内容：仅冻结门店渠道的该货品
        conflict = svc.ingest_report(ChannelReport("R-STORE-1", EX, "STORE", "P-POSTCARD", 40, 5, 3, 2, 0, 0))
        self.assertEqual(conflict.status, ReportStatus.CONFLICT)
        with self.assertRaises(ChannelFrozenError):
            svc.ingest_report(ChannelReport("R-STORE-2", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        # 其他货品继续推进
        figure_reports(svc)
        plan_figure = svc.compute_plan(EX, "P-FIGURE")
        self.assertFalse(plan_figure.stale)
        # 直营网店的既有事实保留；被冻结渠道的数量转为明确占用的差异，总量守恒
        svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 30})
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertEqual(stmt.order_protection["paid_orders"], 10)
        self.assertEqual(stmt.unresolved_difference, 54)
        self.assertTrue(stmt.conserved)
        conflicts = [e for e in svc.events() if e["kind"] == "CHANNEL_REPORT_CONFLICT"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["payload"]["channel"], "STORE")

    def test_report_validation(self):
        svc = make_service()
        with self.assertRaises(SunsetError):  # 渠道不在闭展决定固定的渠道内
            svc.ingest_report(ChannelReport("R-X", EX, "POPUP", "P-POSTCARD", 1, 0, 0, 0, 0, 0))
        with self.assertRaises(SunsetError):  # 数量不得为负
            svc.ingest_report(ChannelReport("R-X", EX, "STORE", "P-POSTCARD", -1, 0, 0, 0, 0, 0))
        with self.assertRaises(UnknownProductError):
            svc.ingest_report(ChannelReport("R-X", EX, "STORE", "P-NONE", 1, 0, 0, 0, 0, 0))
        with self.assertRaises(UnknownExhibitionError):
            svc.ingest_report(ChannelReport("R-X", "EX-NONE", "STORE", "P-POSTCARD", 1, 0, 0, 0, 0, 0))


class DisposalPlanTest(unittest.TestCase):
    def test_shares_are_mutually_exclusive_and_never_touch_protected(self):
        svc = make_service()
        postcard_reports(svc)
        plan = svc.compute_plan(
            EX,
            "P-POSTCARD",
            own_disposal={
                ShareKind.TRANSFER_TO_PERMANENT: 60,
                ShareKind.DONATION: 10,
                ShareKind.DESTRUCTION: 4,
            },
        )
        shares = {s.kind: s.quantity for s in plan.shares}
        self.assertEqual(
            shares,
            {ShareKind.TRANSFER_TO_PERMANENT: 60, ShareKind.DONATION: 10, ShareKind.DESTRUCTION: 4},
        )
        # 售后换货预留不得列入销毁：销毁份额恰为自有划分口径的 4
        protected = 10 + 2 + 3 + 5 + 3 + 2
        self.assertEqual(protected, 25)
        # 互斥且守恒：保护 + 样品 + 份额 + 差异 == 账面
        self.assertEqual(protected + 1 + sum(shares.values()) + 0, 100)
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertTrue(stmt.conserved)
        self.assertEqual(stmt.unresolved_difference, 0)
        self.assertEqual(stmt.samples_retained, 1)

    def test_own_allocation_must_cover_exactly_the_disposable_quantity(self):
        svc = make_service()
        postcard_reports(svc)
        with self.assertRaises(SunsetError):  # 合计必须等于可处置量 74
            svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.DONATION: 10})
        with self.assertRaises(SunsetError):  # 自有商品不能退回供应商
            svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.RETURN_TO_SUPPLIER: 74})

    def test_consignment_returns_to_supplier(self):
        svc = make_service()
        figure_reports(svc)
        plan = svc.compute_plan(EX, "P-FIGURE")
        shares = {s.kind: s.quantity for s in plan.shares}
        self.assertEqual(shares.get(ShareKind.RETURN_TO_SUPPLIER), 28)
        self.assertEqual(shares.get(ShareKind.DESTRUCTION), 3)  # 质量隔离品就地销毁
        with self.assertRaises(SunsetError):  # 寄售商品不接受自有划分口径
            svc.compute_plan(EX, "P-FIGURE", own_disposal={ShareKind.DONATION: 1})


class DutySeparationTest(unittest.TestCase):
    def test_approver_cannot_execute_outbound(self):
        svc = make_service()
        postcard_reports(svc)
        plan = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 74})
        svc.approve_plan(plan.plan_id, "manager-01")
        with self.assertRaises(DutySeparationError):
            svc.run_plan(plan.plan_id, actor="manager-01")
        self.assertEqual(svc.current_plan(EX, "P-POSTCARD").steps, {})  # 未落账任何步骤
        newly = svc.run_plan(plan.plan_id, actor="ops-02")
        self.assertEqual(len(newly), 2)
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertEqual(stmt.responsibility["approved_by"], "manager-01")
        self.assertEqual(stmt.responsibility["executors"], ["ops-02"])
        self.assertTrue(stmt.responsibility["duty_separation_ok"])


class RecoveryTest(unittest.TestCase):
    def test_outage_resume_only_backfills_missing_steps(self):
        svc = make_service()
        postcard_reports(svc)
        plan = svc.compute_plan(
            EX,
            "P-POSTCARD",
            own_disposal={
                ShareKind.TRANSFER_TO_PERMANENT: 60,
                ShareKind.DONATION: 10,
                ShareKind.DESTRUCTION: 4,
            },
            refunds={"ORD-1001": 40.0},
        )
        svc.approve_plan(plan.plan_id, "manager-01")
        # 退款、下架、出库途中停机
        with self.assertRaises(SimulatedOutage):
            svc.run_plan(plan.plan_id, actor="ops-02", fail_after=2)
        recorded = svc.current_plan(EX, "P-POSTCARD").steps
        self.assertEqual(list(recorded), ["DELIST", "REFUND:ORD-1001"])
        # 重新处理只补齐未落账步骤
        newly = svc.run_plan(plan.plan_id, actor="ops-02")
        self.assertEqual(newly, ["OUTBOUND:TRANSFER_TO_PERMANENT", "OUTBOUND:DONATION", "OUTBOUND:DESTRUCTION"])
        self.assertEqual(svc.run_plan(plan.plan_id, actor="ops-02"), [])  # 再次重处理无新增
        step_events = [e for e in svc.events() if e["kind"] == "DISPOSAL_STEP_RECORDED"]
        self.assertEqual(len(step_events), 5)
        disposed = [e for e in svc.events() if e["kind"] == "ITEM_DISPOSED"]
        self.assertEqual(sum(e["payload"]["quantity"] for e in disposed), 74)
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertTrue(stmt.completed)
        self.assertEqual(stmt.order_protection["refunded"], {"ORD-1001": 40.0})
        self.assertEqual(stmt.voucher.refund_total, 40.0)


class LateReportTest(unittest.TestCase):
    def test_late_report_marks_plan_stale_and_recompute_keeps_recorded_steps(self):
        svc = make_service()
        svc.ingest_report(ChannelReport("R-ONLINE-1", EX, "DIRECT_ONLINE", "P-POSTCARD", 30, 10, 2, 3, 0, 1))
        plan_v1 = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 30})
        svc.approve_plan(plan_v1.plan_id, "manager-01")
        with self.assertRaises(SimulatedOutage):
            svc.run_plan(plan_v1.plan_id, actor="ops-02", fail_after=1)  # DELIST 已落账
        # 门店回报迟到且内容为新事实：计划过期
        svc.ingest_report(ChannelReport("R-STORE-1", EX, "STORE", "P-POSTCARD", 44, 5, 3, 2, 0, 0))
        self.assertTrue(svc.current_plan(EX, "P-POSTCARD").stale)
        with self.assertRaises(PlanStaleError):
            svc.run_plan(plan_v1.plan_id, actor="ops-02")
        # 重新划分：v2 覆盖全部事实，已落账步骤保留
        plan_v2 = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 74})
        self.assertEqual(plan_v2.version, 2)
        self.assertIn("DELIST", plan_v2.steps)
        with self.assertRaises(PlanNotApprovedError):  # 新版本需重新批准
            svc.run_plan(plan_v2.plan_id, actor="ops-02")
        svc.approve_plan(plan_v2.plan_id, "manager-01")
        newly = svc.run_plan(plan_v2.plan_id, actor="ops-02")
        self.assertEqual(newly, ["OUTBOUND:TRANSFER_TO_PERMANENT"])  # DELIST 不重复落账
        delist_events = [
            e for e in svc.events()
            if e["kind"] == "DISPOSAL_STEP_RECORDED" and e["payload"]["step"] == "DELIST"
        ]
        self.assertEqual(len(delist_events), 1)
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertTrue(stmt.completed)
        self.assertEqual(stmt.unresolved_difference, 0)


class UnresolvedDifferenceTest(unittest.TestCase):
    def test_unresolved_difference_stays_reserved(self):
        svc = make_service()
        # 门店迟迟未回报：其负责的数量保持明确占用，不进入处置份额
        svc.ingest_report(ChannelReport("R-ONLINE-1", EX, "DIRECT_ONLINE", "P-POSTCARD", 30, 10, 2, 3, 0, 1))
        plan = svc.compute_plan(EX, "P-POSTCARD", own_disposal={ShareKind.TRANSFER_TO_PERMANENT: 30})
        svc.approve_plan(plan.plan_id, "manager-01")
        svc.run_plan(plan.plan_id, actor="ops-02")
        stmt = svc.statement_for_product(EX, "P-POSTCARD")
        self.assertEqual(stmt.unresolved_difference, 54)
        self.assertEqual(stmt.missing_channels, ("STORE",))
        self.assertTrue(stmt.conserved)
        self.assertTrue(stmt.completed)


class SettlementStatementTest(unittest.TestCase):
    def test_voucher_uses_contract_version_locked_at_closure(self):
        svc = make_service()
        figure_reports(svc)
        plan = svc.compute_plan(EX, "P-FIGURE")
        svc.approve_plan(plan.plan_id, "manager-01")
        svc.run_plan(plan.plan_id, actor="warehouse-07")
        # 闭展后出现的新合同版本不影响结算
        with self.assertRaises(DuplicateClosureError):
            svc.lock_closure(EX, ITEMS, ContractTerms("CT-2026-Q4", 0.9, 0.9), "curator-02")
        stmt = svc.statement_for_product(EX, "P-FIGURE")
        voucher = stmt.voucher
        self.assertEqual(voucher.contract_version, "CT-2026-Q3")
        self.assertEqual(voucher.consignment_payout, 1800.0)  # 已售寄售 15 × 300 × 0.4
        self.assertEqual(voucher.disposal_fee, 45.0)  # 销毁 3 × 300 × 0.05
        self.assertEqual(stmt.destinations["RETURN_TO_SUPPLIER"]["outbounded"], 28)
        self.assertEqual(stmt.destinations["DESTRUCTION"]["outbounded"], 3)
        self.assertEqual(stmt.responsibility["approved_by"], "manager-01")
        self.assertEqual(stmt.responsibility["executors"], ["warehouse-07"])

    def test_batch_statement_and_event_contract(self):
        svc = make_service()
        postcard_reports(svc)
        figure_reports(svc)
        p1 = svc.compute_plan(
            EX,
            "P-POSTCARD",
            own_disposal={
                ShareKind.TRANSFER_TO_PERMANENT: 60,
                ShareKind.DONATION: 10,
                ShareKind.DESTRUCTION: 4,
            },
        )
        p2 = svc.compute_plan(EX, "P-FIGURE")
        svc.approve_plan(p1.plan_id, "manager-01")
        svc.approve_plan(p2.plan_id, "manager-01")
        svc.run_plan(p1.plan_id, actor="ops-02")
        svc.run_plan(p2.plan_id, actor="warehouse-07")
        # 财务按批次核对
        batch = svc.statement_for_batch(EX)
        self.assertTrue(batch.conserved)
        self.assertEqual(batch.unresolved_difference, 0)
        self.assertEqual({s.product_id for s in batch.statements}, {"P-POSTCARD", "P-FIGURE"})
        self.assertTrue(all(s.completed for s in batch.statements))
        self.assertTrue(all(s.responsibility["duty_separation_ok"] for s in batch.statements))
        # 事件流水全部满足领域交换契约
        for event in svc.events():
            self.assertEqual(validate_event(event), [], event)
        kinds = {e["kind"] for e in svc.events()}
        self.assertIn("SETTLEMENT_STATEMENT_ISSUED", kinds)
        self.assertIn("ORDER_PROTECTED", kinds)


if __name__ == "__main__":
    unittest.main()
