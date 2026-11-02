"""Pengumon's security wiring (the guards themselves are tested in homeguard): the real app's Host/Origin/token
guards, /ws, secrets file hardening and the credential rate limit."""

import hashlib
import hmac
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pengumon import secrets as secrets_module
from pengumon import security
from pengumon.devices.registry import DeviceRegistry
from pengumon.main import app, security_config
from pengumon import paths
from pengumon.security import SESSION_COOKIE, SESSIONS_PATH, FileSessionStore, SecurityConfig

from device_helpers import build_modem, build_router

TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def _reset(monkeypatch, app_state):
    """app_state: an empty AppState (no config, no devices)."""
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    security.reset_rate_limits()
    yield
    security.reset_rate_limits()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _owner_devices():
    """The modem and the router, behind a transport that refuses every connection (the test poll fails fast)."""
    def refuse(request):
        raise httpx.ConnectError("refused")
    return DeviceRegistry([build_modem(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))),
                           build_router(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))])


@pytest.mark.asyncio
async def test_dns_rebinding_host_rejected():
    async with _client() as ac:
        res = await ac.get("/api/health", headers={"Host": "evil.example"})
        assert res.status_code == 421
        assert (await ac.get("/api/health", headers={"Host": "127.0.0.1:8000"})).status_code != 421


@pytest.mark.asyncio
async def test_cross_origin_post_rejected_same_origin_allowed():
    async with _client() as ac:
        body = {"target_host": "x"}
        bad = await ac.post("/api/mtr/trace", json=body, headers={"Origin": "http://evil.example"})
        assert bad.status_code == 403
        ok = await ac.post("/api/mtr/trace", json=body, headers={"Origin": "http://test"})
        assert ok.status_code != 403
        site = await ac.post("/api/mtr/trace", json=body, headers={"Sec-Fetch-Site": "cross-site"})
        assert site.status_code == 403


@pytest.mark.asyncio
async def test_token_required_when_set(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client() as ac:
        assert (await ac.get("/api/status")).status_code == 401
        assert (await ac.get("/metrics")).status_code == 401
        assert (await ac.get("/api/health")).status_code != 401
        page = await ac.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/login"
        assert "token" in (await ac.get("/login")).text
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})).status_code != 401
        assert (await ac.get("/api/status", headers={"Authorization": "Bearer wrong"})).status_code == 401


