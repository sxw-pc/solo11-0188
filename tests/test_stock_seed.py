"""一次性过滤件联合预留账测试。

覆盖任务要求的全部场景：

* 两路质检原件（放行证明 -> 零缺口锁件；供应商召回/配包前隔离 -> A 级缺口）；
* 跨场实物件号排他与批次库存上限；
* 级别替代方向（高可就低、低不能顶高、同级别优先）；
* 有效期（以实际使用日判断，过期件/过期替代品不可发，FEFO 近效期优先）；
* 批次验收（无放行原件且 require_release 时冻结）；
* 贪心反例（早到 B 级场不能抢走未来 A-only 场的长有效期 A 批次）；
* 已签收考包召回：追到具体考包、撤回/替换流转、保留原预留与签收历史、
  晚到召回只重排受影响考包、无替换货时转采购/移库单；
* 原有 candidate/station/assessment 考评夹具仍可导入与审计。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skillstation.stock_capture import (
    available,
    batch_availability,
    is_fresh,
    grade_covers,
    normalize_documents,
)
from skillstation.kit_reservation import (
    ReservationLedger,
    SerialConflict,
    reserve,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "kit-stock.json"


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def lot(lot_id, grade, qty, expiry, **extra):
    row = {
        "id": lot_id,
        "part": "filter",
        "quantity": qty,
        "expiry": expiry,
        "grade": grade,
        "serials": [f"{lot_id}-{i}" for i in range(1, qty + 1)],
    }
    row.update(extra)
    return row


def req(exam_id, grade, qty=4, date="2026-06-01", **extra):
    row = {"id": exam_id, "part": "filter", "quantity": qty,
           "date": date, "required_grade": grade}
    row.update(extra)
    return row


class SeedShapeTest(unittest.TestCase):
    """旧种子断言必须保持成立。"""

    def test_available_still_sums_part_total(self):
        data = fixture_data()
        self.assertEqual(available(data["lots"], "filter"), 10)

    def test_legacy_reserve_bool_mapping_still_true_true(self):
        data = fixture_data()
        result = reserve(data["lots"], data["requests"])
        # 旧的核心断言：两场在总量层面都是 True。
        self.assertEqual(result, {"exam-A": True, "exam-B": True})
        self.assertEqual(result.flags(), {"exam-A": True, "exam-B": True})

    def test_fixture_seed_values(self):
        data = fixture_data()
        by_id = {row["id"]: row for row in data["lots"]}
        self.assertEqual(by_id["F1"]["grade"], "A")
        self.assertEqual(by_id["F1"]["quantity"], 5)
        self.assertEqual(by_id["F1"]["expiry"], "2026-05-02")
        self.assertEqual(by_id["F2"]["grade"], "B")
        self.assertEqual(by_id["F2"]["quantity"], 5)
        self.assertEqual(by_id["F2"]["expiry"], "2026-05-30")


class QCTwoPathsTest(unittest.TestCase):
    """同一台账、两场都 True 的表象下，按质检原件走两条授权路径。"""

    def test_release_certificate_locks_physical_serials_zero_gap(self):
        data = fixture_data()
        ledger = ReservationLedger(data)  # 自动读取 qc_documents 放行证明
        result = ledger.plan()

        self.assertTrue(result["exam-A"])
        self.assertTrue(result["exam-B"])
        self.assertTrue(result["exam-A"]["can_open_kit"])
        self.assertTrue(result["exam-B"]["can_open_kit"])

        a = result["exam-A"]
        b = result["exam-B"]

        # exam-A 必须吃到 F1 的 4 件实物号，而不是一个 True。
        self.assertEqual(a.lot_ids, ["F1"])
        self.assertEqual(a["shippable_quantity"], 4)
        self.assertEqual(a["shortfall"], 0)
        self.assertEqual(len(a["serials"]), 4)
        self.assertTrue(set(a["serials"]).issubset({
            "F1-01", "F1-02", "F1-03", "F1-04", "F1-05"}))
        self.assertTrue(all(s.startswith("F1-") for s in a["serials"]))

        # exam-B 锁 F2 的 4 件。
        self.assertEqual(b.lot_ids, ["F2"])
        self.assertEqual(len(b["serials"]), 4)
        self.assertTrue(all(s.startswith("F2-") for s in b["serials"]))

        # 跨场排他 + 批次合计不超库存。
        self.assertFalse(set(a["serials"]) & set(b["serials"]))
        self.assertEqual(ledger.batch_totals(), {"F1": 4, "F2": 4})
        ledger.assert_exclusive()
        for balance in ledger.lot_balance():
            self.assertLessEqual(balance["reserved"], balance["quantity"])

        # 采购缺口为零。
        self.assertEqual(ledger.procurement_orders(), [])
        self.assertEqual(
            sum(row["quantity"] for row in ledger.procurement_summary()), 0
        )

        # 逐场批次清单 / 报告必须可 JSON 序列化给采购员与考务。
        json.dumps(ledger.report(), ensure_ascii=False)

    def test_explicit_physical_locks_follow_prose(self):
        data = fixture_data()
        ledger = ReservationLedger(data["lots"], data["requests"],
                                   documents=data["qc_documents"])
        ledger.lock_serials("exam-A", ["F1-01", "F1-02", "F1-03", "F1-04"])
        ledger.lock_serials("exam-B", ["F2-01", "F2-02", "F2-03", "F2-04"])
        plan = ledger.plan()
        self.assertEqual(plan["exam-A"]["serials"],
                         ["F1-01", "F1-02", "F1-03", "F1-04"])
        self.assertEqual(plan["exam-B"]["serials"],
                         ["F2-01", "F2-02", "F2-03", "F2-04"])
        self.assertEqual(ledger.batch_totals(), {"F1": 4, "F2": 4})

    def test_supplier_recall_before_packing_leaves_a_gap(self):
        data = fixture_data()
        # 放行证明与供应商召回原件同时在账：任一 blocked 结论优先。
        ledger = ReservationLedger(data, authorizations=data["recall_originals"])
        result = ledger.plan()

        self.assertFalse(result["exam-A"])
        self.assertTrue(result["exam-B"])

        a = result["exam-A"]
        self.assertEqual(a["shippable_quantity"], 0)
        self.assertEqual(a["actual_quantity"], 0)
        self.assertEqual(a["shortfall"], 4)
        self.assertEqual(a["lot_ids"], [])

        # exam-B 仍从 F2 发 4，且 B 级件绝不能挪给 A。
        b = result["exam-B"]
        self.assertEqual(b.lot_ids, ["F2"])
        self.assertEqual(b["shippable_quantity"], 4)
        self.assertTrue(all(s.startswith("F2-") for s in b["serials"]))
        self.assertNotIn("F2", a.lot_ids)

        self.assertEqual(ledger.batch_totals(), {"F2": 4})
        ledger.assert_exclusive()

        orders = ledger.procurement_orders()
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["exam_id"], "exam-A")
        self.assertEqual(order["required_grade"], "A")
        self.assertEqual(order["quantity"], 4)
        self.assertIn(order["action"], ("purchase_or_transfer", "purchase", "transfer"))
        self.assertIn("采购", order["action_label"])
        summary = ledger.procurement_summary()
        self.assertEqual(summary[0]["quantity"], 4)
        self.assertEqual(summary[0]["required_grade"], "A")

    def test_stock_capture_effective_available_reflects_qc_and_dates(self):
        data = fixture_data()
        lots = data["lots"]
        # 5 月 1 日的 A 级需求：F1 5 件合格，F2 级别不够 -> 5。
        self.assertEqual(
            available(lots, "filter", on_date="2026-05-01", required_grade="A"), 5
        )
        # 5 月 5 日的 B 级需求：F1 已过期，F2 5 件 -> 5。
        self.assertEqual(
            available(lots, "filter", on_date="2026-05-05", required_grade="B"), 5
        )
        # 召回 F1 后，5 月 1 日 A 级可用量直接归零。
        recall = data["recall_originals"]
        self.assertEqual(
            available(lots, "filter", on_date="2026-05-01",
                      required_grade="A", documents=recall),
            0,
        )


class BatchAcceptanceTest(unittest.TestCase):
    def test_pending_lot_blocked_until_release_document(self):
        lots = [lot("P", "A", 2, "2027-01-01", status="pending")]
        requests = [req("E", "A", 2)]

        frozen = ReservationLedger(lots, requests, require_release=True)
        self.assertFalse(frozen.flags()["E"])
        self.assertEqual(frozen["E"]["shortfall"], 2)

        released = ReservationLedger(
            lots, requests, require_release=True,
            documents=[{"id": "QC-P", "lot_id": "P", "decision": "放行"}],
        )
        self.assertTrue(released.flags()["E"])
        self.assertEqual(released["E"].lot_ids, ["P"])

    def test_decision_only_recall_blocks_even_without_serial_list(self):
        lots = [lot("F1", "A", 4, "2027-01-01", status="released")]
        ledger = ReservationLedger(
            lots, [req("E", "A", 4)],
            authorizations=[{"id": "RC-1", "lot_id": "F1", "decision": "recalled"}],
        )
        self.assertFalse(ledger.flags()["E"])
        self.assertEqual(ledger["E"]["shortfall"], 4)

    def test_document_normalization_is_idempotent(self):
        raw = [{"lot_id": "F1", "decision": "released",
                "note": "re-issued after an earlier recall review"}]
        once = normalize_documents(raw)
        twice = normalize_documents(once)
        self.assertEqual(twice["F1"][0]["decision"], "released")
        # 备注里出现 recall 字样不改变放行结论。
        self.assertEqual(
            available([lot("F1", "A", 1, "2027-01-01")], "filter",
                      on_date="2026-06-01", required_grade="A", documents=twice),
            1,
        )


class CrossExamExclusivityTest(unittest.TestCase):
    def test_same_physical_lot_total_never_exceeds_stock(self):
        lots = [lot("L", "A", 5, "2027-01-01")]
        requests = [req("X", "A", 3, "2026-06-01"),
                    req("Y", "A", 3, "2026-06-02")]
        ledger = ReservationLedger(lots, requests)

        sx, sy = set(ledger["X"]["serials"]), set(ledger["Y"]["serials"])
        self.assertEqual(len(sx), 3)
        self.assertEqual(len(sy), 2)          # 第 6 件不存在
        self.assertFalse(sx & sy)             # 件号级排他
        self.assertEqual(ledger.batch_totals(), {"L": 5})
        self.assertEqual(ledger["Y"]["shortfall"], 1)
        ledger.assert_exclusive()

    def test_three_sessions_still_cap_at_physical_count(self):
        lots = [lot("L", "A", 5, "2027-01-01")]
        requests = [req("X", "A", 2), req("Y", "A", 2), req("Z", "A", 2)]
        ledger = ReservationLedger(lots, requests)
        all_serials = [s for e in ("X", "Y", "Z") for s in ledger[e]["serials"]]
        self.assertEqual(len(all_serials), 5)
        self.assertEqual(len(set(all_serials)), 5)
        self.assertEqual(ledger.batch_totals()["L"], 5)
        self.assertEqual(
            sum(ledger[e]["shortfall"] for e in ("X", "Y", "Z")), 1
        )

    def test_double_locking_same_serial_raises(self):
        lots = [lot("L", "A", 5, "2027-01-01")]
        requests = [req("X", "A", 3), req("Y", "A", 3)]
        ledger = ReservationLedger(lots, requests)
        owned_by_x = ledger["X"]["serials"][0]
        with self.assertRaises(SerialConflict):
            ledger.lock_serials("Y", [owned_by_x])


class GradeSubstitutionTest(unittest.TestCase):
    def test_grade_direction(self):
        self.assertTrue(grade_covers("A", "B"))
        self.assertTrue(grade_covers("A", "A"))
        self.assertTrue(grade_covers("B", "C"))
        self.assertFalse(grade_covers("B", "A"))
        self.assertFalse(grade_covers("C", "B"))

    def test_high_grade_substitutes_low_demand(self):
        ledger = ReservationLedger([lot("LA", "A", 2, "2027-12-31")],
                                   [req("E-B", "B", 2)])
        result = ledger.plan()["E-B"]
        self.assertTrue(result.feasible)
        self.assertEqual(result.lot_ids, ["LA"])
        self.assertTrue(result["substituted"])
        self.assertTrue(result["allocations"][0]["substituted"])

    def test_low_grade_cannot_cover_high_demand(self):
        ledger = ReservationLedger([lot("LB", "B", 5, "2027-12-31")],
                                   [req("E-A", "A", 4)])
        result = ledger.plan()["E-A"]
        self.assertFalse(result.feasible)
        self.assertEqual(result["shippable_quantity"], 0)
        self.assertEqual(result["shortfall"], 4)
        availability = batch_availability(
            ledger.lots, ledger.request_by_id["E-A"])
        self.assertIn("grade-too-low",
                      next(iter(availability[0]["serial_issues"].values())))

    def test_exact_grade_is_preferred_over_upgrade(self):
        lots = [lot("LA", "A", 2, "2027-12-31"),
                lot("LB", "B", 2, "2027-06-30")]
        ledger = ReservationLedger(lots, [req("E-B", "B", 2)])
        result = ledger.plan()["E-B"]
        self.assertEqual(result.lot_ids, ["LB"])
        self.assertFalse(result["substituted"])
        # A 批次原样保留给未来的 A 级场。
        self.assertEqual(ledger.batch_totals().get("LA", 0), 0)


class ExpiryTest(unittest.TestCase):
    def test_expiry_judged_on_actual_use_date(self):
        self.assertTrue(is_fresh("2026-05-02", "2026-05-01"))
        self.assertTrue(is_fresh("2026-05-02", "2026-05-02"))   # 有效期至当日
        self.assertFalse(is_fresh("2026-05-02", "2026-05-03"))
        self.assertFalse(is_fresh("2026-05-02", "2026-05-05"))

    def test_expired_a_batch_is_not_a_valid_b_substitute(self):
        # 只有 A 级 F1，但 exam-B 的实际使用日是 5 月 5 日：F1 已过期，
        # 级别虽可替代，效期硬约束仍禁止发放。
        lots = [lot("F1", "A", 5, "2026-05-02")]
        ledger = ReservationLedger(lots, [req("exam-B", "B", 4, "2026-05-05")])
        result = ledger.plan()["exam-B"]
        self.assertEqual(result["shippable_quantity"], 0)
        self.assertEqual(result["shortfall"], 4)
        rows = batch_availability(lots, ledger.request_by_id["exam-B"])
        self.assertEqual(rows[0]["eligible_serials"], [])
        self.assertTrue(
            any("expired-on-use-date" in issues
                for issues in rows[0]["serial_issues"].values())
        )

    def test_fefo_prefers_earlier_expiry_when_otherwise_equal(self):
        lots = [lot("NEAR", "A", 2, "2027-01-01"),
                lot("FAR", "A", 2, "2027-12-31")]
        ledger = ReservationLedger(lots, [req("E-A", "A", 2, "2026-06-01")])
        self.assertEqual(ledger.plan()["E-A"].lot_ids, ["NEAR"])


class GreedySubstitutionRegressionTest(unittest.TestCase):
    """新增回归反例：早到 B 级场 + 未来 A-only 场 + 长有效期 A 批次。"""

    def test_future_a_only_session_is_protected(self):
        lots = [
            lot("LA", "A", 4, "2027-12-31"),   # 长有效期 A，可降级替 B
            lot("LB", "B", 4, "2026-08-31"),    # 较短但对早场仍有效
        ]
        # 故意把“早到的 B 级场”排在请求列表前面，模拟贪心到达顺序。
        requests = [
            req("exam-B-early", "B", 4, "2026-06-01"),
            req("exam-A-future", "A", 4, "2026-11-20"),
        ]
        ledger = ReservationLedger(lots, requests)
        plan = ledger.plan()

        self.assertTrue(plan["exam-B-early"].feasible)
        self.assertTrue(plan["exam-A-future"].feasible)
        # 正确的联合决策：B 场吃 B 批次（近效期），A 批次完整留给 A-only 场。
        self.assertEqual(plan["exam-B-early"].lot_ids, ["LB"])
        self.assertEqual(plan["exam-A-future"].lot_ids, ["LA"])
        self.assertEqual(ledger.batch_totals(), {"LA": 4, "LB": 4})
        ledger.assert_exclusive()
        self.assertEqual(ledger.procurement_orders(), [])

    def test_naive_arrival_greedy_would_have_failed(self):
        """把反例的陷阱显式化：若让 B 场先贪心拿走 LA，A-only 场必然缺口。"""
        lots = [
            lot("LA", "A", 4, "2027-12-31"),
            lot("LB", "B", 4, "2026-08-31"),
        ]
        # 错误示范：B 场只按“第一个兼容批次”贪心取 LA。
        taken_by_b = [s for row in batch_availability(
            lots, req("exam-B-early", "B", 4, "2026-06-01"))
            if row["lot_id"] == "LA" for s in row["available_serials"][:4]]
        occupied = set(taken_by_b)
        a_left = available(
            lots, "filter", on_date="2026-11-20", required_grade="A",
            occupied_serials=occupied,
        )
        self.assertEqual(len(taken_by_b), 4)
        self.assertEqual(a_left, 0)  # 未来 A-only 场被挤掉 —— 正是回归反例。


def signed_two_session_ledger(with_replacement_lot=True):
    lots = [
        lot("F1", "A", 5, "2027-03-01"),
        lot("F2", "B", 5, "2027-06-30"),
    ]
    if with_replacement_lot:
        lots.append(lot("F3", "A", 4, "2027-12-31"))
    requests = [
        req("exam-A", "A", 4, "2026-06-01"),
        req("exam-B", "B", 4, "2026-06-02"),
    ]
    ledger = ReservationLedger(lots, requests)
    ledger.pack_kit(exam_id="exam-A", kit_id="K-A", at="2026-05-20")
    ledger.sign_kit("K-A", by="考务张", at="2026-05-21")
    ledger.pack_kit(exam_id="exam-B", kit_id="K-B", at="2026-05-20")
    ledger.sign_kit("K-B", by="监考李", at="2026-05-21")
    return ledger


class SignedKitRecallTest(unittest.TestCase):
    def test_recall_traces_signed_kit_withdraws_and_replaces(self):
        ledger = signed_two_session_ledger(with_replacement_lot=True)
        before_b = json.dumps(ledger.kits["K-B"], sort_keys=True, ensure_ascii=False)

        report = ledger.recall_lot(
            {"id": "RC-LATE-F1", "type": "supplier_recall", "lot_id": "F1",
             "decision": "recalled", "date": "2026-05-25",
             "note": "晚到的批次召回原件"})

        # 追到具体考包，而不是只减总量。
        self.assertEqual(report["affected_kit_ids"], ["K-A"])
        self.assertEqual(report["unaffected_kit_ids"], ["K-B"])
        self.assertEqual(len(report["withdrawals"]), 1)
        withdrawal = report["withdrawals"][0]
        self.assertEqual(withdrawal["kit_id"], "K-A")
        self.assertEqual(withdrawal["exam_id"], "exam-A")
        self.assertEqual(withdrawal["quantity"], 4)
        self.assertTrue(all(s.startswith("F1-") for s in withdrawal["serials"]))
        self.assertTrue(withdrawal["id"].startswith("WDR"))

        # 替换流转 + 替换考包，新实物来自 F3。
        self.assertEqual(len(report["replacements"]), 1)
        replacement = report["replacements"][0]
        self.assertEqual(replacement["quantity"], 4)
        self.assertTrue(all(s.startswith("F3-") for s in replacement["serials"]))
        self.assertEqual(len(report["replacement_kit_ids"]), 1)
        new_kit_id = report["replacement_kit_ids"][0]
        new_kit = ledger.kits[new_kit_id]
        self.assertEqual(new_kit["status"], "packed")
        self.assertEqual(new_kit["serials"], replacement["serials"])
        self.assertEqual(new_kit["replacement_for"], "K-A")

        # 原预留快照与签收历史完整保留。
        old_kit = ledger.kits["K-A"]
        self.assertEqual(old_kit["status"], "withdrawn")
        self.assertEqual(old_kit["signed_by"], "考务张")
        self.assertEqual(old_kit["signed_at"], "2026-05-21")
        self.assertEqual(old_kit["original"]["serials"], withdrawal["serials"])
        events = [h["event"] for h in old_kit["history"]]
        self.assertEqual(
            events, ["packed", "signed", "recall_notice",
                     "withdrawal_issued", "replacement_issued"]
        )

        # 当前 exam-A 由 F3 足额可发；跨场排他仍成立。
        current_a = ledger["exam-A"]
        self.assertTrue(current_a.feasible)
        self.assertEqual(current_a.lot_ids, ["F3"])
        ledger.assert_exclusive()
        self.assertEqual(ledger.procurement_orders(), [])

        # 晚到召回只重排受影响考包：K-B 原样不动。
        after_b = json.dumps(ledger.kits["K-B"], sort_keys=True, ensure_ascii=False)
        self.assertEqual(before_b, after_b)
        self.assertEqual(ledger.kits["K-B"]["status"], "signed")

        # 全账报告可序列化。
        json.dumps(ledger.report(), ensure_ascii=False)

    def test_recall_other_lot_only_touches_that_kit(self):
        ledger = signed_two_session_ledger(with_replacement_lot=True)
        before_a = json.dumps(ledger.kits["K-A"], sort_keys=True, ensure_ascii=False)

        report = ledger.recall_lot("F2", document_id="RC-F2", at="2026-05-26")

        self.assertEqual(report["affected_kit_ids"], ["K-B"])
        self.assertIn("K-A", report["unaffected_kit_ids"])
        after_a = json.dumps(ledger.kits["K-A"], sort_keys=True, ensure_ascii=False)
        self.assertEqual(before_a, after_a)

    def test_recall_without_replacement_opens_procurement_order(self):
        ledger = signed_two_session_ledger(with_replacement_lot=False)
        report = ledger.recall_lot("F1", document_id="RC-NO-REPL", at="2026-05-25")

        self.assertEqual(report["affected_kit_ids"], ["K-A"])
        self.assertEqual(report["replacements"], [])
        self.assertFalse(ledger["exam-A"].feasible)
        self.assertEqual(ledger["exam-A"]["shortfall"], 4)

        orders = ledger.procurement_orders()
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["quantity"], 4)
        self.assertEqual(order["reason"], "recall")
        self.assertEqual(order["recall_id"], "RC-NO-REPL")
        self.assertEqual(order["source_kit_ids"], ["K-A"])

        # 历史仍在，没有用“总量减去召回数”把记录抹掉。
        old_kit = ledger.kits["K-A"]
        self.assertEqual(old_kit["status"], "withdrawn")
        self.assertEqual(old_kit["signed_by"], "考务张")
        self.assertIn("signed", [h["event"] for h in old_kit["history"]])
        self.assertEqual(len(old_kit["original"]["serials"]), 4)
        ledger.assert_exclusive()

    def test_recall_before_packing_has_no_kit_to_trace(self):
        # 配包前隔离：没有任何考包，召回报告不得伪造撤回单，只暴露缺口。
        ledger = ReservationLedger(
            [lot("F1", "A", 5, "2027-03-01")],
            [req("exam-A", "A", 4)],
        )
        report = ledger.recall_lot("F1", timing="before_packing",
                                   document_id="RC-EARLY")
        self.assertEqual(report["affected_kit_ids"], [])
        self.assertEqual(report["withdrawals"], [])
        self.assertFalse(ledger["exam-A"].feasible)
        self.assertEqual(ledger.procurement_orders()[0]["quantity"], 4)


class TransferOrderTest(unittest.TestCase):
    def test_cross_site_stock_produces_transfer_order(self):
        lots = [lot("R", "A", 4, "2027-01-01", location="WH-2")]
        requests = [req("E", "A", 4, location="SITE-9")]
        ledger = ReservationLedger(lots, requests)
        transfers = ledger.transfer_orders()
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["quantity"], 4)
        self.assertEqual(transfers[0]["from_location"], "WH-2")
        self.assertEqual(transfers[0]["to_location"], "SITE-9")
        self.assertTrue(ledger["E"].feasible)


class OriginalAssessmentFixturesTest(unittest.TestCase):
    """原有 candidate/station/assessment 考评夹具与工作流必须保持绿色。"""

    def test_original_fixtures_import_report_and_audit(self):
        from skillstation.core import connect, import_file, report, audit

        fixtures = ROOT / "fixtures"
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "stock-seed-check.sqlite")
            try:
                for subject in ("candidate", "station", "assessment"):
                    result = import_file(db, subject, fixtures / f"{subject}-ok.json")
                    self.assertGreaterEqual(result["inserted"], 1)
                self.assertEqual(len(report(db)["decisions"]), 10)
                self.assertTrue(audit(db)["ok"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
