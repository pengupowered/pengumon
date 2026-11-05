"""/api/devices (list, status, credentials, history), /api/features and the removal of the old /api/modem and
/api/router endpoints; the security guards and the telemetry exclusion on the credentials routes."""

import json
import os
import stat

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import secrets as secrets_module
from pengumon import security
from pengumon.config import AppConfig
from pengumon.devices.settings import DeviceConfig
from pengumon.devices.registry import DeviceRegistry, build_device
from pengumon.main import app, security_config
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.observability.telemetry import exclude_from_telemetry
from pengumon.secrets import CredentialSources

from device_helpers import build_modem, build_router, router_config
from platform_helpers import assert_mode

SECRET = "pw-never-shown"
TOKEN = "tok-123"


@pytest.fixture(autouse=True)
def _open_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", tmp_path / "secrets.json")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class RecordingStore:
    """Stands in for the app's SecretStore: records what would be written."""

    path = "secrets.json"

    def __init__(self, written):
        self.written = written

    def write(self, updates):
        self.written.append(updates)


def _router_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login.cgi":
            return httpx.Response(200, json={"asus_token": "tok"})
        if request.url.path == "/appGet.cgi":
            return httpx.Response(200, text="INTERNET_rx: 1000, INTERNET_tx: 2000")
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _refusing_transport():
    def handler(request):
        raise httpx.ConnectError("refused")
    return httpx.MockTransport(handler)


def _generic_registry():
    """Devices with ids other than modem and router: nothing may depend on the owner's ids."""
    sources = CredentialSources(env={"PENGUMON_DEVICE_WIFI_PASSWORD": SECRET}, secrets_dir=None, store={})
    cable = build_device(DeviceConfig(id="cable", driver="xfinity_xb", url="http://192.168.100.1"),
                         client=httpx.AsyncClient(transport=_refusing_transport()))
    wifi = build_device(DeviceConfig(id="wifi", driver="asuswrt", url="http://192.168.1.1", model="RT-AX88U"),
                        secrets=sources, client=httpx.AsyncClient(transport=_router_transport()))
    return DeviceRegistry([cable, wifi])


# --- /api/devices ---------------------------------------------------------------------------------------------------

async def test_device_endpoints_answer_503_before_startup(app_state):
    async with _client() as ac:
        for path in ("/api/devices", "/api/devices/router", "/api/features"):
            res = await ac.get(path)
            assert (res.status_code, res.json()["detail"]) == (503, "Devices not initialized")
        res = await ac.post("/api/devices/router/credentials", json={"password": "x"})
        assert res.status_code == 503
        res = await ac.get("/api/devices/router/history")
        assert (res.status_code, res.json()["detail"]) == (503, "Devices not initialized")
    assert not secrets_module.SECRETS_PATH.exists()


async def test_the_device_list_describes_each_device_and_its_last_poll(app_state):
    app_state.devices = _generic_registry()
    cable, wifi = app_state.devices.all()
    wifi.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=123.0, status="healthy")
    async with _client() as ac:
        await ac.get("/api/devices/cable")  # polls once: the gateway refuses the connection
        res = await ac.get("/api/devices")
    assert res.status_code == 200
    listed = res.json()
    assert set(listed[1].pop("ranges")) == {"cpu", "mem", "wan_rx", "wan_tx"}  # the good zones: tests/test_grades.py
    assert set(listed[0]["ranges"]) == {"ds_power", "ds_snr", "us_power", "uncorrectables"}
    assert listed[1] == {
        "id": "wifi", "driver": "asuswrt", "host": "192.168.1.1", "url": "http://192.168.1.1", "model": "RT-AX88U",
        "label": "ASUS router · RT-AX88U", "enabled": True,
        "offers": ["client_inventory", "mesh", "router_system", "wan_counters"], "line_technology": None, "poll_interval_seconds": 10.0,
        "has_credentials": True, "status": "healthy", "last_poll": 123.0,
    }
    assert listed[0]["id"] == "cable" and listed[0]["line_technology"] == "docsis"
    assert listed[0]["label"] == "Xfinity gateway · cable"  # no model: its id, never only an address
    assert listed[0]["has_credentials"] is False and listed[0]["status"] == "unconfigured"
    assert listed[0]["error_message"].startswith("Password for cable not configured")
    assert "error_kind" not in listed[0]
    assert SECRET not in res.text and "password\"" not in res.text


