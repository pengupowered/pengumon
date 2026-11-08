"""The speed test runner (speedtest/runner.py): the schedule, the rules before a scheduled test, the manual run, and the
record of one test. A fake provider and a fake clock; the repository is a real one on a temporary database."""

import asyncio
from datetime import datetime, timezone

import pytest

from pengumon.capabilities import WanCounters
from pengumon.config import AppConfig
from pengumon.devices.wan import WanReading
from pengumon.db.speedtests import SPEED_TEST_COLUMNS
from pengumon.engine.load import LoadReading
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.speedtest import runner as runner_module
from pengumon.speedtest.mask import SpeedMask, mask_tail_seconds
from pengumon.speedtest.providers.base import ProviderError, Transfer
from pengumon.speedtest.runner import OFFSET_KEY, SpeedTestRunner
from pengumon.speedtest.schedule import month_bounds, slot_after, spread_offset, due_slot, open_at
from pengumon.timeofday import zone

DAY = 86400.0
MIDNIGHT = datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp()   # a UTC midnight, to count the day from
OFFSET = 3600 * 10                                                    # slots at 10:00 UTC with a 24 h interval


class Clock:
    def __init__(self, now=MIDNIGHT + 1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeProvider:
    """Scripted: download/upload return a Transfer, raise a ProviderError (or any exception), and advance the clock."""

    instances = []

    def __init__(self, clock, down=None, up=None, seconds=(4.0, 3.0), server="TST"):
        self.clock, self.down, self.up, self.seconds, self.server = clock, down, up, seconds, server
        self.closed = False
        self.calls = []
        FakeProvider.instances.append(self)

    async def download(self, cap, limit, target_s=None):
        self.calls.append(("download", cap, limit, target_s))
        self.clock.now += self.seconds[0]
        return self._answer(self.down, Transfer(cap, self.seconds[0], 94.4, self.server))

    async def upload(self, cap, limit, target_s=None):
        self.calls.append(("upload", cap, limit, target_s))
        self.clock.now += self.seconds[1]
        return self._answer(self.up, Transfer(cap, self.seconds[1], 21.5, self.server))

    @staticmethod
    def _answer(scripted, default):
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted or default

    async def aclose(self):
        self.closed = True


class Line:
    """A LineLoad stand-in: the load now (None: unknown) and the router's WAN rates."""

    def __init__(self, pct=None, rates=()):
        self.pct, self.rates = pct, list(rates)
        self.rates_source = lambda: list(self.rates)

    def current(self, now=None):
        if self.pct is None:
            return None
        return LoadReading(pct=self.pct, rx_mbps=1.0, tx_mbps=1.0, down_mbps=100.0, up_mbps=100.0,
                           down_source="configured", up_source="configured")


def make_config(**speedtest):
    config = AppConfig(speedtest={"enabled": True, **speedtest}, general={"timezone": "UTC"})
    config.targets = [TargetConfig(id="pub", name="Public", category="public", address="203.0.113.1"),
                      TargetConfig(id="gw", name="Gateway", category="gateway", address="192.0.2.1")]
    return config


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def _no_instances():
    FakeProvider.instances = []


def build(repo, clock, config=None, line=None, offset=OFFSET, **provider_kw):
    config = config or make_config()
    runner = SpeedTestRunner(config, repo, line_load=line, clock=clock, offset_source=lambda: offset,
                             provider_factory=lambda cfg: FakeProvider(clock, **provider_kw))
    return runner, config


async def tests_in(repo):
    return await repo.get_speed_tests(0, 10**12, 100)


async def settle(runner):
    await asyncio.gather(*list(runner._tasks))


# --- the schedule: pure functions --------------------------------------------------------------------------------

def test_slots_are_counted_from_the_epoch_and_shifted_by_the_offset():
    assert slot_after(MIDNIGHT, DAY, OFFSET) == MIDNIGHT + OFFSET
    assert slot_after(MIDNIGHT + OFFSET, DAY, OFFSET) == MIDNIGHT + OFFSET + DAY      # strictly after
    assert slot_after(MIDNIGHT + 100, 6 * 3600, 1800) == MIDNIGHT + 1800


def test_the_offset_keeps_its_time_of_day_and_spreads_installs_over_the_days_of_a_long_interval():
    assert spread_offset(OFFSET, DAY) == OFFSET                              # daily: as before
    assert spread_offset(OFFSET, 6 * 3600) == OFFSET % (6 * 3600)
    assert spread_offset(OFFSET, 36 * 3600) == OFFSET                        # under two days: modulo the interval
    week = 7 * DAY
    for raw in (0, 1, OFFSET, 86399):
        offset = spread_offset(raw, week)
        assert offset % DAY == raw and 0 <= offset < week and offset // DAY == raw % 7
    days = {spread_offset(raw, week) // DAY for raw in range(0, 86400, 997)}
    assert days == set(range(7))                                             # every weekday gets installs
    assert {spread_offset(raw, 30 * DAY) // DAY for raw in range(0, 86400, 97)} == set(range(30))


def test_a_slot_long_past_is_not_caught_up_on():
    assert due_slot(MIDNIGHT, MIDNIGHT + 3600, DAY, OFFSET) == MIDNIGHT + OFFSET                # not yet
    assert due_slot(MIDNIGHT, MIDNIGHT + OFFSET + 3600, DAY, OFFSET) == MIDNIGHT + OFFSET       # an hour late: still it
    assert due_slot(MIDNIGHT, MIDNIGHT + 5 * DAY + 3 * 3600, DAY, OFFSET) == MIDNIGHT + 4 * DAY + OFFSET  # days off: the current


def test_the_window_may_cross_midnight():
    tz = zone("UTC")
    at = MIDNIGHT + 12 * 3600
    assert open_at(at, "22:00", "02:00", tz) == MIDNIGHT + 22 * 3600                  # outside: waits for it to open
    assert open_at(MIDNIGHT + 23 * 3600, "22:00", "02:00", tz) == MIDNIGHT + 23 * 3600
    assert open_at(MIDNIGHT + DAY + 3600, "22:00", "02:00", tz) == MIDNIGHT + DAY + 3600     # after midnight, inside
    assert open_at(MIDNIGHT + DAY + 3 * 3600, "22:00", "02:00", tz) == MIDNIGHT + DAY + 22 * 3600
    assert open_at(at, None, None, tz) == at


def test_months_are_calendar_months_in_the_zone():
    start, end = month_bounds(datetime(2026, 3, 31, 23, 30, tzinfo=timezone.utc).timestamp(), zone("UTC"))
    assert (start, end) == (datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp(),
                            datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp())
    # 23:30 on March 31 UTC is April 1 in Berlin (UTC+2 from the 29th)
    start, _ = month_bounds(datetime(2026, 3, 31, 23, 30, tzinfo=timezone.utc).timestamp(), zone("Europe/Berlin"))
    assert start == datetime(2026, 3, 31, 22, 0, tzinfo=timezone.utc).timestamp()
    assert month_bounds(datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp(), zone("UTC"))[1] == \
        datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()


# --- the schedule: the runner ------------------------------------------------------------------------------------

async def test_the_offset_is_created_once_and_kept(repo, clock):
    runner, _ = build(repo, clock, offset=12345)
    await runner.next_run()
    assert await repo.get_meta(OFFSET_KEY) is None                  # a read (GET /api/speedtests) stores nothing
    await runner.tick()                                             # the scheduler does
    assert await repo.get_meta(OFFSET_KEY) == "12345"
    other = SpeedTestRunner(make_config(), repo, clock=clock, offset_source=lambda: 99)      # a restart
    await other.next_run()
    await other.tick()
    assert await repo.get_meta(OFFSET_KEY) == "12345"


async def test_a_read_answers_with_the_offset_the_scheduler_then_stores(repo, clock):
    runner, _ = build(repo, clock, offset=12345)
    first = await runner.next_run()
    assert await repo.get_meta(OFFSET_KEY) is None and await runner.next_run() == first    # (drawn once, not per read)
    await runner.ensure_offset()                                                           # what run() does at its start
    assert await repo.get_meta(OFFSET_KEY) == "12345" and await runner.next_run() == first


async def test_many_busy_skips_after_the_last_test_do_not_move_the_schedule(repo, clock):
    await repo.insert_speed_test({"ts": MIDNIGHT - DAY + OFFSET + 7, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 7.0})
    for i in range(25):                                              # more than _reference ever looked at
        await repo.insert_speed_test({"ts": MIDNIGHT - DAY + OFFSET + 100 + i, "trigger": "scheduled",
                                      "provider": "cloudflare", "status": "skipped", "reason": "line busy (50% load)"})
    clock.now = MIDNIGHT + OFFSET + 5                               # a restart after today's slot
    runner, _ = build(repo, clock)
    assert await runner.next_run() == MIDNIGHT + OFFSET             # the slot after the real test: due, not tomorrow's


async def test_a_new_install_waits_for_the_first_slot_after_it_started(repo, clock):
    runner, _ = build(repo, clock)
    assert await runner.next_run() == MIDNIGHT + OFFSET
    clock.now = MIDNIGHT + 1000.0 + 120
    await runner.tick()
    assert FakeProvider.instances == []                                  # nothing now "because we just started"
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    assert len(FakeProvider.instances) == 1
    (row,) = await tests_in(repo)
    assert row["trigger"] == "scheduled" and row["status"] == "ok"


async def test_a_restart_never_causes_an_extra_test(repo, clock):
    first, _ = build(repo, clock)
    clock.now = MIDNIGHT + OFFSET + 5
    await first.tick()
    assert len(FakeProvider.instances) == 1
    for minutes in (1, 2, 30, 600):                                     # restarts and deploys through the day
        clock.now = MIDNIGHT + OFFSET + 5 + minutes * 60
        restarted, _ = build(repo, clock)
        clock.now += 90
        await restarted.tick()
    assert len(FakeProvider.instances) == 1
    assert await restarted.next_run() == MIDNIGHT + OFFSET + DAY
    clock.now = MIDNIGHT + OFFSET + DAY + 2
    await restarted.tick()
    assert len(FakeProvider.instances) == 2


async def test_a_manual_test_counts_as_the_last_test_for_the_schedule(repo, clock):
    runner, _ = build(repo, clock)
    runner._started_at = MIDNIGHT + 9 * 3600                         # the app has run since 09:00 ...
    clock.now = MIDNIGHT + 10 * 3600 + 40 * 60
    assert await runner.next_run() == MIDNIGHT + OFFSET                # ... so today's 10:00 slot is due
    await repo.insert_speed_test({"ts": MIDNIGHT + 10 * 3600 + 30 * 60, "trigger": "manual", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 7.0})
    assert await runner.next_run() == MIDNIGHT + OFFSET + DAY          # unless a test ran since


async def test_a_slot_is_not_caught_up_after_the_feature_was_off_for_days(repo, clock):
    await repo.insert_speed_test({"ts": MIDNIGHT - 10 * DAY, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 5.0})
    config = make_config(enabled=False)
    runner, _ = build(repo, clock, config=config)
    clock.now = MIDNIGHT + OFFSET + 3600
    await runner.tick()                                                  # off: it notes that
    config.speedtest.enabled = True
    clock.now += 60
    await runner.tick()
    assert FakeProvider.instances == []                                  # turning it on does not run one at once
    assert await runner.next_run() == MIDNIGHT + OFFSET + DAY


async def test_nothing_runs_while_it_is_off(repo, clock):
    runner, _ = build(repo, clock, config=make_config(enabled=False))
    clock.now = MIDNIGHT + OFFSET + 30
    await runner.tick()
    assert FakeProvider.instances == [] and await runner.next_run() is None


async def test_the_interval_setting_applies_live(repo, clock):
    runner, config = build(repo, clock)
    assert await runner.next_run() == MIDNIGHT + OFFSET
    config.speedtest.interval_hours = 6
    assert await runner.next_run() == MIDNIGHT + 4 * 3600                  # slots at 04:00, 10:00, ... (offset 10 h mod 6 h)


async def test_a_weekly_test_runs_once_a_week_on_the_installs_day_and_a_restart_adds_none(repo, clock):
    config = make_config()
    config.speedtest.interval_hours = 168
    runner, _ = build(repo, clock, config=config)
    first = await runner.next_run()
    expected = slot_after(MIDNIGHT, 7 * DAY, OFFSET % DAY + (OFFSET % 7) * DAY)
    assert first == expected and first - MIDNIGHT < 7 * DAY and (first - MIDNIGHT) % DAY == OFFSET
    clock.now = first + 5
    await runner.tick()
    await settle(runner)
    assert len(FakeProvider.instances) == 1
    assert await runner.next_run() == first + 7 * DAY
    again, _ = build(repo, clock, config=config)                         # a restart a day later
    clock.now = first + DAY
    assert await again.next_run() == first + 7 * DAY
    await again.tick()
    assert len(FakeProvider.instances) == 1


async def test_a_thirty_day_interval_runs_once_in_thirty_days(repo, clock):
    config = make_config()
    config.speedtest.interval_hours = 720
    runner, _ = build(repo, clock, config=config)
    first = await runner.next_run()
    clock.now = first + 5
    await runner.tick()
    await settle(runner)
    assert await runner.next_run() == first + 30 * DAY
    for day in range(1, 30):
        clock.now = first + day * DAY
        await runner.tick()
    assert len(FakeProvider.instances) == 1


async def test_scheduled_tests_wait_for_the_window_which_may_cross_midnight(repo, clock):
    runner, _ = build(repo, clock, config=make_config(window_start="22:00", window_end="02:00"))
    assert await runner.next_run() == MIDNIGHT + 22 * 3600                 # the 10:00 slot waits for 22:00
    clock.now = MIDNIGHT + OFFSET + 600
    await runner.tick()
    assert FakeProvider.instances == []
    clock.now = MIDNIGHT + 22 * 3600 + 10
    await runner.tick()
    assert len(FakeProvider.instances) == 1
    assert await runner.next_run() == MIDNIGHT + DAY + 22 * 3600           # tomorrow's 10:00 slot waits for 22:00 again


async def test_the_window_is_read_in_the_configured_zone(repo, clock):
    config = make_config(window_start="01:00", window_end="03:00")
    config.general.timezone = "America/New_York"                          # UTC-4 in March after the 8th: 01:00 is 05:00 UTC
    runner, _ = build(repo, clock, config=config)
    assert await runner.next_run() == datetime(2026, 3, 11, 5, 0, tzinfo=timezone.utc).timestamp()


# --- before a scheduled test --------------------------------------------------------------------------------------

async def test_the_monthly_cap_skips_the_test_once_per_slot(repo, clock):
    config = make_config(monthly_cap_mb=100)
    runner, _ = build(repo, clock, config=config)
    await repo.insert_speed_test({"ts": MIDNIGHT - 3600, "trigger": "manual", "provider": "cloudflare", "status": "ok",
                                  "duration_s": 9.0, "bytes_down": 80_000_000, "bytes_up": 30_000_000})
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    await runner.tick()
    assert FakeProvider.instances == []
    skipped = [r for r in await tests_in(repo) if r["status"] == "skipped"]
    assert len(skipped) == 1                                             # once, not at every tick
    assert skipped[0]["reason"] == "monthly data cap reached (110 of 100 MB)" and skipped[0]["trigger"] == "scheduled"
    assert skipped[0]["bytes_down"] is None
    assert await runner.next_run() == MIDNIGHT + OFFSET + DAY            # and the next slot is tomorrow's


async def test_the_cap_counts_only_this_calendar_month(repo, clock):
    config = make_config(monthly_cap_mb=100)
    runner, _ = build(repo, clock, config=config)
    await repo.insert_speed_test({"ts": MIDNIGHT - 30 * DAY, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 9.0, "bytes_down": 900_000_000, "bytes_up": 1})
    assert await runner.month_used_bytes() == 0
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    assert len(FakeProvider.instances) == 1


async def test_a_busy_line_postpones_the_test_three_times_then_skips_it(repo, clock):
    runner, _ = build(repo, clock, line=Line(pct=50.0))
    clock.now = MIDNIGHT + OFFSET + 5
    slot = MIDNIGHT + OFFSET
    for check in range(1, 4):                                           # the first check and two of the retries ...
        await runner.tick()
        assert await tests_in(repo) == [], check                        # ... only wait
        clock.now += 601
    assert FakeProvider.instances == []
    await runner.tick()                                                 # the third retry finds it busy: given up
    skipped = await tests_in(repo)
    assert len(skipped) == 1 and skipped[0]["status"] == "skipped" and skipped[0]["reason"].startswith("line busy")
    assert skipped[0]["reason"] == "line busy (50% load, over 20% after 3 retries)"
    for _ in range(3):                                                  # later ticks leave the slot alone
        clock.now += 601
        await runner.tick()
    assert FakeProvider.instances == [] and len(await tests_in(repo)) == 1
    assert await runner.next_run() == slot + DAY


async def test_the_busy_retries_come_ten_minutes_apart(repo, clock):
    line = Line(pct=50.0)
    runner, _ = build(repo, clock, line=line)
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    retry = await runner.next_run()
    assert retry == clock.now + 600
    clock.now += 599
    await runner.tick()
    assert await runner.next_run() == retry                              # too early: not checked again
    clock.now += 2
    await runner.tick()
    assert await runner.next_run() == clock.now + 600                    # the second retry


async def test_a_line_that_calms_down_lets_the_test_run(repo, clock):
    line = Line(pct=50.0)
    runner, _ = build(repo, clock, line=line)
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    line.pct = 5.0
    clock.now += 601
    await runner.tick()
    assert len(FakeProvider.instances) == 1 and (await tests_in(repo))[0]["status"] == "ok"


async def test_an_unknown_load_does_not_block_a_test(repo, clock):
    runner, _ = build(repo, clock, line=Line(pct=None))
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    assert len(FakeProvider.instances) == 1


async def test_the_busy_threshold_is_the_speed_test_settings_not_the_classifiers(repo, clock):
    config = make_config(busy_load_pct=60)
    runner, _ = build(repo, clock, config=config, line=Line(pct=50.0))     # 50 % is over the default 20 but under 60
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()
    assert len(FakeProvider.instances) == 1


async def test_a_busy_skip_does_not_move_the_schedule_after_a_restart(repo, clock):
    """It is not a test (docs/speedtest.md): the next one is the next slot after the last recorded test, so a restart
    after a busy day tries the slot again."""
    await repo.insert_speed_test({"ts": MIDNIGHT - DAY + OFFSET + 7, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 7.0})        # yesterday's
    line = Line(pct=50.0)
    runner, _ = build(repo, clock, line=line)
    clock.now = MIDNIGHT + OFFSET + 5
    for _ in range(4):
        await runner.tick()
        clock.now += 601
    assert (await runner.next_run()) == MIDNIGHT + OFFSET + DAY                # this run gave the slot up
    restarted, _ = build(repo, clock, line=Line(pct=None))
    assert await restarted.next_run() == MIDNIGHT + OFFSET                       # a new one has not


# --- the manual run ----------------------------------------------------------------------------------------------

async def test_a_manual_run_starts_a_background_test_and_records_it(repo, clock):
    runner, _ = build(repo, clock, line=Line(pct=90.0))                   # it ignores the busy line: the user asked
    assert await runner.run_now() is None
    assert runner.running
    await settle(runner)
    assert not runner.running
    (row,) = await tests_in(repo)
    assert row["trigger"] == "manual" and row["status"] == "ok"


async def test_a_manual_run_is_refused_when_off(repo, clock):
    runner, _ = build(repo, clock, config=make_config(enabled=False))
    assert await runner.run_now() == "the speed test is off"
    assert FakeProvider.instances == []


async def test_a_manual_run_is_refused_at_the_monthly_cap(repo, clock):
    runner, _ = build(repo, clock, config=make_config(monthly_cap_mb=10))
    await repo.insert_speed_test({"ts": clock.now - 60, "trigger": "scheduled", "provider": "cloudflare", "status": "ok",
                                  "duration_s": 5.0, "bytes_down": 9_000_000, "bytes_up": 2_000_000})
    reason = await runner.run_now()
    assert reason == "the monthly data cap is reached (11 of 10 MB)"
    assert FakeProvider.instances == []


async def test_only_one_test_runs_at_a_time(repo, clock):
    gate = asyncio.Event()
    started = asyncio.Event()

    class Slow(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            started.set()
            await gate.wait()
            return await super().download(cap, limit)

    runner = SpeedTestRunner(make_config(), repo, clock=clock, offset_source=lambda: OFFSET,
                             provider_factory=lambda cfg: Slow(clock))
    assert await runner.run_now() is None
    await started.wait()
    assert await runner.run_now() == "a speed test is already running"      # a second manual run
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()                                                    # and the scheduler does not start another
    assert len(FakeProvider.instances) == 1
    gate.set()
    await settle(runner)
    assert len(await tests_in(repo)) == 1
    assert await runner.run_now() is None                                  # free again
    await settle(runner)


async def test_two_manual_runs_at_the_same_moment_start_one(repo, clock):
    gate = asyncio.Event()

    class Slow(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            await gate.wait()
            return await super().download(cap, limit)

    runner = SpeedTestRunner(make_config(), repo, clock=clock, provider_factory=lambda cfg: Slow(clock))
    answers = await asyncio.gather(runner.run_now(), runner.run_now())
    assert sorted(answers, key=lambda a: a is not None) == [None, "a speed test is already running"]
    gate.set()
    await settle(runner)
    assert len(await tests_in(repo)) == 1


async def test_a_manual_test_started_while_the_scheduler_reads_the_data_use_is_not_run_over(repo, clock):
    gate, release, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Slow(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            await hold.wait()                                    # the user's test goes on until the scheduler is done
            return await super().download(cap, limit)

    runner = SpeedTestRunner(make_config(), repo, clock=clock, offset_source=lambda: OFFSET,
                             provider_factory=lambda cfg: Slow(clock))
    real, calls = repo.speed_test_bytes, []

    async def slow_first(start, end):
        calls.append(1)
        if len(calls) == 1:
            gate.set()
            await release.wait()                                 # the scheduler waits here, a test is due
        return await real(start, end)

    repo.speed_test_bytes = slow_first
    clock.now = MIDNIGHT + OFFSET + 5
    ticking = asyncio.ensure_future(runner.tick())
    await gate.wait()
    assert await runner.run_now() is None                         # the user's test starts in the meantime
    release.set()
    try:
        await asyncio.wait_for(ticking, 5)                        # (a scheduler that ran its own test would wait for `hold`)
    finally:
        hold.set()
    assert len(FakeProvider.instances) == 1                       # and the scheduler does not start a second one
    await settle(runner)
    assert [r["trigger"] for r in await tests_in(repo)] == ["manual"]


# --- one test ----------------------------------------------------------------------------------------------------

async def test_a_test_is_recorded_with_what_it_measured(repo, clock):
    runner, _ = build(repo, clock)
    start = clock.now
    assert await runner.run_now() is None
    await settle(runner)
    (row,) = await tests_in(repo)
    cfg = make_config().speedtest
    assert row["ts"] == start and row["duration_s"] == 7.0 and row["provider"] == "cloudflare"
    assert (row["server"], row["status"], row["reason"]) == ("TST", "ok", None)
    assert (row["down_mbps"], row["up_mbps"]) == (94.4, 21.5)
    assert (row["bytes_down"], row["bytes_up"]) == (cfg.download_bytes, cfg.upload_bytes)
    assert FakeProvider.instances[0].calls == [("download", cfg.download_bytes, 15.0, 8.0),
                                               ("upload", cfg.upload_bytes, 15.0, 8.0)]
    assert FakeProvider.instances[0].closed
    assert set(row) == {"id", *SPEED_TEST_COLUMNS}


async def test_one_direction_failing_is_a_partial_test(repo, clock):
    runner, _ = build(repo, clock, up=ProviderError("Cloudflare refused the test (HTTP 429, probably a rate limit); try "
                                                    "another provider or a longer interval"))
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert row["status"] == "partial" and row["down_mbps"] == 94.4 and row["up_mbps"] is None
    assert row["bytes_up"] == 0 and row["reason"].startswith("upload: Cloudflare refused")


async def test_both_failing_is_a_failed_test_never_an_exception(repo, clock):
    runner, _ = build(repo, clock, down=ProviderError("could not connect to Cloudflare"),
                      up=ProviderError("could not connect to Cloudflare"))
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert row["status"] == "failed" and row["down_mbps"] is None and row["up_mbps"] is None
    assert row["reason"] == "download and upload: could not connect to Cloudflare"       # the same reason, once
    assert not runner.running


async def test_a_long_reason_is_cut_between_words_so_a_host_is_never_cut_in_two(repo, clock):
    """The reason keeps 300 characters. Cut inside a LibreSpeed server's host, what is left of it could not be found
    by the MCP tools and the AI pack, which leave the host out: the word the cut falls in goes."""
    from pengumon.mcp.tools import _speed_row

    host = "canary-" + "a" * 60 + "." + "b" * 60 + ".example-lan.lan"         # (150 characters)
    runner, _ = build(repo, clock, down=ProviderError(f"could not connect to {host}: ConnectError"),
                      up=ProviderError(f"{host} did not answer in time"), server=host)
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    reason = row["reason"]
    whole = f"download: could not connect to {host}: ConnectError; upload: {host} did not answer in time"
    assert len(whole) > 300 and 300 - len(host) < whole.rindex(host) < 300   # (the cut would fall inside the host)
    assert len(reason) <= 300 and reason == f"download: could not connect to {host}: ConnectError; upload:"
    shown = _speed_row({**row, "provider": "librespeed"}, timezone.utc)
    assert "canary" not in str(shown) and "aaaa" not in str(shown)
    assert runner_module.cut_reason("a b", 2) == "a" and runner_module.cut_reason("ab cd", 4) == "ab"
    assert runner_module.cut_reason("abcdef", 3) == "" and runner_module.cut_reason("abc", 3) == "abc"

async def test_two_different_reasons_are_both_given(repo, clock):
    runner, _ = build(repo, clock, down=ProviderError("Cloudflare did not answer in time"),
                      up=ProviderError("could not connect to Cloudflare"))
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert row["reason"] == "download: Cloudflare did not answer in time; upload: could not connect to Cloudflare"


async def test_a_provider_bug_is_a_failed_test_and_the_scheduler_goes_on(repo, clock, caplog):
    runner, _ = build(repo, clock, down=RuntimeError("secret internals"))
    clock.now = MIDNIGHT + OFFSET + 5
    await runner.tick()                                                   # (no exception comes out)
    (row,) = await tests_in(repo)
    assert row["status"] == "partial" and "unexpected error: RuntimeError" in row["reason"]
    assert "secret internals" not in row["reason"]
    assert not runner.running


async def test_a_factory_that_raises_is_a_failed_test(repo, clock):
    def broken(cfg):
        raise ValueError("no provider")
    runner = SpeedTestRunner(make_config(), repo, clock=clock, provider_factory=broken)
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert row["status"] == "failed" and row["reason"] == "unexpected error: ValueError"


async def test_a_test_runs_on_the_settings_it_started_with(repo, clock):
    config = make_config(download_mb=10, upload_mb=5)

    class Changing(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            config.speedtest.download_mb = 250                             # changed mid-test
            return await super().download(cap, limit)

    runner = SpeedTestRunner(config, repo, clock=clock, offset_source=lambda: OFFSET,
                             provider_factory=lambda cfg: Changing(clock))
    await runner.run_now()
    await settle(runner)
    assert FakeProvider.instances[0].calls[1] == ("upload", 5_000_000, 15.0, 8.0)
    assert (await tests_in(repo))[0]["bytes_down"] == 10_000_000


async def test_an_interrupted_test_is_recorded_as_such(repo, clock):
    started = asyncio.Event()

    class Hangs(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            started.set()
            await asyncio.sleep(3600)

    runner = SpeedTestRunner(make_config(), repo, clock=clock, provider_factory=lambda cfg: Hangs(clock))
    await runner.run_now()
    await started.wait()
    await runner.aclose()
    (row,) = await tests_in(repo)
    assert row["status"] == "failed" and row["reason"] == "interrupted: the app stopped" and not runner.running


async def test_an_interrupted_test_counts_the_bytes_it_moved_toward_the_monthly_cap(repo, clock):
    started = asyncio.Event()

    class Hangs(FakeProvider):
        def moved(self):
            return 3_000_000                       # what the download had moved when the app stopped

        async def download(self, cap, limit, target_s=None):
            started.set()
            await asyncio.sleep(3600)

    runner = SpeedTestRunner(make_config(), repo, clock=clock, provider_factory=lambda cfg: Hangs(clock))
    await runner.run_now()
    await started.wait()
    await runner.aclose()
    (row,) = await tests_in(repo)
    assert row["bytes_down"] == 3_000_000 and row["bytes_up"] is None
    assert await runner.month_used_bytes() == 3_000_000


async def test_turning_the_test_off_while_one_runs_stops_it(repo, clock, monkeypatch):
    monkeypatch.setattr(runner_module, "STOP_CHECK_SECONDS", 0.01)
    started = asyncio.Event()
    stopped = []

    class Hangs(FakeProvider):
        def moved(self):
            return 1_000_000

        async def download(self, cap, limit, target_s=None):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                stopped.append(True)
                raise

    config = make_config()
    runner = SpeedTestRunner(config, repo, clock=clock, provider_factory=lambda cfg: Hangs(clock))
    await runner.run_now()
    await started.wait()
    config.speedtest.enabled = False
    await asyncio.wait_for(settle(runner), 5)
    (row,) = await tests_in(repo)
    assert stopped == [True] and not runner.running
    assert row["status"] == "failed" and row["reason"] == "stopped: the speed test was turned off"
    assert row["bytes_down"] == 1_000_000 and FakeProvider.instances[0].closed


async def test_a_direction_that_never_ends_is_cut_off_and_the_mask_ends_with_it(repo, clock, monkeypatch):
    """The mask can never outlast the test's own limits: a provider that hangs (a bug, a stalled socket the provider's
    own guards missed) is canceled after PHASE_LIMIT_SECONDS, and the test ends like any failed one."""
    monkeypatch.setattr(runner_module, "STOP_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "PHASE_LIMIT_SECONDS", 0.05)

    class Hangs(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            await asyncio.sleep(3600)

        async def upload(self, cap, limit, target_s=None):
            await asyncio.sleep(3600)

    config = make_config()
    runner = SpeedTestRunner(config, repo, clock=clock, provider_factory=lambda cfg: Hangs(clock))
    await runner.run_now()
    await asyncio.wait_for(settle(runner), 5)
    (row,) = await tests_in(repo)
    assert row["status"] == "failed" and "did not finish in time" in row["reason"] and not runner.running
    assert runner.is_masking()                     # the tail
    clock.now += mask_tail_seconds(config.classifier.window_probes, config.prober.interval_seconds) + 1
    assert not runner.is_masking()


async def test_a_running_flag_left_set_masks_for_no_longer_than_a_test_can_last(repo, clock):
    runner, _ = build(repo, clock)
    runner._start()                                # as if a test never cleared it
    assert runner.is_masking()
    clock.now += runner_module.MASK_LIMIT_SECONDS - 1
    assert runner.is_masking()
    clock.now += 2
    assert not runner.is_masking()


# --- the latency under load and the router's rates -------------------------------------------------------------------

async def samples(repo, rows):
    await repo.insert_samples([ProbeSample(target_id=t, timestamp=ts, success=lat is not None, latency_ms=lat)
                               for t, ts, lat in rows])
    await repo.flush_samples()


async def test_latency_is_the_median_of_the_public_targets_before_and_during_each_direction(repo, clock):
    runner, _ = build(repo, clock, seconds=(10.0, 6.0))
    t = clock.now
    await samples(repo, [("pub", t - 25, 20.0), ("pub", t - 15, 22.0), ("pub", t - 5, 60.0),     # idle: median 22
                         ("gw", t - 5, 1.0),                                                       # (not a public target)
                         ("pub", t + 2, 90.0), ("pub", t + 5, 110.0), ("pub", t + 8, 100.0),       # download: median 100
                         ("pub", t + 12, 300.0), ("pub", t + 14, None), ("pub", t + 15, 500.0),    # upload: median 400
                         ("pub", t - 60, 999.0)])                                                  # (before the idle window)
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert (row["idle_ms"], row["loaded_down_ms"], row["loaded_up_ms"]) == (22.0, 100.0, 400.0)
    assert runner_module.grade_of(row) == "D"                                    # 400 - 22 = 378 added


async def test_without_public_samples_the_latencies_are_none(repo, clock):
    runner, _ = build(repo, clock)
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert (row["idle_ms"], row["loaded_down_ms"], row["loaded_up_ms"]) == (None, None, None)
    assert runner_module.grade_of(row) is None
    runner2, config = build(repo, clock)
    config.targets = [t for t in config.targets if t.category != "public"]
    await runner2.run_now()
    await settle(runner2)
    assert (await repo.latest_speed_test())["idle_ms"] is None


class Router:
    """A router's light counter read (DeviceRegistry.read_wan_counters): the k-th read is taken at k seconds (its own
    clock), the counters advancing 100 MB/s down (800 Mbps) and 5 MB/s up (40 Mbps). `reads` counts them."""

    def __init__(self):
        self.reads = 0

    async def __call__(self):
        k = self.reads
        self.reads += 1
        return WanReading(float(k), WanCounters(rx_bytes=10**12 + 100_000_000 * k, tx_bytes=5_000_000 * k))

    async def until(self, n):
        while self.reads < n:
            await asyncio.sleep(0.001)


class RouterPaced(FakeProvider):
    """A provider whose directions last until the router has been read six more times (real time, short)."""

    router = None

    async def download(self, cap, limit, target_s=None):
        await self.router.until(self.router.reads + 6)
        return await super().download(cap, limit, target_s)

    async def upload(self, cap, limit, target_s=None):
        await self.router.until(self.router.reads + 6)
        return await super().upload(cap, limit, target_s)


async def test_the_line_rate_of_each_phase_is_sampled_from_the_router_while_it_runs(repo, clock):
    router = Router()
    RouterPaced.router = router
    runner = SpeedTestRunner(make_config(), repo, clock=clock, offset_source=lambda: OFFSET, wan_reader=router,
                             provider_factory=lambda cfg: RouterPaced(clock))
    runner.wan_sample_s = 0.001
    await runner.run_now()
    await settle(runner)
    (row,) = await tests_in(repo)
    assert (row["wan_down_mbps"], row["wan_up_mbps"]) == (800.0, 40.0)    # rx while downloading, tx while uploading
    assert (row["down_mbps"], row["up_mbps"]) == (94.4, 21.5)             # the test's own, kept beside them
    assert router.reads >= 14                                              # both phases, each read until its end


async def test_the_provider_is_asked_for_the_target_time(repo, clock):
    runner, _ = build(repo, clock)
    await runner.run_now()
    await settle(runner)
    calls = FakeProvider.instances[0].calls
    assert [c[0] for c in calls] == ["download", "upload"]
    assert all(c[2:] == (runner_module.TIME_LIMIT_SECONDS, 8.0) for c in calls)   # about 8 s of data, 15 s at most


async def test_without_a_wan_counter_device_the_wan_rates_are_none(repo, clock):
    runner, _ = build(repo, clock)                                          # no reader
    await runner.run_now()
    await settle(runner)
    assert (await tests_in(repo))[0]["wan_down_mbps"] is None

    async def nothing():                                                   # a reader that gives nothing (no device now)
        return None
    runner2 = SpeedTestRunner(make_config(), repo, clock=clock, offset_source=lambda: OFFSET, wan_reader=nothing,
                              provider_factory=lambda cfg: FakeProvider(clock))
    await runner2.run_now()
    await settle(runner2)
    latest = await repo.latest_speed_test()
    assert (latest["wan_down_mbps"], latest["wan_up_mbps"]) == (None, None)


# --- the classifier mask ------------------------------------------------------------------------------------------

async def test_the_mask_holds_while_a_test_runs_and_for_the_judged_window_after_it(repo, clock):
    config = make_config()
    runner, _ = build(repo, clock, config=config)
    assert not runner.is_masking()
    gate = asyncio.Event()
    inside = []

    class Watching(FakeProvider):
        async def download(self, cap, limit, target_s=None):
            inside.append(runner.is_masking())
            return await super().download(cap, limit)

    runner._factory = lambda cfg: Watching(clock)
    await runner.run_now()
    await settle(runner)
    assert inside == [True]
    ended = clock.now
    tail = config.classifier.window_probes * config.prober.interval_seconds + 5.0
    assert tail == mask_tail_seconds(config.classifier.window_probes, config.prober.interval_seconds) == 15.0
    clock.now = ended + tail - 0.1
    assert runner.is_masking()
    clock.now = ended + tail + 0.1
    assert not runner.is_masking()
    config.classifier.window_probes = 30                                   # the tail follows the setting
    await runner.run_now()
    await settle(runner)
    clock.now += 34.9
    assert runner.is_masking()
    clock.now += 0.2
    assert not runner.is_masking()


def test_the_stored_tests_mask_their_tails_for_the_replay():
    mask = SpeedMask([(100.0, 110.0), (112.0, 120.0), (500.0, 503.0)], tail=15.0)
    assert not mask.covers(99.9)
    assert mask.covers(100.0) and mask.covers(135.0) and not mask.covers(135.1)   # the two overlapping tails are one span
    assert mask.covers(518.0) and not mask.covers(518.1) and not mask.covers(300.0)
    assert not SpeedMask().covers(1.0)
