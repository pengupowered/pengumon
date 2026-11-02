"""Device runner and monitors (pengumon.devices): scheduling, error classification, state logging, spans and
metrics, and the snapshots the monitors build."""

import asyncio
import logging
import time
from contextlib import contextmanager

import httpx
import pytest

from pengumon.capabilities import Capability, DriverError, LineReading, PollResult, SystemReading, WanCounters
from pengumon.devices import device as device_module
from pengumon.devices.device import Device, classify
from pengumon.devices.monitors import LineMonitor, RouterMonitor, UnconfiguredMessages
from pengumon.devices.samples import device_sample
from pengumon.devices.wan import WanRates
from pengumon.lines.docsis import line_sample
from pengumon.lines.docsis_model import DocsisChannel

MESSAGES = UnconfiguredMessages(disabled="off in config", missing_credentials="no password yet")


# --- fakes ------------------------------------------------------------------------------------------------

class FakeSpan:
    def __init__(self, name, attributes):
        self.name, self.attributes = name, dict(attributes or {})
        self.status = None
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def record_exception(self, exc):
        self.exceptions.append(exc)


class FakeTracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name, attributes=None, **_):
        span = FakeSpan(name, attributes)
        self.spans.append(span)
        yield span


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    record = add


class FakeDriver:
    name = "fake"
    offers = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM})
    host = "192.0.2.1"

    def __init__(self, outcomes=(), password="pw"):
        self.outcomes = list(outcomes)  # PollResult or an exception, one per poll
        self.password = password
        self.polls = 0
        self.closed = False
        self.credentials = None

    def missing_configuration(self):
        return None if self.password else "no password"

    def set_credentials(self, username, password):
        self.credentials = (username, password)
        self.password = password

    async def poll(self):
        self.polls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else PollResult(readings={})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self):
        self.closed = True


class FakeMonitor:
    def __init__(self):
        self.latest_snapshot = None
        self.events = []

    def on_unconfigured(self, disabled):
        self.events.append(("unconfigured", disabled))
        self.latest_snapshot = ("unconfigured", disabled)
        return self.latest_snapshot

    async def on_error(self, ts, error):
        self.events.append(("error", error.kind, error.message, error.transport))
        self.latest_snapshot = ("error", error.kind)
        return self.latest_snapshot

    async def on_result(self, ts, result):
        self.events.append(("result", sorted(c.value for c in result.readings)))
        self.latest_snapshot = ("ok", ts)
        return self.latest_snapshot


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def telemetry(monkeypatch):
    tracer, polls, durations = FakeTracer(), Recorder(), Recorder()
    monkeypatch.setattr(device_module, "tracer", tracer)
    monkeypatch.setattr(device_module, "device_polls", polls)
    monkeypatch.setattr(device_module, "device_poll_duration", durations)
    return tracer, polls, durations


def _device(driver, monitor=None, clock=None, **kwargs):
    clock = clock or Clock()
    return Device("router", driver, monitor or FakeMonitor(), interval_seconds=10.0, clock=clock, monotonic=clock,
                  **kwargs)


GOOD = PollResult(readings={Capability.WAN_COUNTERS: WanCounters(1, 2),
                            Capability.ROUTER_SYSTEM: SystemReading(cpu_pct=5.0)})


# --- classification ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc, kind, transport", [
    (httpx.ConnectTimeout("timed out"), "timeout", True),
    (httpx.ReadTimeout("read timed out"), "timeout", True),
    (httpx.ConnectError("refused"), "unreachable", True),
    (httpx.RemoteProtocolError("bad"), "unreachable", True),
    (httpx.DecodingError("garbled"), "protocol", True),
    (ValueError("oops"), "internal", False),
])
def test_classify(exc, kind, transport):
    error = classify(exc)
    assert (error.kind, error.transport) == (kind, transport)
    assert error.message.endswith(str(exc))


def test_classify_keeps_driver_errors_and_names_empty_exceptions():
    original = DriverError("auth", "refused")
    assert classify(original) is original
    assert classify(httpx.ConnectError("")).message == "ConnectError"