async def test_an_error_snapshot_carries_its_kind_in_the_list(app_state):
    modem = build_modem(password="pw", client=httpx.AsyncClient(transport=_refusing_transport()))
    app_state.devices = DeviceRegistry([modem])
    await modem.poll(force=True)  # the schedule's first poll; the API itself never polls
    async with _client() as ac:
        snapshot = (await ac.get("/api/devices/modem")).json()
        (entry,) = (await ac.get("/api/devices")).json()
    assert snapshot["status"] == "error" and "error_kind" not in snapshot  # the snapshot itself never sends it
    assert entry["error_kind"] == "unreachable"
    assert entry["error_message"] == "Failed to communicate with modem: refused"


async def test_a_device_snapshot_is_the_latest_one_and_never_a_poll(app_state):
    app_state.devices = _generic_registry()
    wifi = app_state.devices.get("wifi")
    reads, real_poll = [], wifi.driver.poll

    async def counting_poll():
        reads.append(1)
        return await real_poll()

    wifi.driver.poll = counting_poll
    async with _client() as ac:
        first = await ac.get("/api/devices/wifi")  # before the first poll: a waiting snapshot, not a request
        assert first.status_code == 200 and first.json()["status"] == "unconfigured"
        assert "Waiting for the first reading" in first.json()["error_message"]
        assert reads == [] and wifi.monitor.latest_snapshot is None
        await wifi.poll(force=True)
        assert (await ac.get("/api/devices/wifi")).json()["status"] == "healthy"
        assert (await ac.get("/api/devices/wifi")).json()["status"] == "healthy" and len(reads) == 1
        wifi.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=5.0, status="warning")
        assert (await ac.get("/api/devices/wifi")).json()["timestamp"] == 5.0  # no new poll
        res = await ac.get("/api/devices/nope")
        assert (res.status_code, res.json()["detail"]) == (404, "Unknown device")


async def test_device_credentials_are_stored_under_the_device_and_tested(app_state):
    app_state.devices = _generic_registry()
    async with _client() as ac:
        res = await ac.post("/api/devices/wifi/credentials", json={"username": "root", "password": "new-pw"})
    body = res.json()
    assert res.status_code == 200 and body["success"] is True and body["snapshot"]["status"] == "healthy"
    assert "new-pw" not in res.text
    path = secrets_module.SECRETS_PATH
    assert json.loads(path.read_text()) == {"devices": {"wifi": {"username": "root", "password": "new-pw"}}}
    assert_mode(path, 0o600)
    wifi = app_state.devices.get("wifi")
    assert wifi.driver._username == "root" and wifi.driver._password == "new-pw"
    assert wifi.credentials_source == f"{path} (set in the dashboard)"


async def test_a_failed_credential_test_says_so(app_state):
    app_state.devices = _generic_registry()
    async with _client() as ac:
        body = (await ac.post("/api/devices/cable/credentials", json={"password": "pw"})).json()
    assert body["success"] is False and body["snapshot"]["status"] == "error"
    assert body["error_kind"]  # the dashboard tells a refused sign-in from a silent device by it


async def test_credentials_for_an_unknown_device_write_nothing(app_state, monkeypatch):
    written = []
    app_state.secret_store = RecordingStore(written)
    app_state.devices = _generic_registry()
    async with _client() as ac:
        assert (await ac.post("/api/devices/nope/credentials", json={"password": "pw"})).status_code == 404
        assert (await ac.post("/api/devices/wifi/credentials", json={"password": ""})).status_code == 422
    assert written == []


