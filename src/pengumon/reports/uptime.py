"""Uptime strips: one status per block (15 minutes or a calendar day) for every target."""

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Tuple

from pengumon.incidents.taxonomy import is_service_kind
from pengumon.reports.queries import block_stats
from pengumon.reports.common import _canonical_order, _targets
from pengumon.reports.settings import UptimeConfig
from pengumon.timeofday import local_midnight, utc_offset, zone_name


UPTIME_WINDOWS = {"24h": (900, 96), "7d": (86400, 7), "30d": (86400, 30)}  # block seconds, number of blocks
UPTIME_MIN_PROBES = 10  # fewer probes than this in a block say nothing about it: no data
_DEFAULTS = UptimeConfig()


def uptime_levels(config) -> UptimeConfig:
    """The uptime levels in force: the live config's uptime: section (its defaults without one)."""
    return getattr(config, "uptime", None) or _DEFAULTS


def block_status(sent: int, ok: int, dead_minutes: int, levels: Optional[UptimeConfig] = None) -> str:
    """ok / degraded / outage / no_data for one block of one target. `levels` (the uptime: settings; None: the
    defaults): outage_dead_minutes minutes with every probe lost is an outage (one is a blip); degraded_loss_pct %
    loss, or one such minute, is degraded."""
    levels = levels or _DEFAULTS
    if sent < UPTIME_MIN_PROBES:
        return "no_data"
    if dead_minutes >= levels.outage_dead_minutes:
        return "outage"
    if dead_minutes >= 1 or (sent - ok) * 100.0 / sent >= levels.degraded_loss_pct:
        return "degraded"
    return "ok"


async def build_uptime(repository, config, window: str = "24h", now: Optional[float] = None,
                       tz: tzinfo = timezone.utc) -> Dict[str, Any]:
    """Availability strips for every target: `cells` has one status per block, oldest first, ending with the block that
    contains now ("24h": 96 blocks of 15 min; "7d" and "30d": calendar days on the clock in `tz`, 23 or 25 hours long
    on a day the clocks change). A cell is ok, degraded, outage, no_data, or busy: a degraded or outage block whose
    only incidents were explained by heavy line load (expected), so the strip can hatch it instead of coloring it as a
    fault. availability_pct is probes answered / sent over the whole window (null without probes). Block i runs from
    bounds[i] to bounds[i + 1] (epoch seconds, blocks + 1 of them); zone names the clock the days are cut on and the
    times are meant to be read in (timeofday.zone_name), tz_offset_minutes its offset now."""
    block_seconds, count = UPTIME_WINDOWS[window]
    now = now or time.time()
    cut = tz
    if block_seconds == 86400:
        epoch_day = date(1970, 1, 1)
        last = (datetime.fromtimestamp(now, tz).date() - epoch_day).days
        bounds = [local_midnight(epoch_day + timedelta(days=k), tz) for k in range(last - count + 1, last + 2)]
    else:
        cut = timezone.utc  # 15-minute blocks: every zone's offset is a multiple of 15 minutes
        last = int(now // block_seconds)
        bounds = [float(k * block_seconds) for k in range(last - count + 1, last + 2)]
    first = last - count + 1
    start, end = bounds[0], bounds[-1]

    by_target: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
    for r in await block_stats(repository, start, end, block_seconds, cut):
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

    levels = uptime_levels(config)  # read now: the levels in force for this request
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
            status = block_status(r["sent"], r["ok"], r["dead_minutes"], levels) if r else "no_data"
            if status in ("degraded", "outage"):
                lo, hi = bounds[k - first], bounds[k - first + 1]
                if overlaps(explained, lo, hi) and not overlaps(faults, lo, hi):
                    status = "busy"
            cells.append(status)
        sent = sum(r["sent"] for r in blocks.values())
        ok = sum(r["ok"] for r in blocks.values())
        targets.append({"target_id": tid, "name": names.get(tid, tid),
                        "availability_pct": round(ok * 100.0 / sent, 3) if sent else None, "cells": cells})
    return {"window": window, "block_seconds": block_seconds, "blocks": count, "start": start, "end": end,
            "bounds": [int(b) for b in bounds], "zone": zone_name(tz), "tz_offset_minutes": utc_offset(now, tz) // 60,
            "targets": targets}