@pytest.mark.asyncio
async def test_login_sets_cookie_and_wrong_token_does_not(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client() as ac:
        bad = await ac.post("/login", data={"token": "nope"})
        assert bad.status_code == 401 and "set-cookie" not in bad.headers
        good = await ac.post("/login", data={"token": TOKEN})
        assert good.status_code == 303
        assert SESSION_COOKIE in good.headers["set-cookie"] and "HttpOnly" in good.headers["set-cookie"]
        cookie = good.headers["set-cookie"].split(";")[0]
        assert (await ac.get("/api/status", headers={"Cookie": cookie})).status_code != 401


@pytest.mark.asyncio
async def test_security_headers_present():
    async with _client() as ac:
        res = await ac.get("/api/health")
    assert "script-src" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_oversized_body_rejected(monkeypatch):
    monkeypatch.setattr(security_config, "max_body_bytes", 100)
    async with _client() as ac:
        res = await ac.post("/api/mtr/trace", content=b"x" * 500, headers={"Content-Type": "application/json"})
    assert res.status_code == 413


# -- websocket ----------------------------------------------------------------------------------

def _ws_rejected(client, **kw):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", **kw):
            pass
    return exc.value.code


def test_websocket_origin_checks():
    client = TestClient(app)
    assert _ws_rejected(client, headers={"Origin": "http://evil.example"}) == 1008
    assert _ws_rejected(client) == 1008  # no Origin at all
    assert _ws_rejected(client, headers={"Origin": "http://testserver", "Host": "evil.example"}) == 1008
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.close()


def test_websocket_requires_token_when_set(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    client = TestClient(app)
    origin = {"Origin": "http://testserver"}
    assert _ws_rejected(client, headers=origin) == 1008
    assert _ws_rejected(client, headers=origin, params={"token": TOKEN}) == 1008  # URL tokens are not accepted
    with client.websocket_connect("/ws", headers={**origin, "Authorization": f"Bearer {TOKEN}"}) as ws:
        ws.close()
    cookie = f"{SESSION_COOKIE}={SecurityConfig(access_token=TOKEN).issue_session()}"
    with client.websocket_connect("/ws", headers={**origin, "Cookie": cookie}) as ws:
        ws.close()


# -- secrets file -------------------------------------------------------------------------------

def test_secrets_file_mode_and_merge(tmp_path):
    path = tmp_path / "data" / "secrets.json"
    secrets_module.update_secrets({"devices": {"modem": {"password": "a"}}}, path)
    secrets_module.update_secrets({"devices": {"router": {"password": "b"}}}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"devices": {"modem": {"password": "a"}, "router": {"password": "b"}}}
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_credentials_endpoint_writes_secrets_0600(tmp_path, monkeypatch, app_state):
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", path)
    app_state.devices = _owner_devices()
    async with _client() as ac:
        res = await ac.post("/api/devices/modem/credentials", json={"password": "pw"})
    assert res.status_code == 200
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# -- rate limits --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credentials_rate_limited(tmp_path, monkeypatch, app_state):
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", tmp_path / "secrets.json")
    app_state.devices = _owner_devices()
    async with _client() as ac:
        codes = [(await ac.post("/api/devices/router/credentials", json={"password": "pw"})).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


# -- credential payloads ------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"password": 12345},
    {"password": ""},
    {"password": "x" * 257},
    {},
    {"password": "pw", "username": "u" * 65},
], ids=["non-string", "empty", "oversized", "missing", "long-username"])
async def test_credentials_payload_is_validated_before_anything_is_written(body, app_state):
    written = []
    app_state.secret_store = SimpleNamespace(path="secrets.json", write=written.append)
    async with _client() as ac:
        codes = [(await ac.post(path, json=body)).status_code
                 for path in ("/api/devices/modem/credentials", "/api/devices/router/credentials")]
    assert codes == [422, 422]
    assert written == []


@pytest.mark.asyncio
async def test_valid_credentials_reach_the_handler(app_state):
    written = []
    app_state.secret_store = SimpleNamespace(path="secrets.json", write=written.append)
    app_state.devices = _owner_devices()
    async with _client() as ac:
        modem = await ac.post("/api/devices/modem/credentials", json={"password": "pw"})
        router = await ac.post("/api/devices/router/credentials", json={"username": "root", "password": "pw2"})
    assert modem.status_code == 200 and router.status_code == 200
    assert written == [
        {"devices": {"modem": {"username": "admin", "password": "pw"}}},
        {"devices": {"router": {"username": "root", "password": "pw2"}}},
    ]


# -- profile --------------------------------------------------------------------------------------

def test_session_cookie_is_pengumons_and_the_old_format_is_refused():
    """The cookie name and signing purpose are this app's own; the cookie from before signed sessions (a fixed HMAC
    of the token) no longer signs anyone in."""
    assert SESSION_COOKIE == "pg_session" and security.PROFILE.session_purpose == "pg-session-v1"
    cfg = SecurityConfig(access_token="tok")
    assert cfg.session_valid(cfg.issue_session())
    assert not cfg.session_valid(hmac.new(b"tok", b"pg-session-v1", hashlib.sha256).hexdigest())


def test_sign_out_everywhere_is_kept_in_the_data_directory():
    assert isinstance(security_config.session_store, FileSessionStore)
    assert security_config.session_store.path == SESSIONS_PATH == paths.sessions_path()


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    """The real app with a token, its sign-out-everywhere file in tmp_path."""
    store = FileSessionStore(tmp_path / "data" / "sessions.json")
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "session_store", store)
    return store


