"""The history endpoints serialize their payloads directly (no jsonable_encoder walk); the JSON must not change."""

import pytest
from httpx import AsyncClient, ASGITransport

from pengumon.db.samples import DeviceSample
from pengumon.devices.registry import DeviceRegistry
from pengumon.main import app
from pengumon.devices.snapshots import RouterMetricsSnapshot

from device_helpers import build_modem, build_router

ROWS = [
    {"target_id": "gateway", "timestamp": 100.0, "success": 1, "latency_ms": 1.25, "packet_loss_pct": 0.0,
     "jitter_ms": 0.1, "error": None},
    {"target_id": "dns_cloudflare", "timestamp": 100.5, "success": 0, "latency_ms": None,
     "packet_loss_pct": 100.0, "jitter_ms": None, "error": "timeout ü"},
]


class FakeRepository:
    def __init__(self):
        self.calls = []

    async def get_history(self, start_time, end_time, target_ids=None, bucket_seconds=None):
        self.calls.append((start_time, end_time, target_ids, bucket_seconds))
        return ROWS

    async def get_history_answer(self, start_time, end_time, target_ids=None, bucket_seconds=None, compact=False):
        rows = await self.get_history(start_time, end_time, target_ids, bucket_seconds)
        if compact:
            return {"fields": list(rows[0]) if rows else [], "rows": [list(r.values()) for r in rows]}, len(rows)
        return rows, len(rows)

    async def get_device_samples(self, device_id, start_time, end_time, padding_seconds=0.0):
        self.calls.append((device_id, start_time, end_time, padding_seconds))
        return [DeviceSample(device_id=device_id, ts=100.0, wan_rx_mbps=12.5, status="healthy")]


@pytest.fixture
def fake_repo(app_state):
    repo = FakeRepository()
    app_state.repository = repo
    app_state.devices = DeviceRegistry([build_router(password="pw")])
    return repo


@pytest.mark.asyncio
async def test_history_returns_the_repository_rows_as_json(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/history?start_time=10&end_time=20&bucket_seconds=10&target_ids=gateway")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    assert res.json() == ROWS
    assert fake_repo.calls == [(10.0, 20.0, ["gateway"], 10)]


@pytest.mark.asyncio
async def test_device_history_returns_snapshot_models_as_json(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/devices/router/history?start_time=10&end_time=20")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    data = res.json()
    assert data == [RouterMetricsSnapshot(timestamp=100.0, wan_rx_rate_mbps=12.5, status="healthy").model_dump()]
    assert fake_repo.calls == [("router", 10.0, 20.0, 120.0)]  # that device's polls, with the usual padding


@pytest.mark.asyncio
async def test_device_history_defaults_to_the_last_hour(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/devices/router/history")).status_code == 200
    ((device_id, start, end, padding),) = fake_repo.calls
    assert (device_id, padding) == ("router", 120.0) and 3590 < end - start < 3610


@pytest.mark.asyncio
async def test_device_history_of_an_unknown_device_is_404(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/devices/nope/history?start_time=10&end_time=20")
    assert (res.status_code, res.json()["detail"]) == (404, "Unknown device") and fake_repo.calls == []


@pytest.mark.asyncio
async def test_device_history_of_a_device_without_router_capabilities_is_400(fake_repo, app_state):
    app_state.devices = DeviceRegistry([build_modem(), build_router(password="pw")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/devices/modem/history?start_time=10&end_time=20")
    assert res.status_code == 400 and fake_repo.calls == []


@pytest.mark.asyncio
async def test_device_history_needs_the_repository(app_state):
    app_state.devices = DeviceRegistry([build_router(password="pw")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/devices/router/history")).status_code == 503


@pytest.mark.asyncio
async def test_compact_history_lists_fields_once_and_values_per_row(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/history?start_time=10&end_time=20&compact=true")
    assert res.status_code == 200
    data = res.json()
    assert data["fields"] == list(ROWS[0])
    assert [dict(zip(data["fields"], row)) for row in data["rows"]] == ROWS


@pytest.mark.asyncio
async def test_compact_history_without_rows(fake_repo, monkeypatch):
    async def empty(*args, **kwargs):
        return []
    monkeypatch.setattr(fake_repo, "get_history", empty)  # get_history_answer asks it
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/history?compact=true")
    assert res.json() == {"fields": [], "rows": []}


@pytest.mark.asyncio
async def test_a_long_range_without_a_bucket_is_bucketed_not_returned_raw(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.get("/api/history?start_time=0&end_time=3600")            # an hour: raw samples still
        await ac.get("/api/history?start_time=0&end_time=21600")           # 6 h: 10 s buckets
        await ac.get("/api/history?start_time=0&end_time=172800")          # 2 days: minute buckets
        await ac.get("/api/history?start_time=0&end_time=21600&bucket_seconds=30")  # a given bucket is kept
    assert [c[3] for c in fake_repo.calls] == [None, 10, 60, 30]


@pytest.mark.asyncio
async def test_unbucketed_device_history_is_cut_to_its_newest_48_hours(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/devices/router/history?start_time=0&end_time=1000000")).status_code == 200
        assert (await ac.get("/api/devices/router/history?start_time=900000&end_time=1000000")).status_code == 200
    (_, start, end, _), (_, start2, _, _) = fake_repo.calls
    assert (start, end) == (1000000 - 48 * 3600, 1000000)
    assert start2 == 900000  # a range inside the cap is left alone


@pytest.mark.asyncio
async def test_history_is_rate_limited(fake_repo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        codes = [(await ac.get("/api/history?start_time=10&end_time=20")).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
@pytest.mark.parametrize("name", ["start_time", "end_time"])
@pytest.mark.parametrize("path", ["/api/devices/router/history?max_points=100&", "/api/devices/router/history?",
                                  "/api/devices/router/channels/history?"])
async def test_device_history_refuses_a_time_that_is_not_a_finite_number(fake_repo, path, name, value):
    """An infinite end_time reached math.ceil(inf) when the answer was bucketed: a 500 with a traceback."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"{path}{name}={value}")
    assert res.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
@pytest.mark.parametrize("name", ["start_time", "end_time"])
async def test_history_refuses_a_time_that_is_not_a_finite_number(fake_repo, name, value):
    """For /api/history itself: the bucketing of an infinite window is a 500 without this."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/history?{name}={value}")
    assert res.status_code == 422
    assert fake_repo.calls == []
