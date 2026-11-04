"""Wall-clock helpers for a configured time zone: quiet hours and the daily route check are read in
`general.timezone`, not in UTC. Pure: every function takes the epoch seconds it is asked about, so tests drive time.

The zone is an IANA name (zoneinfo, the system's zone data; the image carries it), "UTC" or a fixed offset "+01:00".
Rules at a daylight-saving change: a time of day is compared as the wall clock shows it, so a window "22:00-07:00"
ends at 07:00 on the clock even on the night that has 23 or 25 hours; a wall time that does not exist (the hour
skipped in spring) is taken as the instant the clock first reads that time or later, that is, up to an hour after
the skipped hour started; a wall time that happens twice (autumn) counts at both of its occurrences (a window ending at 01:30 ends at the
first 01:30 when it began before, and at the second when it began after the clocks went back).
"""

from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Optional, Tuple

from zoneinfo import ZoneInfo


def zone(name: Optional[str]) -> tzinfo:
    """The tzinfo for `name` ("UTC", "+HH:MM" / "-HH:MM" or an IANA name); UTC when it cannot be loaded (the setting
    is validated on save, so this only matters for a zone file that vanished since)."""
    name = (name or "UTC").strip()
    if name == "UTC":
        return timezone.utc
    if name[:1] in "+-" and len(name) == 6 and name[3] == ":":
        try:
            sign = -1 if name[0] == "-" else 1
            return timezone(sign * timedelta(hours=int(name[1:3]), minutes=int(name[4:6])))
        except ValueError:
            return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def minutes_of(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def local_minute(epoch: float, tz: tzinfo) -> int:
    """Minutes since local midnight at `epoch` (the wall clock's reading)."""
    local = datetime.fromtimestamp(epoch, tz)
    return local.hour * 60 + local.minute


def next_wall_time(after: float, hhmm: str, tz: tzinfo) -> float:
    """The first instant after `after` at which the wall clock in `tz` reads `hhmm` (see the module docstring for
    the daylight-saving cases)."""
    hour, minute = divmod(minutes_of(hhmm), 60)
    local = datetime.fromtimestamp(after, tz)
    for days in range(0, 4):
        day = (local + timedelta(days=days)).date()
        naive = datetime.combine(day, dtime(hour, minute))
        # both readings of a wall time that happens twice (autumn); the same one twice elsewhere
        found = sorted({naive.replace(tzinfo=tz, fold=fold).timestamp() for fold in (0, 1)})
        if datetime.fromtimestamp(found[0], tz).replace(tzinfo=None) != naive:
            found = [_gap_end(found[0], found[-1], tz)]  # the time does not exist (spring): the clock skips it
        later = [c for c in found if c > after]
        if later:
            return later[0]
    return after + 86400.0  # unreachable: some day within three has a wall time after `after`


def _gap_end(early: float, late: float, tz: tzinfo) -> float:
    """The instant the clock jumps forward over a wall time that does not exist: the first moment it reads that time
    or later. `early` and `late` are the two readings datetime gives for the missing time (one before the jump, one
    after it, the offsets before and after applied to it), so the jump lies between them."""
    after = datetime.fromtimestamp(late, tz).utcoffset()
    lo, hi = int(early), int(late)  # the offset at lo is the old one, at hi the new one
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if datetime.fromtimestamp(mid, tz).utcoffset() == after:
            hi = mid
        else:
            lo = mid
    return float(hi)


def in_window(epoch: float, start: str, end: str, tz: tzinfo) -> bool:
    """Whether the wall clock at `epoch` is in [start, end); the window crosses midnight when end < start."""
    now, a, b = local_minute(epoch, tz), minutes_of(start), minutes_of(end)
    if a == b:
        return False
    return a <= now < b if a < b else (now >= a or now < b)


def window_end(epoch: float, start: str, end: str, tz: tzinfo) -> Optional[float]:
    """When the window that contains `epoch` ends (the next time the clock reads `end`); None outside the window."""
    if not in_window(epoch, start, end, tz):
        return None
    return next_wall_time(epoch, end, tz)


def clock_text(epoch: float, tz: tzinfo) -> str:
    """"03:12" as the wall clock in `tz` showed it."""
    return datetime.fromtimestamp(epoch, tz).strftime("%H:%M")


def day_and_clock(epoch: float, tz: tzinfo) -> Tuple[str, str]:
    local = datetime.fromtimestamp(epoch, tz)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
