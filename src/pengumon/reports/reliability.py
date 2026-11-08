"""The reliability report: availability per hop and day, incidents by cause, and loss by hour of the day."""

import time
from collections import defaultdict
from datetime import timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional

from pengumon.incidents.taxonomy import incident_cause
from pengumon.reports.common import _canonical_order, _split_incidents, _targets
from pengumon.reports.outages import outage_totals, outages_of
from pengumon.reports.queries import daily_availability, hourly_loss
from pengumon.timeofday import utc_offset


async def build_report(repository, config, days: int, now: Optional[float] = None, tz: tzinfo = timezone.utc,
                       ignoring: Iterable[str] = ()) -> Dict[str, Any]:
    """The last `days` days. Day and hour buckets are the clock's in `tz` (timeofday.viewer_zone; the container runs
    in UTC). Loss by hour is the loss grid's rule: the hops that ignore ping (`ignoring`) and the hops removed from the
    path are left out. tz_offset_minutes: the zone's offset now."""
    now = now or time.time()
    start_time = now - days * 86400

    cfg_targets = _targets(config)
    names = {t.id: t.name for t in cfg_targets}
    order = _canonical_order(cfg_targets)

    per_hop: Dict[str, Dict[str, List[int]]] = defaultdict(dict)
    all_days = set()
    for r in await daily_availability(repository, start_time, tz, now=now):
        per_hop[r["target_id"]][r["day"]] = [r["sent"], r["ok"]]
        all_days.add(r["day"])
    day_list = sorted(all_days)
    hops = []
    for tid in sorted(per_hop, key=lambda t: order.index(t) if t in order else 999):
        cells = per_hop[tid]
        sent = sum(v[0] for v in cells.values())
        ok = sum(v[1] for v in cells.values())
        hops.append({
            "target_id": tid,
            "name": names.get(tid, tid),
            "availability_pct": round(ok * 100.0 / sent, 3) if sent else None,
            "per_day": [round(cells[d][1] * 100.0 / cells[d][0], 3) if d in cells and cells[d][0] else None
                        for d in day_list],
        })

    rows = await repository.get_incidents_since(start_time)
    incidents, excluded = _split_incidents(rows)

    def lasted(r):  # an incident still open has no duration yet: it has lasted until now
        return r["duration_seconds"] if r["duration_seconds"] is not None else max(0.0, now - r.get("start_time", now))

    # by cause category (incidents.taxonomy: the incident lanes' words), from the kind where it says enough: a cause
    # code alone can stand for more than one place (older rows, and access_line_drop for upstream latency)
    causes: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for r in incidents:
        cause = incident_cause(r.get("kind"), r["cause_code"], r.get("description"))
        causes[cause][0] += 1
        causes[cause][1] += lasted(r) / 60.0
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

    return {
        "days": days,
        "tz_offset_minutes": utc_offset(now, tz) // 60,
        "generated_at": now,
        "availability": {"days": day_list, "hops": hops},
        "incidents": {
            "count": len(incidents),
            "minutes": round(sum(lasted(r) for r in incidents) / 60.0, 1),
            "by_cause": by_cause,
            "excluded_expected": excluded,
            # drops joined into outages (incidents.outages; the figures above stay per incident)
            "outages": outage_totals(outages_of(incidents, config, now)),
        },
        "loss_by_hour": hourly,
        "worst_hours": worst_hours,
    }
