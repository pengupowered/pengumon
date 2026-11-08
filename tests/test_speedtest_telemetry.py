"""The speed test's instruments (docs/telemetry.md, "Speed test"): the runner counts each recorded test and the bytes it
moved, and keeps the last good test for the gauges (speedtest/gauges.py), which it reloads from the database at the start."""

import obs_helpers
from pengumon.observability.gauges import GAUGES  # noqa: F401  (first: the speed gauges need its Gauge)
from pengumon.speedtest.gauges import SPEED_GAUGES
from pengumon.speedtest.providers.base import ProviderError, Transfer
from test_speedtest_runner import Clock, build, make_config, settle, tests_in

obs_helpers.install()


def counts():
    runs = lambda **kw: obs_helpers.total("pengumon.speedtest.runs", **kw)  # noqa: E731
    return (runs(status="ok", trigger="manual", provider="cloudflare"), runs(status="failed", trigger="manual"),
            runs(status="skipped", trigger="scheduled"),
            obs_helpers.total("pengumon.speedtest.bytes", direction="download"),
            obs_helpers.total("pengumon.speedtest.bytes", direction="upload"))


async def test_each_recorded_test_is_counted_with_its_bytes(repo):
    cfg = make_config().speedtest
    before = counts()
    runner, _ = build(repo, Clock())
    assert await runner.run_now() is None
    await settle(runner)
    failing, _ = build(repo, Clock(), down=ProviderError("refused"), up=ProviderError("refused"))
    assert await failing.run_now() is None
    await settle(failing)
    after = counts()
    assert after[0] - before[0] == 1 and after[1] - before[1] == 1       # one ok, one failed
    assert after[3] - before[3] == cfg.download_bytes and after[4] - before[4] == cfg.upload_bytes  # the failed one moved none


async def test_a_skipped_slot_is_counted_as_skipped(repo):
    config = make_config(monthly_cap_mb=1)
    clock = Clock()
    runner, _ = build(repo, clock, config=config)
    await repo.insert_speed_test({"ts": clock.now - 60, "trigger": "manual", "provider": "cloudflare", "status": "ok",
                                  "bytes_down": 5_000_000, "bytes_up": 0})
    before = counts()
    await runner._skip(clock.now, "monthly data cap reached (5 of 1 MB)")
    assert counts()[2] - before[2] == 1


async def test_the_last_good_test_feeds_the_gauges_and_is_reloaded_at_the_start(repo):
    clock = Clock()
    runner, _ = build(repo, clock, down=Transfer(25_000_000, 4.0, 88.5, "TST"), up=Transfer(10_000_000, 3.0, 9.5, "TST"))
    assert runner.last_ok is None
    assert await runner.run_now() is None
    await settle(runner)
    assert (runner.last_ok["down_mbps"], runner.last_ok["up_mbps"]) == (88.5, 9.5)
    failing, _ = build(repo, clock, down=ProviderError("x"), up=ProviderError("y"))
    assert await failing.run_now() is None
    await settle(failing)
    assert failing.last_ok is None                                        # a failed test is not a "last good"
    restarted, _ = build(repo, clock)
    await restarted.load_last()
    assert restarted.last_ok["down_mbps"] == 88.5 and len(await tests_in(repo)) == 2
    state = type("S", (), {"speedtest": restarted})()
    values = {g.prom: g.collect(state) for g in SPEED_GAUGES}
    assert values["pengumon_speedtest_download_mbps"] == [({}, 88.5)]
    assert values["pengumon_speedtest_upload_mbps"] == [({}, 9.5)]
    assert all(set(labels) <= {"phase"} for samples in values.values() for labels, _ in samples)
