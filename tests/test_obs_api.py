"""Telemetry of what the dashboards ask for: history answers (rows, bytes, caps), rate-limit refusals, and the live
websocket (clients, message size, send time, clients dropped)."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import obs_helpers
from pengumon.api import history as history_api
from pengumon.live.ws import ConnectionManager
from pengumon.db.samples import DeviceSample
from pengumon.devices.registry import DeviceRegistry
from pengumon.incidents.model import SystemDiagnosis
from pengumon.live.message import LiveStreamMessage
from pengumon.main import app
from pengumon.observability import export
from pengumon.observability.gauges import GAUGES

from device_helpers import build_router

obs_helpers.install()

ROWS = [
    {"target_id": "gateway", "timestamp": 100.0 + i, "success": 1, "latency_ms": 1.25, "packet_loss_pct": 0.0,
     "jitter_ms": 0.1, "error": None}
    for i in range(5)
]


class FakeRepository:
    retention_days = 7

    async def get_history(self, start_time, end_time, target_ids=None, bucket_seconds=None):
        return ROWS

    async def get_device_samples(self, device_id, start_time, end_time, padding_seconds=0.0):
        return [DeviceSample(device_id=device_id, ts=100.0, wan_rx_mbps=12.5, status="healthy")]


@pytest.fixture
def repo(app_state):
    app_state.repository = FakeRepository()
    app_state.devices = DeviceRegistry([build_router(password="pw")])
    return app_state.repository


async def _get(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


# --- history answers ------------------------------------------------------------------------------------------------

async def test_a_history_answer_records_its_rows_and_its_bytes(repo):
    rows = obs_helpers.total("pengumon.api.answer.rows", endpoint="history")
    sizes = obs_helpers.histogram_sum("pengumon.api.answer.size", endpoint="history")
    response = await _get("/api/history?start_time=10&end_time=20&bucket_seconds=10")
    assert response.status_code == 200
    assert obs_helpers.total("pengumon.api.answer.rows", endpoint="history") - rows == 1  # one observation ...
    assert obs_helpers.histogram_sum("pengumon.api.answer.rows", endpoint="history") >= len(ROWS)  # ... of 5 rows
    assert obs_helpers.histogram_sum("pengumon.api.answer.size", endpoint="history") - sizes == len(response.content)


async def test_a_compact_history_answer_is_measured_by_its_own_body(repo):
    sizes = obs_helpers.histogram_sum("pengumon.api.answer.size", endpoint="history")
    response = await _get("/api/history?start_time=10&end_time=20&bucket_seconds=10&compact=true")
    assert obs_helpers.histogram_sum("pengumon.api.answer.size", endpoint="history") - sizes == len(response.content)


async def test_an_answer_that_reached_the_row_cap_is_counted_and_a_short_one_is_not(repo, monkeypatch):
    capped = obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="rows")
    await _get("/api/history?start_time=10&end_time=20&bucket_seconds=10")
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="rows") == capped
    monkeypatch.setattr(history_api, "HISTORY_ROW_CAP", len(ROWS))
    await _get("/api/history?start_time=10&end_time=20&bucket_seconds=10")
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="rows") == capped + 1


async def test_a_long_unbucketed_range_is_counted_as_bucketed_by_the_api(repo):
    capped = obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="range")
    await _get("/api/history?start_time=0&end_time=7200")
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="range") == capped + 1
    await _get("/api/history?start_time=0&end_time=600")
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="history", cap="range") == capped + 1


async def test_device_history_records_rows_and_bytes_and_a_range_cut_to_48_hours(repo):
    rows = obs_helpers.total("pengumon.api.answer.rows", endpoint="device_history")
    capped = obs_helpers.total("pengumon.api.answer.capped", endpoint="device_history", cap="range")
    response = await _get("/api/devices/router/history?start_time=10&end_time=20")
    assert response.status_code == 200
    assert obs_helpers.total("pengumon.api.answer.rows", endpoint="device_history") == rows + 1
    assert obs_helpers.histogram_sum("pengumon.api.answer.size", endpoint="device_history") >= len(response.content)
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="device_history", cap="range") == capped
    await _get(f"/api/devices/router/history?start_time=0&end_time={10 * 86400}")
    assert obs_helpers.total("pengumon.api.answer.capped", endpoint="device_history", cap="range") == capped + 1


# --- rate limits ----------------------------------------------------------------------------------------------------

async def test_a_rate_limit_refusal_is_counted_by_bucket_and_the_requests_before_it_are_not(repo, caplog):
    refused = obs_helpers.total("pengumon.api.rate_limited", bucket="history")
    caplog.set_level(logging.DEBUG, logger="pengumon.security")
    codes = [(await _get("/api/history?start_time=10&end_time=20&bucket_seconds=10")).status_code for _ in range(63)]
    assert codes.count(429) == 3 and codes[:60] == [200] * 60
    assert obs_helpers.total("pengumon.api.rate_limited", bucket="history") - refused == 3
    line = next(r for r in caplog.records if "rate limit" in r.getMessage())
    assert line.pengumon_context["bucket"] == "history"


# --- the websocket --------------------------------------------------------------------------------------------------

class FakeSocket:
    def __init__(self, hang=False, fail=False):
        self.hang, self.fail, self.sent, self.closed = hang, fail, [], False

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("closed")
        self.sent.append(text)

    async def close(self):
        self.closed = True


def _message():
    return LiveStreamMessage(timestamp=1.0, diagnosis=SystemDiagnosis(), targets=[], incidents_version=7)


async def test_a_broadcast_records_its_size_and_each_send(caplog):
    manager = ConnectionManager()
    sockets = [FakeSocket(), FakeSocket()]
    for s in sockets:
        await manager.connect(s)
    sizes = obs_helpers.total("pengumon.ws.message.size")
    sent = obs_helpers.total("pengumon.ws.send.duration", outcome="sent")
    await manager.broadcast(_message())
    assert obs_helpers.total("pengumon.ws.message.size") == sizes + 1  # one message, however many clients
    assert obs_helpers.total("pengumon.ws.send.duration", outcome="sent") == sent + 2
    assert obs_helpers.histogram_sum("pengumon.ws.message.size") >= len(sockets[0].sent[0])


async def test_a_client_that_does_not_read_is_dropped_as_a_timeout_and_one_that_is_gone_as_an_error(caplog):
    manager = ConnectionManager(send_timeout=0.1)
    slow, gone, healthy = FakeSocket(hang=True), FakeSocket(fail=True), FakeSocket()
    for s in (slow, gone, healthy):
        await manager.connect(s)
    timeouts = obs_helpers.total("pengumon.ws.clients.dropped", reason="timeout")
    errors = obs_helpers.total("pengumon.ws.clients.dropped", reason="error")
    failed = obs_helpers.total("pengumon.ws.send.duration", outcome="failed")
    with caplog.at_level(logging.INFO, logger="pengumon.ws"):
        await manager.broadcast(_message())
    assert manager.active_connections == [healthy]
    assert obs_helpers.total("pengumon.ws.clients.dropped", reason="timeout") == timeouts + 1
    assert obs_helpers.total("pengumon.ws.clients.dropped", reason="error") == errors + 1
    assert obs_helpers.total("pengumon.ws.send.duration", outcome="failed") == failed + 2
    reasons = sorted(r.pengumon_context["reason"] for r in caplog.records if "dropped" in r.getMessage())
    assert reasons == ["error", "timeout"]


async def test_the_clients_gauge_counts_connected_dashboards():
    from pengumon.live.ws import ws_manager
    gauge = next(g for g in GAUGES if g.otel == "pengumon.ws.clients")
    before = gauge.collect(None)[0][1]
    socket = FakeSocket()
    await ws_manager.connect(socket)
    try:
        assert gauge.collect(None) == [({}, before + 1)]
        export.observe_state(SimpleNamespace())
        assert [o.value for o in export._guard(gauge)(None)] == [before + 1]
    finally:
        export.observe_state(None)
        ws_manager.disconnect(socket)
