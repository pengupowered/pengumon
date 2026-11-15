"""Uptime strips: one status per block (15 minutes, or a calendar day cut at the window) for every target."""

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Tuple

from pengumon.incidents.taxonomy import is_service_kind
from pengumon.probe_interval import UPTIME_MAX_MIN_PROBES, uptime_min_probes
from pengumon.reports.queries import block_stats, oldest_probe_time
from pengumon.reports.common import _canonical_order, _targets
from pengumon.reports.settings import UptimeConfig
from pengumon.timeofday import utc_offset, zone_name
from pengumon.windows import day_cells, week_cells


# block seconds, number of blocks ("1h" is only for the MCP report; "7d", "30d" and "90d": the window in days; its cells
# are calendar days, one more when the window does not begin at a midnight, see windows.day_cells). "all" is not here:
# its window is the retention of the probes, cut at the oldest stored one (ALL_WINDOW)
UPTIME_WINDOWS = {"1h": (900, 4), "24h": (900, 96), "7d": (86400, 7), "30d": (86400, 30), "90d": (86400, 90)}
ALL_WINDOW = "all"
DEFAULT_RETENTION_DAYS = 180  # config.database.latency_retention_days without a config
WEEK_SECONDS = 7 * 86400
WEEK_OFFSET = 3 * 86400  # 1970-01-01 was a Thursday: local seconds + 3 days count whole weeks from a Monday
LONG_DAYS = 90  # "all" is cut in days up to this many, in weeks beyond
UPTIME_MIN_PROBES = UPTIME_MAX_MIN_PROBES  # fewer probes than this in a block say nothing about it: no data (at 1 s)
_DEFAULTS = UptimeConfig()


def uptime_levels(config) -> UptimeConfig:
    """The uptime levels in force: the live config's uptime: section (its defaults without one)."""
    return getattr(config, "uptime", None) or _DEFAULTS


def block_status(sent: int, ok: int, dead_minutes: int, levels: Optional[UptimeConfig] = None,
                 min_probes: float = UPTIME_MIN_PROBES) -> str:
    """ok / degraded / outage / no_data for one block of one target. `levels` (the uptime: settings; None: the
    defaults): outage_dead_minutes minutes with every probe lost is an outage (one is a blip); degraded_loss_pct %
    loss, or one such minute, is degraded. `min_probes`: fewer probes than this say nothing (no_data): 10, or at a longer
    probe interval half of what the block's length gives (probe_interval.uptime_min_probes)."""
    levels = levels or _DEFAULTS
    if sent < min_probes:
        return "no_data"
    if dead_minutes >= levels.outage_dead_minutes:
        return "outage"
    if dead_minutes >= 1 or (sent - ok) * 100.0 / sent >= levels.degraded_loss_pct:
        return "degraded"
    return "ok"


