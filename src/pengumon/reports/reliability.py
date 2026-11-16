"""The reliability report: availability per hop and day, incidents by cause, and loss by hour of the day."""

import time
from collections import defaultdict
from datetime import timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional

from pengumon.reports.common import _canonical_order, _split_incidents, _targets, cause_totals, lasted
from pengumon.reports.outages import outage_totals, outages_of
from pengumon.reports.queries import daily_availability, hourly_loss
from pengumon.reports.spikes import spike_summary
from pengumon.timeofday import utc_offset
from pengumon.windows import day_cells


async def build_report(repository, config, days: int, now: Optional[float] = None, tz: tzinfo = timezone.utc,
                       ignoring: Iterable[str] = (), window_end: Optional[float] = None) -> Dict[str, Any]:
    """The last `days` days: the rolling window [window_end - days * 24 h, window_end], `window_end` being now unless
    given (so a caller can have every part of an answer cover one window). `availability` lists the calendar days
    with probes, each marked in `partial` (the window cuts it: the first and last) with the `hours` it holds inside
    the window. Day and hour buckets are the clock's in `tz` (timeofday.viewer_zone; the container runs
    in UTC). Loss by hour is the loss grid's rule: the hops that ignore ping (`ignoring`) and the hops removed from the
    path are left out. tz_offset_minutes: the zone's offset now. `latency_spikes`: the window's brief latency spikes
    (reports/spikes.py: count, expected, by_stage, incident_after_seconds), and `availability.latency_spikes` their count
    per day of `availability.days`."""
    now = now or time.time()
    end_time = now if window_end is None else window_end
    start_time = end_time - days * 86400

    cfg_targets = _targets(config)
    names = {t.id: t.name for t in cfg_targets}
    order = _canonical_order(cfg_targets)

    per_hop: Dict[str, Dict[str, List[int]]] = defaultdict(dict)
    all_days = set()
    for r in await daily_availability(repository, start_time, tz, now=now):
        per_hop[r["target_id"]][r["day"]] = [r["sent"], r["ok"]]
        all_days.add(r["day"])
    day_list = sorted(all_days)
    cells = {c["day"]: c for c in day_cells(start_time, end_time, tz)}
    hops = []
    for tid in sorted(per_hop, key=lambda t: order.index(t) if t in order else 999):
        counts = per_hop[tid]
        sent = sum(v[0] for v in counts.values())
        ok = sum(v[1] for v in counts.values())
        hops.append({
            "target_id": tid,
            "name": names.get(tid, tid),
            "availability_pct": round(ok * 100.0 / sent, 3) if sent else None,
            "per_day": [round(counts[d][1] * 100.0 / counts[d][0], 3) if d in counts and counts[d][0] else None
                        for d in day_list],
        })

    rows = await repository.get_incidents_since(start_time)
    incidents, excluded = _split_incidents(rows)

    causes = cause_totals(incidents, now)
    by_cause = [
        {"cause": c, "count": n, "minutes": round(m, 1)}
        for c, (n, m) in sorted(causes.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ]

    hourly = [
        {"hour": h["hour"], "sent": h["sent"], "lost": h["lost"],
         "loss_pct": round(h["lost"] * 100.0 / h["sent"], 3) if h["sent"] else 0.0}
        for h in await hourly_loss(repository, start_time, tz, ignoring, now=now)
    ]
    worst_hours = [h for h in sorted(hourly, key=lambda h: (-h["loss_pct"], h["hour"])) if h["lost"] > 0][:5]

    spikes = await spike_summary(repository, config, start_time, end_time, tz)
    spikes_per_day = {d["day"]: d["count"] for d in spikes["days"]}

    return {
        "days": days,
        "tz_offset_minutes": utc_offset(now, tz) // 60,
        "generated_at": now,
        "window_start": start_time,
        "window_end": end_time,
        "availability": {"days": day_list, "hops": hops,
                         "partial": [bool(cells[d]["partial"]) if d in cells else False for d in day_list],
                         "hours": [cells[d]["hours"] if d in cells else 24.0 for d in day_list],
                         "latency_spikes": [spikes_per_day.get(d, 0) for d in day_list]},
        "incidents": {
            "count": len(incidents),
            "minutes": round(sum(lasted(r, now) for r in incidents) / 60.0, 1),
            "by_cause": by_cause,
            "excluded_expected": excluded,
            # drops joined into outages (incidents.outages; the figures above stay per incident)
            "outages": outage_totals(outages_of(incidents, config, now)),
        },
        "loss_by_hour": hourly,
        "worst_hours": worst_hours,
        # brief latency-only episodes, not incidents (incidents/spikes.py); per day: availability.latency_spikes
        "latency_spikes": {k: spikes[k] for k in ("count", "expected", "by_stage", "incident_after_seconds")},
    }
