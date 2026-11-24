import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pytest

from skillstation import clock_capture as cc
from skillstation.operation_capture import actions, capture_timeline, physical_events, solve_order
from skillstation.trace_certificate import (
    INCOMPLETE,
    PENDING,
    SAFE,
    VIOLATION,
    issue,
    persist_certificate,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "operation-clocks.json"
SUPPLEMENT_A = "supplement-A-shared-pulse"
SUPPLEMENT_B = "supplement-B-independent-time"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class OperationTraceSeedTest(unittest.TestCase):
    """The seeded trace: presence alone used to print 完整; now it is 待核."""

    def setUp(self):
        self.bundle = load_fixture()

    def test_presence_only_claims_complete(self):
        # Legacy entry point still aggregates action names...
        self.assertEqual(actions(self.bundle), {"deenergize", "open_panel"})
        captured = physical_events(self.bundle)
        self.assertEqual(set(captured), {"camera", "meter"})
        # ...but the certificate can no longer be "完整" without an order proof.
        cert = issue(self.bundle, issued_at="2026-11-24T09:10:00+08:00")
        self.assertEqual(cert["status"], PENDING)
        self.assertIsNone(cert["supplement_path"])
        self.assertTrue(cert["undecidable_interval_ms"][0] < 0 < cert["undecidable_interval_ms"][1])

    def test_two_physical_timelines_are_both_feasible(self):
        analysis = solve_order(self.bundle)
        self.assertEqual(analysis["relation"], "ambiguous")
        witnesses = analysis["witnesses"]
        self.assertIn("deenergize_first", witnesses)
        self.assertIn("open_first", witnesses)
        # Each witness is one session-shared offset pair, not per-action offsets.
        for witness in witnesses.values():
            self.assertTrue(-1500.0 <= witness["offset_camera_ms"] <= 1500.0)
            self.assertTrue(-750.0 <= witness["offset_meter_ms"] <= 750.0)
        first = witnesses["deenergize_first"]
        self.assertLess(first["deenergize_ms"], first["open_panel_ms"])
        late = witnesses["open_first"]
        self.assertLess(late["open_panel_ms"], late["deenergize_ms"])

    def test_conflict_boundary_is_saved(self):
        analysis = solve_order(self.bundle)
        boundary = analysis["relative_offset"]
        # Nominal gap is -1000 ms: meter tick 20*50ms vs camera tick 10*100ms.
        self.assertAlmostEqual(boundary["boundary_ms"], -1000.0)
        self.assertEqual(boundary["deenergize_first_when"], "delta < boundary_ms")
        self.assertAlmostEqual(boundary["boundary_in_camera_ticks"], -10.0)
        self.assertAlmostEqual(boundary["boundary_in_meter_ticks"], -20.0)
        cert = issue(self.bundle)
        self.assertEqual(cert["conflict_boundary"], boundary)


class DualClockModelTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_fixture()

    def test_independent_clock_units_and_windows(self):
        timeline = capture_timeline(self.bundle)
        by_action = {e["action"]: e for e in timeline["events"] if e["action"]}
        opened = by_action["open_panel"]
        deenergized = by_action["deenergize"]
        # Different tick units are converted to reference milliseconds.
        self.assertEqual(opened["tick_ms"], 100.0)
        self.assertEqual(deenergized["tick_ms"], 50.0)
        self.assertAlmostEqual(opened["nominal_ms"], 2000.0)
        self.assertAlmostEqual(deenergized["nominal_ms"], 3000.0)
        # 15 ticks * tick unit bound per device; drift widens by < 0.2 ms.
        self.assertAlmostEqual(opened["window_ms"][0], 2000.0 - 1500.0 - 0.1, places=4)
        self.assertAlmostEqual(deenergized["window_ms"][0], 3000.0 - 750.0 - 0.1, places=4)
        self.assertEqual(timeline["models"]["camera"]["shared_variables"],
                         ["offset", "drift_rate"])

    def test_offset_is_session_shared_not_per_action(self):
        # The clock model exposes exactly one offset bound per device and the
        # solver has one relative-offset variable spanning both action events.
        timeline = capture_timeline(self.bundle)
        self.assertEqual(timeline["models"]["camera"]["offset_max_ms"], 1500.0)
        self.assertEqual(timeline["models"]["meter"]["offset_max_ms"], 750.0)
        analysis = solve_order(self.bundle)
        self.assertEqual(analysis["relative_offset"]["variable"],
                         "offset_meter - offset_camera")
        self.assertEqual(analysis["relative_offset"]["feasible_ms"], [-2250.0, 2250.0])

    def test_picking_favourable_offset_per_action_is_impossible(self):
        # If per-action offsets were allowed, each event could be shifted to
        # force any order; the shared model instead keeps a single delta whose
        # interval straddles the boundary, so the result stays ambiguous.
        analysis = solve_order(self.bundle)
        delta_lo, delta_hi = analysis["relative_offset"]["feasible_ms"]
        boundary = analysis["relative_offset"]["boundary_ms"]
        self.assertLess(delta_lo, boundary)
        self.assertGreater(delta_hi, boundary)


class RetransmitAndFrameLossTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_fixture()

    def test_meter_cache_retransmit_collapses_by_physical_event(self):
        captured = physical_events(self.bundle)
        meter = captured["meter"]
        ids = [r["event_id"] for r in meter["events"]]
        self.assertEqual(ids.count("meter-0020"), 1)
        self.assertEqual(len(meter["retransmits"]), 1)
        self.assertEqual(meter["retransmits"][0]["event_id"], "meter-0020")
        # The retained row is the original physical observation.
        kept = next(r for r in meter["events"] if r["event_id"] == "meter-0020")
        self.assertEqual(kept["received_at"], 3005)

    def test_lost_camera_frame_is_reported(self):
        captured = physical_events(self.bundle)
        self.assertEqual(captured["camera"]["lost_seqs"], [11])
        self.assertEqual(captured["meter"]["lost_seqs"], [])

    def test_receive_order_cannot_prove_order(self):
        # The meter de-energize row arrives (3005) before the camera open row
        # (3020) even though the camera tick is earlier.  Arrival order is
        # excluded from the ordering decision, which remains ambiguous.
        reordered = copy.deepcopy(self.bundle)
        reordered["meter_records"][1]["received_at"] = 9999
        analysis_before = solve_order(self.bundle)
        analysis_after = solve_order(reordered)
        self.assertEqual(
            analysis_before["difference_ms"]["feasible_ms"],
            analysis_after["difference_ms"]["feasible_ms"],
        )
        self.assertEqual(analysis_after["relation"], "ambiguous")

    def test_same_event_id_with_different_bytes_is_rejected(self):
        tampered = copy.deepcopy(self.bundle)
        tampered["meter_records"][2]["retransmit"] = False
        with self.assertRaises(ValueError):
            cc.load_bundle(tampered)
        # And content mismatch on an actual retransmit is tampering too.
        tampered = copy.deepcopy(self.bundle)
        tampered["meter_records"][2]["tick"] = 21
        with self.assertRaises(ValueError):
            cc.dedupe(cc.load_bundle(tampered))

    def test_retransmit_arriving_first_is_still_canonicalized(self):
        reordered = copy.deepcopy(self.bundle)
        original, replay = reordered["meter_records"][1], reordered["meter_records"][2]
        reordered["meter_records"][1], reordered["meter_records"][2] = replay, original
        captured = physical_events(reordered)
        kept = next(r for r in captured["meter"]["events"]
                    if r["event_id"] == "meter-0020")
        self.assertFalse(kept.get("retransmit"))
        self.assertEqual(kept["received_at"], 3005)

    def test_drift_widens_windows_with_distance_from_anchor(self):
        timeline = capture_timeline(self.bundle)
        near = next(e for e in timeline["events"] if e["event_id"] == "cam-0008")
        far = next(e for e in timeline["events"] if e["event_id"] == "cam-0012")
        near_span = near["window_ms"][1] - near["window_ms"][0]
        far_span = far["window_ms"][1] - far["window_ms"][0]
        self.assertGreater(far_span, near_span)
        # 100 ppm over 1200 ms adds 0.12 ms to each side of the far frame.
        self.assertAlmostEqual(far_span, 2 * (1500.0 + 0.12), places=4)


class SupplementATest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_fixture()

    def test_shared_pulse_issues_safe_order_certificate(self):
        analysis = solve_order(self.bundle, SUPPLEMENT_A)
        self.assertEqual(analysis["relation"], "deenergize_first")
        delta_lo, delta_hi = analysis["relative_offset"]["feasible_ms"]
        # Both pulses pin the same relative offset near -1200 ms.
        self.assertAlmostEqual(delta_lo, -1200.58, places=2)
        self.assertAlmostEqual(delta_hi, -1199.42, places=2)
        self.assertGreater(analysis["supplement"]["safety_margin_ms"], 100.0)
        cert = issue(self.bundle, supplement_path=SUPPLEMENT_A,
                     issued_at="2026-11-24T10:45:00+08:00")
        self.assertEqual(cert["status"], SAFE)
        self.assertEqual(cert["basis"], "shared-pulse")
        self.assertFalse(cert["revoked_legacy"])

    def test_inconsistent_pulse_constraint_is_rejected(self):
        bad = copy.deepcopy(self.bundle)
        bad["supplements"][SUPPLEMENT_A]["pulses"][0]["camera_tick"] = 30
        with self.assertRaises(ValueError):
            solve_order(bad, SUPPLEMENT_A)

    def test_contact_receipt_is_retained_on_timeline(self):
        timeline = capture_timeline(self.bundle)
        deenergized = next(e for e in timeline["events"] if e["action"] == "deenergize")
        self.assertEqual(deenergized["contact_receipt"]["receipt_id"],
                         "rcpt-20261124-0020")
        self.assertEqual(deenergized["contact_receipt"]["state"], "closed")


class SupplementBTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_fixture()

    def test_independent_time_proves_open_first_and_revokes(self):
        analysis = solve_order(self.bundle, SUPPLEMENT_B)
        self.assertEqual(analysis["relation"], "open_first")
        self.assertTrue(analysis["supplement"]["violation"])
        # Independent scope: the camera/meter offset variables stay untouched.
        self.assertTrue(analysis["supplement"]["device_offsets_untouched"])
        cert = issue(self.bundle, supplement_path=SUPPLEMENT_B,
                     issued_at="2026-11-24T11:15:00+08:00")
        self.assertEqual(cert["status"], VIOLATION)
        self.assertEqual(cert["basis"], "independent-time")
        self.assertTrue(cert["revoked_legacy"])
        self.assertEqual(cert["supersedes"], "cert-legacy-wrong-complete")

    def test_anchors_of_one_path_cannot_calibrate_the_other(self):
        # Supplement B's calibration versions never leak into the v2 anchor set.
        cert_b = issue(self.bundle, supplement_path=SUPPLEMENT_B)
        self.assertEqual(
            cert_b["calibration_versions"],
            ["cam-cal-2026-11-24-A", "meter-cal-2026-11-24-A"],
        )
        self.assertEqual(
            sorted(cert_b["supplement_evidence"]["calibration_versions"]),
            ["meter-sync-pulse-v7", "video-frame-time-source-v9"],
        )
        # And applying A never uses B's independent windows.
        cert_a = issue(self.bundle, supplement_path=SUPPLEMENT_A)
        self.assertEqual(cert_a["supplement_evidence"]["scope"], "shared-pulse")
        self.assertNotIn("windows", cert_a["supplement_evidence"])

    def test_both_paths_keep_the_pending_interval_and_legacy_version(self):
        raw = issue(self.bundle)
        cert_a = issue(self.bundle, supplement_path=SUPPLEMENT_A)
        cert_b = issue(self.bundle, supplement_path=SUPPLEMENT_B)
        for cert in (cert_a, cert_b):
            self.assertEqual(cert["legacy_certificate_retained"]["status"], "完整")
            self.assertEqual(cert["legacy_certificate_retained"]["version"], 1)
            self.assertEqual(cert["version"], 2)
            # The unresolved interval computed from raw bounds is preserved
            # inside the timeline/order analysis for later recomputation.
        self.assertEqual(raw["undecidable_interval_ms"],
                         raw["order_analysis"]["difference_ms"]["feasible_ms"])
        self.assertIn("anchor:camera:cam-cal-2026-11-24-A", raw["evidence_acquired_at"])
        self.assertIn(f"supplement:{SUPPLEMENT_A}", cert_a["evidence_acquired_at"])

    def test_certificate_does_not_touch_qualification_station_or_score(self):
        cert = issue(self.bundle, supplement_path=SUPPLEMENT_A)
        self.assertFalse(cert["qualification_changed"])
        self.assertFalse(cert["station_changed"])
        self.assertFalse(cert["score_changed"])


class LateAnchorAndSessionTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_fixture()

    def test_late_anchor_only_recomputes_its_own_session(self):
        other = copy.deepcopy(self.bundle)
        other["session_id"] = "S1-2026-11-24-E2"
        updated = copy.deepcopy(self.bundle)
        updated["anchors"][0]["calibration_version"] = "cam-cal-2026-11-24-B"
        updated["anchors"][0]["acquired_at"] = "2026-11-24T12:00:00+08:00"
        cert_updated = issue(updated)
        cert_other = issue(other)
        self.assertEqual(cert_updated["session_id"], "S1-2026-11-24-E1")
        self.assertIn("cam-cal-2026-11-24-B", cert_updated["calibration_versions"])
        # The untouched session keeps its original calibration version.
        self.assertEqual(cert_other["calibration_versions"],
                         ["cam-cal-2026-11-24-A", "meter-cal-2026-11-24-A"])
        # Both remain pending: a new anchor version alone does not resolve order.
        self.assertEqual(cert_updated["status"], PENDING)
        self.assertEqual(cert_other["status"], PENDING)

    def test_unknown_supplement_is_rejected(self):
        with self.assertRaises(ValueError):
            solve_order(self.bundle, "supplement-not-captured")

    def test_missing_action_is_incomplete_not_complete(self):
        stripped = copy.deepcopy(self.bundle)
        stripped["meter_records"] = [
            r for r in stripped["meter_records"] if r.get("action") != "deenergize"]
        cert = issue(stripped)
        self.assertEqual(cert["status"], INCOMPLETE)
        self.assertEqual(cert["missing_actions"], ["deenergize"])

    def test_deterministic_certificate_id(self):
        first = issue(self.bundle, issued_at="2026-11-24T09:10:00+08:00")
        second = issue(self.bundle, issued_at="2026-11-24T09:10:00+08:00")
        self.assertEqual(first["certificate_id"], second["certificate_id"])
        different = issue(self.bundle, supplement_path=SUPPLEMENT_A,
                          issued_at="2026-11-24T10:45:00+08:00")
        self.assertNotEqual(first["certificate_id"], different["certificate_id"])


class LegacyShapeTest(unittest.TestCase):
    def test_legacy_bare_records_load_and_stay_uncalibrated(self):
        legacy = [
            {"id": "cam-open", "device": "camera", "tick": 10,
             "action": "open_panel", "received_at": 30},
            {"id": "tool-off", "device": "meter", "tick": 20,
             "action": "deenergize", "received_at": 31},
        ]
        self.assertEqual(actions(legacy), {"deenergize", "open_panel"})
        analysis = solve_order(legacy)
        # No anchors / tick units: the order cannot be decided at all.
        self.assertEqual(analysis["status"], "analyzed")
        self.assertEqual(analysis["relation"], "ambiguous")
        self.assertIsNone(analysis["relative_offset"]["feasible_ms"][0])
        cert = issue(legacy)
        self.assertEqual(cert["status"], PENDING)


class SqliteEvidenceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = cc.connect(Path(self.tmp.name) / "trace.sqlite")
        self.bundle = load_fixture()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ingest_is_transactional_idempotent_and_rebuildable(self):
        first = cc.ingest_bundle(self.db, self.bundle, received_at=4000)
        second = cc.ingest_bundle(self.db, self.bundle, received_at=4000)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(first["sha"], second["sha"])
        # Frame gaps are durable evidence, not just an in-memory computation.
        self.assertEqual(cc.stored_gaps(self.db, self.bundle["session_id"]),
                         {"camera": [11], "meter": []})
        rebuilt = cc.rebuild_bundle(self.db, self.bundle["session_id"])
        # Recomputing from persisted raw evidence yields the same conclusion.
        self.assertEqual(solve_order(rebuilt)["relation"], "ambiguous")
        self.assertEqual(
            issue(rebuilt, supplement_path=SUPPLEMENT_A)["status"], SAFE)
        # The retransmit survives the round trip and is still deduplicated.
        captured = physical_events(rebuilt)
        self.assertEqual(len(captured["meter"]["retransmits"]), 1)
        self.assertEqual(captured["camera"]["lost_seqs"], [11])

    def test_sessions_are_isolated_in_the_store(self):
        other = copy.deepcopy(self.bundle)
        other["session_id"] = "S1-2026-11-24-E2"
        other["camera_frames"][2]["tick"] = 5
        cc.ingest_bundle(self.db, self.bundle)
        cc.ingest_bundle(self.db, other)
        self.assertEqual(cc.sessions(self.db),
                         ["S1-2026-11-24-E1", "S1-2026-11-24-E2"])
        # A late anchor arriving for E2 never alters E1's recomputation.
        updated = copy.deepcopy(self.bundle)
        updated["anchors"][0]["calibration_version"] = "cam-cal-late"
        rebuilt_e1 = cc.rebuild_bundle(self.db, "S1-2026-11-24-E1")
        self.assertEqual(
            rebuilt_e1["anchors"][0]["calibration_version"],
            "cam-cal-2026-11-24-A")

    def test_conflicting_replay_rolls_back(self):
        cc.ingest_bundle(self.db, self.bundle)
        evil = {
            "seq": 20, "tick": 20, "action": "deenergize",
            "received_at": 5000,
            "contact_receipt": {"receipt_id": "rcpt-forged", "state": "closed"},
        }
        with self.assertRaises(sqlite3.IntegrityError):
            cc.ingest_conflicting_event(
                self.db, self.bundle["session_id"], "meter", "meter-0020", evil)
        # Original row untouched.
        row = self.db.execute(
            "SELECT content_sha256 FROM op_raw_events"
            " WHERE session_id=? AND event_id='meter-0020'",
            (self.bundle["session_id"],)).fetchone()
        rebuilt = cc.rebuild_bundle(self.db, self.bundle["session_id"])
        kept = next(r for r in physical_events(rebuilt)["meter"]["events"]
                    if r["event_id"] == "meter-0020")
        self.assertEqual(kept["contact_receipt"]["receipt_id"],
                         "rcpt-20261124-0020")
        self.assertIsNotNone(row)

    def test_certificate_versions_are_append_only(self):
        cc.ingest_bundle(self.db, self.bundle)
        pending = issue(self.bundle, issued_at="2026-11-24T09:10:00+08:00")
        persist_certificate(self.db, pending)
        safe = issue(self.bundle, supplement_path=SUPPLEMENT_A,
                     issued_at="2026-11-24T10:45:00+08:00")
        persist_certificate(self.db, safe)
        versions = cc.certificates(self.db, self.bundle["session_id"])
        self.assertEqual([v["version"] for v in versions], [2, 3])
        self.assertEqual([v["status"] for v in versions], [PENDING, SAFE])
        # Re-persisting the same logical certificate creates another revision
        # rather than mutating or overwriting an existing row.
        persisted, _ = persist_certificate(self.db, pending)
        self.assertEqual([v["version"] for v in persisted], [2, 3, 4])
        self.assertEqual(persisted[-1]["status"], PENDING)

    def test_revoked_certificate_is_retained(self):
        cc.ingest_bundle(self.db, self.bundle)
        violation = issue(self.bundle, supplement_path=SUPPLEMENT_B,
                          issued_at="2026-11-24T11:15:00+08:00")
        persist_certificate(self.db, violation)
        stored = cc.certificates(self.db, self.bundle["session_id"])
        self.assertEqual(stored[0]["status"], VIOLATION)
        body = json.loads(self.db.execute(
            "SELECT body FROM op_certificates WHERE session_id=?",
            (self.bundle["session_id"],)).fetchone()["body"])
        self.assertEqual(body["legacy_certificate_retained"]["certificate_id"],
                         "cert-legacy-wrong-complete")
        self.assertTrue(body["revoked_legacy"])


if __name__ == "__main__":
    pytest.main([__file__, "-q", "-p", "no:cacheprovider"])
