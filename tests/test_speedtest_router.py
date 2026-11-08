"""The line's rate during a speed test, from the router (speedtest/router.py, Device.read_wan_counters): the peak maths
on made-up counter readings, the sampler around a phase, and the device's light read behind the poll lock."""

import asyncio

import pytest

from pengumon.capabilities import Capability, PollResult, WanCounters
from pengumon.devices.device import Device
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.wan import WanReading
from pengumon.speedtest import router as router_module
from pengumon.speedtest.router import RouterSampler, peak_mbps

from device_helpers import Clock

MB = 1_000_000


def readings(rates_mbps, start=0.0, step=1.0, rx0=0, tx=0, bits=None, at=None):
    """Counter readings `step` s apart whose rx grows at each listed rate (Mbps) between one reading and the next."""
    out, rx, t = [WanReading(start, WanCounters(rx0, tx, counter_bits=bits))], rx0, start
    for i, rate in enumerate(rates_mbps):
        dt = at[i] if at else step
        t += dt
        rx += int(rate * 1e6 / 8 * dt)
        out.append(WanReading(t, WanCounters(rx % (1 << bits) if bits else rx, tx, counter_bits=bits)))
    return out


# --- the peak maths --------------------------------------------------------------------------------------------------

def test_a_steady_rate_is_read_exactly_in_both_directions():
    samples = [WanReading(float(k), WanCounters(rx_bytes=10**12 + 112_500_000 * k, tx_bytes=5_000_000 * k))
               for k in range(9)]
    assert peak_mbps(samples, rx=True) == 900.0
    assert peak_mbps(samples, rx=False) == 40.0


def test_the_peak_is_the_best_three_seconds_not_the_average_and_not_one_second():
    # the ramp-up, a full line for 4 s at 940, one second of 990, then a drop as the test ends
    samples = readings([200, 600, 940, 940, 940, 990, 940, 300, 50])
    assert peak_mbps(samples, rx=True) == pytest.approx((940 + 990 + 940) / 3, abs=0.1)
    assert peak_mbps(samples, rx=True, window_s=1.0) == 990.0


def test_a_32_bit_counter_that_wraps_mid_test_is_followed():
    start = (1 << 32) - 300 * MB                                       # wraps after about 2.6 s at 900 Mbps
    samples = readings([900] * 6, rx0=start, bits=32)
    assert any(s.counters.rx_bytes < start for s in samples)            # (it did wrap)
    assert peak_mbps(samples, rx=True) == 900.0
    # across the wrap: 600 then 900 Mbps, 1.5 s each, wrapping in the second span; the 3 s window covers both
    across = readings([600, 900], step=1.5, rx0=(1 << 32) - 150 * MB, bits=32)
    assert across[2].counters.rx_bytes < across[1].counters.rx_bytes
    assert peak_mbps(across, rx=True) == 750.0                          # (read as a reset it would be 600)


def test_a_counter_reset_starts_again_and_never_reads_as_traffic():
    before = readings([900] * 4)
    after = [WanReading(5.0 + k, WanCounters(1_000 + 112_500_000 * k, 0)) for k in range(5)]   # router restarted
    assert peak_mbps(before + after, rx=True) == 900.0
    huge = readings([900] * 4) + [WanReading(5.0, WanCounters(10**15, 0))]   # a jump no line makes: a reset too
    assert peak_mbps(huge, rx=True) == 900.0


def test_a_failed_read_is_a_gap_the_next_one_covers():
    samples = readings([900] * 8)
    del samples[3], samples[5]                                         # two reads that failed
    assert peak_mbps(samples, rx=True) == 900.0


