"""Schema v17 and the speed test's storage, retention, and the two API routes (api/speedtests.py)."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import security
from pengumon.config import AppConfig
from pengumon.db import migrations
from pengumon.main import app, security_config
from pengumon.speedtest.runner import SpeedTestRunner
from pengumon.speedtest.schedule import slot_after

from test_speedtest_runner import FakeProvider

TOKEN = "s3cret-token"
DAY = 86400.0


def row(ts, **kw):
    return {"ts": ts, "trigger": "scheduled", "provider": "cloudflare", "status": "ok", "duration_s": 12.0, **kw}


# --- schema v17 ----------------------------------------------------------------------------------------------------

async def test_v17_adds_the_speed_test_table_to_a_v16_database_and_keeps_what_was_there(tmp_path, monkeypatch):
    path = str(tmp_path / "v16.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 16)
    assert await migrations.migrate(path) == 16
    monkeypatch.undo()
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    con.commit()
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'speed_tests'").fetchall()
    con.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION == 17
    con = sqlite3.connect(path)
    columns = [(c[1], c[2], c[5]) for c in con.execute("PRAGMA table_info(speed_tests)")]
    assert columns == [("id", "INTEGER", 1), ("ts", "REAL", 0), ("duration_s", "REAL", 0), ("trigger", "TEXT", 0),
                       ("provider", "TEXT", 0), ("server", "TEXT", 0), ("status", "TEXT", 0), ("reason", "TEXT", 0),
                       ("down_mbps", "REAL", 0), ("up_mbps", "REAL", 0), ("idle_ms", "REAL", 0),
                       ("loaded_down_ms", "REAL", 0), ("loaded_up_ms", "REAL", 0), ("bytes_down", "INTEGER", 0),
                       ("bytes_up", "INTEGER", 0), ("wan_down_mbps", "REAL", 0), ("wan_up_mbps", "REAL", 0)]
    assert con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'speed_tests' AND "
                       "name NOT LIKE 'sqlite_%'").fetchall() == [("idx_speed_tests_ts",)]
    assert con.execute("SELECT value FROM meta WHERE key = 'k'").fetchone() == ("v",)
    assert con.execute("PRAGMA user_version").fetchone() == (17,)
    con.close()
    assert await migrations.migrate(path) == 17                      # runs once


async def test_a_new_database_has_the_table(repo):
    assert await repo.get_speed_tests(0, 10**12) == []


# --- the queries ---------------------------------------------------------------------------------------------------

async def test_tests_are_stored_and_listed_newest_first_within_a_range_and_a_limit(repo):
    ids = [await repo.insert_speed_test(row(1000.0 + n * 100, down_mbps=n, bytes_down=n)) for n in range(5)]
    assert ids == [1, 2, 3, 4, 5]
    listed = await repo.get_speed_tests(1100.0, 1300.0)
    assert [r["down_mbps"] for r in listed] == [3, 2, 1]
    assert [r["id"] for r in await repo.get_speed_tests(0, 10**9, limit=2)] == [5, 4]
    first = (await repo.get_speed_tests(0, 10**9))[-1]
    assert first["status"] == "ok" and first["reason"] is None and first["idle_ms"] is None and first["server"] is None


async def test_the_latest_test_can_be_asked_for_by_status(repo):
    assert await repo.latest_speed_test() is None
    await repo.insert_speed_test(row(100.0, down_mbps=50.0))
    await repo.insert_speed_test(row(200.0, status="skipped", reason="line busy", duration_s=None))
    assert (await repo.latest_speed_test())["status"] == "skipped"
    assert (await repo.latest_speed_test(["ok", "partial"]))["down_mbps"] == 50.0
    assert await repo.latest_speed_test(["failed"]) is None


async def test_an_unknown_column_is_refused(repo):
    with pytest.raises(ValueError, match="unknown speed test column"):
        await repo.insert_speed_test(row(1.0, bogus=1))


async def test_the_bytes_of_a_range_count_both_directions_and_skips_count_nothing(repo):
    await repo.insert_speed_test(row(100.0, bytes_down=25_000_000, bytes_up=10_000_000))
    await repo.insert_speed_test(row(200.0, bytes_down=5, bytes_up=None))                   # one direction failed
    await repo.insert_speed_test(row(300.0, status="skipped", duration_s=None))
    await repo.insert_speed_test(row(400.0, bytes_down=1000))
    assert await repo.speed_test_bytes(100.0, 400.0) == 35_000_005               # [start, end): 400 is out
    assert await repo.speed_test_bytes(0, 100.0) == 0
    assert await repo.speed_test_bytes(100.0, 401.0) == 35_001_005


# --- retention -----------------------------------------------------------------------------------------------------

async def test_retention_removes_tests_older_than_the_rollup_retention_and_keeps_newer_ones(repo):
    now = time.time()
    await repo.insert_speed_test(row(now - 61 * DAY))
    await repo.insert_speed_test(row(now - 59 * DAY))
    await repo.insert_speed_test(row(now - 1 * DAY))
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=60)
    assert removed["speed_tests"] == 1
    assert len(await repo.get_speed_tests(0, now + 1)) == 2
    assert (await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=60))["speed_tests"] == 0


async def test_retention_keeps_a_month_and_a_day_of_tests_for_the_monthly_cap(repo):
    """The shortest rollup retention (30 days) is shorter than a 31-day month: on the 31st, the tests of the 1st would
    be gone and the month's data use would read low."""
    now = time.time()
    await repo.insert_speed_test(row(now - 33 * DAY, bytes_down=1))
    await repo.insert_speed_test(row(now - 31 * DAY, bytes_down=10))
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=30)
    assert removed["speed_tests"] == 1
    assert await repo.speed_test_bytes(0, now) == 10


