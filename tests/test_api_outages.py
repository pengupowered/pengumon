"""Outages over the API: `outage_id` on GET /api/incidents (the same on both sides of a page edge) and GET /api/outages."""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.model import Incident
from pengumon.incidents.outage_read import outage_ids_since, outages_since, with_outage_ids
from pengumon.incidents.outages import Rules
from pengumon.main import app, security_config


@pytest.fixture
def open_access(monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)


async def add(repo, start, end, kind="isp_outage", cause_code="access_line_drop", severity="critical", title="drop",
              expected=False):
    iid = await repo.create_incident(Incident(start_time=start, cause_code=cause_code, title=title, description="d",
                                              severity=severity, kind=kind, expected=expected))
    if end is not None:
        await repo.resolve_incident(iid, end)
    return iid


async def get(path, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path, params=params)


@pytest.fixture
async def event(repo):
    """One event of 8 drops two minutes apart (each lasting 30 s), then a quiet hour, then one lone drop. Returns the
    ids, oldest first, and the start of the first."""
    base = time.time() - 6 * 3600
    ids = [await add(repo, base + k * 150, base + k * 150 + 30) for k in range(8)]
    lone = await add(repo, base + 3 * 3600, base + 3 * 3600 + 20, "isp_loss", severity="warning")
    return ids, lone, base


async def test_an_outage_that_straddles_a_page_edge_has_one_id_on_both_pages(app_state, repo, open_access, event):
    app_state.repository = repo
    ids, lone, _ = event
    app_state.config = AppConfig()
    first = (await get("/api/incidents", limit=5)).json()          # the lone drop + the 4 newest of the event
    second = (await get("/api/incidents", limit=5, offset=5)).json()   # the 4 older of the event
    assert [r["id"] for r in first] == [lone, *reversed(ids[-4:])]
    assert [r["id"] for r in second] == list(reversed(ids[:4]))
    assert {r["outage_id"] for r in first[1:]} == {ids[0]} == {r["outage_id"] for r in second}
    assert first[0]["outage_id"] == lone


async def test_a_page_of_only_the_newest_drops_still_names_the_outages_first_incident(app_state, repo, open_access):
    """A chain of drops 9 minutes apart for 5 hours: the page shows the last three, the first is far outside any one
    gap, so the read has to widen by time until the chain ends."""
    app_state.repository = repo
    app_state.config = AppConfig()
    base = time.time() - 8 * 3600
    ids = [await add(repo, base + k * 540, base + k * 540 + 20) for k in range(32)]
    page = (await get("/api/incidents", limit=3)).json()
    assert [r["id"] for r in page] == ids[:-4:-1]
    assert {r["outage_id"] for r in page} == {ids[0]}


async def test_incidents_that_are_in_no_outage_have_a_null_outage_id(app_state, repo, open_access):
    app_state.repository = repo
    app_state.config = AppConfig()
    now = time.time()
    await add(repo, now - 3000, now - 2990, "busy_latency", "load_induced_latency", "warning")
    await add(repo, now - 2000, now - 1990, expected=True)
    await add(repo, now - 1000, now - 990)
    rows = (await get("/api/incidents")).json()
    assert [r["outage_id"] for r in rows] == [rows[0]["id"], None, None]


async def test_with_grouping_off_every_groupable_incident_is_its_own_outage(app_state, repo, open_access, event):
    app_state.repository = repo
    app_state.config = AppConfig()
    app_state.config.incidents.group_gap_minutes = 0
    rows = (await get("/api/incidents")).json()
    assert [r["outage_id"] for r in rows] == [r["id"] for r in rows]
    body = (await get("/api/outages")).json()
    assert body["gap_minutes"] == 0 and len(body["outages"]) == 9 and all(o["drops"] == 1 for o in body["outages"])


async def test_the_gap_setting_regroups_history_with_no_restart(app_state, repo, open_access, event):
    app_state.repository = repo
    app_state.config = AppConfig()
    assert (await get("/api/outages")).json()["gap_minutes"] == 10
    assert [o["drops"] for o in (await get("/api/outages")).json()["outages"]] == [1, 8]
    app_state.config.incidents.group_gap_minutes = 1   # drops are 2 minutes apart (120 s between ends and starts)
    assert [o["drops"] for o in (await get("/api/outages")).json()["outages"]][:1] == [1]
    assert len((await get("/api/outages")).json()["outages"]) == 9


