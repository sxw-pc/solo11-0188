"""主分表对账增强测试。

覆盖任务硬约束:
1. 两路双方签认协议(热负载 70:30 -> 75/45; 等面积 -> 65/55);
2. 父表不得与下游子表相加(错误口径 240/各 120 -> 正确主表 120);
3. HVAC 回卷 + 主表跨日换表(不同倍率)的原始读数/证据衔接;
4. 时钟错位必须按签署偏差对齐, 最近时刻匹配会漏计并破坏守恒;
5. 晚到校正保留旧账、只更正受影响时段;
6. 争议表计留在待核差额, 不跨租户借量;
7. 既有 candidate/station/assessment 实操夹具与工作流不回归。

运行: ``python -B -m pytest -q -p no:cacheprovider``
"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

from skillstation.meter_capture import (
    BoundaryAlignmentError,
    NonMonotonicReadingError,
    SignatureError,
    interval_energy,
    reconcile_meters,
    seal_record,
    total_kwh,
    verify_sealed,
)
from skillstation.energy_ledger import (
    EnergyLedger,
    AgreementError,
    correct_payload,
    render_statement,
    settle,
    station_kwh,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "exam-energy.json"
FIXTURES = ROOT / "fixtures"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))

P0 = "2026-09-01T00:00:00+08:00"
P1 = "2026-10-01T00:00:00+08:00"
SEPT = {"id": "2026-09-exam", "start": P0, "end": P1}


def heat_agreement(period_id, shared_meter, evidence_id,
                   shares=("0.70", "0.30"), aid=None):
    return seal_record({
        "agreement_id": aid or f"AGR-HEAT-{period_id}",
        "period_id": period_id,
        "parties": ["S7", "S8"],
        "shared_meter": shared_meter,
        "method": "verified_thermal_load",
        "evidence_id": evidence_id,
        "claimed_share": {"S7": shares[0], "S8": shares[1]},
    }, [("S7", "S7 代表"), ("S8", "S8 代表")])


def heat_evidence(evidence_id, s7=350, s8=150):
    return seal_record({
        "evidence_id": evidence_id,
        "thermal_by_station": {"S7": s7, "S8": s8},
    }, [("venue", "计量员"), ("S7", "S7 代表"), ("S8", "S8 代表")])


def simple_period_payload(period_id=SEPT["id"], master=100, s7=30, s8=20,
                          hvac=50, *, agreement="area", with_agreement=True):
    """构造薄 readings 形态的单时段夹具(拓扑显式、无时间轴也可对账)。"""
    payload = {
        "billing_period": dict(SEPT, id=period_id) if period_id == SEPT["id"]
        else {"id": period_id, "start": P0, "end": P1},
        "stations": ["S7", "S8"],
        "topology": {
            "topology_id": f"TOPO-{period_id}", "root": "m",
            "edges": [
                {"parent": "m", "child": "d7", "role": "direct", "station": "S7"},
                {"parent": "m", "child": "d8", "role": "direct", "station": "S8"},
                {"parent": "m", "child": "h", "role": "shared"},
            ]},
        "readings": [
            {"id": "m", "role": "master", "start": 0, "end": master},
            {"id": "d7", "role": "direct", "station": "S7", "parent": "m",
             "start": 0, "end": s7},
            {"id": "d8", "role": "direct", "station": "S8", "parent": "m",
             "start": 0, "end": s8},
            {"id": "h", "role": "shared", "parent": "m", "start": 0, "end": hvac},
        ],
    }
    if with_agreement and agreement == "area":
        payload["current_agreement_id"] = f"AGR-AREA-{period_id}"
        payload["agreement_records"] = [seal_record({
            "agreement_id": f"AGR-AREA-{period_id}",
            "period_id": period_id, "parties": ["S7", "S8"],
            "shared_meter": "h", "method": "certified_area",
            "evidence_id": f"EV-AREA-{period_id}",
            "claimed_share": {"S7": "0.5", "S8": "0.5"},
        }, [("S7", "代表"), ("S8", "代表")])]
        payload["evidence_records"] = [seal_record({
            "evidence_id": f"EV-AREA-{period_id}",
            "area_m2": {"S7": 60, "S8": 60}}, [])]
    elif with_agreement and agreement == "heat":
        eid = f"EV-LOAD-{period_id}"
        aid = f"AGR-HEAT-{period_id}"
        payload["current_agreement_id"] = aid
        payload["agreement_records"] = [heat_agreement(period_id, "h", eid, aid=aid)]
        payload["evidence_records"] = [heat_evidence(eid)]
    return payload


def naive_nearest_value(readings, target):
    """反例方法: 不做时钟校正, 按表面时刻取最近读数。"""
    t = datetime.fromisoformat(target)
    return min(readings, key=lambda r: abs(
        (datetime.fromisoformat(r["at"]) - t).total_seconds()))["value"]


class SeedFixtureShapeTest(unittest.TestCase):
    def test_fixed_four_meter_values(self):
        by_id = {r["id"]: r for r in DATA["readings"]}
        self.assertEqual((by_id["master"]["start"], by_id["master"]["end"]), (1000, 1120))
        self.assertEqual(by_id["master"]["end"] - by_id["master"]["start"], 120)
        self.assertEqual(by_id["S7-direct"]["end"] - by_id["S7-direct"]["start"], 40)
        self.assertEqual(by_id["S8-direct"]["end"] - by_id["S8-direct"]["start"], 30)
        self.assertEqual(by_id["shared-hvac"]["end"] - by_id["shared-hvac"]["start"], 50)
        self.assertEqual(DATA["stations"], ["S7", "S8"])

    def test_topology_sealed_and_signed_by_all_parties(self):
        doc = verify_sealed(DATA["topology"],
                            required_parties=["venue", "S7", "S8"],
                            label="计量拓扑")
        self.assertEqual(doc["root"], "master")
        self.assertEqual({e["child"] for e in doc["edges"]},
                         {"S7-direct", "S8-direct", "shared-hvac"})

    def test_two_agreement_originals_both_signed_by_parties(self):
        for rec in DATA["agreement_records"]:
            doc = verify_sealed(rec, required_parties=["S7", "S8"])
            self.assertIn(doc["method"],
                          ("verified_thermal_load", "certified_area"))

    def test_topology_tampering_is_rejected(self):
        bad = copy.deepcopy(DATA)
        bad["topology"]["doc"]["edges"][0]["child"] = "ghost-meter"
        with self.assertRaises(SignatureError):
            reconcile_meters(bad)

    def test_agreement_tampering_is_rejected(self):
        bad = copy.deepcopy(DATA)
        bad["agreement_records"][0]["doc"]["claimed_share"]["S7"] = "0.99"
        # agreements 别名中是另一份未改原件 -> 多来源摘要冲突
        with self.assertRaises(SignatureError):
            settle(bad)
        # 只剩单一来源时, 信封摘要校验本身同样拒绝篡改
        single = copy.deepcopy(DATA)
        single.pop("agreements", None)
        single["agreement_records"] = [copy.deepcopy(DATA["agreement_records"][0])]
        single["agreement_records"][0]["doc"]["claimed_share"]["S7"] = "0.99"
        with self.assertRaises(SignatureError):
            settle(single)


class ParentChildDoubleCountTest(unittest.TestCase):
    def test_total_kwh_is_master_120_not_240(self):
        # 旧实现的错误结果被显式固定为对照
        buggy = sum(r["end"] - r["start"] for r in DATA["readings"])
        self.assertEqual(buggy, 240)
        # 旧 energy_ledger 把 240 平均分给两站 -> 各 120 的错误口径
        self.assertEqual({s: buggy / len(DATA["stations"])
                           for s in DATA["stations"]},
                          {"S7": 120, "S8": 120})
        self.assertEqual(total_kwh(DATA["readings"]), 120)
        self.assertEqual(total_kwh(DATA), 120)

    def test_reconcile_flags_naive_240_and_double_count_120(self):
        rep = reconcile_meters(DATA)
        self.assertEqual(rep["master_kwh"], 120)
        self.assertEqual(rep["naive_sum_all_meters_kwh"], 240)
        self.assertEqual(rep["parent_child_double_count_kwh"], 120)
        self.assertEqual(rep["known_submeter_kwh"], 120)
        self.assertEqual(rep["meter_difference_kwh"], 0)
        self.assertTrue(rep["conservation_ok"])

    def test_master_covers_three_children_and_40_30_50_sums_120(self):
        rep = reconcile_meters(DATA)
        check = next(c for c in rep["hierarchy_checks"] if c["parent"] == "master")
        self.assertEqual(check["direct_children"],
                         ["S7-direct", "S8-direct", "shared-hvac"])
        self.assertEqual(check["children_kwh"], 120)
        self.assertEqual(check["residual_kwh"], 0)
        self.assertTrue(check["ok"])
        self.assertEqual(40 + 30 + 50, rep["master_kwh"])

    def test_plain_independent_list_still_sums(self):
        # 完全没有拓扑关系信息时, 各表视为独立进线表
        self.assertEqual(
            total_kwh([{"id": "a", "start": 0, "end": 10},
                        {"id": "b", "start": 0, "end": 5}]), 15)


class TwoProtocolSettlementTest(unittest.TestCase):
    def test_thermal_load_70_30_gives_75_45(self):
        st = settle(DATA, agreement_id="AGR-HVAC-202610-HEAT")
        self.assertEqual(st["direct_kwh"], {"S7": 40, "S8": 30})
        self.assertEqual(st["shared_kwh"], 50)
        self.assertEqual(st["shared_allocation_kwh"], {"S7": 35, "S8": 15})
        self.assertEqual(st["billed_kwh"], {"S7": 75, "S8": 45})
        self.assertEqual(st["unassigned_kwh"], 0)
        self.assertTrue(st["conservation"]["ok"])
        self.assertEqual(st["conservation"]["billed_total_kwh"]
                         + st["conservation"]["unassigned_kwh"], 120)

    def test_equal_certified_area_gives_65_55(self):
        st = settle(DATA, agreement_id="AGR-HVAC-202610-AREA")
        self.assertEqual(st["shared_allocation_kwh"], {"S7": 25, "S8": 25})
        self.assertEqual(st["billed_kwh"], {"S7": 65, "S8": 55})
        self.assertEqual(st["unassigned_kwh"], 0)
        self.assertTrue(st["conservation"]["ok"])

    def test_both_paths_share_same_meter_facts_not_just_ratio_swap(self):
        heat = settle(DATA, agreement_id="AGR-HVAC-202610-HEAT")
        area = settle(DATA, agreement_id="AGR-HVAC-202610-AREA")
        for key in ("master_kwh", "direct_kwh", "shared_kwh"):
            self.assertEqual(heat[key], area[key])
        self.assertEqual((heat["master_kwh"], heat["shared_kwh"]), (120, 50))
        # 两路结果都回到同一个主表净增量
        for st in (heat, area):
            self.assertEqual(
                sum(st["billed_kwh"].values()) + st["unassigned_kwh"], 120)

    def test_fixture_expected_settlements_match(self):
        for aid, expected in DATA["expected_settlements"].items():
            st = settle(DATA, agreement_id=aid)
            self.assertEqual(st["billed_kwh"]["S7"], expected["S7"])
            self.assertEqual(st["billed_kwh"]["S8"], expected["S8"])
            self.assertEqual(st["unassigned_kwh"], expected["unassigned_kwh"])

    def test_claimed_ratio_mismatching_verifiable_evidence_rejected(self):
        rogue = {"method": "verified_thermal_load",
                 "evidence_id": "EV-LOAD-202610",
                 "claimed_share": {"S7": "0.60", "S8": "0.40"}}
        with self.assertRaises(AgreementError):
            settle(DATA, agreement=rogue, verify=False)

    def test_station_kwh_compat_entrypoints(self):
        self.assertEqual(station_kwh(DATA), {"S7": 75, "S8": 45})
        self.assertEqual(
            station_kwh(DATA, agreement_id="AGR-HVAC-202610-AREA"),
            {"S7": 65, "S8": 55})
        # 旧签名 readings + stations; 无协议时只对 shared 部分等分摊
        self.assertEqual(
            station_kwh(DATA["readings"], DATA["stations"]),
            {"S7": 65, "S8": 55})
        self.assertEqual(
            station_kwh(DATA["readings"], DATA["stations"],
                        {"method": "equal_shares"}),
            {"S7": 65, "S8": 55})

    def test_rendered_traceable_basis(self):
        text = render_statement(settle(DATA))
        self.assertIn("75", text)
        self.assertIn("45", text)
        self.assertIn("AGR-HVAC-202610-HEAT", text)
        self.assertIn("EV-LOAD-202610", text)
        self.assertIn("[守恒 OK]", text)
        self.assertIn("TOPO-HALL78", text)


class RolloverAndReplacementTest(unittest.TestCase):
    def setUp(self):
        self.payload = DATA["scenarios"]["rollover_replacement"]

    def test_reconstructs_rollover_and_different_ratio_replacement(self):
        rep = reconcile_meters(self.payload)
        self.assertEqual(rep["master_kwh"], 190)
        self.assertEqual(rep["meter_kwh"]["r-hvac"], 120)
        self.assertEqual(rep["meter_kwh"]["r-s7"], 40)
        self.assertEqual(rep["meter_kwh"]["r-s8"], 30)
        self.assertEqual(rep["meter_difference_kwh"], 0)
        master_rep = rep["meters"]["r-master"]
        bridges = [s["bridge"] for s in master_rep["segments"]]
        self.assertIn("replacement", bridges)
        hvac_rep = rep["meters"]["r-hvac"]
        self.assertIn("rollover", [s["bridge"] for s in hvac_rep["segments"]])
        # 新主表倍率 2 的段: 45 原始差 * 2 = 90
        ratio_two = [s for s in master_rep["segments"] if str(s["multiplier"]) == "2"]
        self.assertTrue(any(s["kwh"] == 90 for s in ratio_two))

    def test_naive_clamp_would_zero_out_both_meters(self):
        hvac = next(m for m in self.payload["meters"] if m["id"] == "r-hvac")
        master = next(m for m in self.payload["meters"] if m["id"] == "r-master")
        hvac_boundary = (hvac["readings"][0]["value"], hvac["readings"][-1]["value"])
        master_boundary = (master["readings"][0]["value"], master["readings"][-1]["value"])
        naive_hvac = max(hvac_boundary[1] - hvac_boundary[0], 0)
        naive_master = max(master_boundary[1] - master_boundary[0], 0)
        self.assertEqual((naive_hvac, naive_master), (0, 0))
        # 朴素口径只剩两只直用表 40+30=70, 对不回主表 190
        self.assertEqual(40 + 30 + naive_hvac + naive_master, 70)

    def test_negative_delta_without_bridge_is_not_zeroed(self):
        broken = copy.deepcopy(self.payload)
        hvac = next(m for m in broken["meters"] if m["id"] == "r-hvac")
        hvac["bridges"] = []
        with self.assertRaises(NonMonotonicReadingError):
            reconcile_meters(broken)

    def test_settlement_after_bridges(self):
        st = settle(self.payload)
        self.assertEqual(st["billed_kwh"], {"S7": 124, "S8": 66})
        self.assertEqual(st["unassigned_kwh"], 0)
        self.assertTrue(st["conservation"]["ok"])
        self.assertEqual(sum(st["billed_kwh"].values()), 190)

    def test_missing_bridge_goes_pending_not_to_other_tenant(self):
        broken = copy.deepcopy(self.payload)
        hvac = next(m for m in broken["meters"] if m["id"] == "r-hvac")
        hvac["bridges"] = []
        st = settle(broken)  # strict_meters=False 默认
        self.assertIn("r-hvac", st["pending_meters"])
        # 主表 190 仍在; 已知直用 40+30, 其余 120 全部待核
        self.assertEqual(st["billed_kwh"], {"S7": 40, "S8": 30})
        self.assertEqual(st["unassigned_kwh"], 120)
        self.assertTrue(st["conservation"]["ok"])


class ClockSkewAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.payload = DATA["scenarios"]["clock_skew"]

    def test_aligned_reconstruction_restores_interval(self):
        rep = reconcile_meters(self.payload)
        self.assertEqual(rep["master_kwh"], 120)
        self.assertEqual(rep["meter_kwh"]["c-s7"], 40)
        self.assertEqual(rep["meter_kwh"]["c-s8"], 30)
        self.assertEqual(rep["meter_kwh"]["c-hvac"], 50)
        self.assertEqual(rep["meter_difference_kwh"], 0)

    def test_naive_nearest_match_misses_energy_and_breaks_conservation(self):
        period = self.payload["billing_period"]
        s7 = next(m for m in self.payload["meters"] if m["id"] == "c-s7")
        s8 = next(m for m in self.payload["meters"] if m["id"] == "c-s8")
        naive_s7 = (naive_nearest_value(s7["readings"], period["end"])
                    - naive_nearest_value(s7["readings"], period["start"]))
        naive_s8 = (naive_nearest_value(s8["readings"], period["end"])
                    - naive_nearest_value(s8["readings"], period["start"]))
        self.assertEqual((naive_s7, naive_s8), (38, 28))
        self.assertEqual(naive_s7 + naive_s8 + 50, 116)  # 对不回主表 120

    def test_missing_clock_evidence_refuses_nearest_guess(self):
        untimed = copy.deepcopy(self.payload)
        s7 = next(m for m in untimed["meters"] if m["id"] == "c-s7")
        s7["clock_fast_seconds"] = 0
        with self.assertRaises(BoundaryAlignmentError):
            reconcile_meters(untimed)

    def test_settlement_conservation_under_skew(self):
        st = settle(self.payload)
        self.assertEqual(st["billed_kwh"], {"S7": 65, "S8": 55})
        self.assertEqual(st["unassigned_kwh"], 0)
        self.assertTrue(st["conservation"]["ok"])


class NoCrossTenantBorrowingTest(unittest.TestCase):
    def test_disputed_s7_direct_stays_unassigned_s8_unchanged(self):
        rep = reconcile_meters(DATA, disputed_meter_ids=["S7-direct"])
        st = settle(DATA, reconciliation=rep,
                    disputed_meter_ids=["S7-direct"])
        # S7 只拿有协议+证据的 HVAC 35; S8 仍是 30+15=45, 不替 S7 背 40
        self.assertEqual(st["billed_kwh"], {"S7": 35, "S8": 45})
        self.assertEqual(st["unassigned_kwh"], 40)
        self.assertTrue(st["conservation"]["ok"])
        self.assertEqual(
            sum(st["billed_kwh"].values()) + st["unassigned_kwh"], 120)

    def test_no_agreement_keeps_shared_50_pending(self):
        payload = copy.deepcopy(DATA)
        payload["current_agreement_id"] = None
        st = settle(payload)
        self.assertEqual(st["billed_kwh"], {"S7": 40, "S8": 30})
        self.assertEqual(st["unassigned_kwh"], 50)
        self.assertEqual(st["pending_allocation_kwh"], 50)
        self.assertTrue(st["conservation"]["ok"])
        with self.assertRaises(AgreementError):
            settle(payload, allow_provisional=False)


class LateRevisionTest(unittest.TestCase):
    def test_late_agreement_revision_keeps_old_book_and_scopes_period(self):
        september = simple_period_payload(agreement="area")
        october = copy.deepcopy(DATA)  # 2026-10-exam, heat 75/45

        ledger = EnergyLedger()
        sept_v1 = settle(september)
        oct_v1 = settle(october)
        ledger.post(sept_v1, posted_at="2026-10-02T09:00:00+08:00")
        ledger.post(oct_v1, posted_at="2026-11-02T09:00:00+08:00")
        oct_entry_before = ledger.current_entry("2026-10-exam")

        # 九月晚到的签认热负载原件
        sept_heat = simple_period_payload(agreement="heat")
        sept_v2 = settle(sept_heat)
        entry = ledger.revise(
            "2026-09-exam", sept_v2,
            reason="晚到的九月热量表校准报告与双方补充协议",
            evidence_id=f"EV-LOAD-2026-09-exam",
            posted_at="2026-11-08T15:00:00+08:00")

        versions = ledger.versions("2026-09-exam")
        self.assertEqual(len(versions), 2)
        # 旧账保留: v1 仍是等面积 55/45
        self.assertEqual(versions[0]["billed_kwh"], {"S7": 55, "S8": 45})
        self.assertIsNone(versions[0]["correction"])
        # 新账: 70:30 热负载 -> 65/35
        self.assertEqual(versions[1]["billed_kwh"], {"S7": 65, "S8": 35})
        self.assertEqual(entry["correction"]["scope"], ["2026-09-exam"])
        self.assertEqual(entry["correction"]["corrects_revision"], 1)
        self.assertIn("billed_kwh.S7", entry["correction"]["changed_fields"])
        # 十月完全不受影响, 摘要不变、版本号仍为 1
        oct_versions = ledger.versions("2026-10-exam")
        self.assertEqual(len(oct_versions), 1)
        self.assertEqual(oct_versions[0]["sha256"], oct_entry_before["sha256"])
        self.assertEqual(ledger.current("2026-10-exam")["billed_kwh"],
                         {"S7": 75, "S8": 45})
        # 两个时段各版本守恒
        for pid in ("2026-09-exam", "2026-10-exam"):
            for v in ledger.versions(pid):
                self.assertTrue(v["conservation_ok"])

    def test_late_rollover_bridge_reconstructs_and_revises_only_period(self):
        payload = copy.deepcopy(DATA["scenarios"]["rollover_replacement"])
        hvac = next(m for m in payload["meters"] if m["id"] == "r-hvac")
        bridge = hvac.pop("bridges")[0]  # 先丢失回卷证据

        ledger = EnergyLedger()
        v1 = settle(payload)
        ledger.post(v1, period_id="rollover-period",
                    posted_at="2026-11-02T09:00:00+08:00")
        self.assertEqual(v1["billed_kwh"], {"S7": 40, "S8": 30})
        self.assertEqual(v1["unassigned_kwh"], 120)

        # 晚到的回卷照片/工单: 不可变校正后重算受影响时段
        corrected = correct_payload(payload, [
            {"op": "add_bridge", "meter_id": "r-hvac", "bridge": bridge}])
        self.assertNotIn("bridges", hvac)  # 原 payload 未被修改
        v2 = settle(corrected)
        ledger.revise("rollover-period", v2,
                      reason="晚到的 HVAC 回卷现场照片与寄存器满度核证",
                      evidence_id=bridge["evidence"])

        versions = ledger.versions("rollover-period")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["billed_kwh"], {"S7": 40, "S8": 30})
        self.assertEqual(versions[0]["unassigned_kwh"], 120)
        self.assertEqual(versions[1]["billed_kwh"], {"S7": 124, "S8": 66})
        self.assertEqual(versions[1]["unassigned_kwh"], 0)
        self.assertTrue(versions[1]["conservation_ok"])


class TrustedObservedAtTest(unittest.TestCase):
    def test_observed_at_overrides_skewed_device_clock(self):
        meter = {"id": "x", "clock_fast_seconds": 99999, "readings": [
            {"at": "2026-10-01T09:00:00+08:00",
             "observed_at": "2026-10-01T00:00:00+08:00", "value": 10},
            {"at": "2026-11-01T09:30:00+08:00",
             "observed_at": "2026-11-01T00:00:00+08:00", "value": 40},
        ]}
        rep = interval_energy(meter, DATA["billing_period"])
        self.assertEqual(rep["kwh"], 30)


ORIGINAL_SEED = {
    "stations": ["S7", "S8"],
    "readings": [
        {"id": "master", "start": 1000, "end": 1120},
        {"id": "S7-direct", "start": 30, "end": 70},
        {"id": "S8-direct", "start": 20, "end": 50},
        {"id": "shared-hvac", "start": 200, "end": 250},
    ],
}


class OriginalSeedShapeCompatTest(unittest.TestCase):
    """即使只有最初的四表 {id,start,end}, 增强入口也不得再报 240/120。"""

    def test_total_is_120_with_id_hint(self):
        self.assertEqual(total_kwh(ORIGINAL_SEED["readings"]), 120)

    def test_station_default_equal_shared_is_65_55(self):
        self.assertEqual(station_kwh(ORIGINAL_SEED["readings"],
                                     ORIGINAL_SEED["stations"]),
                         {"S7": 65, "S8": 55})

    def test_station_inline_heat_weights_75_45(self):
        st = station_kwh(
            ORIGINAL_SEED["readings"], ORIGINAL_SEED["stations"],
            {"method": "verified_thermal_load", "weights": {"S7": 7, "S8": 3}})
        self.assertEqual(st, {"S7": 75, "S8": 45})

    def test_statement_is_json_serializable(self):
        text = json.dumps(settle(DATA), ensure_ascii=False)
        self.assertIn("2026-10-exam", text)


class ExistingPracticeFixturesTest(unittest.TestCase):
    """既有 candidate/station/assessment 实操夹具仍完整可导入、可审计。"""

    def test_all_energy_and_practice_fixtures_are_valid_json(self):
        for path in FIXTURES.glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_practice_ok_fixtures_import_and_audit(self):
        from skillstation.core import connect, import_file, report, audit
        db = connect(":memory:")
        try:
            for sub in ("candidate", "station", "assessment"):
                result = import_file(db, sub, FIXTURES / f"{sub}-ok.json")
                self.assertGreaterEqual(result["inserted"], 1)
            self.assertEqual(len(report(db)["decisions"]), 10)
            self.assertTrue(audit(db)["ok"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
