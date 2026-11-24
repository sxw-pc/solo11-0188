"""Offline capture for the dual-clock electrical operation trace.

The monitoring camera and the de-energizing meter record on independent
clocks.  This module parses the raw offline evidence (video frames, meter
records, de-energizing contact receipts, clock anchors and supplementary
calibrations), removes physical duplicates (meter cache retransmits and
frame re-delivery), reports lost frames, and builds one *session-shared*
clock model per device.

A device has a single unknown offset and a single unknown drift rate for
the whole session.  The same pair of variables must explain every event of
that device: favourable offsets may not be picked independently per action.
All math is deterministic interval arithmetic in reference milliseconds so
that the action timeline can be recomputed byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA_NAME = "skillstation.operation_clocks"
SCHEMA_VERSION = 2
CAMERA = "camera"
METER = "meter"
DEVICES = (CAMERA, METER)
DEENERGIZE = "deenergize"
OPEN_PANEL = "open_panel"
REQUIRED_ACTIONS = (DEENERGIZE, OPEN_PANEL)

SQL = """
CREATE TABLE IF NOT EXISTS op_sources (
    sha TEXT PRIMARY KEY, session_id TEXT NOT NULL, received_at REAL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS op_clock_config (
    session_id TEXT NOT NULL, device TEXT NOT NULL, tick_ms REAL NOT NULL,
    offset_max_ticks REAL NOT NULL, drift_max_ppm REAL NOT NULL,
    PRIMARY KEY(session_id, device));
CREATE TABLE IF NOT EXISTS op_anchors (
    session_id TEXT NOT NULL, device TEXT NOT NULL, calibration_version TEXT NOT NULL,
    tick INTEGER NOT NULL, ref_ms REAL NOT NULL, acquired_at REAL NOT NULL,
    evidence_sha256 TEXT NOT NULL, source_sha TEXT NOT NULL,
    PRIMARY KEY(session_id, device, calibration_version));
CREATE TABLE IF NOT EXISTS op_raw_events (
    session_id TEXT NOT NULL, device TEXT NOT NULL, event_id TEXT NOT NULL,
    seq INTEGER NOT NULL, tick INTEGER NOT NULL, action TEXT, received_at REAL NOT NULL,
    content_sha256 TEXT NOT NULL, body TEXT NOT NULL, source_sha TEXT NOT NULL,
    PRIMARY KEY(session_id, device, event_id));
CREATE UNIQUE INDEX IF NOT EXISTS op_event_content ON op_raw_events(session_id, device, event_id, content_sha256);
CREATE TABLE IF NOT EXISTS op_retransmits (
    session_id TEXT NOT NULL, device TEXT NOT NULL, event_id TEXT NOT NULL,
    received_at REAL NOT NULL, content_sha256 TEXT NOT NULL, source_sha TEXT NOT NULL,
    PRIMARY KEY(session_id, device, event_id, received_at));
CREATE TABLE IF NOT EXISTS op_gaps (
    session_id TEXT NOT NULL, device TEXT NOT NULL, lost_seq INTEGER NOT NULL,
    PRIMARY KEY(session_id, device, lost_seq));
CREATE TABLE IF NOT EXISTS op_supplements (
    session_id TEXT NOT NULL, path TEXT NOT NULL, scope TEXT NOT NULL,
    acquired_at REAL NOT NULL, body TEXT NOT NULL, source_sha TEXT NOT NULL,
    PRIMARY KEY(session_id, path));
CREATE TABLE IF NOT EXISTS op_certificates (
    session_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
    basis TEXT NOT NULL, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
    supersedes TEXT, issued_at REAL NOT NULL,
    PRIMARY KEY(session_id, version));
"""


# --------------------------------------------------------------------------- #
# deterministic serialization / hashing
# --------------------------------------------------------------------------- #
def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# bundle parsing
# --------------------------------------------------------------------------- #
def load_bundle(source) -> dict:
    """Accept a fixture path, a parsed v2 bundle, or a legacy raw record list."""
    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
        payload = json.loads(raw)
    else:
        payload = source
    if isinstance(payload, list):
        # Legacy shape: bare rows carrying only action/device/tick/received_at.
        return {
            "schema": f"{SCHEMA_NAME}/1",
            "session_id": "legacy",
            "tick_ms": {},
            "constraints": [],
            "anchors": [],
            "events": [_legacy_row(row) for row in payload],
            "supplements": {},
            "legacy_certificate": None,
        }
    if not isinstance(payload, dict):
        raise ValueError("operation clock bundle must be an object or a record list")
    raw_constraints = payload.get("clock_constraints", payload.get("constraints", ()))
    bundle = {
        "schema": payload.get("schema", f"{SCHEMA_NAME}/{SCHEMA_VERSION}"),
        "session_id": payload["session_id"],
        "tick_ms": dict(payload["tick_ms"]),
        "constraints": [dict(c) for c in raw_constraints],
        "anchors": [dict(a) for a in payload.get("anchors", ())],
        "events": _parse_events(payload),
        "supplements": {k: dict(v) for k, v in payload.get("supplements", {}).items()},
        "legacy_certificate": payload.get("legacy_certificate"),
    }
    _validate(bundle)
    return bundle


def _legacy_row(row):
    out = dict(row)
    out.setdefault("event_id", row.get("id") or f"{row['device']}-{row['tick']}")
    out.setdefault("seq", int(row["tick"]))
    out.setdefault("received_at", row.get("received_at", 0))
    out["retransmit"] = bool(row.get("retransmit", False))
    return out


def _parse_events(payload):
    # Bundles rebuilt from the evidence store carry a generic "events" list;
    # fresh fixtures split them into camera frames and meter records.
    if payload.get("events"):
        events = []
        for row in payload["events"]:
            out = dict(row)
            out.setdefault("retransmit", bool(row.get("retransmit", False)))
            events.append(out)
        return events
    events = []
    for frame in payload.get("camera_frames", ()):
        row = dict(frame)
        row["device"] = CAMERA
        row.setdefault("event_id", row.pop("frame_id", None) or f"cam-{frame['seq']:04d}")
        row["retransmit"] = bool(frame.get("retransmit", False))
        events.append(row)
    for record in payload.get("meter_records", ()):
        row = dict(record)
        row["device"] = METER
        row.setdefault("event_id", row.pop("record_id", None) or f"meter-{record['seq']:04d}")
        row["retransmit"] = bool(record.get("retransmit", False))
        events.append(row)
    return events


def _validate(bundle):
    seen = {}
    for row in bundle["events"]:
        for key in ("device", "event_id", "seq", "tick", "received_at"):
            if key not in row:
                raise ValueError(f"raw event missing {key}: {row}")
        if row["device"] not in DEVICES:
            raise ValueError(f"unknown device {row['device']!r}")
        key = (row["device"], row["event_id"])
        seen.setdefault(key, []).append(row)
    # Repeated physical event ids are only legitimate as cache retransmits;
    # arrival order is not trusted, so the marker may sit on either copy.
    for (device, event_id), rows in seen.items():
        if len(rows) > 1 and not any(r.get("retransmit") for r in rows):
            raise ValueError(
                f"duplicate event id without retransmit marker: {(device, event_id)}")
    anchor_devices = {a["device"] for a in bundle["anchors"]}
    for device in {r["device"] for r in bundle["events"]}:
        if device in bundle["tick_ms"] and device not in anchor_devices:
            raise ValueError(f"device {device} has a tick unit but no clock anchor")
    for path, sup in bundle["supplements"].items():
        if sup.get("scope") not in ("shared-pulse", "independent-time"):
            raise ValueError(f"supplement {path} has an unknown scope")


# --------------------------------------------------------------------------- #
# physical deduplication and frame gaps
# --------------------------------------------------------------------------- #
def dedupe_device(events, device):
    """Collapse retransmits of one physical event; flag content conflicts.

    Retransmitted meter cache rows and re-delivered frames share the physical
    event id.  Identical content is collapsed: the non-retransmit observation
    is the canonical row regardless of which copy arrived first, so network
    arrival order can never influence the timeline.  The same id carrying
    different bytes is evidence tampering and raises ``ValueError``.
    """
    rows = [r for r in events if r["device"] == device]
    groups = {}
    for row in rows:
        groups.setdefault(row["event_id"], []).append(row)
    unique, duplicates = [], []
    for event_id, group in groups.items():
        contents = {digest(_dedup_body(r)) for r in group}
        if len(contents) != 1:
            raise ValueError(
                f"physical event {device}/{event_id} replayed with different content")
        originals = [r for r in group if not r.get("retransmit")]
        canonical = (originals or group)[0]
        unique.append(canonical)
        for r in group:
            if r is not canonical:
                duplicates.append({"event_id": event_id, "received_at": r["received_at"]})
    # Ordering follows the device's own tick/seq, never network arrival time.
    unique.sort(key=lambda r: (r["tick"], r["seq"], r["received_at"]))
    duplicates.sort(key=lambda d: (d["event_id"], d["received_at"]))
    return unique, duplicates


def _dedup_body(row):
    return {k: v for k, v in row.items() if k not in ("received_at", "retransmit")}


def lost_sequences(unique_rows):
    """Report frame/meter sequence numbers that never arrived."""
    seqs = sorted(r["seq"] for r in unique_rows)
    if not seqs:
        return []
    present = set(seqs)
    return [s for s in range(seqs[0], seqs[-1] + 1) if s not in present]


def dedupe(bundle):
    """Return per-device physical events, collapsed retransmits and gaps."""
    result = {}
    for device in sorted({r["device"] for r in bundle["events"]}):
        unique, duplicates = dedupe_device(bundle["events"], device)
        result[device] = {
            "events": unique,
            "retransmits": duplicates,
            "lost_seqs": lost_sequences(unique),
        }
    return result


# --------------------------------------------------------------------------- #
# session-shared clock models and interval arithmetic
# --------------------------------------------------------------------------- #
def build_models(bundle):
    """One clock model per device: a single offset + drift for the session."""
    constraints = {c["device"]: c for c in bundle["constraints"]}
    anchors = {}
    for anchor in bundle["anchors"]:
        # A late calibration replaces an old version, but every version is
        # retained in the evidence store; only one anchor is active per device.
        anchors[anchor["device"]] = anchor
    models = {}
    for device in sorted({r["device"] for r in bundle["events"]}):
        constraint = constraints.get(device)
        anchor = anchors.get(device)
        tick_ms = bundle["tick_ms"].get(device)
        if constraint is None or anchor is None or tick_ms is None:
            # Without a clock model the device is wholly uncalibrated: its
            # events span an infinite reference-time interval.
            models[device] = {
                "device": device, "tick_ms": tick_ms,
                "offset_max_ms": float("inf"), "drift_max": float("inf"),
                "anchor": anchor, "calibration_version": None,
            }
            continue
        offset_max_ms = float(constraint["offset_max_ticks"]) * float(tick_ms)
        models[device] = {
            "device": device,
            "tick_ms": float(tick_ms),
            "offset_max_ms": offset_max_ms,
            "drift_max": float(constraint["drift_max_ppm"]) * 1e-6,
            "anchor": anchor,
            "calibration_version": anchor["calibration_version"],
        }
    return models


def nominal_ms(model, tick):
    """Reference time without the unknown offset/drift (unit conversion)."""
    anchor = model["anchor"]
    if anchor is None:
        # Uncalibrated legacy device: the local tick is the only scale we have.
        return float(tick) * (model["tick_ms"] or 1.0)
    return float(anchor["ref_ms"]) + (float(tick) - float(anchor["tick"])) * model["tick_ms"]


def event_window(model, tick):
    """Feasible reference-time interval for one device tick.

    t_ref = nominal + offset + rate * local_elapsed_ms, with
    |offset| <= offset_max_ms and |rate| <= drift_max.  Offset and rate are
    shared by every event of the device; the window only widens by the drift
    contribution accumulated at this tick.  An uncalibrated device spans the
    whole reference-time line.
    """
    nominal = nominal_ms(model, tick)
    anchor = model["anchor"]
    if anchor is None or model["offset_max_ms"] == float("inf"):
        return nominal, float("-inf"), float("inf")
    elapsed_ms = abs(float(tick) - float(anchor["tick"])) * model["tick_ms"]
    span = model["offset_max_ms"] + model["drift_max"] * elapsed_ms
    return nominal, nominal - span, nominal + span


def _jsonable(value):
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def timeline(bundle):
    """Recomputable action timeline with per-event feasible intervals."""
    models = build_models(bundle)
    captured = dedupe(bundle)
    rows = []
    for device in DEVICES:
        if device not in captured:
            continue
        for row in captured[device]["events"]:
            nominal, lo, hi = event_window(models[device], row["tick"])
            rows.append({
                "action": row.get("action"),
                "device": device,
                "event_id": row["event_id"],
                "seq": row["seq"],
                "tick": row["tick"],
                "tick_ms": models[device]["tick_ms"],
                "nominal_ms": nominal,
                "window_ms": [lo, hi],
                "received_at": row["received_at"],
                "contact_receipt": row.get("contact_receipt"),
                "content_sha256": digest(_dedup_body(row)),
            })
    rows.sort(key=lambda r: (r["device"], r["tick"], r["seq"]))
    return {
        "schema": f"{SCHEMA_NAME}/{SCHEMA_VERSION}",
        "session_id": bundle["session_id"],
        "events": [_jsonable(r) for r in rows],
        "dedupe": _jsonable(captured),
        "models": _jsonable(_model_summary(models)),
        "anchors": _jsonable(sorted(bundle["anchors"], key=lambda a: (a["device"], a["calibration_version"]))),
    }


def _model_summary(models):
    out = {}
    for device, m in models.items():
        out[device] = {
            "tick_ms": m["tick_ms"],
            "offset_max_ms": m["offset_max_ms"],
            "drift_max_ppm": None if m["drift_max"] == float("inf") else m["drift_max"] * 1e6,
            "calibration_version": m["calibration_version"],
            "shared_variables": ["offset", "drift_rate"],
        }
    return out


def action_events(bundle):
    """Physical (deduplicated) events that carry an observed action."""
    out = []
    for device, captured in dedupe(bundle).items():
        for row in captured["events"]:
            if row.get("action"):
                out.append(row)
    return out


def action_names(bundle):
    return {row["action"] for row in action_events(bundle)}


# --------------------------------------------------------------------------- #
# order solving over the shared offset variables
# --------------------------------------------------------------------------- #
def _pair(models, bundle):
    events = {r["device"]: r for r in action_events(bundle)}
    if CAMERA not in events or METER not in events:
        return None
    cam_row, meter_row = events[CAMERA], events[METER]
    cam, meter = models[CAMERA], models[METER]
    cam_nom, _, _ = event_window(cam, cam_row["tick"])
    meter_nom, _, _ = event_window(meter, meter_row["tick"])
    if cam["anchor"] is None or meter["anchor"] is None:
        cam_drift = meter_drift = float("inf")
    else:
        cam_drift = cam["drift_max"] * abs(cam_row["tick"] - cam["anchor"]["tick"]) * cam["tick_ms"]
        meter_drift = meter["drift_max"] * abs(meter_row["tick"] - meter["anchor"]["tick"]) * meter["tick_ms"]
    return {
        "camera": cam_row, "meter": meter_row,
        "camera_nominal": cam_nom, "meter_nominal": meter_nom,
        "delta_offset_bound": cam["offset_max_ms"] + meter["offset_max_ms"],
        "drift_span": cam_drift + meter_drift,
        "models": (cam, meter),
        "uncalibrated": cam["anchor"] is None or meter["anchor"] is None,
    }


def _relation_from_range(lo, hi):
    if hi < 0:
        return "deenergize_first"
    if lo > 0:
        return "open_first"
    return "ambiguous"


def _witnesses(pair, delta_lo, delta_hi, boundary_ms):
    """Concrete feasible (offset_camera, offset_meter) pairs per ordering.

    For a target relative offset delta = offset_meter - offset_camera, pick
    offset_camera at the clamp of the two per-device bounds, then derive the
    meter offset.  Zero drift is assumed; the drift span is already accounted
    for in the feasible interval, so the witness remains valid.  Ordering
    follows ``delta < boundary_ms`` (de-energize first), not delta < 0.
    """
    cam, meter = pair["models"]
    bound_c, bound_m = cam["offset_max_ms"], meter["offset_max_ms"]

    def pick(delta):
        # delta = offset_meter - offset_camera, so offset_meter =
        # offset_camera + delta must stay in [-bound_m, bound_m].  Hence
        # offset_camera is in [-delta-bound_m, -delta+bound_m], intersected
        # with the camera's own bound.
        lo = max(-bound_c, -delta - bound_m)
        hi = min(bound_c, -delta + bound_m)
        if lo > hi:
            return None
        oc = (lo + hi) / 2
        om = oc + delta
        open_ms = pair["camera_nominal"] + oc
        deenergize_ms = pair["meter_nominal"] + om
        return {
            "offset_camera_ms": oc,
            "offset_meter_ms": om,
            "drift_rate": 0.0,
            "open_panel_ms": open_ms,
            "deenergize_ms": deenergize_ms,
            "difference_ms": deenergize_ms - open_ms,
        }

    found = {}
    if delta_lo <= boundary_ms <= delta_hi:
        found["boundary"] = pick(boundary_ms)
    if delta_hi < boundary_ms:
        found["deenergize_first"] = pick((delta_lo + delta_hi) / 2)
    elif delta_lo > boundary_ms:
        found["open_first"] = pick((delta_lo + delta_hi) / 2)
    else:
        # Ambiguous interval straddling the boundary: demonstrate both
        # physical timelines with interior offsets on each side.
        margin = min(bound_c, bound_m) / 2
        found["deenergize_first"] = pick(max(delta_lo, boundary_ms - margin))
        found["open_first"] = pick(min(delta_hi, boundary_ms + margin))
    return {k: v for k, v in found.items() if v is not None}


def solve_order(bundle, supplement_path=None):
    """Interval analysis of de-energize vs. panel-open ordering.

    The decision variable is the single session-shared relative offset
    ``delta = offset_meter - offset_camera``.  Both physical timelines are
    feasible while the feasible delta interval straddles the order boundary.
    """
    models = build_models(bundle)
    pair = _pair(models, bundle)
    present = action_names(bundle)
    missing = [a for a in REQUIRED_ACTIONS if a not in present]
    if missing:
        return {"status": "incomplete", "missing_actions": missing}
    if pair is None:
        return {"status": "incomplete", "missing_actions": list(REQUIRED_ACTIONS)}

    if pair["uncalibrated"]:
        # No anchors / tick units: nothing pins either clock, so neither
        # ordering can be ruled out and there is no numeric boundary.
        analysis = {
            "status": "analyzed",
            "session_id": bundle["session_id"],
            "relation": "ambiguous",
            "difference_ms": {
                "expression": "t(deenergize) - t(open_panel)",
                "feasible_ms": [None, None],
            },
            "relative_offset": {
                "variable": "offset_meter - offset_camera",
                "feasible_ms": [None, None],
                "boundary_ms": None,
                "deenergize_first_when": "delta < boundary_ms",
                "boundary_in_camera_ticks": None,
                "boundary_in_meter_ticks": None,
            },
            "witnesses": {},
            "supplement": None,
            "reason": "one or both devices have no clock anchor",
        }
        if supplement_path is not None:
            raise ValueError("supplements require anchored device clocks")
        return analysis

    bound = pair["delta_offset_bound"]
    drift_span = pair["drift_span"]
    nominal_gap = pair["meter_nominal"] - pair["camera_nominal"]
    # D - C = nominal_gap + delta + drift, delta in [-bound, bound]
    delta_lo, delta_hi = -bound, bound
    diff_lo = nominal_gap + delta_lo - drift_span
    diff_hi = nominal_gap + delta_hi + drift_span
    relation = _relation_from_range(diff_lo, diff_hi)
    boundary_delta = -nominal_gap  # delta at which deenergize == open_panel

    analysis = {
        "status": "analyzed",
        "session_id": bundle["session_id"],
        "relation": relation,
        "difference_ms": {
            "expression": "t(deenergize) - t(open_panel)",
            "feasible_ms": [diff_lo, diff_hi],
        },
        "relative_offset": {
            "variable": "offset_meter - offset_camera",
            "feasible_ms": [delta_lo, delta_hi],
            "boundary_ms": boundary_delta,
            "deenergize_first_when": "delta < boundary_ms",
            "boundary_in_camera_ticks": _safe_div(boundary_delta, models[CAMERA]["tick_ms"]),
            "boundary_in_meter_ticks": _safe_div(boundary_delta, models[METER]["tick_ms"]),
        },
        "witnesses": _witnesses(pair, delta_lo, delta_hi, boundary_delta),
        "supplement": None,
    }

    if supplement_path is not None:
        supplement = bundle["supplements"].get(supplement_path)
        if supplement is None:
            raise ValueError(f"supplement {supplement_path!r} not captured for session {bundle['session_id']}")
        if supplement["scope"] == "shared-pulse":
            analysis = _apply_shared_pulse(bundle, pair, analysis, supplement, supplement_path)
        else:
            analysis = _apply_independent_time(pair, analysis, supplement, supplement_path)
    return _jsonable(analysis)


def _safe_div(a, b):
    if b in (None, 0) or b == float("inf"):
        return None
    return a / b


def _apply_shared_pulse(bundle, pair, analysis, supplement, path):
    """Supplement A: shared electrical pulses pin the relative offset.

    Every shared pulse is one physical event observed on both clocks.  The
    constraint it imposes on the same offset/drift variables must hold
    globally; the intersection of all constraints decides the order.
    """
    cam, meter = pair["models"]
    delta_lo, delta_hi = analysis["relative_offset"]["feasible_ms"]
    for pulse in supplement["pulses"]:
        cam_local = (pulse["camera_tick"] - cam["anchor"]["tick"]) * cam["tick_ms"]
        meter_local = (pulse["meter_tick"] - meter["anchor"]["tick"]) * meter["tick_ms"]
        # One physical pulse: A_c + local_c + o_c = A_m + local_m + o_m
        # => delta = o_m - o_c = (A_c - A_m) + local_c - local_m.
        base = (cam["anchor"]["ref_ms"] - meter["anchor"]["ref_ms"]) + cam_local - meter_local
        drift = cam["drift_max"] * abs(cam_local) + meter["drift_max"] * abs(meter_local)
        tol = float(pulse.get("tolerance_ms", 0.0))
        lo = base - tol - drift
        hi = base + tol + drift
        delta_lo = max(delta_lo, lo)
        delta_hi = min(delta_hi, hi)
    if delta_lo > delta_hi:
        raise ValueError("shared pulse evidence is inconsistent with the per-device clock constraints")
    nominal_gap = pair["meter_nominal"] - pair["camera_nominal"]
    drift_span = pair["drift_span"]
    diff_lo = nominal_gap + delta_lo - drift_span
    diff_hi = nominal_gap + delta_hi + drift_span
    relation = _relation_from_range(diff_lo, diff_hi)
    analysis["relation"] = relation
    analysis["relative_offset"]["feasible_ms"] = [delta_lo, delta_hi]
    analysis["difference_ms"]["feasible_ms"] = [diff_lo, diff_hi]
    analysis["witnesses"] = _witnesses(
        pair, delta_lo, delta_hi, analysis["relative_offset"]["boundary_ms"])
    analysis["supplement"] = {
        "path": path, "scope": "shared-pulse",
        "acquired_at": supplement["acquired_at"],
        "calibration_versions": sorted(supplement["calibration_versions"]),
        "pulse_ids": [p["pulse_id"] for p in supplement["pulses"]],
        "contact_trajectory": supplement.get("contact_trajectory"),
        "safety_margin_ms": max(0.0, -diff_hi) if relation == "deenergize_first" else 0.0,
    }
    return analysis


def _apply_independent_time(pair, analysis, supplement, path):
    """Supplement B: independent time sources give disjoint absolute windows.

    These anchors have their own scope and never touch the device offset
    variables: one path's anchors must not calibrate the other path.
    """
    windows = {item["action"]: item["window_ms"] for item in supplement["time_windows"]}
    if set(windows) != set(REQUIRED_ACTIONS):
        raise ValueError("independent-time supplement must window both actions")
    open_lo, open_hi = windows[OPEN_PANEL]
    off_lo, off_hi = windows[DEENERGIZE]
    if open_hi < off_lo:
        relation = "open_first"
    elif off_hi < open_lo:
        relation = "deenergize_first"
    else:
        relation = "ambiguous"
    analysis["relation"] = relation
    analysis["supplement"] = {
        "path": path, "scope": "independent-time",
        "acquired_at": supplement["acquired_at"],
        "calibration_versions": sorted(supplement["calibration_versions"]),
        "windows": windows,
        "device_offsets_untouched": True,
        "violation": relation == "open_first",
    }
    return analysis


# --------------------------------------------------------------------------- #
# transactional SQLite evidence store
# --------------------------------------------------------------------------- #
def connect(path):
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(SQL)
    return db


def ingest_bundle(db, source, received_at=None):
    """Persist raw evidence in a single transaction; replay is idempotent."""
    bundle = load_bundle(source)
    raw = canonical(bundle).encode("utf-8") if isinstance(source, (dict, list)) else Path(source).read_bytes()
    sha = digest_bytes(raw)
    if db.execute("SELECT 1 FROM op_sources WHERE sha=?", (sha,)).fetchone():
        db.commit()
        return {"inserted": 0, "replayed": 1, "sha": sha}
    db.execute("SAVEPOINT op_ingest")
    try:
        db.execute("INSERT INTO op_sources VALUES(?,?,?,?)", (sha, bundle["session_id"], received_at, raw))
        for c in bundle["constraints"]:
            db.execute(
                "INSERT OR REPLACE INTO op_clock_config VALUES(?,?,?,?,?)",
                (bundle["session_id"], c["device"], bundle["tick_ms"][c["device"]],
                 c["offset_max_ticks"], c["drift_max_ppm"]),
            )
        for anchor in bundle["anchors"]:
            db.execute(
                "INSERT OR IGNORE INTO op_anchors VALUES(?,?,?,?,?,?,?,?)",
                (bundle["session_id"], anchor["device"], anchor["calibration_version"],
                 anchor["tick"], anchor["ref_ms"], anchor["acquired_at"],
                 digest(anchor), sha),
            )
        for row in bundle["events"]:
            if row.get("retransmit"):
                db.execute(
                    "INSERT INTO op_retransmits VALUES(?,?,?,?,?,?)",
                    (bundle["session_id"], row["device"], row["event_id"],
                     row["received_at"], digest(_dedup_body(row)), sha))
                continue
            db.execute(
                "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (bundle["session_id"], row["device"], row["event_id"], row["seq"],
                 row["tick"], row.get("action"), row["received_at"],
                 digest(_dedup_body(row)), canonical(row), sha),
            )
        for device, captured in dedupe(bundle).items():
            for lost in captured["lost_seqs"]:
                db.execute(
                    "INSERT OR IGNORE INTO op_gaps VALUES(?,?,?)",
                    (bundle["session_id"], device, lost))
        for path, sup in bundle.get("supplements", {}).items():
            db.execute(
                "INSERT OR REPLACE INTO op_supplements VALUES(?,?,?,?,?,?)",
                (bundle["session_id"], path, sup["scope"], sup["acquired_at"],
                 canonical(sup), sha),
            )
        db.execute("RELEASE SAVEPOINT op_ingest")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT op_ingest")
        db.execute("RELEASE SAVEPOINT op_ingest")
        db.rollback()
        raise
    return {"inserted": 1, "replayed": 0, "sha": sha}


def ingest_conflicting_event(db, session_id, device, event_id, body):
    """A re-delivery of a known physical event id with different bytes fails."""
    db.execute("SAVEPOINT op_conflict")
    try:
        db.execute(
            "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_id, device, event_id, body["seq"], body["tick"], body.get("action"),
             body["received_at"], digest(_dedup_body(body)), canonical(body), "conflict"),
        )
        db.execute("RELEASE SAVEPOINT op_conflict")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT op_conflict")
        db.execute("RELEASE SAVEPOINT op_conflict")
        db.rollback()
        raise


def rebuild_bundle(db, session_id):
    """Reconstruct the full in-memory bundle from persisted raw evidence."""
    configs = db.execute(
        "SELECT device,tick_ms,offset_max_ticks,drift_max_ppm FROM op_clock_config WHERE session_id=?",
        (session_id,)).fetchall()
    anchors = [dict(r) for r in db.execute(
        "SELECT device,calibration_version,tick,ref_ms,acquired_at,evidence_sha256 FROM op_anchors"
        " WHERE session_id=? ORDER BY device,calibration_version", (session_id,)).fetchall()]
    events = [json.loads(r["body"]) for r in db.execute(
        "SELECT body FROM op_raw_events WHERE session_id=? ORDER BY device,tick,seq",
        (session_id,)).fetchall()]
    for r in db.execute(
            "SELECT device,event_id,received_at FROM op_retransmits"
            " WHERE session_id=? ORDER BY device,event_id,received_at", (session_id,)):
        original = next(e for e in events
                        if e["device"] == r["device"] and e["event_id"] == r["event_id"])
        replay = dict(original)
        replay["received_at"] = r["received_at"]
        replay["retransmit"] = True
        events.append(replay)
    supplements = {}
    for r in db.execute("SELECT path,body FROM op_supplements WHERE session_id=?", (session_id,)):
        supplements[r["path"]] = json.loads(r["body"])
    legacy = db.execute(
        "SELECT body FROM op_certificates WHERE session_id=? ORDER BY version LIMIT 1",
        (session_id,)).fetchone()
    raw_bundle = {
        "schema": f"{SCHEMA_NAME}/{SCHEMA_VERSION}",
        "session_id": session_id,
        "tick_ms": {r["device"]: r["tick_ms"] for r in configs},
        "clock_constraints": [
            {"device": r["device"], "offset_max_ticks": r["offset_max_ticks"],
             "drift_max_ppm": r["drift_max_ppm"]} for r in configs],
        "events": events,
        "anchors": [{
            "device": a["device"], "calibration_version": a["calibration_version"],
            "tick": a["tick"], "ref_ms": a["ref_ms"], "acquired_at": a["acquired_at"],
            "evidence_sha256": a["evidence_sha256"]} for a in anchors],
        "supplements": supplements,
        "legacy_certificate": json.loads(legacy["body"]) if legacy else None,
    }
    return load_bundle(raw_bundle)


def sessions(db):
    return [r[0] for r in db.execute("SELECT DISTINCT session_id FROM op_sources ORDER BY session_id")]


def certificates(db, session_id):
    return [dict(r) for r in db.execute(
        "SELECT session_id,version,status,basis,body_sha256,supersedes,issued_at"
        " FROM op_certificates WHERE session_id=? ORDER BY version", (session_id,))]


def stored_gaps(db, session_id):
    return {device: [r[0] for r in db.execute(
        "SELECT lost_seq FROM op_gaps WHERE session_id=? AND device=? ORDER BY lost_seq",
        (session_id, device))] for device in DEVICES}
