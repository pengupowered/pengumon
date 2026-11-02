"""Server-side grades (pengumon.grades): the Range rules, the DOCSIS and router metrics they are applied to, the
ranges /api/devices serves, and that the one threshold set is the one the status and the dossier use."""

from types import SimpleNamespace

import pytest

from pengumon.capabilities import Capability, LineReading, PollResult, SystemReading, WanCounters
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.monitors import router_ranges
from pengumon.grades import CRITICAL, GOOD, WARN, Range, known_grades, worst
from pengumon.lines import docsis
from pengumon.lines.docsis_model import DocsisChannel
from pengumon.lines.docsis import channel_finding

from device_helpers import build_modem, build_router
from test_api_devices import _client


def test_a_range_grades_its_edges_towards_the_better_side():
    r = Range("dB", 20.0, 50.0, good_low=33.0, critical_low=30.0)
    assert [r.grade(v) for v in (33.0, 32.9, 30.0, 29.9, 50.0, None)] == [GOOD, WARN, WARN, CRITICAL, GOOD, None]
    both = Range("dBmV", 30.0, 56.0, good_low=38.0, good_high=49.0, critical_low=35.0, critical_high=51.0)
    assert [both.grade(v) for v in (38.0, 49.0, 49.1, 51.0, 51.1, 37.9, 35.0, 34.9)] == [
        GOOD, GOOD, WARN, WARN, CRITICAL, WARN, WARN, CRITICAL]


def test_as_dict_fills_open_ends_from_the_scale():
    d = Range("dB", 20.0, 50.0, good_low=33.0, critical_low=30.0).as_dict()
    assert d == {"unit": "dB", "min": 20.0, "good_low": 33.0, "good_high": 50.0, "max": 50.0,
                 "critical_low": 30.0, "critical_high": None}


def test_worst_and_known_grades():
    assert worst([GOOD, None, CRITICAL, WARN]) == CRITICAL
    assert worst([None]) is None and worst([]) is None
    assert known_grades(a=GOOD, b=None) == {"a": GOOD}


def channel(direction="downstream", power=1.0, snr=38.0, recent=0, cid=1):
    return DocsisChannel(channel_id=cid, direction=direction, frequency_mhz=600.0, power_dbmv=power, snr_db=snr,
                         uncorrectables_recent=recent)


@pytest.mark.parametrize("ds_power,expected", [(8.0, GOOD), (-8.0, GOOD), (8.1, WARN), (-9.5, WARN)])
def test_downstream_power_uses_8_dbmv_not_7(ds_power, expected):
    snapshot = docsis.summarize([channel(power=ds_power)], 1.0)
    assert snapshot.grades["ds_power"] == expected
    assert snapshot.channels[0].grades["power"] == expected


@pytest.mark.parametrize("us_power,expected", [(49.0, GOOD), (49.1, WARN), (51.1, CRITICAL), (38.0, GOOD),
                                               (37.5, WARN), (34.0, CRITICAL)])
def test_upstream_power_uses_49_dbmv_not_48(us_power, expected):
    snapshot = docsis.summarize([channel(), channel("upstream", power=us_power, snr=None, cid=9)], 1.0)
    assert snapshot.grades["us_power"] == expected
    assert snapshot.channels[1].grades["power"] == expected and "snr" not in snapshot.channels[1].grades
    if us_power > 49.0:
        assert snapshot.status in ("warning", "critical")  # the grade and the status agree on the high side


def test_a_snapshot_with_no_upstream_has_no_upstream_grade():
    snapshot = docsis.summarize([channel(snr=None)], 1.0)
    assert set(snapshot.grades) == {"ds_power", "uncorrectables"}  # no SNR and no upstream average: no grade for them
    assert docsis.summarize([], 1.0).grades == {"uncorrectables": GOOD}


def test_snr_grades_and_the_status_agree_at_the_thresholds():
    for snr, grade, status in ((33.0, GOOD, "healthy"), (32.9, WARN, "warning"), (30.0, WARN, "warning"),
                               (29.9, CRITICAL, "critical")):
        snapshot = docsis.summarize([channel(snr=snr)], 1.0)
        assert (snapshot.grades["ds_snr"], snapshot.status) == (grade, status)


def ds_channels(n, errors=0, unerrored=None, corrected=0, first_mhz=543.0, hit=0):
    """n adjacent downstream channels; the first `hit` carry `errors` cumulative uncorrectables each."""
    return [DocsisChannel(channel_id=i + 1, direction="downstream", frequency_mhz=first_mhz + 6 * i, power_dbmv=1.0,
                          snr_db=38.0, correcteds=corrected, uncorrectables=errors if i < hit else 0,
                          unerrored=unerrored)
            for i in range(n)]


