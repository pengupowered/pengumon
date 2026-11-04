"""WAN rates from cumulative byte counters, for any device with the wan_counters capability."""

import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

from pengumon.capabilities import WanCounters

Rate = Tuple[float, float, float]  # (ts, rx_mbps, tx_mbps)

# How much rate history recent_rates() keeps; the load-aware classifier looks a few seconds around an incident.
RATE_HISTORY_SECONDS = 600.0
# Readings closer than this give no rate (the baseline stays): over a fraction of a second the counters' own
# update granularity dominates, and stretching the interval to 1 s would understate the rate.
MIN_RATE_INTERVAL_SECONDS = 1.0


def _delta(current: int, previous: int, bits: Optional[int]) -> Optional[int]:
    """Bytes between two counter readings. A counter that went backwards wrapped (when its width is known) or was
    reset by a router restart (then there is no delta to report)."""
    if current >= previous:
        return current - previous
    if bits:
        return current + (1 << bits) - previous
    return None


class WanRates:
    """Turns successive counter readings into Mbps and keeps the last RATE_HISTORY_SECONDS of rates.

    A rate needs two readings: the first after a start (or after a counter reset) only sets the baseline. A poll
    without counters leaves the baseline alone, so the next rate covers the whole interval since the last reading.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._prev: Optional[Tuple[float, int, int]] = None  # (ts, rx_bytes, tx_bytes)
        self._rates: Deque[Rate] = deque()
        self.resets = 0  # counters seen going backwards without wrapping (a router restart); the monitor counts them

    def update(self, ts: float, counters: Optional[WanCounters]) -> Tuple[Optional[float], Optional[float]]:
        """(rx_mbps, tx_mbps) since the previous reading, or (None, None) when there is no rate to report. The
        reading's own time (`counters.read_at`) is used when the driver gives one, else `ts`."""
        if counters is None:
            return None, None
        if counters.read_at is not None:
            ts = counters.read_at
        if self._prev is not None and 0 <= ts - self._prev[0] < MIN_RATE_INTERVAL_SECONDS:
            return None, None  # too soon: keep the baseline
        # (a reading older than the baseline, after the clock was set back, starts a new baseline below)
        prev, self._prev = self._prev, (ts, counters.rx_bytes, counters.tx_bytes)
        if prev is None:
            return None, None
        prev_ts, prev_rx, prev_tx = prev
        if ts < prev_ts:
            return None, None
        delta_rx = _delta(counters.rx_bytes, prev_rx, counters.counter_bits)
        delta_tx = _delta(counters.tx_bytes, prev_tx, counters.counter_bits)
        if delta_rx is None or delta_tx is None:
            self.resets += 1
            return None, None
        dt = ts - prev_ts
        rx = round((delta_rx * 8.0) / (dt * 1_000_000.0), 2)
        tx = round((delta_tx * 8.0) / (dt * 1_000_000.0), 2)
        self._rates.append((ts, rx, tx))
        self._trim(ts)
        return rx, tx

    def recent_rates(self) -> List[Rate]:
        """(timestamp, rx_mbps, tx_mbps) of the last 10 minutes, oldest first. Only polls that produced a rate are
        in here, so a gap means the counters could not be read."""
        self._trim(self._clock())
        return list(self._rates)

    def _trim(self, now: float) -> None:
        while self._rates and self._rates[0][0] < now - RATE_HISTORY_SECONDS:
            self._rates.popleft()
