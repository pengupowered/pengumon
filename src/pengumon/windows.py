"""The one rule every range follows (0.28): a range ("24 h", "7 d", "30 d") is a rolling window that ends now, snapped to
the resolution of the data it reads, never "since a midnight". Display cells (calendar days) are cut by the window:
`day_cells` marks the ones it cuts as partial and says how many hours they hold. Pure: no I/O; every function takes
the times it is asked about."""

from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Tuple

from pengumon.timeofday import local_midnight

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}  # as the MCP `period` names them


def rolling(seconds: float, end: float) -> Tuple[float, float]:
    """(start, end) of the window of `seconds` that ends at `end`."""
    return end - seconds, end


def day_cells(start: float, end: float, tz: tzinfo) -> List[Dict[str, Any]]:
    """The calendar-day cells of [start, end) on the clock in `tz`, oldest first: {start, end, day ("YYYY-MM-DD"),
    partial, hours}. The first and last are cut at the window's edges and are `partial` (unless the edge is the day's
    own midnight); `hours` is how long the cell lasts inside the window. A day of 23 or 25 hours is a whole day, not
    partial. A window that ends exactly at a midnight has no cell for the day that begins there."""
    out: List[Dict[str, Any]] = []
    if not end > start:
        return out
    day = datetime.fromtimestamp(start, tz).date()
    while True:
        lo, hi = local_midnight(day, tz), local_midnight(day + timedelta(days=1), tz)
        if lo >= end:
            break
        if hi > start:
            s, e = max(lo, start), min(hi, end)
            out.append({"start": s, "end": e, "day": day.isoformat(), "partial": s != lo or e != hi,
                        "hours": round((e - s) / 3600, 4)})
        day = _next(day)
    return out


def week_cells(start: float, end: float, tz: tzinfo) -> List[Dict[str, Any]]:
    """The local weeks (Monday 00:00 to the next Monday 00:00 on the clock in `tz`) of [start, end), oldest first, the
    cells of `day_cells` with `week` (the Monday's date, "YYYY-MM-DD") in place of `day`: {start, end, week, partial,
    hours}. The first and last are cut at the window's edges and are `partial` (unless the edge is the week's own
    Monday midnight); `hours` is how long the cell lasts inside the window. A week with a clock change (167 or 169
    hours) is whole. A window that ends exactly at a Monday midnight has no cell for the week that begins there."""
    out: List[Dict[str, Any]] = []
    if not end > start:
        return out
    first = datetime.fromtimestamp(start, tz).date()
    monday = first - timedelta(days=first.weekday())
    while True:
        lo, hi = local_midnight(monday, tz), local_midnight(monday + timedelta(days=7), tz)
        if lo >= end:
            break
        if hi > start:
            s, e = max(lo, start), min(hi, end)
            out.append({"start": s, "end": e, "week": monday.isoformat(), "partial": s != lo or e != hi,
                        "hours": round((e - s) / 3600, 4)})
        monday += timedelta(days=7)
    return out


def _next(day: date) -> date:
    return day + timedelta(days=1)