def _signed_in(seconds_ago: float = 1.0) -> str:
    """A cookie the app issued `seconds_ago` (made directly: the app's login throttle is shared by every test here)."""
    return security_config.issue_session(now_ms=int((time.time() - seconds_ago) * 1000))


def _cookie(value: str) -> dict:
    return {"Cookie": f"{SESSION_COOKIE}={value}"}


@pytest.mark.asyncio
async def test_info_tells_the_page_whether_to_offer_sign_out(sessions, monkeypatch):
    async with _client() as ac:
        value = _signed_in()
        by_cookie = (await ac.get("/api/info", headers=_cookie(value))).json()
        assert by_cookie["session"] == {"sign_out": True, "sign_out_everywhere": True}
        by_bearer = (await ac.get("/api/info", headers={"Authorization": f"Bearer {TOKEN}"})).json()
        assert by_bearer["session"] == {"sign_out": False, "sign_out_everywhere": False}
        monkeypatch.setattr(security_config, "access_token", "")
        assert (await ac.get("/api/info")).json()["session"] == {"sign_out": False, "sign_out_everywhere": False}


@pytest.mark.asyncio
async def test_sign_out_and_sign_out_everywhere(sessions):
    async with _client() as ac:
        mine, other = _signed_in(2), _signed_in(1)
        out = await ac.post("/logout", headers={"Origin": "http://test", **_cookie(mine)})
        assert out.status_code == 303 and out.headers["location"] == "/login" and "Max-Age=0" in out.headers["set-cookie"]
        assert (await ac.get("/api/status", headers=_cookie(mine))).status_code == 401  # a copy of it is refused too
        assert (await ac.get("/api/status", headers=_cookie(other))).status_code != 401
        everywhere = await ac.post("/logout?everywhere=1", headers={"Origin": "http://test", **_cookie(other)})
        assert everywhere.status_code == 303
        assert (await ac.get("/api/status", headers=_cookie(other))).status_code == 401
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})).status_code != 401
        assert json.loads(sessions.path.read_text())["not_before_ms"] > 0
        assert stat.S_IMODE(os.stat(sessions.path).st_mode) == 0o600
        cross = await ac.post("/logout?everywhere=1", headers={"Origin": "http://evil.example", **_cookie(other)})
        assert cross.status_code == 403


def test_websocket_refuses_a_cookie_signed_out_everywhere(sessions):
    client = TestClient(app)
    origin = {"Origin": "http://testserver"}
    value = _signed_in()
    with client.websocket_connect("/ws", headers={**origin, **_cookie(value)}) as ws:
        ws.close()
    sessions.revoke_all(int(time.time() * 1000))
    assert _ws_rejected(client, headers={**origin, **_cookie(value)}) == 1008


def test_env_prefix_is_pengumon(monkeypatch):
    monkeypatch.setenv("PENGUMON_ACCESS_TOKEN", "abc")
    monkeypatch.setenv("PENGUMON_MAX_BODY_BYTES", "99")
    cfg = SecurityConfig.from_env()
    assert cfg.access_token == "abc" and cfg.max_body_bytes == 99


@pytest.mark.asyncio
async def test_sign_in_page_and_rejections_carry_the_security_headers(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client() as ac:
        responses = [await ac.get("/login"), await ac.get("/api/status"),
                     await ac.get("/api/status", headers={"Host": "evil.example"})]
    assert [r.status_code for r in responses] == [200, 401, 421]
    for res in responses:
        assert "frame-ancestors 'self'" in res.headers["content-security-policy"]
        assert res.headers["x-frame-options"] == "SAMEORIGIN"
        assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert res.headers.get_list("cache-control") == ["no-store"]
        assert "x-xss-protection" not in res.headers
    assert "Pengumon - sign in" in responses[0].text


@pytest.mark.asyncio
async def test_non_ascii_credentials_are_unauthorized_not_an_error(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client() as ac:
        res = await ac.get("/api/status", headers={"Authorization": "Bearer caf\u00e9".encode("latin-1")})
        assert res.status_code == 401
        assert (await ac.post("/login", data={"token": "caf\u00e9"})).status_code == 401
