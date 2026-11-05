"""Telemetry of the process, the event loop and the probe loop: runtime.py, the probe loop's work time and late rounds,
and the DNS lookups that outlive their timeout."""

import asyncio
import logging
import threading
import time

import pytest

import obs_helpers
from pengumon.live.ws import ConnectionManager
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine import loop as probe_loop
from pengumon.engine.loop import late_round, probe_background_worker
from pengumon.engine.prober import NetworkProber
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.incidents.model import SystemDiagnosis
from pengumon.observability import export, runtime
from pengumon.observability.gauges import GAUGES
from pengumon.observability.runtime import LoopMonitor, process_stats

obs_helpers.install()


def _gauge(name):
    return next(g for g in GAUGES if g.otel == name)


# --- process figures ------------------------------------------------------------------------------------------------

def test_process_stats_read_this_process():
    before = process_stats()
    sink = [bytearray(1024) for _ in range(2000)]  # ~2 MB of live memory
    spin = time.process_time()
    while time.process_time() - spin < 0.05:
        pass
    after = process_stats()
    assert len(sink) == 2000
    assert after["threads"] >= threading.active_count()
    assert after["user"] + after["system"] > before["user"] + before["system"]
    assert after["fds"] is None or after["fds"] >= 3
    assert after["rss"] is None or after["rss"] > 10_000_000  # Linux gives it; a platform that can't leaves it None


def test_process_gauges_are_in_the_table_and_read_without_an_app_state():
    for name in ("pengumon.process.memory.rss", "pengumon.process.cpu.time", "pengumon.process.threads",
                 "pengumon.process.open_fds", "pengumon.asyncio.tasks"):
        assert _gauge(name).prom.startswith("pengumon_")
    cpu = dict((labels["mode"], v) for labels, v in _gauge("pengumon.process.cpu.time").collect(None))
    assert set(cpu) == {"user", "system"} and cpu["user"] >= 0
    assert _gauge("pengumon.process.threads").collect(None)[0][1] >= 1


def test_a_figure_the_platform_cannot_give_leaves_its_sample_out(monkeypatch):
    monkeypatch.setattr("pengumon.observability.gauges.process_stats",
                        lambda: {"rss": None, "user": 1.0, "system": 0.5, "threads": 3, "fds": None})
    observe = export._guard(_gauge("pengumon.process.memory.rss"))
    export.observe_state(object())
    try:
        assert observe(None) == []
        assert [o.value for o in export._guard(_gauge("pengumon.process.threads"))(None)] == [3]
    finally:
        export.observe_state(None)


# --- the event-loop monitor -----------------------------------------------------------------------------------------

def test_a_lag_sample_is_recorded_in_the_histogram():
    before = obs_helpers.total("pengumon.asyncio.loop.lag")
    LoopMonitor().sample(0.004)
    assert obs_helpers.total("pengumon.asyncio.loop.lag") == before + 1


def test_a_loop_that_stays_late_warns_once_and_says_when_it_is_over(caplog):
    monitor = LoopMonitor(warn_seconds=0.25, warn_samples=3, log=runtime.get_logger("pengumon.runtime"))
    with caplog.at_level(logging.INFO, logger="pengumon.runtime"):
        monitor.sample(0.5)
        monitor.sample(0.6)
        assert caplog.records == []  # two samples are a hiccup
        monitor.sample(0.7)
        monitor.sample(0.9)  # the same condition goes on: no second line
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "Event loop running late" in warned[0].getMessage()
        monitor.sample(0.01)
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.INFO]


def test_an_isolated_late_sample_between_good_ones_never_warns(caplog):
    monitor = LoopMonitor(warn_seconds=0.25, warn_samples=3)
    with caplog.at_level(logging.DEBUG, logger="pengumon.runtime"):
        for lag in (0.5, 0.01, 0.5, 0.01, 0.5, 0.01):
            monitor.sample(lag)
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


async def test_the_monitor_measures_a_blocked_loop_and_counts_tasks_on_the_loop():
    monitor = LoopMonitor(interval=0.05)
    task = asyncio.create_task(monitor.run())
    extra = [asyncio.create_task(asyncio.sleep(5)) for _ in range(3)]
    await asyncio.sleep(0.12)
    time.sleep(0.3)  # blocks the loop: the monitor's sleep wakes up late
    await asyncio.sleep(0.12)
    for t in (task, *extra):
        t.cancel()
    await asyncio.gather(task, *extra, return_exceptions=True)
    assert monitor.lag is not None
    assert monitor.tasks >= 4  # the monitor, the three sleepers and this test
    assert max(monitor.lag, 0.0) >= 0 and obs_helpers.histogram_sum("pengumon.asyncio.loop.lag") >= 0.2