def read(tracker, ts, channels):
    return tracker.apply(docsis.summarize(channels, ts))


def test_the_real_event_is_information_not_a_warning():
    """2026-11-01: 258 new uncorrectables on 9 of 33 adjacent channels in one 1800 s read, nothing in the reads
    around it. Without error-free counts a single such burst (8.6 a minute) is good, with a note and the context."""
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, ds_channels(33, errors=0))
    event = read(tracker, 2800.0, ds_channels(33, errors=29, hit=9))
    assert event.uncorrectables_recent == 261 and event.errors_affected_channels == 9
    assert event.errors_span_mhz == [543.0, 543.0 + 6 * 8]
    assert event.uncorrectables_interval_seconds == 1800.0 and event.uncorrectables_ratio is None
    assert event.grades["uncorrectables"] == GOOD and event.status == "healthy"
    assert "short burst" in event.errors_note
    assert {c.grades["uncorrectables"] for c in event.channels} == {GOOD}
    after = read(tracker, 4600.0, ds_channels(33, errors=29, hit=9))  # the next read: nothing new
    assert after.uncorrectables_recent == 0 and after.errors_note is None and after.errors_recurring is False


def test_errors_that_recur_on_consecutive_reads_warn_without_a_ratio():
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, ds_channels(33))
    first = read(tracker, 2800.0, ds_channels(33, errors=10, hit=9))
    second = read(tracker, 4600.0, ds_channels(33, errors=20, hit=9))
    assert first.grades["uncorrectables"] == GOOD and second.errors_recurring
    assert second.grades["uncorrectables"] == WARN and second.status == "warning"
    assert "previous read" in second.errors_note
    third = read(tracker, 6400.0, ds_channels(33, errors=20, hit=9))  # quiet again: the recurrence ends
    assert third.grades["uncorrectables"] == GOOD


def test_a_high_rate_warns_at_once_and_a_burst_is_critical_without_a_ratio():
    for seconds, errors, grade in ((600, 600, WARN), (600, 6000, CRITICAL), (10, 12, WARN), (1800, 100, GOOD)):
        tracker = docsis.DocsisErrorTracker()
        read(tracker, 1000.0, ds_channels(2))
        snap = read(tracker, 1000.0 + seconds, ds_channels(2, errors=errors, hit=1))
        assert snap.grades["uncorrectables"] == grade, (seconds, errors)


def test_with_error_free_counts_the_ratio_decides():
    """The real event's size: about 1e9 codewords over the interval, so 258 lost is 2.6e-7."""
    for lost, grade, status in ((258, GOOD, "healthy"), (2_000, WARN, "warning"), (200_000, CRITICAL, "critical")):
        tracker = docsis.DocsisErrorTracker()
        read(tracker, 1000.0, ds_channels(33, unerrored=1_000_000, corrected=10))
        snap = read(tracker, 2800.0, ds_channels(33, errors=lost, hit=1, unerrored=1_000_000_000 // 33 + 1_000_000,
                                                 corrected=10))
        assert snap.uncorrectables_ratio == pytest.approx(lost / (1_000_000_000 + lost), rel=0.05)
        assert (snap.grades["uncorrectables"], snap.status) == (grade, status)
        assert "of all codewords" in snap.errors_note


def test_the_ratio_thresholds_sit_exactly_on_their_edges():
    assert docsis.judge_errors(1, 1800, 1e-6, False)[0] == WARN
    assert docsis.judge_errors(1, 1800, 9.9e-7, False)[0] == GOOD
    assert docsis.judge_errors(1, 1800, 1e-4, False)[0] == CRITICAL
    assert docsis.judge_errors(1, 1800, 9.9e-5, False)[0] == WARN
    assert docsis.judge_errors(0, 1800, None, True) == (GOOD, None)


def test_the_ratio_is_unknown_without_a_baseline_or_after_a_counter_reset():
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, ds_channels(3, unerrored=None))  # a modem that does not report the row
    assert read(tracker, 2000.0, ds_channels(3, errors=5, hit=1, unerrored=None)).uncorrectables_ratio is None
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, ds_channels(3, unerrored=500))
    assert read(tracker, 2000.0, ds_channels(3, errors=5, hit=1, unerrored=100)).uncorrectables_ratio is None  # went backwards
    seeded = docsis.DocsisErrorTracker()
    seeded.seed(read(docsis.DocsisErrorTracker(), 1.0, ds_channels(3)))  # a restart: stored readings have no counts
    assert read(seeded, 2.0, ds_channels(3, errors=5, hit=1, unerrored=900)).uncorrectables_ratio is None