async def test_the_default_retention_keeps_a_year_of_tests(repo):
    now = time.time()
    await repo.insert_speed_test(row(now - 300 * DAY))
    await repo.insert_speed_test(row(now - 500 * DAY))
    assert (await repo.cleanup_old_samples())["speed_tests"] == 1


# --- the routes ----------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _open_app(monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", []), ("view_without_token", False)):
        monkeypatch.setattr(security_config, name, value)
    security.reset_rate_limits()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def runner_for(repo, app_state, **speedtest):
    config = AppConfig(speedtest={"enabled": True, **speedtest}, general={"timezone": "UTC"}, line={"down_mbps": 500.0})
    runner = SpeedTestRunner(config, repo, clock=time.time, offset_source=lambda: 0,
                             provider_factory=lambda cfg: FakeProvider(SimpleNamespace(now=0.0)))
    app_state.config, app_state.repository, app_state.speedtest = config, repo, runner
    return runner, config


async def settle(runner):
    await asyncio.gather(*list(runner._tasks))


async def test_get_before_the_app_has_started_answers_empty(app_state, client):
    async with client as ac:
        res = await ac.get("/api/speedtests")
    assert res.status_code == 200
    assert res.json() == {"enabled": False, "provider": None, "next_run": None, "running": False, "month_used_mb": 0,
                          "monthly_cap_mb": 0, "plan": {"down_mbps": None, "up_mbps": None}, "tests": []}


async def test_get_lists_the_tests_with_their_grade_and_the_state(app_state, repo, client):
    runner, config = runner_for(repo, app_state, monthly_cap_mb=500)
    now = time.time()
    await repo.insert_speed_test(row(now - 3600, down_mbps=480.2, up_mbps=22.0, idle_ms=20.0, loaded_down_ms=45.0,
                                     loaded_up_ms=120.0, bytes_down=25_000_000, bytes_up=10_000_000, server="FRA"))
    await repo.insert_speed_test(row(now - 100 * 3600, down_mbps=470.0))                        # outside 24 h
    await repo.insert_speed_test(row(now - 7200, status="skipped", reason="line busy", duration_s=None))
    async with client as ac:
        data = (await ac.get("/api/speedtests", params={"hours": 24})).json()
    assert (data["enabled"], data["provider"], data["running"]) == (True, "cloudflare", False)
    assert data["monthly_cap_mb"] == 500 and data["plan"] == {"down_mbps": 500.0, "up_mbps": None}
    assert data["month_used_mb"] == round(await runner.month_used_bytes() / 1e6, 1)      # (an hour ago may be last month)
    # the first slot (00:00 UTC, offset 0) after the last test an hour ago: in the past in the first hour after midnight
    # UTC, when that slot is due and the next tick runs it
    assert data["next_run"] == await runner.next_run() == slot_after(now - 3600, 86400.0, 0)
    assert [t["status"] for t in data["tests"]] == ["ok", "skipped"]            # newest first, the old one left out
    ok, skipped = data["tests"]
    assert ok["bufferbloat"] == "C" and ok["server"] == "FRA" and ok["down_mbps"] == 480.2   # 120 - 20 = 100 ms added
    assert skipped["bufferbloat"] is None and skipped["reason"] == "line busy"
    # 25 MB at 480 Mbps is 0.4 s: too little to trust (speedtest/quality.py); a skipped test has no speed to judge
    assert ok["confidence"] == "low" and ok["note"].startswith("download: 25 MB lasted 0.4 s")
    assert skipped["confidence"] is None and skipped["note"] is None
    assert {"id", "ts", "duration_s", "trigger", "provider", "status", "idle_ms", "loaded_down_ms", "loaded_up_ms",
            "bytes_down", "bytes_up", "wan_down_mbps", "wan_up_mbps", "bufferbloat", "confidence", "note"} <= set(ok)


