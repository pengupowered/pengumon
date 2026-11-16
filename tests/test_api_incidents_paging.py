"""GET /api/incidents pages with limit and offset: the dashboard loads a first page and the older ones on demand."""

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.model import Incident
from pengumon.main import app, security_config


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "paging.db"))
    await r.initialize()
    for i in range(12):  # titles "inc 0" (oldest) .. "inc 11" (newest)
        await r.create_incident(Incident(start_time=1000.0 + i, cause_code="unknown_outage", title=f"inc {i}", description="d",
                                         severity="warning", kind="gateway_down"))
    yield r
    await r.close()


@pytest.fixture
async def bare(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "range.db"))
    await r.initialize()
    yield r
    await r.close()


def _titles(rows):
    return [row["title"] for row in rows]


async def _get(**params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get("/api/incidents", params=params)


async def test_pages_follow_each_other_newest_first(app_state, repo, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = repo
    first = _titles((await _get(limit=5)).json())
    second = _titles((await _get(limit=5, offset=5)).json())
    third = _titles((await _get(limit=5, offset=10)).json())
    assert first == ["inc 11", "inc 10", "inc 9", "inc 8", "inc 7"]
    assert second == ["inc 6", "inc 5", "inc 4", "inc 3", "inc 2"]
    assert third == ["inc 1", "inc 0"]  # a short page: there is nothing older
    assert first + second + third == _titles((await _get(limit=100)).json())
    assert (await _get(limit=5, offset=12)).json() == []
    assert _titles((await _get(limit=3)).json()) == ["inc 11", "inc 10", "inc 9"]  # offset defaults to 0


async def test_offset_is_bounded(app_state, repo, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = repo
    assert (await _get(offset=-1)).status_code == 422
    assert (await _get(offset=1001)).status_code == 422


async def test_the_count_is_the_real_total_not_a_page(app_state, repo, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    await repo.create_incident(Incident(start_time=2000.0, cause_code="unknown_outage", title="busy", description="d",
                                        severity="warning", kind="gateway_down", expected=True))
    app_state.repository = repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/incidents/count")
    assert res.json() == {"total": 13, "expected": 1}


async def _codes(path, n, app_state, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = None  # the limit comes before the handler: each call that gets in answers 503
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return [(await ac.get(path)).status_code for _ in range(n)]


async def test_the_dossier_is_limited_to_twenty_a_minute(app_state, monkeypatch):
    assert await _codes("/api/incidents/dossier?hours=720", 21, app_state, monkeypatch) == [503] * 20 + [429]


async def test_the_inspector_is_limited_to_sixty_a_minute(app_state, monkeypatch):
    assert await _codes("/api/incidents/1/inspect", 61, app_state, monkeypatch) == [503] * 60 + [429]


async def test_one_dossier_is_built_at_a_time_and_a_second_asker_is_told_to_wait(app_state, repo, monkeypatch):
    """A build reads up to 720 hours of evidence; like the AI pack, the dossier has one build at a time."""
    import asyncio

    from pengumon.api import incidents as incidents_api

    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = repo
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await release.wait()
        return {"formatted_markdown": "# done"}
    monkeypatch.setattr(incidents_api, "build_dossier", slow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        first = asyncio.create_task(ac.get("/api/incidents/dossier?hours=24"))
        await started.wait()
        second = await ac.get("/api/incidents/dossier?hours=72")
        assert second.status_code == 429 and "Another dossier" in second.json()["detail"]
        assert second.headers["retry-after"] == "5"
        release.set()
        assert (await first).status_code == 200
        assert (await ac.get("/api/incidents/dossier?hours=72")).status_code == 200  # the lock was let go


async def test_a_range_answers_every_incident_that_overlaps_it(app_state, bare, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = bare
    for i in range(1200):  # far past the old 1000-row page limit
        iid = await bare.create_incident(Incident(start_time=5000.0 + i, cause_code="unknown_outage", title=f"r {i}", description="d",
                                                  severity="warning", kind="gateway_down"))
        await bare.resolve_incident(iid, 5000.0 + i + 0.5)
    rows = (await _get(since=5000.0)).json()
    assert len(rows) == 1200 and rows[0]["title"] == "r 1199" and rows[-1]["title"] == "r 0"  # newest first, none dropped
    assert len((await _get(since=5000.0 + 1000)).json()) == 200
    assert [r["title"] for r in (await _get(since=5000.0, until=5002.0)).json()] == ["r 2", "r 1", "r 0"]
    assert (await _get(since=9e9)).json() == []  # nothing is that new


async def test_a_range_includes_an_incident_that_began_before_it_and_one_still_open(app_state, bare, monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = bare
    straddler = await bare.create_incident(Incident(start_time=100.0, cause_code="unknown_outage", title="straddles", description="d",
                                                    severity="warning", kind="gateway_down"))
    await bare.resolve_incident(straddler, 300.0)
    await bare.create_incident(Incident(start_time=150.0, cause_code="unknown_outage", title="open", description="d",
                                        severity="warning", kind="gateway_down"))
    done = await bare.create_incident(Incident(start_time=10.0, cause_code="unknown_outage", title="over", description="d",
                                               severity="warning", kind="gateway_down"))
    await bare.resolve_incident(done, 20.0)
    titles = _titles((await _get(since=200.0)).json())
    assert titles == ["open", "straddles"]  # "over" ended before the range


async def test_a_range_beyond_the_bound_answers_its_newest_and_pages_on(app_state, repo, monkeypatch):
    from pengumon.api import incidents as api
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = repo
    assert (await _get(since=0, limit=api.RANGE_MAX + 1)).status_code == 422
    first = _titles((await _get(since=0, limit=5)).json())
    second = _titles((await _get(since=0, limit=5, offset=5)).json())
    assert first == ["inc 11", "inc 10", "inc 9", "inc 8", "inc 7"] and second[0] == "inc 6"
    assert (await _get(limit=1001)).status_code == 422  # without since the old bounds stay
    assert (await _get(until=5.0)).status_code == 422


async def test_the_list_fields_leave_out_the_path_trace(app_state, bare, monkeypatch):
    from pengumon.models.probes import PathTraceHop
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = bare
    iid = await bare.create_incident(Incident(start_time=7000.0, cause_code="unknown_outage", title="traced", description="d",
                                              severity="warning", kind="gateway_down"))
    await bare.update_incident_path_trace(iid, [PathTraceHop(hop=1, ip="192.0.2.1")])
    full = (await _get(since=7000.0)).json()[0]
    slim = (await _get(since=7000.0, fields="list")).json()[0]
    assert full["path_trace"] and slim["path_trace"] is None
    assert {k: v for k, v in full.items() if k != "path_trace"} == {k: v for k, v in slim.items() if k != "path_trace"}


async def test_the_list_fields_also_slim_a_plain_page(app_state, repo, monkeypatch):
    from pengumon.models.probes import PathTraceHop
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)
    app_state.repository = repo
    await repo.update_incident_path_trace(1, [PathTraceHop(hop=1, ip="192.0.2.1")])
    assert (await _get(limit=12)).json()[-1]["path_trace"]
    assert all(r["path_trace"] is None for r in (await _get(limit=12, fields="list")).json())