# --- polling ----------------------------------------------------------------------------------------------

async def test_a_good_poll_reaches_the_monitor_with_a_span_and_metrics(telemetry):
    tracer, polls, durations = telemetry
    device = _device(FakeDriver([GOOD]))
    assert await device.poll(force=True) == ("ok", 1000.0)
    assert device.monitor.events == [("result", ["router_system", "wan_counters"])]
    span = tracer.spans[0]
    assert span.name == "device.poll" and span.status is None
    assert span.attributes == {"device.id": "router", "device.driver": "fake",
                               "device.readings": ["router_system", "wan_counters"]}
    assert polls.calls == [(1, {"device.id": "router", "device.driver": "fake", "outcome": "ok"})]
    assert durations.calls[0][0] >= 0 and durations.calls[0][1]["outcome"] == "ok"


async def test_recent_poll_is_reused_unless_forced(telemetry):
    clock = Clock()
    driver = FakeDriver([GOOD, GOOD, GOOD])
    device = _device(driver, clock=clock)
    await device.poll()
    clock.now += 5
    await device.poll()
    assert driver.polls == 1
    await device.poll(force=True)
    clock.now += 10
    await device.poll()
    assert driver.polls == 3


async def test_errors_are_classified_and_logged_by_state(telemetry, caplog):
    tracer, polls, _ = telemetry
    clock = Clock()
    driver = FakeDriver([httpx.ConnectTimeout("timed out")] * 3 + [GOOD])
    device = _device(driver, clock=clock)
    with caplog.at_level(logging.DEBUG, logger="pengumon.devices"):
        for _ in range(4):
            await device.poll(force=True)
            clock.now += 10
    assert device.monitor.events[:3] == [("error", "timeout", "timed out", True)] * 3
    visible = [(r.levelname, r.getMessage()) for r in caplog.records
               if r.name == "pengumon.devices" and r.levelno > logging.DEBUG]
    assert visible == [
        ("WARNING", "Polling router (fake at 192.0.2.1) failing: timeout: timed out"),
        ("INFO", "Polling router (fake at 192.0.2.1) recovered after 30 s (3 failed attempts)"),
    ]
    assert tracer.spans[0].attributes["error.type"] == "timeout" and tracer.spans[0].status is not None
    assert [c[1]["outcome"] for c in polls.calls] == ["error", "error", "error", "ok"]
    assert polls.calls[0][1]["error.type"] == "timeout"


async def test_an_unexpected_exception_is_logged_with_its_traceback(telemetry, caplog):
    tracer, _, _ = telemetry
    device = _device(FakeDriver([KeyError("netdev")]))
    with caplog.at_level(logging.ERROR, logger="pengumon.devices"):
        await device.poll(force=True)
    assert device.monitor.events == [("error", "internal", "KeyError: 'netdev'", False)]
    record = next(r for r in caplog.records if r.getMessage() == "driver raised an unexpected exception")
    assert record.exc_info is not None
    assert isinstance(tracer.spans[0].exceptions[0], KeyError)


async def test_missing_capabilities_are_logged_by_state(telemetry, caplog):
    tracer, polls, _ = telemetry
    partial = PollResult(readings={Capability.ROUTER_SYSTEM: SystemReading(cpu_pct=1.0)},
                         missing={Capability.WAN_COUNTERS: "no WAN counters in the answer"})
    device = _device(FakeDriver([partial, partial, GOOD]))
    with caplog.at_level(logging.INFO, logger="pengumon.devices"):
        for _ in range(3):
            await device.poll(force=True)
    messages = [r.getMessage() for r in caplog.records if r.name == "pengumon.devices"]
    assert messages == ["router wan_counters failing: no WAN counters in the answer",
                        "router wan_counters recovered after 0 s (2 failed attempts)"]
    assert tracer.spans[0].attributes["device.missing"] == ["wan_counters"]
    assert [c[1]["outcome"] for c in polls.calls] == ["partial", "partial", "ok"]


