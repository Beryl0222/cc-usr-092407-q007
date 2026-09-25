import json
import unittest
from pathlib import Path

from src.exhibition_product_sunset import validate_event
from src.liquidation import LiquidationLedger, LedgerError

DATA = Path(__file__).parents[1] / "data"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def replay(events: list[dict]) -> tuple[LiquidationLedger, list[str]]:
    ledger = LiquidationLedger()
    notes = ledger.apply_all(events)
    return ledger, notes


class ScenarioContractTest(unittest.TestCase):
    def test_every_scenario_event_matches_contract(self):
        for ev in load("scenario.json"):
            self.assertEqual(validate_event(ev), [], ev["event_id"])

    def test_legacy_sample_still_valid(self):
        self.assertEqual(validate_event(load("sample.json")), [])


class ScenarioReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger, cls.notes = replay(load("scenario.json"))

    def test_conservation_holds_everywhere(self):
        self.assertEqual(self.ledger.conservation_check(), [])

    def test_duplicate_report_recognized_by_content(self):
        # evt-004 是 evt-003 的同内容重发：只入账一次
        self.assertTrue(any("same content redelivery ignored" in n for n in self.notes))
        cs = self.ledger.states[("SKU-LX-001", "consignment")]
        self.assertEqual(cs.protected.get("ORDER_PROTECTED", 0), 6)
        self.assertEqual(cs.late_recorded, 6 + 3)  # 首报6 + 异内容增量3

    def test_conflicting_fact_freezes_only_that_channel(self):
        self.assertTrue(any("conflicting fact frozen channel only" in n for n in self.notes))
        cs = self.ledger.states[("SKU-LX-001", "consignment")]
        self.assertTrue(cs.blocked)
        self.assertEqual(cs.frozen, 3)
        # 其他渠道未被冻结
        self.assertFalse(self.ledger.states[("SKU-LX-001", "warehouse")].blocked)
        self.assertFalse(self.ledger.states[("SKU-LX-001", "flagship_ec")].blocked)

    def test_aftersales_reserve_never_destroyed(self):
        wh = self.ledger.states[("SKU-LX-001", "warehouse")]
        destroyed = wh.executed.get("DESTRUCTION", 0)
        self.assertEqual(destroyed, 12)
        # 售后预留8件仍在保护位，未进任何去向
        self.assertEqual(wh.protected.get("AFTERSALES_RESERVED"), 8)

    def test_destinations_are_mutually_exclusive_and_balanced(self):
        wh = self.ledger.states[("SKU-LX-001", "warehouse")]
        # 100 = 保护(8售后+4隔离) + 退回60 + 转常设10 + 销毁12 + 余6
        self.assertEqual(wh.total, 100)
        self.assertEqual(wh.executed_qty, 82)
        self.assertEqual(wh.protected_qty, 12)
        self.assertEqual(wh.available, 6)

    def test_paid_order_share_is_occupied_not_disposed(self):
        ec = self.ledger.states[("SKU-LX-001", "flagship_ec")]
        self.assertEqual(ec.protected.get("ORDER_PROTECTED"), 10)
        self.assertEqual(ec.protected.get("RETAINED_SAMPLE"), 2)

    def test_settlement_uses_locked_contract(self):
        p1 = self.ledger.plans["CS-P1"]
        d1 = self.ledger.plans["CS-D1"]
        self.assertEqual(p1.status, "settled")
        self.assertEqual(p1.settlement_amount, 40 * 1200)
        self.assertEqual(p1.contract_version, "CV-2026-09-01")
        self.assertEqual(d1.settlement_amount, 20 * 30)

    def test_subject_traceability(self):
        view = self.ledger.subject_view("SKU-LX-001")
        self.assertTrue(view["conserved"])
        self.assertEqual(view["protected"]["ORDER_PROTECTED"], 10 + 6)
        self.assertEqual(view["protected"]["AFTERSALES_RESERVED"], 8)
        self.assertEqual(view["protected"]["QUALITY_QUARANTINED"], 4)
        self.assertEqual(view["protected"]["RETAINED_SAMPLE"], 2)
        self.assertEqual(view["destinations"]["RETURN_TO_SUPPLIER"], 60 + 18)
        self.assertEqual(view["destinations"]["PERMANENT_SALE"], 10 + 40)
        self.assertEqual(view["destinations"]["DONATION"], 8 + 20)
        self.assertEqual(view["destinations"]["DESTRUCTION"], 12)
        self.assertEqual(view["frozen"], 3)
        approvers = {a["approver"] for a in view["approvals"]}
        executors = {a["executor"] for a in view["approvals"]}
        self.assertTrue(approvers.isdisjoint(executors))
        vouchers = {v["voucher"] for v in view["settlement_vouchers"]}
        self.assertEqual(vouchers, {"V-2026-0923-01", "V-2026-0923-02"})

    def test_batch_traceability(self):
        view = self.ledger.batch_view("B202608")
        self.assertEqual(view["destinations"]["DESTRUCTION"], 12)
        self.assertEqual(view["protected"]["QUALITY_QUARANTINED"], 4)
        self.assertEqual(view["approvals"][0]["approver"], "ma-li")
        self.assertEqual(view["settlement_vouchers"], [])  # 销毁费用本样例未过账


