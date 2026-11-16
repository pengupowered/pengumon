"""Self-test of the fake clock (scripts/test-clock.sh): when the suite runs with PENGUMON_FAKE_EPOCH set, every wall clock
the code can read must show that instant (plus the time since the run started), and the monotonic clock must be real.
Skipped in a normal run; a normal run is checked the other way round (the clock is the real one)."""

import asyncio
import os
import sqlite3
import subprocess
import time
from datetime import date, datetime, timezone

import pytest

FAKE = os.environ.get("PENGUMON_FAKE_EPOCH")
# the run started at most this long ago when the test runs (container start, imports, xdist startup)
SLACK_S = 120.0


def _near(value: float, expected: float) -> bool:
    return expected - 1.0 <= value <= expected + SLACK_S


def _expected() -> float:
    return float(FAKE) if FAKE else time.time()


def _fake_run() -> bool:
    return FAKE is not None


def test_every_wall_clock_agrees():
    if FAKE is None:
        pytest.skip("not a fake-clock run")
    want = float(FAKE)
    assert _near(time.time(), want)
    assert _near(time.time_ns() / 1e9, want)
    assert _near(datetime.now(timezone.utc).timestamp(), want)
    assert _near(datetime.now().timestamp(), want)  # the naive local now, read back in local time
    assert _near(datetime.utcnow().replace(tzinfo=timezone.utc).timestamp(), want)  # noqa: DTZ003
    assert date.today() == datetime.fromtimestamp(want).date() or date.today() == datetime.fromtimestamp(want + SLACK_S).date()
    assert _near(float(sqlite3.connect(":memory:").execute("select strftime('%s','now')").fetchone()[0]), want)
    assert _near(float(sqlite3.connect(":memory:").execute("select unixepoch('now')").fetchone()[0]), want)
    assert _near(float(subprocess.run(["date", "+%s"], capture_output=True, text=True, check=True).stdout), want)


def test_monotonic_clocks_stay_real():
    """Timeouts and the hang guard run on the monotonic clock: a shift there would end a test run early or never."""
    if FAKE is None:
        pytest.skip("not a fake-clock run")
    # monotonic time is the machine's uptime, not a date: it is far from the fake epoch, and it advances in real time
    assert abs(time.monotonic() - float(FAKE)) > 1e6
    t0 = time.monotonic()
    p0 = time.perf_counter()
    time.sleep(0.2)
    assert 0.15 <= time.monotonic() - t0 <= 1.5
    assert 0.15 <= time.perf_counter() - p0 <= 1.5


def test_asyncio_sleep_and_call_later_run_in_real_time():
    if FAKE is None:
        pytest.skip("not a fake-clock run")

    async def go() -> float:
        t0 = time.monotonic()
        await asyncio.sleep(0.2)
        return time.monotonic() - t0

    assert 0.15 <= asyncio.run(go()) <= 1.5


def test_without_the_fake_clock_the_clock_is_real():
    if FAKE is not None:
        pytest.skip("a fake-clock run")
    assert "LD_PRELOAD" not in os.environ or "faketime" not in os.environ["LD_PRELOAD"]
    assert abs(time.time() - float(subprocess.run(["date", "+%s"], capture_output=True, text=True, check=True).stdout)) < 2