def test_counters_the_router_refreshes_every_two_seconds_are_measured_over_a_longer_window():
    # the true rate: 900 Mbps; the router updates its counter only every 2 s, so every other read repeats a value
    true = readings([900] * 12)
    lumpy = [WanReading(s.at, true[k - k % 2].counters) for k, s in enumerate(true)]
    assert lumpy[3].counters == lumpy[2].counters
    # a window between two changes, at least 2.5 of their 2 s steps: exact here (the reads see each change at once)
    assert peak_mbps(lumpy, rx=True) == 900.0
    # the naive best 3 s of the raw readings would read 1200 (4 s of counted traffic in 3 s)
    raw = max((lumpy[i + 3].counters.rx_bytes - lumpy[i].counters.rx_bytes) * 8 / 3 / 1e6 for i in range(len(lumpy) - 3))
    assert raw == pytest.approx(1200.0)


def test_a_phase_shorter_than_the_window_gives_its_whole_span_and_too_few_readings_give_none():
    assert peak_mbps(readings([800, 800]), rx=True) == 800.0          # 2 s: all of it
    assert peak_mbps(readings([800], step=0.5), rx=True) is None       # under MIN_SPAN_S
    assert peak_mbps(readings([]), rx=True) is None
    assert peak_mbps([], rx=True) is None


def test_readings_with_times_that_do_not_advance_are_not_compared():
    samples = readings([900] * 5)
    samples.insert(2, WanReading(samples[1].at, WanCounters(samples[1].counters.rx_bytes + 10**9, 0)))
    assert peak_mbps(samples, rx=True) == 900.0


# --- the sampler ---------------------------------------------------------------------------------------------------

class Router:
    """A light counter read: the k-th read at k s (its own clock), rx at 100 MB/s, tx at 5 MB/s; `fail` reads fail."""

    def __init__(self, fail=()):
        self.reads = 0
        self.fail = set(fail)

    async def __call__(self):
        k = self.reads
        self.reads += 1
        if k in self.fail:
            raise OSError("no answer")
        return WanReading(float(k), WanCounters(rx_bytes=100 * MB * k, tx_bytes=5 * MB * k))


async def test_the_sampler_reads_through_the_phase_and_once_more_at_its_end():
    router = Router(fail={2})
    async with RouterSampler(router, interval_s=0.001) as sampler:
        while router.reads < 8:
            await asyncio.sleep(0.001)
        reads = router.reads
    assert router.reads == reads + 1                                    # the last read, after the phase
    assert [s.at for s in sampler.samples][:3] == [0.0, 1.0, 3.0]       # the failed read is a gap
    assert sampler.peak(rx=True) == 800.0 and sampler.peak(rx=False) == 40.0


async def test_without_a_router_the_sampler_does_nothing():
    async with RouterSampler(None) as sampler:
        await asyncio.sleep(0)
    assert sampler.samples == [] and sampler.peak(rx=True) is None


async def test_a_phase_that_raises_takes_no_last_reading():
    router = Router()
    with pytest.raises(RuntimeError):
        async with RouterSampler(router, interval_s=0.001):
            while router.reads < 2:
                await asyncio.sleep(0.001)
            reads = router.reads
            raise RuntimeError("stopped")
    assert router.reads <= reads + 1                                    # (at most the read that was under way)


async def test_a_read_under_way_when_the_phase_ends_is_let_finish(monkeypatch):
    finished = []

    async def slow():
        await asyncio.sleep(0.05)
        finished.append(True)
        return WanReading(float(len(finished)), WanCounters(len(finished) * MB, 0))

    async with RouterSampler(slow, interval_s=0.001):
        await asyncio.sleep(0.01)                                       # the first read is under way
    assert len(finished) == 2                                           # it finished, then the last one


# --- the device's light read -----------------------------------------------------------------------------------------

class CounterDriver:
    name = "fake"
    offers = frozenset({Capability.WAN_COUNTERS})
    host = "192.0.2.1"

    def __init__(self, password="pw"):
        self.password = password
        self.log = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail = False

    def missing_configuration(self):
        return None if self.password else "no password"

    def set_credentials(self, username, password):
        self.password = password

    async def poll(self):
        self.log.append("poll start")
        await self.gate.wait()
        self.log.append("poll end")
        return PollResult(readings={})

    async def read_wan_counters(self):
        self.log.append("read")
        if self.fail:
            raise ConnectionError("reset")
        return WanCounters(123, 45)

    async def aclose(self):
        pass


