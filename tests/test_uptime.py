"""GET /api/uptime: availability strips per target from the rollups."""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

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
    assert utc["blocks"] == 7 and len(utc["targets"][0]["cells"]) == 7
    assert utc["targets"][0]["cells"][3] == "outage" and utc["targets"][0]["cells"][6] == "no_data"
    month = await build_uptime(repo, None, "30d", now=NOW, tz=timezone(timedelta(minutes=-480)))
    assert len(month["targets"][0]["cells"]) == 30 and month["targets"][0]["cells"].count("outage") == 1
    assert month["tz_offset_minutes"] == -480 and month["end"] - month["start"] == 30 * 86400
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
    assert out["zone"] == "America/New_York" and len(bounds) == 8
    assert bounds[0] == out["start"] == nov1 - 4 * 86400 + 4 * 3600 and bounds[-1] == out["end"]
    assert [b - a for a, b in zip(bounds, bounds[1:])] == [86400] * 4 + [90000] + [86400] * 2
    assert out["targets"][0]["cells"][4] == "outage" and bounds[4] <= dead < bounds[5]
    await repo.close()


async def test_the_endpoint_validates_and_answers(app_state, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/uptime")).status_code == 503
        app_state.repository = await make_repo(tmp_path)
        ok = await ac.get("/api/uptime", params={"window": "7d"})
        assert ok.status_code == 200 and ok.json()["blocks"] == 7
        assert (await ac.get("/api/uptime", params={"window": "1y"})).status_code == 422
        assert (await ac.get("/api/uptime", params={"tz_offset_minutes": -900})).status_code == 422
    await app_state.repository.close()