async def test_the_device_routes_answer_for_the_owners_devices(app_state):
    """A disabled modem still shows its "disabled" text; credentials go to devices.<id>."""
    app_state.devices = DeviceRegistry([build_modem(enabled=False),
                                        build_router(client=httpx.AsyncClient(transport=_router_transport()))])
    async with _client() as ac:
        modem = (await ac.get("/api/devices/modem")).json()
        assert (modem["status"], modem["error_message"]) == ("unconfigured", "Device modem is disabled in configuration.")
        router = (await ac.get("/api/devices/router")).json()
        assert router["error_message"] == ("Password for router not configured. Set PENGUMON_DEVICE_ROUTER_PASSWORD, "
                                           "add /run/secrets/pengumon_router_password, or enter it in the settings.")
        res = await ac.post("/api/devices/router/credentials", json={"username": "root", "password": "pw"})
        assert res.json()["success"] is True
        modem_res = (await ac.post("/api/devices/modem/credentials", json={"password": "pw2"})).json()
        assert modem_res["success"] is False and modem_res["snapshot"]["status"] == "unconfigured"
    assert json.loads(secrets_module.SECRETS_PATH.read_text()) == {
        "devices": {"router": {"username": "root", "password": "pw"}, "modem": {"username": "admin", "password": "pw2"}},
    }


async def test_info_reports_the_isp_name_and_no_device_urls(app_state):
    app_state.config = AppConfig(devices=[
        {"id": "modem", "driver": "xfinity_xb", "url": "http://10.1.0.1"},
        {"id": "router", "driver": "asuswrt", "url": "http://192.168.1.1"}], line={"isp_name": "Acme Cable"})
    app_state.devices = DeviceRegistry([build_device(d) for d in app_state.config.devices])
    async with _client() as ac:
        info = (await ac.get("/api/info")).json()
    assert "modem_url" not in info and "router_url" not in info  # the devices panels link to their own devices
    assert info["isp_name"] == "Acme Cable"


async def test_info_has_no_isp_name_unless_configured(app_state):
    app_state.config = AppConfig()
    async with _client() as ac:
        assert (await ac.get("/api/info")).json()["isp_name"] is None


# --- /api/features --------------------------------------------------------------------------------------------------

async def test_features_endpoint(app_state):
    app_state.devices = DeviceRegistry([build_modem(password="pw"), build_router()])
    async with _client() as ac:
        res = await ac.get("/api/features")
    assert res.status_code == 200
    assert res.json() == [
        {"feature": "line_load", "available": False, "reason_kind": "not_configured", "provided_by": [],
         "reason": "router (asuswrt) provides wan_counters but is not configured: no password"},
        {"feature": "line_health", "available": True, "reason_kind": "available", "reason": None,
         "provided_by": ["modem"]},
        {"feature": "router_system", "available": False, "reason_kind": "not_configured", "provided_by": [],
         "reason": "router (asuswrt) provides router_system but is not configured: no password"},
        {"feature": "clients", "available": False, "reason_kind": "not_configured", "provided_by": [],
         "reason": "router (asuswrt) provides client_inventory but is not configured: no password"},
        {"feature": "mesh", "available": False, "reason_kind": "not_configured", "provided_by": [],
         "reason": "router (asuswrt) provides mesh but is not configured: no password"},
        {"feature": "host_services", "available": False, "reason_kind": "none", "provided_by": [],
         "reason": "no service_checks.services are configured"},
    ]


async def test_the_metrics_collector_status_follows_the_devices_by_capability(app_state):
    app_state.devices = _generic_registry()
    cable, wifi = app_state.devices.all()
    cable.monitor.latest_snapshot = DocsisSignalSnapshot(status="warning")
    wifi.monitor.latest_snapshot = RouterMetricsSnapshot(status="healthy")
    async with _client() as ac:
        text = (await ac.get("/metrics")).text
    assert 'pengumon_collector_status{device="cable",status="warning"} 1.0' in text
    assert 'pengumon_collector_status{device="wifi",status="healthy"} 1.0' in text


