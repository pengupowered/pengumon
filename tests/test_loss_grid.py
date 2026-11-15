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
    # the last 7 x 24 hours to the end of the hour in progress (13:00 on the 31st): 8 dates, the first and last cut
    assert grid["days"][-1] == "2026-10-31" and grid["days"][0] == "2026-10-24" and len(grid["days"]) == 8
    assert grid["weekdays"][-1] == 5  # Saturday
    assert all(len(row) == 24 for key in ("sent", "lost", "loss_pct", "outside") for row in grid[key])
    assert len(grid["sent"]) == 8
    assert grid["end"] - grid["start"] == 7 * 86400 and grid["end"] == NOON + 3600
    assert grid["outside"][0] == [True] * 13 + [False] * 11 and grid["outside"][-1] == [False] * 13 + [True] * 11
    assert not any(any(row) for row in grid["outside"][1:-1])
    assert sum(map(sum, grid["outside"])) == 8 * 24 - 7 * 24
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
    assert grid["days"] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"]
    assert (grid["sent"][2][14], grid["lost"][2][14], grid["loss_pct"][2][14]) == (120, 10, round(10 * 100 / 120, 3))
    assert (grid["sent"][3][11], grid["loss_pct"][3][11]) == (30, 0.0)
    assert grid["loss_pct"][1][14] is None
    await repo.close()


async def test_the_time_zone_moves_hours_and_days(tmp_path):
    repo = await make_repo(tmp_path)
    late = NOON + 11.75 * 3600  # 23:45 UTC
    await repo.insert_samples([sample("a", NOON + 11 * 3600 + 30 * 60, False)])  # 23:30 UTC on the 31st
    utc = await build_loss_grid(repo, 2, now=late)
    east = await build_loss_grid(repo, 2, now=late, tz=timezone(timedelta(minutes=60)))  # 00:30 on 1 November
    assert utc["days"][-1] == "2026-10-31" and utc["lost"][-1][23] == 1
    assert east["days"][-1] == "2026-11-01" and east["lost"][-1][0] == 1
    noon = await build_loss_grid(repo, 2, now=NOON, tz=timezone(timedelta(minutes=60)))
    assert noon["days"][-1] == "2026-10-31"  # it is 13:00 there at NOON: still the 31st
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
    grid = await build_loss_grid(repo, 3, now=nov1 + 36 * 3600, tz=ZoneInfo("America/New_York"))
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
        assert ok.status_code == 200 and len(ok.json()["days"]) in (2, 3)
        assert (await ac.get("/api/report/loss-grid", params={"days": 0})).status_code == 422
        assert (await ac.get("/api/report/loss-grid", params={"days": 3651})).status_code == 422
        assert (await ac.get("/api/report/loss-grid", params={"days": 32})).json()["fold"] == "weekday"
        assert (await ac.get("/api/report/loss-grid", params={"tz_offset_minutes": 900})).status_code == 422
    await app_state.repository.close()


# --- long ranges: folded to one row per day of the week (0.28 step 7) ------------------------------------------------

NY = ZoneInfo("America/New_York")


def hourly_probes(first_hour_back, last_hour_back):
    """Three probes at the start of every hour from `first_hour_back` to `last_hour_back` hours before NOON (UTC), the
    number lost (0-2) following the New York date and hour so each weekday-hour gets its own totals."""
    from datetime import datetime
    out = []
    for back in range(first_hour_back, last_hour_back + 1):
        t = NOON - back * 3600
        local = datetime.fromtimestamp(t, NY)
        lost = (local.toordinal() + local.hour) % 3
        out += [sample("a", t + i, i >= lost) for i in range(3)]
    return out


def counted(probes, start, end):
    """Independent count: (sent, lost) per [weekday][hour] on the New York clock of the probes in [start, end)."""
    from datetime import datetime
    sent = [[0] * 24 for _ in range(7)]
    lost = [[0] * 24 for _ in range(7)]
    for p in probes:
        if start <= p.timestamp < end:
            local = datetime.fromtimestamp(p.timestamp, NY)
            sent[local.weekday()][local.hour] += 1
            lost[local.weekday()][local.hour] += 0 if p.success else 1
    return sent, lost


async def test_more_than_31_days_fold_into_seven_weekday_rows_that_add_up(tmp_path):
    repo = await make_repo(tmp_path)
    probes = hourly_probes(0, 70 * 24)  # 70 days, 10 more than the window
    await repo.insert_samples(probes)
    grid = await build_loss_grid(repo, 60, now=NOON, tz=NY)
    assert grid["fold"] == "weekday" and grid["days"] == 60 and grid["weekdays"] == list(range(7))
    assert grid["end"] - grid["start"] == 60 * 86400 and grid["end"] == NOON + 3600 and grid["zone"] == "America/New_York"
    assert all(len(grid[k]) == 7 and all(len(r) == 24 for r in grid[k]) for k in ("sent", "lost", "loss_pct", "outside"))
    assert not any(any(r) for r in grid["outside"]) and "day_starts" not in grid
    sent, lost = counted(probes, grid["start"], grid["end"])  # the window only: the 10 older days are left out
    assert grid["sent"] == sent and grid["lost"] == lost
    cell = next((d, h) for d in range(7) for h in range(24) if lost[d][h])
    assert grid["loss_pct"][cell[0]][cell[1]] == round(lost[cell[0]][cell[1]] * 100.0 / sent[cell[0]][cell[1]], 3)
    await repo.close()


async def test_the_fold_sums_what_the_per_date_grid_shows_over_the_same_window(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_samples(hourly_probes(0, 29 * 24))  # data only in the last 29 days: inside both windows
    dated = await build_loss_grid(repo, 31, now=NOON, tz=NY)
    folded = await build_loss_grid(repo, 45, now=NOON, tz=NY)
    assert "fold" not in dated and folded["fold"] == "weekday"
    for key in ("sent", "lost"):
        by_weekday = [[0] * 24 for _ in range(7)]
        for weekday, row in zip(dated["weekdays"], dated[key]):
            for h, v in enumerate(row):
                by_weekday[weekday][h] += v
        assert folded[key] == by_weekday, key
    await repo.close()


async def test_31_days_stay_a_per_date_grid_and_the_endpoint_folds_longer_ones(app_state, tmp_path):
    repo = await make_repo(tmp_path)
    assert "fold" not in await build_loss_grid(repo, 31, now=NOON)
    assert (await build_loss_grid(repo, 32, now=NOON))["fold"] == "weekday"
    await repo.close()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        app_state.repository = await make_repo(tmp_path)
        out = await ac.get("/api/report/loss-grid", params={"days": 365})
        assert out.status_code == 200 and out.json()["fold"] == "weekday" and len(out.json()["sent"]) == 7
        assert (await ac.get("/api/report/loss-grid", params={"days": 3650})).status_code == 200
    await app_state.repository.close()
