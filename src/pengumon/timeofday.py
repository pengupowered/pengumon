"""Wall-clock helpers for a configured time zone: quiet hours and the daily route check are read in
`general.timezone`, not in UTC; the dashboard's reports and the AI pack put days and hours in viewer_zone's (the
setting, else the viewer's browser). Pure: every function takes the epoch seconds it is asked about, so tests drive
time.

The zone is an IANA name (zoneinfo, the system's zone data; the image carries it), "UTC" or a fixed offset "+01:00".
Rules at a daylight-saving change: a time of day is compared as the wall clock shows it, so a window "22:00-07:00"
ends at 07:00 on the clock even on the night that has 23 or 25 hours; a wall time that does not exist (the hour
skipped in spring) is taken as the instant the clock first reads that time or later, that is, up to an hour after
the skipped hour started; a wall time that happens twice (autumn) counts at both of its occurrences (a window ending at 01:30 ends at the
first 01:30 when it began before, and at the second when it began after the clocks went back).
"""

import logging
from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from typing import List, Optional, Set, Tuple

from zoneinfo import ZoneInfo

_log = logging.getLogger("pengumon.timeofday")
_warned: Set[str] = set()  # zones already reported as unusable, so the warning comes once per name


def _unusable(name: str) -> tzinfo:
    if name not in _warned:
        _warned.add(name)
        _log.warning(f"The time zone {name!r} cannot be loaded; UTC is used instead (quiet hours, the daily route "
                     "check and report days follow UTC until the zone is fixed in Settings)")
    return timezone.utc


def zone(name: Optional[str]) -> tzinfo:
    """The tzinfo for `name` ("UTC", "+HH:MM" / "-HH:MM" or an IANA name); UTC when it cannot be loaded, with one
    warning per name (the setting is validated on save, so this only matters for a zone file that vanished since)."""
    name = (name or "UTC").strip()
    if name == "UTC":
        return timezone.utc
    if name[:1] in "+-" and len(name) == 6 and name[3] == ":":
        try:
            sign = -1 if name[0] == "-" else 1
            return timezone(sign * timedelta(hours=int(name[1:3]), minutes=int(name[4:6])))
        except ValueError:
            return _unusable(name)
    try:
        return ZoneInfo(name)
    except Exception:
        return _unusable(name)


# The names a UTC clock goes by: a Settings zone that is one of them is the container's default, not the household's.
UTC_NAMES = frozenset({"UTC", "UCT", "GMT", "GMT0", "GMT+0", "GMT-0", "Greenwich", "Universal", "Zulu", "+00:00",
                       "-00:00", "Etc/UTC", "Etc/UCT", "Etc/GMT", "Etc/GMT0", "Etc/GMT+0", "Etc/GMT-0", "Etc/Greenwich",
                       "Etc/Universal", "Etc/Zulu"})


def viewer_zone(config, tz_name: Optional[str] = None, tz_offset_minutes: int = 0) -> Tuple[tzinfo, str, str]:
    """(tzinfo, kind, name) the dashboard's reports and the AI pack put days and hours in. In this order: the zone set
    in Settings (general.timezone), when it is not UTC (the default in a container that runs in UTC); the IANA zone
    the caller names (the viewer's browser); a fixed offset the caller gives (minutes east of UTC); else UTC.
    kind: named (follows daylight saving) | offset | utc."""
    from pengumon.settings.general import known_zone
    setting = (getattr(getattr(config, "general", None), "timezone", None) or "").strip()
    if setting and setting not in UTC_NAMES:
        if setting[:1] in "+-":
            return zone(setting), "offset", f"UTC{setting}"
        if known_zone(setting):
            return zone(setting), "named", setting
    name = (tz_name or "").strip()
    if name and name not in UTC_NAMES and known_zone(name):
        return zone(name), "named", name
    if tz_offset_minutes:
        return timezone(timedelta(minutes=tz_offset_minutes)), "offset", ""
    return timezone.utc, "utc", "UTC"


def utc_offset(epoch: float, tz: tzinfo) -> int:
    """Seconds east of UTC the clock in `tz` is at `epoch`."""
    return int(datetime.fromtimestamp(epoch, tz).utcoffset().total_seconds())


def clock_changes(tz: tzinfo, start: float, end: float) -> List[Tuple[float, float, float]]:
    """The daylight-saving changes of `tz` in [start, end]: (the first second of the new offset, the offset before,
    the offset after), offsets in seconds. A fixed offset has none."""
    if isinstance(tz, timezone):
        return []

    def offset(t: float) -> float:
        return (datetime.fromtimestamp(t, tz).utcoffset() or timedelta(0)).total_seconds()

    out = []
    t, before = int(start), offset(int(start))
    while t < end:
        nxt = int(min(end, t + 3600))
        after = offset(nxt)
        if after != before:
            lo, hi = t, nxt           # the offset at lo is the old one, at hi the new one
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if offset(mid) == before else (lo, mid)
            out.append((float(hi), before, after))
            before = after
        t = nxt if nxt > t else t + 1
    return out


def local_midnight(day, tz: tzinfo) -> float:
    """The epoch at which the date `day` begins on the clock in `tz` (a midnight the clocks skip: the first instant
    after it)."""
    return local_hour_start(day, 0, tz)


def local_hour_start(day, hour: int, tz: tzinfo) -> float:
    """The epoch at which the clock in `tz` first reads `hour`:00 on the date `day`: the first of the two in the hour
    the clocks go back; in the hour they skip, the instant they jump (so that hour lasts no time). Hour 24 is the next
    day's midnight."""
    naive = datetime.combine(day, dtime(0, 0)) + timedelta(hours=hour)
    found = sorted({naive.replace(tzinfo=tz, fold=fold).timestamp() for fold in (0, 1)})
    if datetime.fromtimestamp(found[0], tz).replace(tzinfo=None) != naive:  # the clock skips that time
        return _gap_end(found[0], found[-1], tz)
    return found[0]


def zone_name(tz: tzinfo) -> str:
    """The name of `tz` as a browser's Intl.DateTimeFormat takes it (timeZone): the IANA name, "UTC", or a fixed
    offset "+05:30"."""
    key = getattr(tz, "key", None)
    if key:
        return key
    minutes = int((tz.utcoffset(None) or timedelta(0)).total_seconds() // 60)
    if not minutes:
        return "UTC"
    hours, rest = divmod(abs(minutes), 60)
    return f"{'-' if minutes < 0 else '+'}{hours:02d}:{rest:02d}"


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