class GuardRuleTest(unittest.TestCase):
    CLOSING = {
        "event_id": "c1", "kind": "EXHIBITION_CLOSING",
        "occurred_at": "2026-09-20T09:00:00+08:00", "subject_id": "S1",
        "payload": {
            "product_version": "v1", "contract_version": "CV1",
            "channels": ["warehouse"],
            "ownership": {"warehouse": "owned"},
            "positions": {"warehouse": 10},
        },
    }

    def _led(self):
        l = LiquidationLedger()
        l.apply(self.CLOSING)
        return l

    def test_no_action_before_closing(self):
        l = LiquidationLedger()
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "x", "kind": "ORDER_PROTECTED",
                     "occurred_at": "t", "subject_id": "S1",
                     "payload": {"channel": "warehouse", "quantity": 1, "order_ref": "o1"}})

    def test_closing_snapshot_cannot_reopen(self):
        l = self._led()
        with self.assertRaises(LedgerError):
            l.apply(self.CLOSING | {"event_id": "c2"})

    def test_stock_position_after_closing_cannot_rewrite_snapshot(self):
        l = self._led()
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "sp", "kind": "STOCK_POSITION", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"channel": "warehouse", "ownership": "owned", "quantity": 99}})

    def test_destruction_including_aftersales_rejected(self):
        l = self._led()
        l.apply({"event_id": "a1", "kind": "AFTERSALES_RESERVED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"channel": "warehouse", "quantity": 8, "reservation_id": "r1"}})
        # 仓库把售后预留一并列入销毁口径：theoretical 含预留
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "d1", "kind": "DESTRUCTION_PLANNED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "channel": "warehouse",
                                 "quantity": 10, "theoretical_qty": 10, "available_qty": 2}})

    def test_cannot_overallocate_paid_order_stock(self):
        l = self._led()
        l.apply({"event_id": "o1", "kind": "ORDER_PROTECTED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"channel": "warehouse", "quantity": 6, "order_ref": "ord"}})
        with self.assertRaises(LedgerError):  # 只剩4件可分配，申请5件
            l.apply({"event_id": "p1", "kind": "DONATION_PLANNED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 5}})

    def test_approver_cannot_be_executor(self):
        l = self._led()
        l.apply({"event_id": "p1", "kind": "DONATION_PLANNED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 2}})
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "ap", "kind": "DISPOSAL_APPROVED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "destination": "DONATION",
                                 "channel": "warehouse", "quantity": 2,
                                 "approver_id": "ma", "executor_id_hint": "ma"}})

    def test_execution_requires_approval_and_ordered_steps(self):
        l = self._led()
        l.apply({"event_id": "p1", "kind": "DONATION_PLANNED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 2}})
        dispose = {"event_id": "e1", "kind": "ITEM_DISPOSED", "occurred_at": "t",
                   "subject_id": "S1",
                   "payload": {"plan_id": "P1", "destination": "DONATION",
                               "channel": "warehouse", "step": "pick",
                               "quantity": 2, "executor_id": "chen"}}
        with self.assertRaises(LedgerError):  # 未批准
            l.apply(dispose)
        l.apply({"event_id": "ap", "kind": "DISPOSAL_APPROVED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "destination": "DONATION",
                             "channel": "warehouse", "quantity": 2,
                             "approver_id": "ma", "executor_id_hint": "chen"}})
        with self.assertRaises(LedgerError):  # 跳步
            l.apply(dispose | {"payload": dispose["payload"] | {"step": "confirm"}})
        with self.assertRaises(LedgerError):  # 执行人=批准人
            l.apply(dispose | {"payload": dispose["payload"] | {"executor_id": "ma"}})

    def test_conflict_freezes_channel_but_other_channels_continue(self):
        events = [
            self.CLOSING,
            {"event_id": "cl2", "kind": "CONTRACT_LOCKED", "occurred_at": "t", "subject_id": "S1",
             "payload": {"contract_version": "CV1", "consignment_rate": 100,
                         "destruction_fee_rule": 0}},
        ]
        # 双渠道头寸
        events[0] = self.CLOSING | {"payload": self.CLOSING["payload"] | {
            "channels": ["warehouse", "consignment"],
            "ownership": {"warehouse": "owned", "consignment": "consigned"},
            "positions": {"warehouse": 5, "consignment": 5}}}
        l = LiquidationLedger()
        l.apply_all(events)
        # 寄售渠道同标识异内容
        l.apply({"event_id": "r1", "kind": "CHANNEL_REPORTED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"channel": "consignment", "facts": [
                     {"kind": "STOCK_POSITION", "quantity": 2, "fact_id": "F1",
                      "ownership": "in_transit", "late": True}]}})
        l.apply({"event_id": "r2", "kind": "CHANNEL_REPORTED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"channel": "consignment", "facts": [
                     {"kind": "STOCK_POSITION", "quantity": 4, "fact_id": "F1",
                      "ownership": "in_transit", "late": True}]}})
        # 寄售渠道新计划必须被挡住
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "p2", "kind": "DONATION_PLANNED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P2", "channel": "consignment", "quantity": 1}})
        # 仓库渠道照常推进、批准、出库、结算
        l.apply({"event_id": "p1", "kind": "DONATION_PLANNED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 2}})
        l.apply({"event_id": "ap1", "kind": "DISPOSAL_APPROVED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "destination": "DONATION",
                             "channel": "warehouse", "quantity": 2,
                             "approver_id": "ma", "executor_id_hint": "chen"}})
        for i, step in enumerate(("pick", "outbound", "confirm")):
            l.apply({"event_id": f"x{i}", "kind": "ITEM_DISPOSED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "destination": "DONATION",
                                 "channel": "warehouse", "step": step,
                                 "quantity": 2, "executor_id": "chen"}})
        self.assertEqual(l.conservation_check(), [])

    def test_settlement_rejects_wrong_contract_version(self):
        l = self._led()
        l.apply({"event_id": "cl", "kind": "CONTRACT_LOCKED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"contract_version": "CV1", "consignment_rate": 100,
                             "destruction_fee_rule": 0}})
        l.apply({"event_id": "p1", "kind": "RETURN_TO_SUPPLIER_PLANNED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 2}})
        l.apply({"event_id": "ap", "kind": "DISPOSAL_APPROVED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "destination": "RETURN_TO_SUPPLIER",
                             "channel": "warehouse", "quantity": 2,
                             "approver_id": "ma", "executor_id_hint": "chen"}})
        for i, step in enumerate(("pick", "outbound", "confirm")):
            l.apply({"event_id": f"x{i}", "kind": "ITEM_DISPOSED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "destination": "RETURN_TO_SUPPLIER",
                                 "channel": "warehouse", "step": step,
                                 "quantity": 2, "executor_id": "chen"}})
        with self.assertRaises(LedgerError):
            l.apply({"event_id": "st", "kind": "SETTLEMENT_POSTED", "occurred_at": "t",
                     "subject_id": "S1",
                     "payload": {"plan_id": "P1", "contract_version": "CV2",
                                 "consignment_rate": 100, "amount": 0, "voucher_id": "V9"}})