async def test_outages_come_newest_first_with_every_field(app_state, repo, open_access, event):
    app_state.repository = repo
    app_state.config = AppConfig()
    ids, lone, base = event
    body = (await get("/api/outages")).json()
    assert set(body) == {"gap_minutes", "outages"} and body["gap_minutes"] == 10
    newest, big = body["outages"]
    assert newest["id"] == lone and newest["incident_ids"] == [lone] and newest["severity"] == "warning"
    assert set(big) == {"id", "start_time", "end_time", "ongoing", "settling", "duration_seconds", "down_seconds",
                        "affected_seconds", "drops", "critical_drops", "severity", "places", "title", "cause_code",
                        "likely_maintenance", "incident_ids"}
    assert big["id"] == ids[0] and big["incident_ids"] == ids and big["drops"] == 8 and big["critical_drops"] == 8
    assert big["places"] == ["line"] and big["title"] == "drop" and big["ongoing"] is False and big["settling"] is False
    assert big["start_time"] == pytest.approx(base) and big["end_time"] == pytest.approx(base + 7 * 150 + 30)
    assert big["down_seconds"] == pytest.approx(8 * 30) and big["duration_seconds"] == pytest.approx(7 * 150 + 30)


async def test_the_period_is_by_overlap_and_limit_caps_the_list(app_state, repo, open_access, event):
    app_state.repository = repo
    app_state.config = AppConfig()
    ids, lone, base = event
    assert [o["id"] for o in (await get("/api/outages", hours=1)).json()["outages"]] == []
    # the big outage ended about 5.7 hours ago, the lone drop 3 hours ago
    assert [o["id"] for o in (await get("/api/outages", hours=4)).json()["outages"]] == [lone]
    assert [o["id"] for o in (await get("/api/outages", hours=6)).json()["outages"]] == [lone, ids[0]]
    assert [o["id"] for o in (await get("/api/outages", limit=1)).json()["outages"]] == [lone]


async def test_an_outage_that_began_before_the_period_but_reaches_into_it_is_listed_whole(app_state, repo, open_access):
    app_state.repository = repo
    app_state.config = AppConfig()
    now = time.time()
    ids = [await add(repo, now - 7200 + k * 300, now - 7200 + k * 300 + 60) for k in range(24)]   # 2 h to 5 min ago
    body = (await get("/api/outages", hours=1)).json()["outages"]
    assert [(o["id"], o["drops"]) for o in body] == [(ids[0], 24)]


async def test_an_open_incident_makes_an_ongoing_outage(app_state, repo, open_access):
    app_state.repository = repo
    app_state.config = AppConfig()
    now = time.time()
    first = await add(repo, now - 900, now - 800)
    await add(repo, now - 300, None)
    (o,) = (await get("/api/outages")).json()["outages"]
    assert o["id"] == first and o["ongoing"] is True and o["end_time"] is None and o["drops"] == 2


async def test_the_parameters_are_bounded(app_state, repo, open_access):
    app_state.repository = repo
    for params in ({"hours": 0}, {"hours": 10001}, {"limit": 0}, {"limit": 501}):
        assert (await get("/api/outages", **params)).status_code == 422, params
    assert (await get("/api/outages", hours=10000, limit=500)).status_code == 200


async def test_without_a_repository_the_answer_is_empty(app_state, open_access):
    app_state.config = AppConfig()
    assert (await get("/api/outages")).json() == {"gap_minutes": 10, "outages": []}


async def test_the_read_helpers_agree(repo):
    now = time.time()
    ids = [await add(repo, now - 3000 + k * 200, now - 3000 + k * 200 + 30) for k in range(5)]
    rules = Rules(600.0)
    found = await outages_since(repo, rules, now - 2500, now)
    assert [o.id for o in found] == [ids[0]] and len(found[0].members) == 5
    assert await outage_ids_since(repo, rules, now - 2500, now) == {i: ids[0] for i in ids}
    models = await repo.get_recent_incidents(limit=2, scope="network")
    assert [m.outage_id for m in await with_outage_ids(repo, rules, models, now)] == [ids[0], ids[0]]
    assert all(m.outage_id is None for m in models)   # the cached models are not touched
    assert await with_outage_ids(repo, rules, [], now) == []