async def test_a_timer_that_raises_ends_the_monitor_instead_of_spinning():
    calls = []

    async def failing_sleep(seconds):
        calls.append(seconds)
        raise RuntimeError("timer broke")

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(LoopMonitor(sleep=failing_sleep).run(), 2)
    assert len(calls) == 1


async def test_the_runtime_component_starts_the_monitor_as_a_background_task():
    from pengumon.observability.component import register

    spawned = []

    class Ctx:
        def spawn(self, coro):
            spawned.append(coro)
            coro.close()

    component = await register(Ctx())
    await component.start()
    assert component.name == "runtime" and len(spawned) == 1


# --- late and skipped probe rounds ----------------------------------------------------------------------------------

def test_a_round_on_time_is_not_late_and_a_long_gap_says_why():
    assert late_round(10.0, 11.2, 1.0, 0.1) is None  # 1.2 s after: normal jitter
    assert late_round(10.0, 11.5, 1.0, 0.1) is None
    assert late_round(10.0, 14.1, 1.0, 0.2) == ("starved", 3)  # three rounds fell into the gap
    assert late_round(10.0, 13.0, 1.0, 2.8) == ("overran", 2)  # the round itself took 2.8 s


class _Prober:
    def __init__(self, sleeps):
        self.stats = {}
        self.sleeps = list(sleeps)

    def probing_problem(self):
        return None

    async def execute_probe_round(self, targets, dns_probes):
        if self.sleeps:
            await asyncio.sleep(self.sleeps.pop(0))
        return []


class _Classifier:
    async def evaluate(self, samples, stats):
        return SystemDiagnosis()


async def test_the_probe_loop_counts_a_round_that_ran_long_and_times_its_own_steps(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    config = AppConfig()
    config.prober.interval_seconds = 0.05
    late_before = obs_helpers.total("pengumon.probe.rounds.late", cause="overran")
    skipped_before = obs_helpers.total("pengumon.probe.rounds.skipped")
    work_before = {s: obs_helpers.total("pengumon.probe.round.work", step=s) for s in ("store", "classify")}
    ws = ConnectionManager()
    task = asyncio.create_task(probe_background_worker(config, _Prober([0, 0.4, 0, 0]), _Classifier(), repo, ws=ws))
    await asyncio.sleep(0.9)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert obs_helpers.total("pengumon.probe.rounds.late", cause="overran") > late_before
    assert obs_helpers.total("pengumon.probe.rounds.skipped") > skipped_before
    for step, before in work_before.items():
        assert obs_helpers.total("pengumon.probe.round.work", step=step) > before, step
    assert probe_loop.LATE_FACTOR > 1


# --- DNS lookups past their timeout ---------------------------------------------------------------------------------

async def test_a_system_lookup_that_outlives_its_timeout_is_counted_until_the_resolver_returns(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def slow_getaddrinfo(*args, **kwargs):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr("pengumon.engine.prober.socket.getaddrinfo", slow_getaddrinfo)
    monkeypatch.setattr("pengumon.engine.prober.HAS_DNSPYTHON", False)
    prober = NetworkProber(ping_timeout=0.1)
    sample = await prober.probe_dns(DnsProbeConfig(id="dns_slow", name="Slow", domain="example.com", enabled=True))
    assert not sample.success and "Timeout" in sample.error
    assert started.is_set() and prober.lookups_stuck == 1
    observed = export._guard(_gauge("pengumon.dns.lookups.stuck"))
    state = type("S", (), {"prober": prober})()
    export.observe_state(state)
    try:
        assert [o.value for o in observed(None)] == [1]
    finally:
        export.observe_state(None)
    release.set()
    for _ in range(100):
        if prober.lookups_stuck == 0:
            break
        await asyncio.sleep(0.02)
    assert prober.lookups_stuck == 0


async def test_a_lookup_that_answers_in_time_is_never_stuck(monkeypatch):
    monkeypatch.setattr("pengumon.engine.prober.socket.getaddrinfo", lambda *a, **k: [])
    monkeypatch.setattr("pengumon.engine.prober.HAS_DNSPYTHON", False)
    prober = NetworkProber(ping_timeout=1.0)
    sample = await prober.probe_dns(DnsProbeConfig(id="dns_fast", name="Fast", domain="example.com", enabled=True))
    assert sample.success and prober.lookups_stuck == 0