async def test_the_probes_are_cross_checked_against_new_errors():
    for lost, fragment in ((False, "no measurable effect"), (True, "lost packets"), (None, None)):
        async def probes(start, end, lost=lost):
            probes.window = (start, end)
            return lost
        monitor = build_modem(password="pw").monitor
        monitor.watch_probes(probes)
        reading = lambda e: PollResult({Capability.ACCESS_LINE: LineReading("docsis", tuple(ds_channels(2, errors=e, hit=1)))})
        await monitor.on_result(1000.0, reading(0))
        snap = await monitor.on_result(2800.0, reading(20))
        assert snap.errors_probe_loss is lost
        assert (fragment is None) or fragment in snap.errors_note
        assert probes.window == (1000.0, 2800.0)


def test_no_reading_has_no_grades():
    snapshot = docsis.summarize([channel()], 1.0)
    snapshot.status = "error"
    assert docsis.grade_snapshot(snapshot).grades == {}


def test_the_dossier_and_the_grades_share_one_threshold_set():
    assert docsis.is_degraded(channel(power=8.1)) and not docsis.is_degraded(channel(power=8.0))
    assert docsis.is_degraded(channel("upstream", power=49.1, snr=None)) and not docsis.is_degraded(
        channel("upstream", power=49.0, snr=None))
    assert channel_finding(channel("upstream", power=48.5, snr=40.0)) == "Power level out of range"  # 48.5 is fine now


async def test_a_stored_reading_restored_at_start_is_graded(tmp_path):
    from pengumon.db.repository import DatabaseRepository
    repo = DatabaseRepository(db_path=str(tmp_path / "g.db"))
    await repo.initialize()
    try:
        device = build_modem(repo, password="pw")
        await device.monitor.on_result(
            1000.0, PollResult({Capability.ACCESS_LINE: LineReading("docsis", (channel(power=9.0),))}))
        again = build_modem(repo, password="pw")
        assert await again.monitor.restore(0.0) == 1000.0
        assert again.monitor.latest_snapshot.grades["ds_power"] == WARN
        assert again.monitor.latest_snapshot.channels[0].grades["power"] == WARN
    finally:
        await repo.close()


# --- router ---------------------------------------------------------------------------------------------------------

def router_result(cpu=10.0, mem=40.0, rx_bytes=0, tx_bytes=0):
    return PollResult({Capability.ROUTER_SYSTEM: SystemReading(cpu_pct=cpu, mem_pct=mem),
                       Capability.WAN_COUNTERS: WanCounters(rx_bytes=rx_bytes, tx_bytes=tx_bytes)})


@pytest.mark.parametrize("cpu,mem,expected", [
    (90.0, 85.0, {"cpu": GOOD, "mem": GOOD}), (90.1, 85.1, {"cpu": WARN, "mem": WARN}),
    (50.0, 95.0, {"cpu": GOOD, "mem": WARN}), (100.0, 95.1, {"cpu": WARN, "mem": CRITICAL}),
])
async def test_router_cpu_and_memory_grades(cpu, mem, expected):
    monitor = build_router().monitor
    snapshot = await monitor.on_result(1000.0, router_result(cpu, mem))
    assert {k: v for k, v in snapshot.grades.items() if k in ("cpu", "mem")} == expected
    assert snapshot.status == ("warning" if expected["cpu"] == WARN else "healthy")


async def test_a_router_without_cpu_or_memory_has_no_such_grades():
    snapshot = await build_router().monitor.on_result(1000.0, PollResult({Capability.WAN_COUNTERS: WanCounters(1, 1)}))
    assert snapshot.grades == {}