async def test_each_test_carries_its_headline_and_how_it_was_measured(app_state, repo, client):
    runner_for(repo, app_state)
    now = time.time()
    await repo.insert_speed_test(row(now - 60, down_mbps=300.0, up_mbps=38.0, bytes_down=300_000_000,
                                     bytes_up=38_000_000, wan_down_mbps=905.0, wan_up_mbps=40.5))
    await repo.insert_speed_test(row(now - 120, down_mbps=670.0, up_mbps=41.0, bytes_down=670_000_000,
                                     bytes_up=41_000_000))
    async with client as ac:
        by_router, by_test = (await ac.get("/api/speedtests", params={"hours": 1})).json()["tests"]
    assert (by_router["headline_down_mbps"], by_router["headline_up_mbps"], by_router["measured_by"]) == (905.0, 40.5,
                                                                                                        "router")
    assert (by_router["down_measured_by"], by_router["up_measured_by"]) == ("router", "router")
    assert (by_router["down_mbps"], by_router["up_mbps"]) == (300.0, 38.0)         # the test's own, kept
    assert "300 of the 905 Mbps the line carried" in by_router["traffic_note"]
    assert (by_test["headline_down_mbps"], by_test["measured_by"], by_test["traffic_note"]) == (670.0, "test", None)


async def test_the_state_when_it_is_off(app_state, repo, client):
    runner_for(repo, app_state, enabled=False)
    async with client as ac:
        data = (await ac.get("/api/speedtests")).json()
    assert data["enabled"] is False and data["next_run"] is None


async def test_limit_and_hours_are_bounded_and_applied(app_state, repo, client):
    runner_for(repo, app_state)
    now = time.time()
    for n in range(5):
        await repo.insert_speed_test(row(now - 100 * (n + 1)))
    async with client as ac:
        assert len((await ac.get("/api/speedtests", params={"limit": 2})).json()["tests"]) == 2
        assert len((await ac.get("/api/speedtests")).json()["tests"]) == 5
        for bad in ({"hours": 0}, {"hours": 8761}, {"limit": 0}, {"limit": 501}, {"hours": "x"}):
            assert (await ac.get("/api/speedtests", params=bad)).status_code == 422, bad
        assert (await ac.get("/api/speedtests", params={"hours": 8760, "limit": 500})).status_code == 200


async def test_post_run_starts_a_manual_test_in_the_background(app_state, repo, client):
    runner, _ = runner_for(repo, app_state)
    async with client as ac:
        res = await ac.post("/api/speedtests/run")
        assert res.status_code == 202 and res.json() == {"started": True}
        await settle(runner)
        tests = (await ac.get("/api/speedtests")).json()["tests"]
    assert [(t["trigger"], t["status"]) for t in tests] == [("manual", "ok")]


async def test_post_run_answers_409_with_the_reason_when_refused(app_state, repo, client):
    runner, config = runner_for(repo, app_state, enabled=False)
    async with client as ac:
        res = await ac.post("/api/speedtests/run")
        assert res.status_code == 409 and res.json() == {"started": False, "reason": "the speed test is off"}
        config.speedtest.enabled = True
        runner.running = True
        res = await ac.post("/api/speedtests/run")
        assert res.status_code == 409 and res.json()["reason"] == "a speed test is already running"
        runner.running = False
        config.speedtest.monthly_cap_mb = 1
        await repo.insert_speed_test(row(time.time(), bytes_down=5_000_000))
        res = await ac.post("/api/speedtests/run")
        assert res.status_code == 409 and res.json()["reason"].startswith("the monthly data cap is reached")


async def test_post_run_before_the_app_has_started_is_a_503(app_state, client):
    async with client as ac:
        assert (await ac.post("/api/speedtests/run")).status_code == 503


async def test_post_needs_the_token_when_one_is_set_and_get_too(app_state, repo, client, monkeypatch):
    runner, _ = runner_for(repo, app_state)
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with client as ac:
        assert (await ac.post("/api/speedtests/run")).status_code == 401
        assert (await ac.post("/api/speedtests/run", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await ac.get("/api/speedtests")).status_code == 401
        assert runner.running is False and await repo.get_speed_tests(0, 10**12) == []
        good = {"Authorization": f"Bearer {TOKEN}"}
        assert (await ac.get("/api/speedtests", headers=good)).status_code == 200
        assert (await ac.post("/api/speedtests/run", headers=good)).status_code == 202
        await settle(runner)


async def test_post_run_is_rate_limited_to_three_in_ten_minutes(app_state, repo, client):
    runner, _ = runner_for(repo, app_state)
    async with client as ac:
        codes = []
        for _ in range(4):
            codes.append((await ac.post("/api/speedtests/run")).status_code)
            await settle(runner)
    assert codes == [202, 202, 202, 429]


async def test_both_routes_are_in_the_openapi_schema_whether_or_not_it_is_on():
    paths = app.openapi()["paths"]
    assert "get" in paths["/api/speedtests"] and "post" in paths["/api/speedtests/run"]
