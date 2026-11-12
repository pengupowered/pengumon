"""The answers of /api/report, /api/report/loss-grid and /api/uptime are kept for the rest of the minute
(reports/cache.py), and dropped at once when the path, the settings or the incidents change."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.api import reports as report_routes
from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.model import Incident
from pengumon.main import app, security_config
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.db.core import READERS
from pengumon.reports.cache import MAX_BUILDS, MAX_ENTRIES, AnswerCache

GW = TargetConfig(id="gw", name="Gateway", address="192.0.2.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop 1", address="198.51.100.9", category="isp")


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


async def test_an_answer_is_kept_for_its_minute_and_its_stamp_only():
    clock = Clock(6000.0)  # minute 100
    cache, builds = AnswerCache(clock), []

    async def build():
        builds.append(clock.now)
        return {"n": len(builds)}

    assert await cache.get("k", 1, build) == {"n": 1}
    clock.now = 6059.9
    assert await cache.get("k", 1, build) == {"n": 1}         # the same minute and stamp
    assert await cache.get("k", 2, build) == {"n": 2}         # another stamp
    assert await cache.get("other", 2, build) == {"n": 3}     # another key
    clock.now = 6060.0
    assert await cache.get("k", 2, build) == {"n": 4}         # the next minute
    for i in range(MAX_ENTRIES + 5):
        await cache.get(("many", i), 2, build)
    assert len(cache) == MAX_ENTRIES


@pytest.fixture
async def served(app_state, tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "c.db"), sample_flush_seconds=0)
    await repo.initialize()
    await repo.import_path([GW, ISP])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=now - 600 + i, success=True, latency_ms=2.0)
                               for t in (GW, ISP) for i in range(120)])
    app_state.repository = repo
    app_state.config = SimpleNamespace(targets=[GW, ISP], dns_probes=[])
    app_state.settings = SimpleNamespace(revision=1)
    yield app_state
    await repo.close()


async def test_a_second_ask_in_the_same_minute_is_not_built_again(served, monkeypatch):
    calls = {"report": 0, "loss-grid": 0, "uptime": 0}

    def counting(name, real):
        async def wrapper(*a, **k):
            calls[name] += 1
            return await real(*a, **k)
        return wrapper

    monkeypatch.setattr(report_routes, "build_report", counting("report", report_routes.build_report))
    monkeypatch.setattr(report_routes, "build_loss_grid", counting("loss-grid", report_routes.build_loss_grid))
    monkeypatch.setattr(report_routes, "build_uptime", counting("uptime", report_routes.build_uptime))
    served.answers = AnswerCache(Clock(time.time() // 60 * 60 + 1))  # early in a minute that does not end in the test
    async with _client() as ac:
        for _ in range(2):
            for url in ("/api/report?days=7", "/api/report/loss-grid?days=7", "/api/uptime?window=24h"):
                assert (await ac.get(url)).status_code == 200
        assert calls == {"report": 1, "loss-grid": 1, "uptime": 1}
        # other parameters are other answers
        await ac.get("/api/report?days=30")
        await ac.get("/api/uptime?window=24h&tz=Asia/Kolkata")
        assert calls == {"report": 2, "loss-grid": 1, "uptime": 2}
        served.settings.revision = 2  # a saved setting (uptime levels, the zone, ...) drops every kept answer
        await ac.get("/api/uptime?window=24h")
        assert calls["uptime"] == 3


async def test_a_new_incident_shows_at_once(served):
    served.answers = AnswerCache(Clock(time.time() // 60 * 60 + 1))
    async with _client() as ac:
        assert (await ac.get("/api/report?days=1")).json()["incidents"]["count"] == 0
        now = time.time()
        await served.repository.create_incident(Incident(
            start_time=now - 300, end_time=now - 240, cause_code="access_line_drop", title="t", description="d",
            severity="warning", affected_targets=["isp_1"], kind="isp_loss"))
        assert (await ac.get("/api/report?days=1")).json()["incidents"]["count"] == 1


async def test_a_path_edit_shows_at_once(served):
    served.answers = AnswerCache(Clock(time.time() // 60 * 60 + 1))
    async with _client() as ac:
        ids = lambda body: [t["target_id"] for t in body["targets"]]  # noqa: E731
        assert ids((await ac.get("/api/uptime")).json()) == ["gw", "isp_1"]
        assert sorted(h["target_id"] for h in (await ac.get("/api/report?days=1")).json()["availability"]["hops"]) \
            == ["gw", "isp_1"]
        await served.repository.save_path(1, [GW])  # the hop is removed from the path: its history is hidden
        assert ids((await ac.get("/api/uptime")).json()) == ["gw"]
        assert [h["target_id"] for h in (await ac.get("/api/report?days=1")).json()["availability"]["hops"]] == ["gw"]
        await served.repository.delete_path_history("isp_1")
        served.repository.hidden_targets = set()  # shown again if anything were left: nothing is
        assert ids((await ac.get("/api/uptime")).json()) == ["gw"]


async def test_misses_wait_for_a_turn_and_one_built_meanwhile_is_taken():
    cache, release, running, peak, builds = AnswerCache(Clock(6000.0)), asyncio.Event(), [0], [0], []

    async def build(n):
        builds.append(n)
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await release.wait()
        running[0] -= 1
        return n

    distinct = [asyncio.create_task(cache.get(("k", n), 1, lambda n=n: build(n))) for n in range(6)]
    same = [asyncio.create_task(cache.get("same", 1, lambda: build("same"))) for _ in range(4)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert running[0] == MAX_BUILDS and cache.building == MAX_BUILDS
    release.set()
    assert await asyncio.gather(*distinct) == list(range(6)) and await asyncio.gather(*same) == ["same"] * 4
    assert peak[0] == MAX_BUILDS and builds.count("same") == 1 and cache.building == 0
    assert MAX_BUILDS < READERS


async def test_report_misses_with_ever_changing_parameters_leave_a_reader_for_everything_else(served, monkeypatch):
    # each miss holds a database reader while it builds; enough of them at once took all READERS, and every other read
    # of the app waited behind them
    release, holding = asyncio.Event(), [0]

    async def slow_build(repository, *args, **kwargs):
        async with repository.read():
            holding[0] += 1
            await release.wait()
            holding[0] -= 1
        return {"built": True}

    monkeypatch.setattr(report_routes, "build_report", slow_build)
    served.answers = AnswerCache(Clock(time.time() // 60 * 60 + 1))
    async with _client() as ac:
        asks = [asyncio.create_task(ac.get(f"/api/report?days={days}")) for days in range(1, READERS + 4)]
        for _ in range(100):
            if holding[0] >= MAX_BUILDS:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        try:
            assert await asyncio.wait_for(served.repository.get_meta("nothing-here"), 2) is None  # a quick read
            assert holding[0] == MAX_BUILDS
        finally:
            release.set()
        answers = await asyncio.gather(*asks)
    assert [a.status_code for a in answers] == [200] * (READERS + 3)


async def test_answers_that_are_built_count_against_a_rate_limit_and_kept_ones_do_not(served, monkeypatch):
    async def quick(*args, **kwargs):
        return {"built": True}

    for name in ("build_report", "build_loss_grid", "build_uptime"):
        monkeypatch.setattr(report_routes, name, quick)
    served.answers = AnswerCache(Clock(time.time() // 60 * 60 + 1))
    async with _client() as ac:
        built = [(await ac.get(f"/api/report?days={days}")).status_code for days in range(1, 61)]
        kept = [(await ac.get("/api/report?days=60")).status_code for _ in range(20)]  # (the newest of the 32 kept)
        refused = [await ac.get(url) for url in ("/api/report?days=61", "/api/report/loss-grid?days=3",
                                                 "/api/uptime?window=7d")]
    assert built == [200] * 60 and kept == [200] * 20
    assert [r.status_code for r in refused] == [429] * 3 and "Retry-After" in refused[0].headers
