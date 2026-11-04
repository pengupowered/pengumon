"""The AI pack's delivery: GET /api/ai-pack (defaults, who may ask, limits, the counter). The pack itself is built by
pengumon.reports.ai_pack and tested there; here `build_ai_pack` is a stub that records how it was called."""

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import obs_helpers
from pengumon.api import reports as reports_api
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.model import Incident
from pengumon.main import app, security_config

obs_helpers.install()
TOKEN = "s3cret-token"


@pytest.fixture
def stub(monkeypatch):
    """A stand-in `pengumon.reports.ai_pack` that records each call and answers a small pack."""
    calls, gate = [], asyncio.Event()
    gate.set()

    async def build_ai_pack(repository, config, **kwargs):
        calls.append(kwargs)
        await gate.wait()
        return {"markdown": "# Network troubleshooting pack\n", "modules": [], "roles": {}}

    module = types.ModuleType("pengumon.reports.ai_pack")
    module.build_ai_pack = build_ai_pack
    monkeypatch.setitem(sys.modules, "pengumon.reports.ai_pack", module)
    return SimpleNamespace(calls=calls, gate=gate)


@pytest.fixture
async def repo(tmp_path, app_state):
    repository = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repository.initialize()
    await repository.create_incident(Incident(start_time=1000.0, cause_code="unknown_outage", title="t", description="d",
                                              severity="warning"))
    app_state.repository, app_state.config = repository, AppConfig()
    yield repository
    await repository.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_default_is_full_for_24_hours_and_it_is_counted(repo, stub):
    before = obs_helpers.total("pengumon.ai_pack.exports", level="full", scope="window")
    async with _client() as ac:
        response = await ac.get("/api/ai-pack")
    assert response.status_code == 200 and response.json()["markdown"].startswith("# Network")
    assert stub.calls[0]["level"] == "full" and stub.calls[0]["hours"] == 24 and stub.calls[0]["scope"] == "window"
    assert obs_helpers.total("pengumon.ai_pack.exports", level="full", scope="window") == before + 1


async def test_a_viewer_without_the_token_may_export_full(repo, stub, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "view_without_token", True)
    async with _client() as ac:
        assert (await ac.get("/api/ai-pack?level=full")).status_code == 200
        assert (await ac.get("/api/status")).status_code != 401  # the same access as the rest of the dashboard
    monkeypatch.setattr(security_config, "view_without_token", False)
    async with _client() as ac:
        assert (await ac.get("/api/ai-pack")).status_code == 401


async def test_the_scope_must_agree_with_the_incident_id(repo, stub):
    async with _client() as ac:
        assert (await ac.get("/api/ai-pack?scope=incident")).status_code == 422
        assert (await ac.get("/api/ai-pack?incident_id=1")).status_code == 422
        assert (await ac.get("/api/ai-pack?scope=incident&incident_id=999")).status_code == 404
        assert (await ac.get("/api/ai-pack?scope=incident&incident_id=1&level=vague&tz=America/New_York")).status_code == 200
        assert (await ac.get("/api/ai-pack?hours=169")).status_code == 422
    assert [c["incident_id"] for c in stub.calls] == [1] and stub.calls[0]["level"] == "vague"
    assert stub.calls[0]["tz_name"] == "America/New_York"     # the browser's zone, for when Settings has none


async def test_one_build_at_a_time_and_six_a_minute(repo, stub):
    stub.gate.clear()
    async with _client() as ac:
        first = asyncio.ensure_future(ac.get("/api/ai-pack"))
        while not stub.calls:
            await asyncio.sleep(0.01)
        assert (await ac.get("/api/ai-pack")).status_code == 429  # the first is still building
        stub.gate.set()
        assert (await first).status_code == 200
        codes = [(await ac.get("/api/ai-pack")).status_code for _ in range(6)]
    assert codes == [200, 200, 200, 200, 429, 429]  # the refused busy request used one of the six