class ResumeTest(unittest.TestCase):
    def test_outage_replay_only_fills_missing_steps(self):
        closing = {
            "event_id": "c1", "kind": "EXHIBITION_CLOSING",
            "occurred_at": "t", "subject_id": "S1",
            "payload": {"product_version": "v1", "contract_version": "CV1",
                        "channels": ["warehouse"], "ownership": {"warehouse": "owned"},
                        "positions": {"warehouse": 5}},
        }
        l = LiquidationLedger()
        l.apply(closing)
        l.apply({"event_id": "p1", "kind": "DONATION_PLANNED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "channel": "warehouse", "quantity": 3}})
        l.apply({"event_id": "ap", "kind": "DISPOSAL_APPROVED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "destination": "DONATION",
                             "channel": "warehouse", "quantity": 3,
                             "approver_id": "ma", "executor_id_hint": "chen"}})
        l.apply({"event_id": "s1", "kind": "ITEM_DISPOSED", "occurred_at": "t",
                 "subject_id": "S1",
                 "payload": {"plan_id": "P1", "destination": "DONATION",
                             "channel": "warehouse", "step": "pick",
                             "quantity": 3, "executor_id": "chen"}})
        # 出库途中停机：open_steps 指出未落账步骤
        self.assertEqual(l.open_steps(), ["P1:outbound", "P1:confirm"])
        # 重放：先重发 pick（幂等空操作），再依次补齐
        notes = []
        for step in ("pick", "outbound", "confirm"):
            notes += l.apply({"event_id": f"s-{step}", "kind": "ITEM_DISPOSED",
                              "occurred_at": "t", "subject_id": "S1",
                              "payload": {"plan_id": "P1", "destination": "DONATION",
                                          "channel": "warehouse", "step": step,
                                          "quantity": 3, "executor_id": "chen"}})
        self.assertTrue(any("already posted" in n for n in notes))
        self.assertEqual(l.open_steps(), [])
        st = l.states[("S1", "warehouse")]
        self.assertEqual(st.executed.get("DONATION"), 3)  # 只扣一次
        self.assertEqual(st.allocated.get("DONATION", 0), 0)
        self.assertEqual(l.plans["P1"].status, "executed")
        self.assertEqual(l.conservation_check(), [])


if __name__ == "__main__":
    unittest.main()
