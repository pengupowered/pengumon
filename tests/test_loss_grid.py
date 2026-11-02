"""GET /api/report/loss-grid: loss per calendar day and hour from the rollups."""

import time

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
    east = await build_loss_grid(repo, 2, now=NOON, tz_offset_minutes=60)  # 00:30 on 1 November
    assert utc["days"][-1] == "2026-10-31" and utc["lost"][-1][23] == 1
    assert east["days"][-1] == "2026-10-31"  # it is 13:00 there at NOON: still the 31st
    east_later = await build_loss_grid(repo, 2, now=NOON + 12 * 3600, tz_offset_minutes=60)
    assert east_later["days"][-1] == "2026-11-01" and east_later["lost"][-1][0] == 1
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
