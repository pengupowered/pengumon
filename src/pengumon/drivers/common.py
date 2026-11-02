"""Helpers more than one driver uses: CPU % from cumulative counters, and cleaning names and MACs a device reports."""

import re
from typing import Callable, Dict, Optional, Tuple

CpuCounters = Dict[str, Tuple[int, int]]  # core (or "cpu" for all of them) -> (total, busy) jiffies since boot

# CPU % from readings closer than this is mostly noise (a few jiffies); a poll that soon after the last one (the
# API's test poll right after a scheduled one) repeats the last percentage and keeps the baseline.
CPU_MIN_INTERVAL_SECONDS = 2.0

TEXT_MAX = 64
_RE_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}$")


def cpu_pct(previous: CpuCounters, current: CpuCounters) -> Optional[float]:
    """Average CPU use over the cores between two readings, in percent; None without a usable pair (first
    reading, counters reset by a reboot, or no time passed)."""
    pcts = []
    for core, (total, busy) in current.items():
        if core not in previous:
            continue
        d_total, d_busy = total - previous[core][0], busy - previous[core][1]
        if d_total > 0 and 0 <= d_busy <= d_total:
            pcts.append(d_busy / d_total * 100.0)
    return round(sum(pcts) / len(pcts), 1) if pcts else None


class CpuMeter:
    """CPU % since the previous reading, which becomes the new baseline; readings less than CPU_MIN_INTERVAL_SECONDS
    apart repeat the last percentage and leave the baseline alone. `clock` is monotonic (injectable for tests)."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._counters: CpuCounters = {}
        self._at: Optional[float] = None
        self._pct: Optional[float] = None

    def update(self, counters: CpuCounters) -> Optional[float]:
        if not counters:
            return None
        now = self._clock()
        if self._at is not None and now - self._at < CPU_MIN_INTERVAL_SECONDS:
            return self._pct
        self._pct = cpu_pct(self._counters, counters)
        self._counters, self._at = counters, now
        return self._pct


def proc_stat_cpu(text: str) -> CpuCounters:
    """{"cpu": (total, busy)} from the aggregate line of a Linux /proc/stat; empty when it is not there. Busy is
    everything but idle and iowait; guest time is already counted in user, so only the first eight fields add up."""
    for line in text.splitlines()[:1]:
        fields = line.split()
        if len(fields) < 5 or fields[0] != "cpu" or not all(f.isdigit() for f in fields[1:9]):
            return {}
        numbers = [int(f) for f in fields[1:9]]
        total = sum(numbers)
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        return {"cpu": (total, total - idle)}
    return {}


def clean_text(value: object, limit: int = TEXT_MAX) -> Optional[str]:
    """A name, model or vendor from a device: printable, trimmed and short; None when empty or not a string."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:limit].strip()
    return cleaned or None


def normal_mac(value: object) -> Optional[str]:
    """A MAC address in upper case with ':' separators; None if `value` is not one."""
    if isinstance(value, str) and _RE_MAC.match(value.strip()):
        return value.strip().upper().replace("-", ":")
    return None
