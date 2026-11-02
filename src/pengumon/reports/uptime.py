"""Uptime strips: one status per block (15 minutes or a calendar day) for every target."""

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from pengumon.incidents.taxonomy import is_service_kind
from pengumon.reports.queries import block_stats
from pengumon.reports.common import _canonical_order, _targets


UPTIME_WINDOWS = {"24h": (900, 96), "7d": (86400, 7), "30d": (86400, 30)}  # block seconds, number of blocks
UPTIME_MIN_PROBES = 10  # fewer probes than this in a block say nothing about it: no data
UPTIME_DEGRADED_LOSS_PCT = 1.0  # a block with this much loss, or one minute with every probe lost, is degraded
UPTIME_OUTAGE_DEAD_MINUTES = 2  # this many minutes with every probe lost is an outage (one is a blip)


def block_status(sent: int, ok: int, dead_minutes: int) -> str:
    """ok / degraded / outage / no_data for one block of one target."""
    if sent < UPTIME_MIN_PROBES:
        return "no_data"
    if dead_minutes >= UPTIME_OUTAGE_DEAD_MINUTES:
        return "outage"
    if dead_minutes >= 1 or (sent - ok) * 100.0 / sent >= UPTIME_DEGRADED_LOSS_PCT:
        return "degraded"
    return "ok"


async def build_uptime(repository, config, window: str = "24h", now: Optional[float] = None,
                       tz_offset_minutes: int = 0) -> Dict[str, Any]:
    """Availability strips for every target: `cells` has one status per block, oldest first, ending with the block that
    contains now ("24h": 96 blocks of 15 min; "7d" and "30d": calendar days in the time zone tz_offset_minutes east of
    UTC). A cell is ok, degraded, outage, no_data, or busy: a degraded or outage block whose only incidents were
    explained by heavy line load (expected), so the strip can hatch it instead of colouring it as a fault.
    availability_pct is probes answered / sent over the whole window (null without probes)."""
    block_seconds, count = UPTIME_WINDOWS[window]
    now = now or time.time()
    offset = tz_offset_minutes * 60 if block_seconds == 86400 else 0
    last = int((now + offset) // block_seconds)
    first = last - count + 1
    start = first * block_seconds - offset
    end = (last + 1) * block_seconds - offset

    by_target: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
    for r in await block_stats(repository, first, last, block_seconds, offset):
        by_target[r["target_id"]][r["block"]] = r

    explained: List[Tuple[float, float]] = []
    faults: List[Tuple[float, float]] = []
    for r in await repository.get_incidents_since(start):
        if is_service_kind(r.get("kind")):
            continue
        span = (r["start_time"], r["end_time"] if r["end_time"] is not None else now)
        (explained if r.get("expected") else faults).append(span)

    def overlaps(spans, lo, hi) -> bool:
        return any(a < hi and b >= lo for a, b in spans)

    cfg_targets = _targets(config)
    order = _canonical_order(cfg_targets)
    names = {t.id: t.name for t in cfg_targets}
    ids = sorted(by_target, key=lambda t: order.index(t) if t in order else len(order))
    targets = []
    for tid in ids:
        blocks = by_target[tid]
        cells = []
        for k in range(first, last + 1):
            r = blocks.get(k)
            status = block_status(r["sent"], r["ok"], r["dead_minutes"]) if r else "no_data"
            if status in ("degraded", "outage"):
                lo, hi = k * block_seconds - offset, (k + 1) * block_seconds - offset
                if overlaps(explained, lo, hi) and not overlaps(faults, lo, hi):
                    status = "busy"
            cells.append(status)
        sent = sum(r["sent"] for r in blocks.values())
        ok = sum(r["ok"] for r in blocks.values())
        targets.append({"target_id": tid, "name": names.get(tid, tid),
                        "availability_pct": round(ok * 100.0 / sent, 3) if sent else None, "cells": cells})
    return {"window": window, "block_seconds": block_seconds, "blocks": count, "start": start, "end": end,
            "tz_offset_minutes": tz_offset_minutes if offset else 0, "targets": targets}
