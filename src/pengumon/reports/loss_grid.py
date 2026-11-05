"""The loss grid ("when it breaks"): probe loss per calendar day and hour."""

import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, Optional

from pengumon.reports.queries import loss_grid_rows
from pengumon.timeofday import local_hour_start, local_midnight, utc_offset, zone_name


async def build_loss_grid(repository, days: int = 7, now: Optional[float] = None, tz: tzinfo = timezone.utc,
                          ignoring: Iterable[str] = ()) -> Dict[str, Any]:
    """Probe loss per calendar day and hour for the last `days` days ("when it breaks"), all targets together but the
    hops that ignore ping (`ignoring`, engine.path_rules: their unanswered pings are no loss), from the rollups. Days
    and hours are the clock's in `tz` (timeofday.viewer_zone): the hour the clocks go back twice counts both in its
    cell, the hour they skip stays empty. `days` lists the dates oldest first (today last), `weekdays` their day of
    the week (0 Monday); `sent`, `lost` and `loss_pct` are one row of 24 hours per day. An hour with no probes (before
    the monitor ran, or not yet happened) is null in loss_pct and 0 in sent. tz_offset_minutes: the zone's offset
    now; zone: its name (timeofday.zone_name).

    When each cell is: day d begins at day_starts[d] (days + 1 of them, the last one tomorrow's); hour h of it runs
    from hour_starts[d][h] to hour_starts[d][h + 1] (25 per day: the hour the clocks go back lasts two hours, the one
    they skip none) on a day the clocks change, and from day_starts[d] + h * 3600 an hour long where hour_starts[d]
    is null (every other day)."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, tz).date()
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]
    day_starts = [int(local_midnight(d, tz)) for d in dates + [today + timedelta(days=1)]]
    # listed for a day the clocks change (23 or 25 hours long); a day of 24 hours has none
    hour_starts = [[int(local_hour_start(d, h, tz)) for h in range(25)] if day_starts[i + 1] - day_starts[i] != 86400
                   else None for i, d in enumerate(dates)]
    start_time = float(day_starts[0])
    names = [d.isoformat() for d in dates]
    row_of = {name: i for i, name in enumerate(names)}
    sent = [[0] * 24 for _ in names]
    lost = [[0] * 24 for _ in names]
    for r in await loss_grid_rows(repository, start_time, tz, exclude=ignoring, now=now):
        i = row_of.get(r["day"])
        if i is not None and 0 <= r["hour"] < 24:
            sent[i][r["hour"]], lost[i][r["hour"]] = r["sent"], r["lost"]
    return {
        "days": names,
        "weekdays": [d.weekday() for d in dates],
        "tz_offset_minutes": utc_offset(now, tz) // 60,
        "zone": zone_name(tz),
        "day_starts": day_starts,
        "hour_starts": hour_starts,
        "generated_at": now,
        "sent": sent,
        "lost": lost,
        "loss_pct": [[round(l * 100.0 / s, 3) if s else None for s, l in zip(srow, lrow)]
                     for srow, lrow in zip(sent, lost)],
    }
