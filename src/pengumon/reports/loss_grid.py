"""The loss grid ("when it breaks"): probe loss per calendar day and hour of a rolling window."""

import time
from datetime import date, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, Optional

from pengumon.reports.queries import loss_grid_rows
from pengumon.timeofday import local_hour_start, local_midnight, utc_offset, zone_name
from pengumon.windows import day_cells


FOLD_AFTER_DAYS = 31  # more days than this are folded into one row per day of the week


async def build_loss_grid(repository, days: int = 7, now: Optional[float] = None, tz: tzinfo = timezone.utc,
                          ignoring: Iterable[str] = ()) -> Dict[str, Any]:
    """Probe loss per calendar day and hour for the last `days` * 24 hours ("when it breaks"): a window that ends at
    the end of the hour in progress and begins `days` * 24 hours earlier, so the oldest row holds only the hours from
    the window's start and the newest the hours up to now (`outside` is true for the hours of those two rows the
    window does not contain: drawn blank, not as "no data"). All targets together but the hops that ignore ping
    (`ignoring`, engine.path_rules: their unanswered pings are no loss), from the rollups. Days and hours are the
    clock's in `tz` (timeofday.viewer_zone): the hour the clocks go back twice counts both in its cell, the hour they
    skip stays empty. `days` lists the dates the window touches, oldest first, today last (one more than the number
    asked unless the window begins at a midnight), `weekdays` their day of the week (0 Monday); `sent`, `lost` and
    `loss_pct` are one row of 24 hours per day. An hour with no probes (before the monitor ran, or not yet happened) is
    null in loss_pct and 0 in sent. tz_offset_minutes: the zone's offset now; zone: its name (timeofday.zone_name);
    start and end: the window.

    When each cell is: day d begins at day_starts[d] (days + 1 of them, the last one tomorrow's); hour h of it runs
    from hour_starts[d][h] to hour_starts[d][h + 1] (25 per day: the hour the clocks go back lasts two hours, the one
    they skip none) on a day the clocks change, and from day_starts[d] + h * 3600 an hour long where hour_starts[d]
    is null (every other day).

    More than FOLD_AFTER_DAYS days are folded: `fold` is "weekday", `days` the number asked, `weekdays` 0 to 6, and
    `sent`, `lost`, `loss_pct` and `outside` (all false) have 7 rows, Monday to Sunday on the clock in `tz`, of 24
    hours: each cell the sum of that weekday's hour over the whole window (start to end, as above). There are no
    day_starts and hour_starts (no row is a date)."""
    now = now or time.time()
    end = (int(now // 3600) + 1) * 3600.0
    start = end - days * 86400
    if days > FOLD_AFTER_DAYS:
        return await _fold(repository, days, now, start, end, tz, ignoring)
    dates = [date.fromisoformat(c["day"]) for c in day_cells(start, end, tz)]
    day_starts = [int(local_midnight(d, tz)) for d in dates + [dates[-1] + timedelta(days=1)]]
    # listed for a day the clocks change (23 or 25 hours long); a day of 24 hours has none
    hour_starts = [[int(local_hour_start(d, h, tz)) for h in range(25)] if day_starts[i + 1] - day_starts[i] != 86400
                   else None for i, d in enumerate(dates)]
    edges = [[_hour_edges(day_starts, hour_starts, i, h) for h in range(24)] for i in range(len(dates))]
    outside = [[not (hi > start and lo < end) for lo, hi in row] for row in edges]
    start_time = start
    names = [d.isoformat() for d in dates]
    row_of = {name: i for i, name in enumerate(names)}
    sent = [[0] * 24 for _ in names]
    lost = [[0] * 24 for _ in names]
    for r in await loss_grid_rows(repository, start_time, tz, exclude=ignoring, now=now):
        i = row_of.get(r["day"])
        if i is not None and 0 <= r["hour"] < 24 and not outside[i][r["hour"]]:
            sent[i][r["hour"]], lost[i][r["hour"]] = r["sent"], r["lost"]
    return {
        "days": names,
        "weekdays": [d.weekday() for d in dates],
        "tz_offset_minutes": utc_offset(now, tz) // 60,
        "zone": zone_name(tz),
        "day_starts": day_starts,
        "hour_starts": hour_starts,
        "start": int(start),
        "end": int(end),
        "outside": outside,
        "generated_at": now,
        "sent": sent,
        "lost": lost,
        "loss_pct": [[round(l * 100.0 / s, 3) if s else None for s, l in zip(srow, lrow)]
                     for srow, lrow in zip(sent, lost)],
    }


def _hour_edges(day_starts, hour_starts, i: int, h: int):
    """(begin, end) of hour `h` of day row `i`: from hour_starts on a day the clocks change, else an hour long."""
    if hour_starts[i] is not None:
        return hour_starts[i][h], hour_starts[i][h + 1]
    return day_starts[i] + h * 3600, day_starts[i] + (h + 1) * 3600


async def _fold(repository, days: int, now: float, start: float, end: float, tz: tzinfo, ignoring: Iterable[str]):
    """The loss grid of a long window: one row per day of the week (0 Monday), summed in SQL."""
    sent = [[0] * 24 for _ in range(7)]
    lost = [[0] * 24 for _ in range(7)]
    for r in await loss_grid_rows(repository, start, tz, exclude=ignoring, weekday=True, now=now):
        if 0 <= int(r["day"]) < 7 and 0 <= r["hour"] < 24:
            sent[int(r["day"])][r["hour"]], lost[int(r["day"])][r["hour"]] = r["sent"], r["lost"]
    return {
        "fold": "weekday",
        "days": days,
        "weekdays": list(range(7)),
        "tz_offset_minutes": utc_offset(now, tz) // 60,
        "zone": zone_name(tz),
        "start": int(start),
        "end": int(end),
        "outside": [[False] * 24 for _ in range(7)],
        "generated_at": now,
        "sent": sent,
        "lost": lost,
        "loss_pct": [[round(l * 100.0 / s, 3) if s else None for s, l in zip(srow, lrow)]
                     for srow, lrow in zip(sent, lost)],
    }