async def build_uptime(repository, config, window: str = "24h", now: Optional[float] = None,
                       tz: tzinfo = timezone.utc) -> Dict[str, Any]:
    """Availability strips for every target: `cells` has one status per block, oldest first. "24h": the 96 blocks of 15
    min (the MCP report's "1h": the last 4) that end with the one that contains now. "7d" and "30d": the window is the last 7 or 30 days ending now (at the
    minute, the resolution of the rollups) and the cells are the calendar days on the clock in `tz` it covers, the
    first and last cut at the window's edges (`partial`, with the `hours` each holds); a day of 23 or 25 hours is whole.
    A cell is ok, degraded, outage, no_data, or busy: a degraded or outage block whose only incidents were explained
    by heavy line load (expected), so the strip can hatch it instead of coloring it as a fault. availability_pct is
    probes answered / sent over exactly the window (null without probes), and a cell counts only the probes inside
    it. Block i runs from bounds[i] to bounds[i + 1] (epoch seconds, blocks + 1 of them); zone names the clock the
    days are cut on and the times are meant to be read in (timeofday.zone_name), tz_offset_minutes its offset now."""
    now = now or time.time()
    cut, shift, cell = tz, 0, "day"
    if window == ALL_WINDOW:
        end = float(int(now // 60) * 60)
        days = getattr(getattr(config, "database", None), "latency_retention_days", DEFAULT_RETENTION_DAYS)
        oldest = await oldest_probe_time(repository)
        # from the oldest stored probe (its minute) when that is younger than the retention; no probes: one day
        start = max(end - days * 86400, float(int(oldest // 60) * 60)) if oldest is not None else end - 86400
        block_seconds = 86400 if end - start <= LONG_DAYS * 86400 else WEEK_SECONDS
    else:
        block_seconds, count = UPTIME_WINDOWS[window]
        end = float(int(now // 60) * 60)
        start = end - count * 86400
    if block_seconds == WEEK_SECONDS:
        epoch_monday = date(1969, 12, 29)
        cells = week_cells(start, end, tz)
        block_ids = [(date.fromisoformat(c["week"]) - epoch_monday).days // 7 for c in cells]
        shift, cell = WEEK_OFFSET, "week"
    elif block_seconds == 86400:
        epoch_day = date(1970, 1, 1)
        cells = day_cells(start, end, tz)
        block_ids = [(date.fromisoformat(c["day"]) - epoch_day).days for c in cells]
    if block_seconds >= 86400:
        bounds = [cells[0]["start"]] + [c["end"] for c in cells]
        partial, hours = [c["partial"] for c in cells], [c["hours"] for c in cells]
    else:
        cell, count = "15m", UPTIME_WINDOWS[window][1]
        cut = timezone.utc  # 15-minute blocks: every zone's offset is a multiple of 15 minutes
        last = int(now // block_seconds)
        block_ids = list(range(last - count + 1, last + 1))
        bounds = [float(k * block_seconds) for k in range(last - count + 1, last + 2)]
        partial, hours = [False] * count, [block_seconds / 3600.0] * count
    start, end = bounds[0], bounds[-1]

    by_target: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
    for r in await block_stats(repository, start, end, block_seconds, cut, shift):
        by_target[r["target_id"]][r["block"]] = r

    explained: List[Tuple[float, float]] = []
    faults: List[Tuple[float, float]] = []
    # overlapping: a drop that began before the window and reaches into it colors its first blocks too (a cell asks
    # whether a span overlaps the block, so a span that starts earlier needs no clipping)
    for r in await repository.get_incidents_since(start, overlapping=True):
        if is_service_kind(r.get("kind")):
            continue
        span = (r["start_time"], r["end_time"] if r["end_time"] is not None else now)
        (explained if r.get("expected") else faults).append(span)

    def overlaps(spans, lo, hi) -> bool:
        return any(a < hi and b >= lo for a, b in spans)

    levels = uptime_levels(config)  # read now: the levels in force for this request
    # a block's probes follow the interval in force now; a day cell cut by the window is still judged as a whole day's
    min_probes = uptime_min_probes(block_seconds, getattr(getattr(config, "prober", None), "interval_seconds", 1.0))
    cfg_targets = _targets(config)
    order = _canonical_order(cfg_targets)
    names = {t.id: t.name for t in cfg_targets}
    ids = sorted(by_target, key=lambda t: order.index(t) if t in order else len(order))
    targets = []
    for tid in ids:
        blocks = by_target[tid]
        cells = []
        for i, k in enumerate(block_ids):
            r = blocks.get(k)
            status = block_status(r["sent"], r["ok"], r["dead_minutes"], levels, min_probes) if r else "no_data"
            if status in ("degraded", "outage"):
                lo, hi = bounds[i], bounds[i + 1]
                if overlaps(explained, lo, hi) and not overlaps(faults, lo, hi):
                    status = "busy"
            cells.append(status)
        sent = sum(r["sent"] for r in blocks.values())
        ok = sum(r["ok"] for r in blocks.values())
        targets.append({"target_id": tid, "name": names.get(tid, tid),
                        "availability_pct": round(ok * 100.0 / sent, 3) if sent else None, "cells": cells})
    return {"window": window, "cell": cell, "block_seconds": block_seconds, "blocks": len(block_ids), "start": start, "end": end,
            "bounds": [int(b) for b in bounds], "partial": partial, "hours": hours, "zone": zone_name(tz),
            "tz_offset_minutes": utc_offset(now, tz) // 60, "targets": targets}