async def test_unconfigured_and_disabled_devices_are_not_polled_and_log_once(telemetry, caplog):
    driver = FakeDriver(password=None)
    device = _device(driver)
    with caplog.at_level(logging.INFO, logger="pengumon.devices"):
        await device.poll(force=True)
        await device.poll(force=True)
        disabled = _device(FakeDriver(), enabled=False)
        await disabled.poll(force=True)
    assert driver.polls == 0
    assert device.monitor.events == [("unconfigured", False)] * 2
    assert disabled.monitor.events == [("unconfigured", True)]
    assert [r.getMessage() for r in caplog.records if r.name == "pengumon.devices"] == [
        "router is not polled: no password", "router is not polled: disabled in configuration"]

    device.set_credentials("admin", "secret")  # credentials arrive: the next poll goes to the device
    await device.poll(force=True)
    assert driver.polls == 1 and driver.credentials == ("admin", "secret")


async def test_run_polls_until_cancelled_and_survives_a_crashing_monitor(telemetry, caplog):
    class Crashing(FakeMonitor):
        async def on_result(self, ts, result):
            self.events.append("crash")
            raise RuntimeError("monitor bug")

    device = Device("router", FakeDriver(), Crashing(), interval_seconds=0.01)
    with caplog.at_level(logging.ERROR, logger="pengumon.devices"):
        task = asyncio.create_task(device.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(device.monitor.events) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(device.monitor.events) >= 2
    assert any(r.getMessage() == "poll crashed" and r.exc_info for r in caplog.records)


async def test_aclose_closes_the_driver():
    driver = FakeDriver()
    await _device(driver).aclose()
    assert driver.closed


# --- monitors ---------------------------------------------------------------------------------------------

class Repo:
    """The repository's two sample inserts, keeping what they were given (the stored forms, per device id)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.lines, self.router = [], []

    async def insert_line_sample(self, s):
        if self.fail:
            raise OSError("disk full")
        self.lines.append(s)

    async def insert_device_sample(self, s):
        if self.fail:
            raise OSError("disk full")
        self.router.append(s)


def _channels(uncorrectables=0):
    return (
        DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0, snr_db=38.0,
                      uncorrectables=uncorrectables),
        DocsisChannel(channel_id=9, direction="upstream", frequency_mhz=30.0, power_dbmv=42.0),
    )


async def test_line_monitor_builds_and_stores_snapshots_with_error_deltas():
    repo = Repo()
    monitor = LineMonitor(repo, messages=MESSAGES, device_id="modem")
    first = await monitor.on_result(1000.0, PollResult({Capability.ACCESS_LINE: LineReading("docsis", _channels(10))}))
    assert first.status == "healthy" and first.uncorrectables_recent == 0 and first.downstream_snr_avg == 38.0
    second = await monitor.on_result(1900.0, PollResult({Capability.ACCESS_LINE: LineReading("docsis", _channels(40))}))
    # 30 new codewords in 15 min, nothing before: a short burst is information, not a warning
    assert second.uncorrectables_recent == 30 and second.status == "healthy" and "short burst" in second.errors_note
    assert repo.lines == [line_sample("modem", first), line_sample("modem", second)]
    assert monitor.latest_snapshot is second


async def test_line_monitor_error_messages_and_storage():
    repo = Repo()
    monitor = LineMonitor(repo, messages=MESSAGES, device_id="modem")
    transport = await monitor.on_error(1.0, DriverError("timeout", "timed out", transport=True))
    assert transport.error_message == "Failed to communicate with gateway: timed out"
    auth = await monitor.on_error(2.0, DriverError("auth", "Authentication failed on gateway (192.168.100.1)."))
    assert auth.error_message == "Authentication failed on gateway (192.168.100.1)." and auth.status == "error"
    missing = await monitor.on_result(3.0, PollResult(readings={}, missing={Capability.ACCESS_LINE: "no table"}))
    assert missing.status == "error" and missing.error_message == "no table"
    assert len(repo.lines) == 3 and {s.device_id for s in repo.lines} == {"modem"}
    assert monitor.on_unconfigured(True).error_message == "off in config"
    assert monitor.on_unconfigured(False).error_message == "no password yet"
    assert len(repo.lines) == 3  # "not configured" is not stored


async def test_router_monitor_reports_unmeasured_rates_as_none():
    repo = Repo()
    monitor = RouterMonitor(repo, messages=MESSAGES, model="Example_Router", wan=WanRates(clock=lambda: 1020.0),
                            device_id="router")
    reading = {Capability.WAN_COUNTERS: WanCounters(0, 0), Capability.ROUTER_SYSTEM: SystemReading(50.0, 60.0, 2)}
    first = await monitor.on_result(1000.0, PollResult(reading))
    assert first.wan_rx_rate_mbps is None and first.wan_tx_rate_mbps is None and first.wan_rx_bytes == 0
    assert (first.cpu_usage_pct, first.mem_usage_pct, first.client_count) == (50.0, 60.0, 2)
    assert first.model_name == "Example_Router" and first.status == "healthy"
    second = await monitor.on_result(1010.0, PollResult({
        Capability.WAN_COUNTERS: WanCounters(12_500_000, 1_250_000), Capability.ROUTER_SYSTEM: SystemReading(95.0)}))
    assert (second.wan_rx_rate_mbps, second.wan_tx_rate_mbps) == (10.0, 1.0) and second.status == "warning"
    no_counters = await monitor.on_result(1020.0, PollResult({Capability.ROUTER_SYSTEM: SystemReading(5.0)}))
    assert no_counters.wan_rx_bytes is None and no_counters.wan_rx_rate_mbps is None
    assert [r[1] for r in monitor.recent_rates()] == [10.0]
    assert repo.router == [device_sample("router", s) for s in (first, second, no_counters)]


async def test_router_monitor_error_message():
    monitor = RouterMonitor(None, messages=MESSAGES)
    snap = await monitor.on_error(5.0, DriverError("unreachable", "All connection attempts failed", transport=True))
    assert snap.error_message == "Failed to communicate with router: All connection attempts failed"
    assert snap.status == "error" and snap.timestamp == 5.0


async def test_storage_failures_never_become_device_errors(caplog):
    monitor = RouterMonitor(Repo(fail=True), messages=MESSAGES, device_id="router")
    with caplog.at_level(logging.WARNING, logger="pengumon.devices"):
        for ts in (1.0, 2.0, 3.0):
            snap = await monitor.on_result(ts, PollResult({Capability.ROUTER_SYSTEM: SystemReading(5.0)}))
    assert snap.status == "healthy" and monitor.latest_snapshot is snap
    assert [r.getMessage() for r in caplog.records] == ["Storing router snapshots failing: OSError: disk full"]


def test_a_monitor_that_stores_needs_its_device_id():
    for monitor in (LineMonitor, RouterMonitor):
        with pytest.raises(ValueError, match="needs the device id"):
            monitor(Repo(), messages=MESSAGES)
        monitor(None, messages=MESSAGES)  # nothing to store, no id needed


# --- the runner under concurrency and repeated failures ---------------------------------------------------------------

async def test_concurrent_forced_polls_never_overlap_inside_the_driver(telemetry):
    class Gated(FakeDriver):
        def __init__(self):
            super().__init__()
            self.gate = asyncio.Event()
            self.in_flight = self.max_in_flight = 0

        async def poll(self):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await self.gate.wait()
                return await super().poll()
            finally:
                self.in_flight -= 1

    driver = Gated()
    device = _device(driver, min_interval_seconds=600.0)
    first = asyncio.create_task(device.poll(force=True))
    second = asyncio.create_task(device.poll(force=True))
    for _ in range(10):
        await asyncio.sleep(0)  # both are started; one is inside the driver, the other waits for it
    assert driver.in_flight == 1
    driver.gate.set()
    await asyncio.gather(first, second)
    assert driver.polls == 1 and driver.max_in_flight == 1  # the second caller gets the first one's reading


class RestoringMonitor(FakeMonitor):
    """A monitor that can take a stored reading back (as LineMonitor does), taken at `stored_ts`."""

    def __init__(self, stored_ts):
        super().__init__()
        self.stored_ts = stored_ts

    async def restore(self, oldest_ts):
        if self.stored_ts < oldest_ts:
            return None
        self.latest_snapshot = ("restored", self.stored_ts)
        return self.stored_ts


async def _run_briefly(device, seconds=0.1):
    task = asyncio.create_task(device.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_burst_of_forced_polls_costs_the_device_one_read(telemetry):
    driver = FakeDriver()
    device = _device(driver, min_interval_seconds=600.0)
    results = await asyncio.gather(*(device.poll(force=True) for _ in range(10)))
    assert driver.polls == 1 and len(set(results)) == 1


async def test_the_minimum_interval_holds_for_forced_polls_until_it_has_passed(telemetry):
    clock = Clock()
    driver = FakeDriver()
    device = _device(driver, clock=clock, min_interval_seconds=600.0)
    await device.poll(force=True)
    clock.now += 599
    await device.poll(force=True)
    assert driver.polls == 1
    clock.now += 2
    await device.poll(force=True)
    assert driver.polls == 2


async def test_the_credentials_test_skips_the_minimum_interval_but_not_a_minute(telemetry):
    clock = Clock()
    driver = FakeDriver()
    device = _device(driver, clock=clock, min_interval_seconds=600.0)
    await device.poll(force=True)
    clock.now += 30
    await device.poll(force=True, bypass_gate=True)
    assert driver.polls == 1  # a typo-and-retry within a minute is not a second login
    clock.now += 31
    await device.poll(force=True, bypass_gate=True)
    assert driver.polls == 2
    await device.poll(force=True)  # an ordinary caller is still held back by the full interval
    assert driver.polls == 2


async def test_without_a_minimum_interval_forced_polls_all_go_through(telemetry):
    driver = FakeDriver()
    device = _device(driver)
    await asyncio.gather(*(device.poll(force=True) for _ in range(3)))
    assert driver.polls == 3


async def test_a_reading_younger_than_the_interval_is_not_read_again_at_start(telemetry):
    driver = FakeDriver()
    device = Device("modem", driver, RestoringMonitor(time.time() - 10), interval_seconds=60.0)
    await _run_briefly(device)
    assert driver.polls == 0 and device.latest_snapshot[0] == "restored"


async def test_an_older_reading_is_read_again_at_start(telemetry):
    driver = FakeDriver()
    monitor = RestoringMonitor(time.time() - 100)
    device = Device("modem", driver, monitor, interval_seconds=60.0)
    await _run_briefly(device)
    assert driver.polls == 1 and device.latest_snapshot[0] == "ok"


async def test_a_device_that_cannot_restore_is_polled_at_start(telemetry):
    driver = FakeDriver()
    await _run_briefly(Device("router", driver, FakeMonitor(), interval_seconds=60.0))
    assert driver.polls == 1


async def test_the_wait_doubles_with_each_failure_up_to_an_hour_and_resets(telemetry):
    device = _device(FakeDriver([httpx.ConnectTimeout("t")] * 9 + [GOOD]), min_interval_seconds=5.0)
    waits = []
    for _ in range(9):
        await device.poll(force=True)
        waits.append(device._wait())
        device._last_attempt = 0.0
    assert waits == [20.0, 40.0, 80.0, 160.0, 320.0, 640.0, 1280.0, 2560.0, 3600.0]
    await device.poll(force=True)
    assert device._wait() == 10.0


async def test_a_device_without_a_minimum_interval_keeps_its_pace_after_failures(telemetry):
    device = _device(FakeDriver([httpx.ConnectTimeout("t")] * 3))
    for _ in range(3):
        await device.poll(force=True)
    assert device._wait() == 10.0


async def test_a_refused_login_stops_the_schedule_until_the_credentials_change(telemetry, caplog):
    driver = FakeDriver([DriverError("auth", "wrong password")])
    device = Device("modem", driver, FakeMonitor(), interval_seconds=0.02, min_interval_seconds=0.01)
    with caplog.at_level(logging.WARNING, logger="pengumon.devices"):
        task = asyncio.create_task(device.run())
        await asyncio.sleep(0.2)
        assert driver.polls == 1  # several intervals passed, no second attempt
        device.set_credentials("admin", "better")
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert driver.polls > 1 and driver.credentials == ("admin", "better")
    stopped = [r for r in caplog.records if "refused the login" in r.getMessage()]
    assert len(stopped) == 1 and "until its credentials are changed" in stopped[0].getMessage()


async def test_a_device_without_a_minimum_interval_keeps_trying_after_a_refused_login(telemetry, caplog):
    # A router reports a passing lockout like a wrong password: it keeps its schedule instead of going quiet
    driver = FakeDriver([DriverError("auth", "locked out")] * 3)
    device = Device("router", driver, FakeMonitor(), interval_seconds=0.02)
    with caplog.at_level(logging.WARNING, logger="pengumon.devices"):
        task = asyncio.create_task(device.run())
        await asyncio.sleep(0.45)  # run() waits at least 0.1 s between polls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert driver.polls >= 3
    assert not [r for r in caplog.records if "refused the login" in r.getMessage()]


async def test_current_waits_for_a_poll_in_flight_and_never_starts_one(telemetry):
    class Gated(FakeDriver):
        gate = None

        async def poll(self):
            await self.gate.wait()
            return await super().poll()

    driver = Gated()
    driver.gate = asyncio.Event()
    device = _device(driver)
    pending = await device.current()  # nothing yet and nothing in flight: no read is started
    assert driver.polls == 0 and pending is None  # (FakeMonitor has no waiting snapshot)
    poll = asyncio.create_task(device.poll(force=True))
    await asyncio.sleep(0)
    waiting = asyncio.create_task(device.current())
    await asyncio.sleep(0)
    assert not waiting.done()
    driver.gate.set()
    assert await waiting == await poll and driver.polls == 1


class StoredLines:
    def __init__(self, sample):
        self.sample = sample

    async def insert_line_sample(self, s):
        pass

    async def get_latest_line_sample(self, device_id):
        return self.sample


async def test_a_line_monitor_restores_its_newest_stored_reading_and_the_error_baseline():
    channel = DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0, snr_db=38.0,
                            modulation="QAM256", correcteds=0, uncorrectables=40)
    stored = line_sample("modem", summarize_one(channel, ts=1000.0))
    monitor = LineMonitor(StoredLines(stored), messages=MESSAGES, device_id="modem")
    assert await monitor.restore(oldest_ts=1001.0) is None and monitor.latest_snapshot is None  # too old
    assert await monitor.restore(oldest_ts=900.0) == 1000.0
    assert monitor.latest_snapshot.status == "healthy" and monitor.latest_snapshot.uncorrectables_total == 40
    # The first poll after the restart has a delta against the stored reading, not a fresh baseline.
    channel_now = channel.model_copy(update={"uncorrectables": 47})
    snapshot = await monitor.on_result(1600.0, PollResult(readings={
        Capability.ACCESS_LINE: LineReading(technology="docsis", channels=(channel_now,))}))
    assert snapshot.uncorrectables_recent == 7
    assert await LineMonitor(None, messages=MESSAGES).restore(0.0) is None


def summarize_one(channel, ts):
    from pengumon.lines.docsis import summarize
    snapshot = summarize([channel], ts)
    snapshot.uncorrectables_recent = 0
    return snapshot


async def test_the_traceback_of_an_unexpected_exception_is_logged_only_when_the_state_log_logs(telemetry, caplog):
    clock = Clock()
    device = _device(FakeDriver([KeyError("netdev")] * 5), clock=clock)

    def tracebacks():
        return [r for r in caplog.records if r.getMessage() == "driver raised an unexpected exception"
                and r.levelno >= logging.ERROR and r.exc_info]

    with caplog.at_level(logging.DEBUG, logger="pengumon.devices"):
        await device.poll(force=True)
        assert len(tracebacks()) == 1  # the first failure
        for _ in range(3):
            clock.now += 10
            await device.poll(force=True)
        assert len(tracebacks()) == 1  # repeats within 15 minutes: no new traceback at ERROR
        repeats = [r for r in caplog.records if r.getMessage() == "driver raised an unexpected exception"][1:]
        assert [r.levelno for r in repeats] == [logging.DEBUG] * 3 and not any(r.exc_info for r in repeats)
        clock.now += 900  # the state log reminds again
        await device.poll(force=True)
        assert len(tracebacks()) == 2


async def test_alternating_errors_of_one_kind_produce_one_warning(telemetry, caplog):
    clock = Clock()
    outcomes = [httpx.ConnectTimeout("connect timed out"), httpx.ReadTimeout("read timed out")] * 3
    device = _device(FakeDriver(outcomes), clock=clock)
    with caplog.at_level(logging.DEBUG, logger="pengumon.devices"):
        for _ in range(6):
            await device.poll(force=True)
            clock.now += 10
    assert [e[1] for e in device.monitor.events] == ["timeout"] * 6
    warnings = [r for r in caplog.records if r.name == "pengumon.devices" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "Polling router (fake at 192.0.2.1) failing: timeout: connect timed out"


# --- monitors: edge cases ---------------------------------------------------------------------------------------------

async def test_line_monitor_baselines_against_the_last_good_reading_across_an_error():
    monitor = LineMonitor(None, messages=MESSAGES)

    async def good(ts, uncorrectables):
        return await monitor.on_result(ts, PollResult({Capability.ACCESS_LINE: LineReading("docsis", _channels(uncorrectables))}))

    await good(1000.0, 100)
    error = await monitor.on_error(1010.0, DriverError("timeout", "timed out", transport=True))
    assert error.status == "error" and error.error_kind == "timeout"
    after = await good(1020.0, 130)
    assert after.uncorrectables_recent == 30 and after.channels[0].uncorrectables_recent == 30  # not 0, not 130


async def test_router_monitor_without_any_reading_names_the_missing_reasons():
    monitor = RouterMonitor(None, messages=MESSAGES, model="Example-Mesh")
    snap = await monitor.on_result(5.0, PollResult(readings={}, missing={
        Capability.WAN_COUNTERS: "no WAN counters in the answer", Capability.ROUTER_SYSTEM: "no CPU data"}))
    assert snap.status == "error" and snap.error_kind == "protocol" and snap.model_name == "Example-Mesh"
    assert "no WAN counters in the answer" in snap.error_message and "no CPU data" in snap.error_message
    assert monitor.latest_snapshot is snap

    nothing = await monitor.on_result(6.0, PollResult(readings={}))
    assert nothing.status == "error" and nothing.error_message


async def test_line_monitor_refuses_a_technology_it_has_no_rules_for():
    repo = Repo()
    monitor = LineMonitor(repo, messages=MESSAGES, device_id="modem")
    reading = LineReading("gpon", _channels())  # the Literal hint is not enforced at runtime
    snap = await monitor.on_result(1.0, PollResult({Capability.ACCESS_LINE: reading}))
    assert snap.status == "error" and snap.error_kind == "unsupported"
    assert "gpon" in snap.error_message and snap.channels == []
    assert repo.lines == [line_sample("modem", snap)]


async def test_summarizing_does_not_change_the_readings_channels():
    monitor = LineMonitor(None, messages=MESSAGES)
    first = LineReading("docsis", _channels(10))
    second = LineReading("docsis", _channels(40))
    await monitor.on_result(1000.0, PollResult({Capability.ACCESS_LINE: first}))
    snap = await monitor.on_result(1010.0, PollResult({Capability.ACCESS_LINE: second}))
    assert snap.channels[0].uncorrectables_recent == 30
    assert [c.uncorrectables_recent for c in second.channels] == [0, 0]  # the reading stays as it was read
    assert all(a is not b for a, b in zip(snap.channels, second.channels))