# --- security -------------------------------------------------------------------------------------------------------

CREDENTIAL_PATHS = ("/api/devices/wifi/credentials", "/api/devices/cable/credentials")


@pytest.mark.parametrize("path", CREDENTIAL_PATHS)
async def test_credentials_posts_must_be_same_origin(app_state, path, monkeypatch):
    written = []
    app_state.secret_store = RecordingStore(written)
    app_state.devices = _generic_registry()
    async with _client() as ac:
        assert (await ac.post(path, json={"password": "pw"}, headers={"Origin": "http://evil.example"})).status_code == 403
        assert (await ac.post(path, json={"password": "pw"}, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await ac.post(path, json={"password": "pw"}, headers={"Origin": "http://test"})).status_code == 200
    assert len(written) == 1


@pytest.mark.parametrize("path", ("/api/devices", "/api/devices/wifi", "/api/features", *CREDENTIAL_PATHS))
async def test_the_token_applies_to_the_device_routes(app_state, monkeypatch, path):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    app_state.secret_store = RecordingStore([])
    app_state.devices = _generic_registry()
    method = "post" if path.endswith("/credentials") else "get"
    kwargs = {"json": {"password": "pw"}} if method == "post" else {}
    async with _client() as ac:
        assert (await getattr(ac, method)(path, **kwargs)).status_code == 401
        authed = await getattr(ac, method)(path, headers={"Authorization": f"Bearer {TOKEN}"}, **kwargs)
        assert authed.status_code == 200


async def test_the_body_limit_applies_to_device_credentials(app_state, monkeypatch):
    monkeypatch.setattr(security_config, "max_body_bytes", 100)
    app_state.devices = _generic_registry()
    async with _client() as ac:
        res = await ac.post("/api/devices/wifi/credentials", content=b'{"password": "' + b"x" * 500 + b'"}',
                            headers={"Content-Type": "application/json"})
    assert res.status_code == 413


async def test_every_credentials_route_shares_one_rate_limit(app_state, monkeypatch):
    app_state.secret_store = RecordingStore([])
    app_state.devices = _generic_registry()
    security.reset_rate_limits()
    async with _client() as ac:
        codes = [(await ac.post(CREDENTIAL_PATHS[i % 2], json={"password": "pw"})).status_code for i in range(6)]
    assert codes == [200] * 5 + [429]


# --- telemetry ------------------------------------------------------------------------------------------------------

def _credential_routes():
    """Every credentials path the app serves (from its OpenAPI schema: included routers are not flattened into
    app.routes)."""
    return [path for path in app.openapi()["paths"] if path.endswith("/credentials")]


def test_every_credentials_route_of_the_app_is_excluded_from_telemetry():
    paths = _credential_routes()
    assert set(paths) == {"/api/devices/{device_id}/credentials"}
    for path in paths:
        concrete = path.replace("{device_id}", "upstairs-ap")
        assert exclude_from_telemetry({"type": "http", "path": concrete})
        assert exclude_from_telemetry({"type": "http", "path": concrete + "/"})
    assert not exclude_from_telemetry({"type": "http", "path": "/api/devices/upstairs-ap"})
    assert not exclude_from_telemetry({"type": "http", "path": "/api/devices"})


def test_fastapi_never_observes_a_device_credentials_request():
    """As test_observability's check for the old route, with the new route's own path template."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry import trace
    from pydantic import BaseModel

    seen = []

    def exclude(scope):
        excluded = exclude_from_telemetry(scope)
        if scope.get("type") == "http":
            seen.append((scope["path"], excluded))
        return excluded

    class Body(BaseModel):
        password: str

    mini = FastAPI(telemetry={"exclude": exclude, "tracer_provider": trace.NoOpTracerProvider()})

    @mini.post("/api/devices/{device_id}/credentials")
    async def creds(device_id: str, body: Body):
        return {}

    @mini.get("/api/devices/{device_id}")
    async def device(device_id: str):
        return {}

    with TestClient(mini) as client:
        assert client.post("/api/devices/wifi/credentials", json={"password": 5}).status_code == 422
        assert client.get("/api/devices/wifi").status_code == 200
    assert seen == [("/api/devices/wifi/credentials", True), ("/api/devices/wifi", False)]


# --- saving credentials that a higher source overrides --------------------------------------------------------------

async def test_saved_credentials_that_a_higher_source_overrides_say_so(app_state, caplog):
    import logging
    env = {"PENGUMON_DEVICE_ROUTER_PASSWORD": "from-env"}
    sources = CredentialSources(env=env, secrets_dir=None, store={})
    router = build_device(router_config(), secrets=sources,
                          client=httpx.AsyncClient(transport=_router_transport()))
    app_state.devices, app_state.credentials = DeviceRegistry([router]), sources
    written = []
    app_state.secret_store = RecordingStore(written)
    with caplog.at_level(logging.WARNING, logger="pengumon.api"):
        async with _client() as ac:
            body = (await ac.post("/api/devices/router/credentials", json={"password": "typed-in"})).json()
    assert body["success"] is True and body["overridden_by"] == "environment PENGUMON_DEVICE_ROUTER_PASSWORD"
    assert set(body) == {"success", "snapshot", "overridden_by"}
    assert len(written) == 1
    (record,) = [r for r in caplog.records if r.name == "pengumon.api"]
    assert "ranks above it" in record.getMessage() and record.pengumon_context["overridden_by"] == \
        "environment PENGUMON_DEVICE_ROUTER_PASSWORD"
    assert "typed-in" not in caplog.text and "from-env" not in caplog.text


async def test_saved_credentials_without_a_higher_source_keep_the_old_answer(app_state):
    app_state.devices = _generic_registry()
    app_state.credentials = CredentialSources(env={}, secrets_dir=None, store={})
    app_state.secret_store = RecordingStore([])
    async with _client() as ac:
        body = (await ac.post("/api/devices/wifi/credentials", json={"password": "pw"})).json()
    assert set(body) == {"success", "snapshot"}


@pytest.mark.parametrize("path", CREDENTIAL_PATHS)
async def test_an_invalid_credentials_body_is_not_echoed(app_state, path):
    app_state.devices = _generic_registry()
    app_state.secret_store = RecordingStore([])
    async with _client() as ac:
        long = await ac.post(path, json={"password": "Echo-Me-" * 40})
        wrong_type = await ac.post(path, json={"password": 987654321})
    assert long.status_code == wrong_type.status_code == 422
    assert "Echo-Me-" not in long.text and "987654321" not in wrong_type.text
    assert long.json()["detail"][0]["loc"] == ["body", "password"]  # still says what is wrong


async def test_other_validation_errors_keep_fastapis_answer():
    async with _client() as ac:
        res = await ac.get("/api/incidents?limit=99999")
    assert res.status_code == 422 and res.json()["detail"][0]["input"] == "99999"


async def test_the_endpoints_write_through_the_apps_store(app_state, tmp_path):
    from pengumon.secrets import SecretStore
    store = SecretStore(tmp_path / "own.json", data={})
    app_state.devices, app_state.secret_store = _generic_registry(), store
    async with _client() as ac:
        await ac.post("/api/devices/wifi/credentials", json={"password": "pw"})
    assert json.loads((tmp_path / "own.json").read_text()) == {"devices": {"wifi": {"username": "admin", "password": "pw"}}}
    assert app_state.devices.get("wifi").credentials_source == f"{tmp_path / 'own.json'} (set in the dashboard)"
    assert not secrets_module.SECRETS_PATH.exists()