async def test_wan_rates_are_graded_as_a_share_of_the_line_capacity():
    load = SimpleNamespace(down_mbps=1000.0, up_mbps=40.0)
    ticks = iter(range(1000, 1100, 10))
    monitor = build_router().monitor
    monitor.watch_load(load, 60.0)
    await monitor.on_result(next(ticks), router_result(rx_bytes=0, tx_bytes=0))
    # 10 s later: download 300 Mbps (30 %) and upload 30 Mbps (75 %)
    snapshot = await monitor.on_result(next(ticks), router_result(rx_bytes=375_000_000, tx_bytes=37_500_000))
    assert (snapshot.wan_rx_load_pct, snapshot.wan_tx_load_pct) == (30.0, 75.0)
    assert (snapshot.grades["wan_rx"], snapshot.grades["wan_tx"]) == (GOOD, WARN)
    # a different capacity, and a saturated upload (the same 30 Mbps on 30 Mbps)
    load.down_mbps, load.up_mbps = 300.0, 30.0
    snapshot = await monitor.on_result(next(ticks), router_result(rx_bytes=750_000_000, tx_bytes=75_000_000))
    assert (snapshot.wan_rx_load_pct, snapshot.wan_tx_load_pct) == (100.0, 100.0)
    assert (snapshot.grades["wan_rx"], snapshot.grades["wan_tx"]) == (CRITICAL, CRITICAL)


async def test_without_a_known_capacity_the_wan_rates_have_no_percentage_or_grade():
    monitor = build_router().monitor
    monitor.watch_load(SimpleNamespace(down_mbps=None, up_mbps=None), 60.0)
    await monitor.on_result(1000.0, router_result())
    snapshot = await monitor.on_result(1010.0, router_result(rx_bytes=10_000_000, tx_bytes=10_000_000))
    assert snapshot.wan_rx_rate_mbps is not None and snapshot.wan_rx_load_pct is None and snapshot.wan_tx_load_pct is None
    assert "wan_rx" not in snapshot.grades and "wan_tx" not in snapshot.grades


def test_the_wan_range_follows_the_busy_threshold():
    r = router_ranges(75.0)["wan_rx"]
    assert [r.grade(v) for v in (75.0, 75.1, 95.0, 95.1)] == [GOOD, WARN, WARN, CRITICAL]


# --- the API ---------------------------------------------------------------------------------------------------------

async def test_devices_list_the_good_ranges_and_the_snapshot_carries_the_grades(app_state):
    app_state.devices = DeviceRegistry([build_modem(password="pw"), build_router(password="pw", id="wifi")])
    modem, wifi = app_state.devices.all()
    modem.monitor.latest_snapshot = docsis.summarize([channel(power=9.0)], 1.0)
    async with _client() as ac:
        listed = (await ac.get("/api/devices")).json()
        snapshot = (await ac.get(f"/api/devices/{modem.id}")).json()
        unknown = await ac.get("/api/devices/nope")
    assert listed[0]["ranges"]["us_power"] == {"unit": "dBmV", "min": 30.0, "good_low": 38.0, "good_high": 49.0,
                                               "max": 56.0, "critical_low": 35.0, "critical_high": 51.0}
    assert listed[0]["ranges"]["ds_power"]["good_high"] == 8.0
    assert set(listed[1]["ranges"]) == {"cpu", "mem", "wan_rx", "wan_tx"} and listed[1]["ranges"]["cpu"]["good_high"] == 90.0
    assert snapshot["grades"]["ds_power"] == WARN and snapshot["channels"][0]["grades"]["power"] == WARN
    assert unknown.status_code == 404


def _one_without_count(channels, index=32):
    channels[index].unerrored = None  # like the live modem's one channel (probably the OFDM one)
    return channels


def test_the_ratio_is_used_when_one_channel_lacks_the_error_free_count():
    """32 of 33 channels report error-free codewords: the ratio covers those, and grading uses it."""
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, _one_without_count(ds_channels(33, unerrored=1_000_000, corrected=10)))
    snap = read(tracker, 2800.0, _one_without_count(ds_channels(
        33, errors=2_000, hit=1, unerrored=1_000_000_000 // 33 + 1_000_000, corrected=10)))
    assert snap.uncorrectables_ratio == pytest.approx(2_000 / (32 * (1_000_000_000 // 33)), rel=0.05)
    assert (snap.grades["uncorrectables"], snap.status) == (WARN, "warning")
    assert "of all codewords" in snap.errors_note


def test_the_ratio_is_unknown_when_the_channel_without_a_count_has_new_errors():
    tracker = docsis.DocsisErrorTracker()
    read(tracker, 1000.0, _one_without_count(ds_channels(33, unerrored=1_000_000)))
    chans = _one_without_count(ds_channels(33, unerrored=31_000_000))
    chans[32].uncorrectables = 7
    snap = read(tracker, 2800.0, chans)
    assert snap.uncorrectables_recent == 7 and snap.uncorrectables_ratio is None
    assert "a minute" in snap.errors_note  # judged by rate instead
