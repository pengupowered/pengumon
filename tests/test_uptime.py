"""GET /api/uptime: availability strips per target from the rollups."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.db.repository import DatabaseRepository
from pengumon.main import app
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample
from pengumon.reports.uptime import block_status, build_uptime

NOW = 1_793_448_000.0 + 7 * 60  # 12:07 UTC; its 15-minute block is 12:00-12:15
BLOCK = int(NOW // 900)


def minute_samples(target, start, lost_seconds=0, minutes=1):
    """Per second probes over `minutes` minutes; the first `lost_seconds` seconds of every minute are lost."""
    out = []
    for i in range(minutes * 60):
        ok = (i % 60) >= lost_seconds
        out.append(ProbeSample(target_id=target, timestamp=start + i, success=ok, latency_ms=5.0 if ok else None,
                               packet_loss_pct=0.0 if ok else 100.0, jitter_ms=None, error=None if ok else "x"))
    return out


async def make_repo(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "u.db"), sample_flush_seconds=0)
    await repo.initialize()
    return repo


def block_start(back):
    return (BLOCK - back) * 900.0


def test_block_status_rules():
    assert block_status(5, 5, 0) == "no_data" and block_status(0, 0, 0) == "no_data"
    assert block_status(900, 900, 0) == "ok" and block_status(900, 892, 0) == "ok"       # 0.9 % loss
    assert block_status(900, 891, 0) == "degraded"                                       # 1 %
    assert block_status(900, 840, 1) == "degraded" and block_status(900, 780, 2) == "outage"


async def test_no_data_gives_no_targets(tmp_path):
    repo = await make_repo(tmp_path)
    out = await build_uptime(repo, None, "24h", now=NOW)
    assert out["targets"] == [] and out["blocks"] == 96 and out["block_seconds"] == 900
    assert out["end"] - out["start"] == 96 * 900 and out["start"] == block_start(95)
    await repo.close()


async def test_blocks_get_their_status_and_one_target_is_enough(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples(
        minute_samples("a", block_start(0), 0, 5)                 # the current block: fine
        + minute_samples("a", block_start(1), 10, 5)              # 10 s of every minute lost: degraded, no dead minute
        + minute_samples("a", block_start(2), 60, 3)              # three dead minutes: outage
        + minute_samples("a", block_start(4), 0, 1)               # one minute only: 60 probes is enough
        + minute_samples("b", block_start(0), 0, 2))
    out = await build_uptime(repo, None, "24h", now=NOW)
    cells = {t["target_id"]: t["cells"] for t in out["targets"]}
    a = cells["a"]
    assert len(a) == 96
    assert a[95] == "ok" and a[94] == "degraded" and a[93] == "outage" and a[92] == "no_data" and a[91] == "ok"
    assert cells["b"][95] == "ok" and set(cells["b"][:95]) == {"no_data"}
    pct = {t["target_id"]: t["availability_pct"] for t in out["targets"]}
    assert pct["b"] == 100.0 and 70 < pct["a"] < 100
    await repo.close()


async def test_a_block_explained_by_load_is_busy_unless_a_fault_overlaps_it(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples(minute_samples("a", block_start(1), 10, 5) + minute_samples("a", block_start(2), 10, 5))
    explained = await repo.create_incident(Incident(
        start_time=block_start(1) + 30, end_time=block_start(1) + 60, cause_code="load_induced_latency", title="t",
        description="d", severity="info", affected_targets=[], kind="busy_latency"))
    await repo.set_incident_load(explained, 90.0, expected=True)
    await repo.create_incident(Incident(start_time=block_start(2) + 30, end_time=block_start(2) + 60,
                                        cause_code="access_line_drop", title="t", description="d",
                                        severity="warning", affected_targets=[], kind="isp_loss"))
    cells = (await build_uptime(repo, None, "24h", now=NOW))["targets"][0]["cells"]
    assert cells[94] == "busy" and cells[93] == "degraded"
    await repo.close()


async def test_a_fault_from_before_the_window_keeps_its_first_block_from_being_busy(tmp_path):
    """The first block is explained by load and also reached by a drop that began before the window."""
    repo = await make_repo(tmp_path)
    window_start = block_start(95)
    await repo.insert_samples(minute_samples("a", window_start, 10, 5))
    explained = await repo.create_incident(Incident(
        start_time=window_start + 30, end_time=window_start + 60, cause_code="load_induced_latency", title="t",
        description="d", severity="info", affected_targets=[], kind="busy_latency"))
    await repo.set_incident_load(explained, 90.0, expected=True)
    await repo.create_incident(Incident(start_time=window_start - 300, end_time=window_start + 120, cause_code="access_line_drop",
                                        title="t", description="d", severity="warning", affected_targets=[], kind="isp_loss"))
    cells = (await build_uptime(repo, None, "24h", now=NOW))["targets"][0]["cells"]
    assert cells[0] == "degraded"
    await repo.close()


async def test_an_explained_incident_from_before_the_window_makes_its_first_block_busy(tmp_path):
    repo = await make_repo(tmp_path)
    window_start = block_start(95)
    await repo.insert_samples(minute_samples("a", window_start, 10, 5))
    explained = await repo.create_incident(Incident(
        start_time=window_start - 600, end_time=window_start + 60, cause_code="load_induced_latency", title="t",
        description="d", severity="info", affected_targets=[], kind="busy_latency"))
    await repo.set_incident_load(explained, 90.0, expected=True)
    cells = (await build_uptime(repo, None, "24h", now=NOW))["targets"][0]["cells"]
    assert cells[0] == "busy"
    await repo.close()


async def test_daily_windows_follow_the_time_zone(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples(minute_samples("a", NOW - 3 * 86400, 60, 3))  # three dead minutes at 12:07 UTC, 3 days ago
    utc = await build_uptime(repo, None, "7d", now=NOW)
    # 7 days ending 12:07 on 31 October: the 24th (from 12:07) to the 31st (to 12:07), 8 cells, the ends partial
    assert utc["blocks"] == 8 and len(utc["targets"][0]["cells"]) == 8 and len(utc["bounds"]) == 9
    assert utc["partial"] == [True] + [False] * 6 + [True] and utc["end"] - utc["start"] == 7 * 86400
    assert utc["hours"][0] == pytest.approx(11 + 53 / 60, abs=1e-3) and sum(utc["hours"]) == pytest.approx(168)
    assert utc["hours"][-1] == pytest.approx(12 + 7 / 60, abs=1e-3)
    assert utc["targets"][0]["cells"][4] == "outage" and utc["targets"][0]["cells"][7] == "no_data"
    month = await build_uptime(repo, None, "30d", now=NOW, tz=timezone(timedelta(minutes=-480)))
    assert len(month["targets"][0]["cells"]) == 31 and month["targets"][0]["cells"].count("outage") == 1
    assert month["tz_offset_minutes"] == -480 and month["end"] - month["start"] == 30 * 86400
    assert month["partial"][0] and month["partial"][-1] and not any(month["partial"][1:-1])
    await repo.close()


async def test_daily_blocks_say_when_they_begin_and_a_day_can_have_25_hours(tmp_path):
    """New York, 1 November 2026: the clocks go back, so that day's block is 25 hours long and the blocks after it
    begin at 05:00 UTC, not 04:00. Three dead minutes at 23:30 EST that day are in its block, which the bounds say."""
    repo = await make_repo(tmp_path)
    nov1 = 1_793_491_200  # 2026-11-01 00:00 UTC
    dead = nov1 + 28 * 3600 + 30 * 60
    await repo.insert_samples(minute_samples("a", dead, 60, 3))
    out = await build_uptime(repo, None, "7d", now=nov1 + 2 * 86400 + 17 * 3600, tz=ZoneInfo("America/New_York"))
    bounds = out["bounds"]
    assert out["zone"] == "America/New_York" and len(bounds) == 9
    # the window is the 7 days to 12:00 on 3 November; its first day (27 October, from 13:00 EDT: 7 x 24 h back across the extra hour) and last are cut
    assert bounds[0] == out["start"] == nov1 - 5 * 86400 + 17 * 3600 and bounds[-1] == out["end"]
    assert [b - a for a, b in zip(bounds, bounds[1:])] == [39600] + [86400] * 4 + [90000] + [86400] + [43200]
    assert out["partial"] == [True] + [False] * 6 + [True]  # the 25-hour day is whole
    assert out["targets"][0]["cells"][5] == "outage" and bounds[5] <= dead < bounds[6]
    await repo.close()


async def test_the_endpoint_validates_and_answers(app_state, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/uptime")).status_code == 503
        app_state.repository = await make_repo(tmp_path)
        ok = await ac.get("/api/uptime", params={"window": "7d"})
        assert ok.status_code == 200 and ok.json()["blocks"] in (7, 8)
        assert (await ac.get("/api/uptime", params={"window": "1y"})).status_code == 422
        assert (await ac.get("/api/uptime", params={"tz_offset_minutes": -900})).status_code == 422
    await app_state.repository.close()


# --- 90 days and all (0.28 step 7) -----------------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")
DAY = 86400


def config_keeping(days):
    from types import SimpleNamespace
    return SimpleNamespace(database=SimpleNamespace(latency_retention_days=days), targets=[], dns_probes=[])


def cell_of(out, at):
    """The index of the cell whose [bounds[i], bounds[i + 1]) holds `at`."""
    return next(i for i in range(len(out["bounds"]) - 1) if out["bounds"][i] <= at < out["bounds"][i + 1])


async def test_90d_is_calendar_days_over_exactly_ninety_days(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples(minute_samples("a", NOW - 80 * DAY, 60, 3) + minute_samples("a", NOW - 100 * DAY, 60, 3)
                              + minute_samples("a", NOW - 5 * DAY, 0, 1))
    out = await build_uptime(repo, None, "90d", now=NOW)
    assert (out["window"], out["cell"], out["block_seconds"]) == ("90d", "day", 86400)
    assert out["end"] - out["start"] == 90 * DAY and out["blocks"] == 91 and len(out["bounds"]) == 92
    assert out["partial"][0] and out["partial"][-1] and not any(out["partial"][1:-1])
    assert sum(out["hours"]) == pytest.approx(90 * 24)
    cells = out["targets"][0]["cells"]
    assert cells.count("outage") == 1 and cells[cell_of(out, NOW - 80 * DAY)] == "outage"  # the one 100 days ago is outside
    assert out["targets"][0]["availability_pct"] == round(100 * 60 / 240, 3)  # 240 probes in the window, 60 answered
    for window, cell in (("24h", "15m"), ("7d", "day"), ("30d", "day")):
        assert (await build_uptime(repo, None, window, now=NOW))["cell"] == cell
    await repo.close()


async def test_all_without_probes_is_one_day(tmp_path):
    repo = await make_repo(tmp_path)
    out = await build_uptime(repo, None, "all", now=NOW)
    assert out["cell"] == "day" and out["targets"] == [] and out["end"] - out["start"] == DAY
    await repo.close()


async def test_all_is_days_from_the_oldest_stored_probe_when_that_is_within_90_days(tmp_path):
    repo = await make_repo(tmp_path)
    oldest = NOW - 40 * DAY - 3 * 3600  # minute-aligned
    await repo.insert_samples(minute_samples("a", oldest, 0, 1) + minute_samples("a", NOW - 10 * DAY, 60, 3))
    out = await build_uptime(repo, None, "all", now=NOW)
    assert (out["window"], out["cell"], out["block_seconds"]) == ("all", "day", 86400)
    assert out["start"] == oldest and out["end"] == NOW and out["bounds"][0] == oldest and out["bounds"][-1] == NOW
    assert out["partial"][0] and out["partial"][-1] and sum(out["hours"]) == pytest.approx((NOW - oldest) / 3600)
    assert out["targets"][0]["availability_pct"] == round(100 * 60 / 240, 3)  # exactly the window: 60 of 240 answered
    assert out["targets"][0]["cells"][cell_of(out, NOW - 10 * DAY)] == "outage"
    assert out["hours"][0] == pytest.approx((out["bounds"][1] - oldest) / 3600, abs=1e-3)  # the first day is cut at the oldest probe
    assert out["hours"][0] < 24
    await repo.close()


async def seed_weeks(repo):
    """Probes on the New York clock, minutes only: 150 days ago (ok), 100 days ago (3 dead minutes), Sunday 6 September
    23:59 (one dead minute: degraded), Monday 7 September 00:00 (ok), 7 days ago (ok)."""
    local = lambda *a: datetime(*a, tzinfo=NY).timestamp()  # noqa: E731
    await repo.insert_samples(
        minute_samples("a", NOW - 150 * DAY, 0, 1) + minute_samples("a", NOW - 100 * DAY, 60, 3)
        + minute_samples("a", local(2026, 9, 6, 23, 59), 60, 1) + minute_samples("a", local(2026, 9, 7, 0, 0), 0, 1)
        + minute_samples("a", NOW - 7 * DAY, 0, 1))


async def test_all_is_local_weeks_from_monday_beyond_90_days(tmp_path):
    repo = await make_repo(tmp_path)
    await seed_weeks(repo)
    out = await build_uptime(repo, None, "all", now=NOW, tz=NY)
    assert (out["window"], out["cell"], out["block_seconds"]) == ("all", "week", 7 * DAY)
    assert out["start"] == NOW - 150 * DAY and out["end"] == NOW and out["zone"] == "America/New_York"
    bounds = out["bounds"]
    assert len(bounds) == out["blocks"] + 1 == len(out["partial"]) + 1 == len(out["targets"][0]["cells"]) + 1
    # every inner edge is a Monday midnight on the local clock; the cells are cut at the window's edges only
    for b in bounds[1:-1]:
        local = datetime.fromtimestamp(b, NY)
        assert (local.weekday(), local.hour, local.minute) == (0, 0, 0)
    assert out["partial"][0] and out["partial"][-1] and not any(out["partial"][1:-1])
    assert sum(out["hours"]) == pytest.approx((NOW - out["start"]) / 3600)
    # availability over exactly the window: 60 + 180 + 60 + 60 + 60 sent, 60 + 0 + 0 + 60 + 60 answered
    assert out["targets"][0]["availability_pct"] == round(100 * 180 / 420, 3)
    cells = out["targets"][0]["cells"]
    sunday, monday = cell_of(out, datetime(2026, 9, 6, 23, 59, tzinfo=NY).timestamp()), cell_of(
        out, datetime(2026, 9, 7, 0, 0, tzinfo=NY).timestamp())
    assert monday == sunday + 1  # the Sunday minute and the Monday minute are in different weeks
    assert cells[sunday] == "degraded" and cells[monday] == "ok"  # one dead minute is not in the Monday cell
    assert cells[cell_of(out, NOW - 100 * DAY)] == "outage" and cells[0] == "ok" and cells[-2] == "ok"
    assert cells[-1] == "no_data"  # the week in progress (from Monday 26 October) holds no probes
    await repo.close()


async def test_a_week_with_the_clocks_going_back_is_one_whole_cell_of_169_hours(tmp_path):
    repo = await make_repo(tmp_path)
    now = NOW + 10 * DAY  # 10 November
    sunday = datetime(2026, 11, 1, 23, 30, tzinfo=NY).timestamp()  # the last hour of the 25-hour day, in EST
    monday = datetime(2026, 11, 2, 0, 0, tzinfo=NY).timestamp()
    await repo.insert_samples(minute_samples("a", now - 120 * DAY, 0, 1) + minute_samples("a", sunday, 60, 2)
                              + minute_samples("a", monday, 0, 1))
    out = await build_uptime(repo, None, "all", now=now, tz=NY)
    assert out["cell"] == "week" and 169 in out["hours"] and not out["partial"][out["hours"].index(169)]
    i = cell_of(out, sunday)
    assert out["bounds"][i + 1] - out["bounds"][i] == 169 * 3600 and cell_of(out, monday) == i + 1
    cells = out["targets"][0]["cells"]
    assert cells[i] == "outage" and cells[i + 1] == "ok"  # two dead minutes in the week's last hour: its cell, not the next
    await repo.close()


async def test_all_is_cut_at_the_retention_when_the_oldest_probe_is_older(tmp_path):
    repo = await make_repo(tmp_path)
    await seed_weeks(repo)
    for retention, start, outages in ((99, NOW - 99 * DAY, 0), (365, NOW - 150 * DAY, 1)):
        out = await build_uptime(repo, config_keeping(retention), "all", now=NOW, tz=NY)
        assert out["start"] == start and out["cell"] == "week", retention
        assert out["targets"][0]["cells"].count("outage") == outages, retention
    await repo.close()


async def test_a_week_cell_counts_only_the_probes_inside_it(tmp_path):
    repo = await make_repo(tmp_path)
    await seed_weeks(repo)
    out = await build_uptime(repo, None, "all", now=NOW, tz=NY)
    from pengumon.reports.queries import block_stats
    rows = await block_stats(repo, out["start"], out["end"], 7 * DAY, NY, 3 * DAY)
    sent = {r["block"]: r["sent"] for r in rows}
    assert sum(sent.values()) == 420 and sorted(sent.values()) == [60, 60, 60, 60, 180]
    await repo.close()


async def test_the_endpoint_answers_90d_and_all(app_state, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        app_state.repository = await make_repo(tmp_path)
        for window, cell in (("90d", "day"), ("all", "day")):
            ok = await ac.get("/api/uptime", params={"window": window})
            assert ok.status_code == 200 and ok.json()["cell"] == cell and ok.json()["window"] == window
    await app_state.repository.close()
