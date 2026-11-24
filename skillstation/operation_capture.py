"""Recomputable operation timeline from two offline, independently-clocked devices.

The legacy ``actions()`` aggregate only proved that both action names were
ever observed.  It could not support any ordering claim because the camera
and the meter tick on separate clocks.  The enhanced capture builds a
per-device session-shared clock model, deduplicates physical events and
emits a timeline whose reference-time intervals can be independently
recomputed.
"""
from __future__ import annotations

from . import clock_capture as cc


def actions(records):
    """Backward-compatible action-name aggregate.

    Accepts the legacy bare record list or a v2 bundle (dict).  Presence of
    names alone never certifies a complete or safe operation order.
    """
    bundle = cc.load_bundle(records)
    return cc.action_names(bundle)


def devices(records):
    bundle = cc.load_bundle(records)
    return sorted({r["device"] for r in bundle["events"]})


def capture_timeline(records):
    """Full recomputable timeline: events, windows, dedupe, clock models."""
    bundle = cc.load_bundle(records)
    return cc.timeline(bundle)


def solve_order(records, supplement_path=None):
    """Ordering analysis over the shared offset variables."""
    bundle = cc.load_bundle(records)
    return cc.solve_order(bundle, supplement_path=supplement_path)


def physical_events(records):
    """Deduplicated per-device events plus retransmit/frame-gap report."""
    bundle = cc.load_bundle(records)
    return cc.dedupe(bundle)
