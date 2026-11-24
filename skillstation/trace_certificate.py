"""Safety-order certificates for the dual-clock operation trace.

Presence of both action names is never enough.  A safe-order certificate is
issued only when *every* feasible clock alignment places de-energization
before panel opening.  If both physical timelines survive the per-device
offset bounds, the trace is explicitly pending review ("待核") and the
conflict boundary is saved.  Supplementary evidence can only resolve the
trace through its own scope:

* path A ("shared-pulse") constrains the shared offset variables and may
  issue the safe-order certificate when the order is fixed;
* path B ("independent-time") proves the panel was opened first and revokes
  the earlier certificate as a safety-step violation.
"""
from __future__ import annotations

from . import clock_capture as cc

SAFE = "安全顺序"            # certificate issued: de-energize precedes open, all alignments
PENDING = "待核"             # both orderings feasible: conflict boundary retained
VIOLATION = "安全步骤违规"    # supplement B proves open happened first; old cert revoked
INCOMPLETE = "缺步骤"         # an action was never observed

CERT_SCHEMA = "skillstation.trace_certificate/2"


def issue(records, *, supplement_path=None, issued_at=None):
    """Issue (or revise) an order certificate for one captured session."""
    bundle = cc.load_bundle(records)
    present = cc.action_names(bundle)
    missing = [a for a in cc.REQUIRED_ACTIONS if a not in present]
    if missing:
        return _certificate(bundle, INCOMPLETE, None, supplement_path, issued_at, missing=missing)

    analysis = cc.solve_order(bundle, supplement_path=supplement_path)
    relation = analysis["relation"]
    sup = analysis.get("supplement")

    if relation == "deenergize_first":
        status = SAFE
        if sup and sup["scope"] == "shared-pulse":
            basis = "shared-pulse"
        elif sup and sup["scope"] == "independent-time":
            basis = "independent-time"
        else:
            basis = "clock-bounds"
    elif relation == "open_first":
        # Only independent absolute time sources (or, hypothetically, hard
        # clock bounds) can prove the panel was opened while still energized.
        status = VIOLATION
        basis = sup["scope"] if sup else "clock-bounds"
    else:
        status = PENDING
        basis = "clock-bounds"

    return _certificate(bundle, status, basis, supplement_path, issued_at, analysis=analysis)


def _certificate(bundle, status, basis, supplement_path, issued_at, *, missing=None, analysis=None):
    timeline = cc.timeline(bundle)
    legacy = bundle.get("legacy_certificate")
    cert = {
        "schema": CERT_SCHEMA,
        "session_id": bundle["session_id"],
        "version": (legacy["version"] + 1) if legacy else 2,
        "status": status,
        "basis": basis,
        "scope": "operation-order",
        "actions_observed": sorted(cc.action_names(bundle)),
        "missing_actions": missing or [],
        "timeline": timeline,
        "order_analysis": analysis,
        "supplement_path": supplement_path,
        "supplement_evidence": analysis.get("supplement") if analysis else None,
        "undecidable_interval_ms": (
            analysis["difference_ms"]["feasible_ms"]
            if analysis and analysis["relation"] == "ambiguous" else None),
        "conflict_boundary": (
            analysis["relative_offset"] if analysis and analysis["relation"] == "ambiguous" else None),
        "evidence_acquired_at": _acquisition_times(bundle),
        "calibration_versions": sorted(a["calibration_version"] for a in bundle["anchors"]),
        "legacy_certificate_retained": legacy,
        "supersedes": legacy["certificate_id"] if legacy else None,
        "revoked_legacy": bool(legacy and status == VIOLATION),
        "issued_at": issued_at,
        "qualification_changed": False,
        "station_changed": False,
        "score_changed": False,
    }
    cert["certificate_id"] = cc.digest({k: v for k, v in cert.items() if k != "certificate_id"})
    return cert


def _acquisition_times(bundle):
    times = {}
    for anchor in bundle["anchors"]:
        times[f"anchor:{anchor['device']}:{anchor['calibration_version']}"] = anchor["acquired_at"]
    for path, sup in bundle.get("supplements", {}).items():
        times[f"supplement:{path}"] = sup["acquired_at"]
    for device, captured in cc.dedupe(bundle).items():
        times[f"device:{device}:last_received"] = max(
            (r["received_at"] for r in captured["events"]), default=None)
    return dict(sorted((k, v) for k, v in times.items() if v is not None))


def persist_certificate(db, certificate):
    """Append a certificate version; every old version remains queryable.

    The version number is assigned by the append-only store, never by the
    caller: recomputation after a late anchor always takes the next revision.
    A retained legacy (v1, set-membership) certificate is never written, so
    the first recomputed certificate takes version 2.
    """
    session_id = certificate["session_id"]
    prior = db.execute(
        "SELECT COALESCE(MAX(version),0) FROM op_certificates WHERE session_id=?",
        (session_id,)).fetchone()[0]
    base = max(prior, certificate["version"] - 1)
    stored = dict(certificate)
    stored["version"] = base + 1
    stored["certificate_id"] = cc.digest(
        {k: v for k, v in stored.items() if k != "certificate_id"})
    db.execute("INSERT INTO op_certificates VALUES(?,?,?,?,?,?,?,?)", (
        session_id, stored["version"], stored["status"], stored["basis"],
        cc.canonical(stored), stored["certificate_id"],
        stored["supersedes"], stored["issued_at"]))
    db.commit()
    return cc.certificates(db, session_id), stored
