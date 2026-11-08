"""The line's own rate during a speed test, from the router's WAN counters: what the line carried while it was full.

A speed test measures what reached this one machine from one server; the line also carries the household's traffic,
and the server may not fill it (Cloudflare's free endpoint often does not). The router counts every byte that crosses
the WAN port. While a direction runs, `RouterSampler` reads those counters about once a second (SAMPLE_S) through the
app's own device (DeviceRegistry.read_wan_counters: the same driver and session the polls use, behind the device's
lock), and `peak_mbps` takes the best rate over a short window: the line's rate while it was full. The 10-second polls
cannot say this: averaged over 10 s, a test of 8 s reads well under the line (an 894 Mbps transfer read 558).

The maths (`peak_mbps`, pure):

* Samples are (monotonic time of the read, counters). A failed read is simply missing: the counters are cumulative,
  so the next one covers the gap.
* A counter that went back is a wrap when the driver knows the width (`counter_bits`, 32 for UPnP and 32-bit SNMP),
  else a reset (a router restart): the samples on either side are not compared. A step that would mean more than
  MAX_MBPS is taken as a reset too (a restart a 32-bit counter cannot tell from a wrap).
* Routers that refresh their counters every few seconds instead of when asked (net-snmp caches its interface table)
  show the same value twice: only the samples at which the counter changed are used (the moment a new value was first
  seen), and when they change only every other read or less often, the window is made long enough to hold a few of
  their steps (2.5 times the typical step, at least PEAK_WINDOW_S), so the step timing errs little.
* The peak is the highest rate between two such samples at least the window apart (the shortest such span from each
  starting sample). A phase shorter than the window gives the rate over all of it, when it lasted MIN_SPAN_S.
"""

import asyncio
import statistics
import time
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from pengumon.devices.wan import WanReading

SAMPLE_S = 1.0  # one read of the counters about this often (from the start of one read to the next)
PEAK_WINDOW_S = 3.0  # the window the peak is taken over: the best 3 s of 1 s samples
WINDOW_SLACK = 0.85  # a span this much of the window counts as one (the reads are not exactly a second apart)
LUMPY_STEPS = 1.5  # counters that change this many reads apart (typically) or more are refreshed by the router slowly ...
LUMPY_FACTOR = 2.5  # ... and are measured over a window of this many of their typical steps
MIN_SPAN_S = 1.0  # a phase whose samples span less than this gives no rate
MAX_MBPS = 100_000.0  # a step faster than this is a reset, not traffic
STOP_WAIT_S = 3.0  # at the end of a phase: how long a read under way, and the last read, may take

Reader = Callable[[], Awaitable[Optional[WanReading]]]


def _deltas(samples: Sequence[WanReading], rx: bool) -> List[List[Tuple[float, int]]]:
    """The samples as runs of (time, cumulative bytes since the run's first sample), split at a reset; within a run,
    only the samples at which the counter changed (the first time a new value was seen)."""
    runs: List[List[Tuple[float, int]]] = []
    previous: Optional[WanReading] = None
    for sample in samples:
        value = sample.counters.rx_bytes if rx else sample.counters.tx_bytes
        if previous is None:
            runs.append([(sample.at, 0)])
            previous = sample
            continue
        before = previous.counters.rx_bytes if rx else previous.counters.tx_bytes
        bits = sample.counters.counter_bits
        dt = sample.at - previous.at
        if value >= before:
            delta: Optional[int] = value - before
        elif bits:
            delta = value + (1 << bits) - before
        else:
            delta = None
        if dt <= 0 or delta is None or delta * 8 / dt / 1e6 > MAX_MBPS:
            runs.append([(sample.at, 0)])  # a reset: start again from here
        elif delta > 0:
            runs[-1].append((sample.at, runs[-1][-1][1] + delta))
        else:
            continue  # unchanged: the counter was not refreshed yet (the run keeps the moment it last changed)
        previous = sample
    return runs


def peak_mbps(samples: Sequence[WanReading], rx: bool, window_s: float = PEAK_WINDOW_S) -> Optional[float]:
    """The line's peak rate (Mbps) in one direction (`rx`: download) over the samples of a phase, or None when they
    cannot give one (fewer than two usable samples, or spanning less than MIN_SPAN_S)."""
    best: Optional[float] = None
    gaps = [b.at - a.at for a, b in zip(samples, samples[1:]) if b.at > a.at]
    read_every = statistics.median(gaps) if gaps else 0.0  # how often the counters were read
    for run in _deltas(samples, rx):
        if len(run) < 2 or run[-1][0] - run[0][0] < MIN_SPAN_S:
            continue
        step = statistics.median([b[0] - a[0] for a, b in zip(run, run[1:])])  # how often they changed
        lumpy = step > LUMPY_STEPS * read_every
        window = (max(window_s, LUMPY_FACTOR * step) if lumpy else window_s) * WINDOW_SLACK
        if run[-1][0] - run[0][0] < window:  # shorter than the window: all of it
            rates = [(run[-1][1] - run[0][1]) * 8 / (run[-1][0] - run[0][0]) / 1e6]
        else:
            rates = []
            j = 0
            for i in range(len(run)):
                while j < len(run) and run[j][0] - run[i][0] < window:
                    j += 1
                if j >= len(run):
                    break
                rates.append((run[j][1] - run[i][1]) * 8 / (run[j][0] - run[i][0]) / 1e6)
        top = max(rates)
        best = top if best is None else max(best, top)
    return round(best, 1) if best is not None else None


class RouterSampler:
    """Reads the WAN counters about once a second while it is entered (an async context manager around one direction
    of a test), and once more at the end, so the last window covers the end of the phase. A read under way when the
    phase ends is let finish (up to STOP_WAIT_S; a driver is never cut off in the middle of a request when it can be
    helped). Without a reader (no router with WAN counters) it does nothing and gives no rate. A read that fails is
    skipped."""

    def __init__(self, read: Optional[Reader], interval_s: float = SAMPLE_S,
                 monotonic: Callable[[], float] = time.monotonic):
        self._read, self._interval, self._monotonic = read, interval_s, monotonic
        self.samples: List[WanReading] = []
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _take(self) -> None:
        try:
            sample = await self._read()
        except asyncio.CancelledError:
            raise
        except Exception:  # a read that fails is a missing sample (Device.read_wan_counters already says why)
            return
        if sample is not None and (not self.samples or sample.at > self.samples[-1].at):
            self.samples.append(sample)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            began = self._monotonic()
            await self._take()
            wait = max(0.0, self._interval - (self._monotonic() - began))
            try:
                await asyncio.wait_for(self._stop.wait(), wait)
            except TimeoutError:
                pass

    async def __aenter__(self) -> "RouterSampler":
        if self._read is not None:
            self._task = asyncio.ensure_future(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait({self._task}, timeout=STOP_WAIT_S)
        finally:
            if not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        if exc[0] is None:
            try:
                await asyncio.wait_for(self._take(), STOP_WAIT_S)
            except TimeoutError:
                pass

    def peak(self, rx: bool) -> Optional[float]:
        return peak_mbps(self.samples, rx)
