"""The live stream gives a connection that may not edit the device summaries the API gives it: a failed poll's message
in fixed words, never the driver's raw text (which can name the device's address or the login it tried)."""

import json

import pytest
from anyio.from_thread import start_blocking_portal
from starlette.testclient import TestClient

from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.devices.summary import POLL_FAILURES, LiveSummaries, viewer_devices
from pengumon.incidents.model import SystemDiagnosis
from pengumon.live.message import LiveStreamMessage
from pengumon.live.ws import ConnectionManager, ws_manager
from pengumon.main import app, security_config

from device_helpers import build_router
from test_obs_api import FakeSocket

TOKEN = "tok-123"
RAW = "connect to http://192.0.2.9/planted-path/login failed: refused the login of 'planted-admin'"
ORIGIN = {"Origin": "http://testserver"}


def message_with_failed_device():
    router = build_router(password="pw", id="wifi", url="http://192.0.2.9/planted-path")
    router.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=5.0, status="error", error_message=RAW,
                                                           error_kind="auth")
    ok = build_router(password="pw", id="fine")
    ok.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=5.0, status="healthy", cpu_usage_pct=4.0)
    devices = LiveSummaries(DeviceRegistry([router, ok])).current()
    return LiveStreamMessage(timestamp=1.0, diagnosis=SystemDiagnosis(), targets=[], devices=devices)


@pytest.fixture
def viewers(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "view_without_token", True)


def test_a_viewer_and_an_editor_websocket_side_by_side_through_the_real_app(viewers):
    msg = message_with_failed_device()
    with start_blocking_portal() as portal:
        client = TestClient(app)
        client.portal = portal  # both sockets and the broadcast share one event loop
        bearer = {**ORIGIN, "Authorization": f"Bearer {TOKEN}"}
        with client.websocket_connect("/ws", headers=ORIGIN) as viewer, \
                client.websocket_connect("/ws?delta=1", headers=bearer) as editor, \
                client.websocket_connect("/ws?delta=1", headers=ORIGIN) as viewer_delta:
            portal.call(ws_manager.broadcast, msg)
            seen_by_viewer, seen_by_editor, seen_by_viewer_delta = (w.receive_text() for w in (viewer, editor, viewer_delta))
    for text in (seen_by_viewer, seen_by_viewer_delta):
        assert "planted-path" not in text and "planted-admin" not in text and "192.0.2.9" not in text
        shown = json.loads(text)["devices"]["wifi"]
        assert shown["error_message"].startswith(POLL_FAILURES["auth"]) and shown["error_kind"] == "auth"
        assert json.loads(text)["devices"]["fine"]["cpu_usage_pct"] == 4.0  # the dashboard still gets its figures
    editor_device = json.loads(seen_by_editor)["devices"]["wifi"]
    assert editor_device["error_message"] == RAW


@pytest.mark.asyncio
async def test_the_viewer_form_is_built_once_per_broadcast_and_not_at_all_without_viewers(monkeypatch):
    from pengumon.live import ws as live_ws

    calls = []
    real = live_ws.viewer_devices
    monkeypatch.setattr(live_ws, "viewer_devices", lambda d: calls.append(1) or real(d))
    manager = ConnectionManager()
    editors, viewers_ = [FakeSocket(), FakeSocket()], [FakeSocket(), FakeSocket(), FakeSocket()]
    for s in editors:
        await manager.connect(s)
    await manager.broadcast(message_with_failed_device())
    assert calls == []  # only editors: nothing is paid
    for s in viewers_:
        await manager.connect(s, editor=False)
    await manager.broadcast(message_with_failed_device())
    assert calls == [1]  # three viewers, one build
    assert all(RAW in s.sent[-1] for s in editors) and all(RAW not in s.sent[-1] for s in viewers_)
    assert viewers_[0].sent[-1] == viewers_[1].sent[-1]


def test_viewer_devices_returns_the_same_dict_when_nothing_fails_and_keeps_unconfigured_text():
    ok = {"a": {"status": "healthy"}}
    assert viewer_devices(ok) is ok
    own = {"a": {"status": "unconfigured", "error_message": "Device a is not set up"}}
    assert viewer_devices(own)["a"]["error_message"] == "Device a is not set up"
