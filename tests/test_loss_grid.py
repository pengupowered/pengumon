"""GET /api/report/loss-grid: loss per calendar day and hour from the rollups."""

import time
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from httpx import ASGITransport, AsyncClient

from pengumon.db.repository import DatabaseRepository
from pengumon.main import app
from pengumon.models.probes import ProbeSample
from pengumon.reports.loss_grid import build_loss_grid

NOON = 1_793_448_000.0  # 2026-10-31 12:00:00 UTC


def sample(target, ts, ok):
    return ProbeSample(target_id=target, timestamp=ts, success=ok, latency_ms=5.0 if ok else None,
                       packet_loss_pct=0.0 if ok else 100.0, jitter_ms=None, error=None if ok else "x")


async def make_repo(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "g.db"), sample_flush_seconds=0)
    await repo.initialize()
    return repo


async def test_an_empty_database_gives_a_full_grid_of_nulls(tmp_path):
    repo = await make_repo(tmp_path)
    grid = await build_loss_grid(repo, 7, now=NOON)
    assert grid["days"][-1] == "2026-10-31" and grid["days"][0] == "2026-10-25" and len(grid["days"]) == 7
    assert grid["weekdays"][-1] == 5  # Saturday
    assert all(len(row) == 24 for key in ("sent", "lost", "loss_pct") for row in grid[key]) and len(grid["sent"]) == 7
    assert all(v is None for row in grid["loss_pct"] for v in row) and sum(map(sum, grid["sent"])) == 0
    await repo.close()


async def test_loss_lands_in_its_day_and_hour_across_targets(tmp_path):
    repo = await make_repo(tmp_path)
    base = NOON - 86400 + 3600 * 2  # yesterday 14:00 UTC
    rows = [sample("a", base + i, i >= 10) for i in range(60)]  # 10 of 60 lost on a
    rows += [sample("b", base + i, True) for i in range(60)]
    rows += [sample("a", NOON - 3600 + i, True) for i in range(30)]  # today 11:00
    await repo.insert_samples(rows)
    grid = await build_loss_grid(repo, 3, now=NOON)
    assert grid["days"] == ["2026-10-29", "2026-10-30", "2026-10-31"]
    assert (grid["sent"][1][14], grid["lost"][1][14], grid["loss_pct"][1][14]) == (120, 10, round(10 * 100 / 120, 3))
    assert (grid["sent"][2][11], grid["loss_pct"][2][11]) == (30, 0.0)
    assert grid["loss_pct"][0][14] is None
    await repo.close()


async def test_the_time_zone_moves_hours_and_days(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples([sample("a", NOON + 11 * 3600 + 30 * 60, False)])  # 23:30 UTC on the 31st
    utc = await build_loss_grid(repo, 2, now=NOON)
    east = await build_loss_grid(repo, 2, now=NOON, tz=timezone(timedelta(minutes=60)))  # 00:30 on 1 November
    assert utc["days"][-1] == "2026-10-31" and utc["lost"][-1][23] == 1
    assert east["days"][-1] == "2026-10-31"  # it is 13:00 there at NOON: still the 31st
    east_later = await build_loss_grid(repo, 2, now=NOON + 12 * 3600, tz=timezone(timedelta(minutes=60)))
    assert east_later["days"][-1] == "2026-11-01" and east_later["lost"][-1][0] == 1
    await repo.close()


async def test_a_named_zone_follows_daylight_saving_and_the_day_it_ends_has_25_hours(tmp_path):
    """New York, 1 November 2026: 02:00 EDT becomes 01:00 EST at 06:00 UTC. Both passes through 01:30 land in hour 1
    of that day, and 23:30 EST still belongs to it (a fixed offset taken before the change would put it on the 2nd)."""
    repo = await make_repo(tmp_path)
    nov1 = NOON + 12 * 3600  # 2026-11-01 00:00 UTC
    await repo.insert_samples([sample("a", nov1 - 20.5 * 3600, False),   # 23:30 EDT on 30 October
                               sample("a", nov1 + 5.5 * 3600, False),    # 01:30 EDT
                               sample("a", nov1 + 6.5 * 3600, False),    # 01:30 EST, an hour later
                               sample("a", nov1 + 28.5 * 3600, False)])  # 23:30 EST on 1 November
    grid = await build_loss_grid(repo, 4, now=nov1 + 36 * 3600, tz=ZoneInfo("America/New_York"))
    assert grid["days"] == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    assert (grid["lost"][0][23], grid["lost"][2][1], grid["lost"][2][23], sum(grid["sent"][3])) == (1, 2, 1, 0)
    assert grid["tz_offset_minutes"] == -300 and grid["zone"] == "America/New_York"
    # when each cell is, for the dashboard to click through: midnights at 04:00 UTC, then at 05:00 from 2 November
    assert grid["day_starts"] == [nov1 - 2 * 86400 + 4 * 3600, nov1 - 86400 + 4 * 3600, nov1 + 4 * 3600,
                                  nov1 + 86400 + 5 * 3600, nov1 + 2 * 86400 + 5 * 3600]
    hours = grid["hour_starts"][2]  # 1 November: hour 1 runs from 01:00 EDT to 02:00 EST, two hours
    assert [hours[h] - nov1 for h in (0, 1, 2, 3)] == [4 * 3600, 5 * 3600, 7 * 3600, 8 * 3600]
    assert len(hours) == 25 and hours[24] == grid["day_starts"][3]
    assert [grid["hour_starts"][d] for d in (0, 1, 3)] == [None, None, None]
    await repo.close()


async def test_the_endpoint_validates_and_answers(app_state, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/report/loss-grid")).status_code == 503  # no repository yet
        app_state.repository = await make_repo(tmp_path)
        ok = await ac.get("/api/report/loss-grid", params={"days": 2})
        assert ok.status_code == 200 and len(ok.json()["days"]) == 2
        assert (await ac.get("/api/report/loss-grid", params={"days": 0})).status_code == 422
        assert (await ac.get("/api/report/loss-grid", params={"days": 32})).status_code == 422
        assert (await ac.get("/api/report/loss-grid", params={"tz_offset_minutes": 900})).status_code == 422
    await app_state.repository.close()
