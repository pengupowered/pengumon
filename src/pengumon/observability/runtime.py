"""The process and the event loop: what pengumon itself costs the machine it runs on.

`process_stats` reads the process's resident memory, CPU time, threads and open file descriptors (from /proc on Linux,
`os.times` and `threading` elsewhere; a figure the platform cannot give is None, and its gauge is left out). The gauges
that publish them are in gauges.py; they are read on the SDK's export thread, so every read here is a few syscalls.

`LoopMonitor` is a small task on the event loop: it sleeps `interval` seconds, measures how much later than that it
woke up (the loop's lag: something ran, or blocked, for that long) and records it in the loop-lag histogram. It also
counts the asyncio tasks, on the loop, and keeps the latest count for the gauge: `asyncio.all_tasks()` must not be
called from another thread. A lag over `warn_seconds` for `warn_samples` samples in a row is one warning (StateLog, so a
stuck loop logs a line per 15 minutes, not one per sample) and one line when it is over.
"""

import asyncio
import os
import threading
import time
from typing import Awaitable, Callable, Dict, Optional

from pengumon.observability.logs import ContextLogger, StateLog, get_logger
from pengumon.observability.telemetry import loop_lag

LAG_INTERVAL_SECONDS = 0.5
LAG_WARN_SECONDS = 0.25
LAG_WARN_SAMPLES = 4  # in a row: two seconds of a loop that is late


def _open_fds() -> Optional[int]:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))  # (includes the descriptor listdir itself opens)
        except OSError:
            continue
    return None


def process_stats() -> Dict[str, Optional[float]]:
    """{"rss": bytes, "user": s, "system": s, "threads": n, "fds": n}; None for what this platform does not give."""
    rss: Optional[int] = None
    threads: Optional[int] = None
    try:
        with open("/proc/self/status", "rb") as status:
            for line in status:
                if line.startswith(b"VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith(b"Threads:"):
                    threads = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    if threads is None:
        threads = threading.active_count()  # the threads Python started: all there is to know off Linux
    times = os.times()
    return {"rss": rss, "user": times.user, "system": times.system, "threads": threads, "fds": _open_fds()}


class LoopMonitor:
    def __init__(self, interval: float = LAG_INTERVAL_SECONDS, warn_seconds: float = LAG_WARN_SECONDS,
                 warn_samples: int = LAG_WARN_SAMPLES, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, log: Optional[ContextLogger] = None):
        self.interval = interval
        self.warn_seconds = warn_seconds
        self.warn_samples = warn_samples
        self._clock = clock
        self._sleep = sleep
        self.lag: Optional[float] = None  # the latest sample, seconds
        self.tasks: Optional[int] = None  # asyncio tasks alive at the latest sample (counted on the loop)
        self._late = 0  # samples in a row over warn_seconds
        self._state = StateLog(log or get_logger("pengumon.runtime"), "Event loop running late", clock=clock)

    def sample(self, lag: float) -> None:
        """One lag measurement: recorded, and judged for the warning."""
        lag = max(0.0, lag)
        self.lag = lag
        loop_lag.record(lag)
        if lag <= self.warn_seconds:
            self._late = 0
            self._state.success()
            return
        self._late += 1
        if self._late >= self.warn_samples:
            self._state.failure(f"{lag * 1000:.0f} ms behind", key="lag", lag_ms=round(lag * 1000),
                                late_samples=self._late)

    async def run(self) -> None:
        """Samples until canceled. The sleep is awaited as it is: an error in it ends the task (and the done-callback
        logs it), never a busy loop."""
        while True:
            before = self._clock()
            await self._sleep(self.interval)
            self.sample(self._clock() - before - self.interval)
            self.tasks = len(asyncio.all_tasks())


loop_monitor = LoopMonitor()
