"""The reliability report: availability per hop and day, incidents by cause, and loss by hour of the day."""

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from pengumon.incidents.taxonomy import current_cause
from pengumon.reports.common import _canonical_order, _split_incidents, _targets


async def build_report(repository, config, days: int, now: Optional[float] = None,
                       tz_offset_minutes: int = 0) -> Dict[str, Any]:
    """tz_offset_minutes (minutes east of UTC) decides where day and hour buckets start; the container runs in UTC."""
    now = now or time.time()
    start_time = now - days * 86400

    cfg_targets = _targets(config)
    names = {t.id: t.name for t in cfg_targets}
    order = _canonical_order(cfg_targets)

    per_hop: Dict[str, Dict[str, List[int]]] = defaultdict(dict)
    all_days = set()
    for r in await repository.get_daily_availability(start_time, tz_offset_minutes):
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
    causes: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for r in incidents:
        causes[current_cause(r["cause_code"])][0] += 1
        causes[current_cause(r["cause_code"])][1] += (r["duration_seconds"] or 0) / 60.0
    by_cause = [
        {"cause_code": c, "count": n, "minutes": round(m, 1)}
        for c, (n, m) in sorted(causes.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ]

    hourly = [
        {"hour": h["hour"], "sent": h["sent"], "lost": h["lost"],
         "loss_pct": round(h["lost"] * 100.0 / h["sent"], 3) if h["sent"] else 0.0}
        for h in await repository.get_hourly_loss(start_time, tz_offset_minutes)
    ]
    worst_hours = [h for h in sorted(hourly, key=lambda h: (-h["loss_pct"], h["hour"])) if h["lost"] > 0][:5]

    return {
        "days": days,
        "tz_offset_minutes": tz_offset_minutes,
        "generated_at": now,
        "availability": {"days": day_list, "hops": hops},
        "incidents": {
            "count": len(incidents),
            "minutes": round(sum(r["duration_seconds"] or 0 for r in incidents) / 60.0, 1),
            "by_cause": by_cause,
            "excluded_expected": excluded,
        },
        "loss_by_hour": hourly,
        "worst_hours": worst_hours,
    }