class Monitor:
    latest_snapshot = None

    def on_unconfigured(self, disabled):
        return None

    async def on_error(self, ts, error):
        return None

    async def on_result(self, ts, result):
        return None


def device(driver, **kwargs):
    clock = Clock()
    return Device("router", driver, Monitor(), interval_seconds=10.0, clock=clock, monotonic=clock, **kwargs), clock


async def test_a_read_waits_for_a_poll_under_way_and_never_runs_inside_it():
    driver = CounterDriver()
    dev, clock = device(driver)
    driver.gate.clear()
    poll = asyncio.create_task(dev.poll(force=True))
    for _ in range(5):
        await asyncio.sleep(0)
    read = asyncio.create_task(dev.read_wan_counters())
    for _ in range(5):
        await asyncio.sleep(0)
    assert driver.log == ["poll start"]                                 # the read waits for the lock
    driver.gate.set()
    await poll
    reading = await read
    assert driver.log == ["poll start", "poll end", "read"]
    assert reading.counters == WanCounters(123, 45) and reading.at == clock.now


async def test_a_device_that_cannot_be_read_now_gives_none():
    driver = CounterDriver()
    driver.fail = True
    assert await device(driver)[0].read_wan_counters() is None          # a failed read
    assert await device(CounterDriver(), enabled=False)[0].read_wan_counters() is None
    assert await device(CounterDriver(password=None))[0].read_wan_counters() is None
    assert await device(CounterDriver(), min_interval_seconds=60.0)[0].read_wan_counters() is None  # easily upset
    stopped, _ = device(CounterDriver())
    stopped._auth_stopped = True                                         # a refused login: no reads until new ones
    assert await stopped.read_wan_counters() is None

    class Plain(CounterDriver):
        read_wan_counters = None
    assert await device(Plain())[0].read_wan_counters() is None


async def test_a_refused_login_stops_the_counter_reads_until_a_poll_works_or_the_credentials_change():
    """A wrong password (or a router locking logins out) cost one login attempt per read: about one a second through a
    test, against a router that may lock its admin logins after a few. Polls carry on as before (a passing lockout must
    not silence a router for good); the test's reads wait for one that works, or for new credentials."""
    import httpx
    from pengumon.drivers.asuswrt import AsusWrtDriver

    logins = []

    def refusing(request):
        if request.url.path.endswith("login.cgi"):
            logins.append(request)
            return httpx.Response(200, json={"error_status": "3"})     # refused (a lockout reads the same)
        return httpx.Response(200, json={"error_status": "2"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(refusing))
    asus = AsusWrtDriver("http://192.0.2.1", "u", "wrong", client=client, clock=Clock())
    dev, _ = device(asus)
    for _ in range(5):                                                  # five seconds of a test
        assert await dev.read_wan_counters() is None
    assert len(logins) == 1
    await dev.poll(force=True)                                          # the polls still try, on their own schedule
    assert len(logins) == 2
    assert await dev.read_wan_counters() is None and len(logins) == 2

    driver = CounterDriver()                                            # a poll that works lifts it
    dev, _ = device(driver)
    dev._login_refused = True
    assert await dev.read_wan_counters() is None and "read" not in driver.log
    await dev.poll(force=True)
    assert (await dev.read_wan_counters()).counters == WanCounters(123, 45)
    dev._login_refused = True                                           # so do new credentials
    dev.set_credentials("u", "new")
    assert (await dev.read_wan_counters()).counters == WanCounters(123, 45)

async def test_the_registry_reads_the_wan_device_and_without_one_gives_none():
    empty = DeviceRegistry([])
    assert await empty.read_wan_counters() is None
    driver = CounterDriver()
    registry = DeviceRegistry([device(driver)[0]])
    assert (await registry.read_wan_counters()).counters == WanCounters(123, 45)


def test_the_window_constants():
    assert router_module.SAMPLE_S == 1.0 and router_module.PEAK_WINDOW_S == 3.0
