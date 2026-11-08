"""When the speed test is due, as pure functions of the clock. Slots are every `interval_s` seconds counted from the epoch,
shifted by the install's offset, so every install tests at its own moment of the day (not all on the hour).
"""

import math
from datetime import date, tzinfo
from typing import Optional, Tuple

from pengumon.timeofday import in_window, local_midnight, next_wall_time


DAY_S = 86400


def spread_offset(raw: int, interval_s: float) -> float:
    """The install's offset within an interval, from its stored random number `raw` (0 to a day): the time of day, and
    for an interval of two whole days or more also a day within it (`raw` modulo the number of days), so installs that
    test weekly or monthly spread over the days instead of all testing on the epoch's weekday. Under two days: `raw`
    modulo the interval, as before."""
    days = int(interval_s // DAY_S)
    if days < 2:
        return raw % interval_s
    time_of_day = raw % DAY_S
    return float(time_of_day + (raw % days) * DAY_S)


def slot_after(t: float, interval_s: float, offset_s: float) -> float:
    """The first slot strictly after `t`."""
    return (math.floor((t - offset_s) / interval_s) + 1) * interval_s + offset_s


def slot_at_or_before(t: float, interval_s: float, offset_s: float) -> float:
    return math.floor((t - offset_s) / interval_s) * interval_s + offset_s


def due_slot(reference: float, now: float, interval_s: float, offset_s: float) -> float:
    """The slot the next scheduled test belongs to: the first one after `reference` (the last recorded test, or when this
    run began). A slot more than one interval old (the feature was off, or the app was down for days) is not caught up on:
    the current slot is due instead."""
    due = slot_after(reference, interval_s, offset_s)
    if now - due > interval_s:
        due = slot_at_or_before(now, interval_s, offset_s)
    return due


def open_at(at: float, start: Optional[str], end: Optional[str], tz: tzinfo) -> float:
    """`at` itself when it is inside the window (no window: always), else the next moment the window opens."""
    if not start or not end or in_window(at, start, end, tz):
        return at
    return next_wall_time(at, start, tz)


def month_bounds(ts: float, tz: tzinfo) -> Tuple[float, float]:
    """(start, end) of the calendar month that contains `ts` on the clock in `tz`."""
    from datetime import datetime
    local = datetime.fromtimestamp(ts, tz)
    first = date(local.year, local.month, 1)
    following = date(local.year + (local.month == 12), local.month % 12 + 1, 1)
    return local_midnight(first, tz), local_midnight(following, tz)
