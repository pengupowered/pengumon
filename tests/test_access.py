"""The sign-in (pengumon.access, api/access.py): the first-run question, the password set on the page, the environment's
token winning, removing the password, a damaged secrets file, and HTTPS with your own certificate. The middleware itself
is homeguard's (test_security.py); these run the real app with an AccessManager on the test's own repository and
secrets file."""

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.access.manager import SETUP_KEY, AccessManager, AccessRefused
from pengumon.access.tls import tls_files
from pengumon.db.repository import DatabaseRepository
from pengumon.main import app, security_config
from pengumon.secrets import SecretStore
from pengumon.security import SESSION_COOKIE

PASSWORD = "correct-horse-battery"  # made up for the tests


@pytest.fixture(autouse=True)
def _open_app(monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", []),
                        ("view_without_token", False), ("cookie_secure", False)):
        monkeypatch.setattr(security_config, name, value)


class Live:
    """Stands in for the live connections: counts how often they were all closed, and notes the rule in force at each
    closing (the password, whether viewers may look)."""

    def __init__(self):
        self.closed = 0
        self.rules = []

    async def __call__(self):
        self.closed += 1
        self.rules.append((security_config.access_token, security_config.view_without_token))


class Socket:
    """A live connection as pengumon.live.ws.ConnectionManager holds it: notes its close code and whether viewers were
    still let in when it was closed."""

    def __init__(self):
        self.closed_with = None

    async def accept(self):
        pass

    async def close(self, code=None):
        self.closed_with = (code, security_config.view_without_token)


async def _started(app_state, repo, tmp_path, environ=None) -> AccessManager:
    """The sign-in as the settings component builds it at start, on this test's repository and secrets file."""
    access = AccessManager(security_config, SecretStore(tmp_path / "secrets.json"), repo, environ=environ or {},
                           close_live=Live())
    await access.load()
    app_state.access = access
    return access


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _cookie(response) -> dict:
    """The session cookie an answer set, as a request header (the client keeps none of its own: cookies are cleared)."""
    value = response.headers["set-cookie"].split(";")[0]
    assert value.startswith(f"{SESSION_COOKIE}=")
    return {"Cookie": value}


async def test_the_question_is_asked_once_and_never_with_a_token_from_the_environment(app_state, repo, tmp_path):
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        assert (await ac.get("/api/access")).json()["pending"] is True
        assert (await ac.post("/api/access/setup", json={"choice": "open"})).json() == {"mode": "open"}
        assert (await ac.get("/api/access")).json()["pending"] is False
        late = await ac.post("/api/access/setup", json={"choice": "password", "password": "someone-else-1"})
        assert late.status_code == 409 and "Settings > Sign-in" in late.json()["detail"]
        assert security_config.access_token == "" and "set-cookie" not in late.headers
    assert not (tmp_path / "secrets.json").exists()
    assert await repo.get_meta(SETUP_KEY) == "open"
    restarted = await _started(app_state, repo, tmp_path)
    assert not restarted.pending and access is not restarted

    other = DatabaseRepository(db_path=str(tmp_path / "other.db"))  # a fresh install whose token is set where it starts
    await other.initialize()
    from_env = await _started(app_state, other, tmp_path, {"PENGUMON_ACCESS_TOKEN": "env-token-example"})
    assert not from_env.pending and security_config.access_token == "env-token-example"
    assert await other.get_meta(SETUP_KEY) == "env"
    assert not (await _started(app_state, other, tmp_path)).pending  # the variable gone later: still not asked


async def test_a_password_chosen_at_the_first_start_is_required_and_this_browser_stays_signed_in(
        app_state, repo, tmp_path, monkeypatch):
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        res = await ac.post("/api/access/setup", json={"choice": "password", "password": f"  {PASSWORD} "})
        ac.cookies.clear()
        assert res.status_code == 200 and res.json()["password"] == PASSWORD  # shown once; the spaces dropped
        assert "HttpOnly" in res.headers["set-cookie"] and "SameSite=Strict" in res.headers["set-cookie"]
        me = _cookie(res)
        assert (await ac.get("/api/status", headers=me)).status_code != 401
        assert (await ac.get("/api/access", headers=me)).json() == {
            "mode": "password", "from_environment": False, "pending": False, "https": False}
        # anyone else: the sign-in page, and nothing changes without the password
        assert (await ac.get("/api/status")).status_code == 401
        assert (await ac.get("/", headers={"Accept": "text/html"})).headers["location"] == "/login"
        assert (await ac.put("/api/access/password", json={"password": "taken-over-1"})).status_code == 401
        assert (await ac.delete("/api/access/password")).status_code == 401
        assert (await ac.post("/login", data={"token": PASSWORD})).status_code == 303
    assert access.security.access_token == PASSWORD and access._close_live.closed == 1
    stored = tmp_path / "secrets.json"
    assert json.loads(stored.read_text(encoding="utf-8")) == {"access_token": PASSWORD}
    if os.name == "posix":
        assert stored.stat().st_mode & 0o077 == 0
    monkeypatch.setattr(security_config, "access_token", "")
    await _started(app_state, repo, tmp_path)  # restarted: the stored password is in force again
    assert security_config.access_token == PASSWORD


async def test_the_environments_token_wins_over_the_stored_password(app_state, repo, tmp_path):
    (tmp_path / "secrets.json").write_text(json.dumps({"access_token": PASSWORD}), encoding="utf-8")
    await _started(app_state, repo, tmp_path, {"PENGUMON_ACCESS_TOKEN": "env-token-example"})
    bearer = {"Authorization": "Bearer env-token-example"}
    async with _client() as ac:
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {PASSWORD}"})).status_code == 401
        assert (await ac.get("/api/access", headers=bearer)).json()["from_environment"] is True
        refused = await ac.put("/api/access/password", json={"password": "another-one-1"}, headers=bearer)
        assert refused.status_code == 409 and "PENGUMON_ACCESS_TOKEN" in refused.json()["detail"]
        assert (await ac.delete("/api/access/password", headers=bearer)).status_code == 409
    assert security_config.access_token == "env-token-example"
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {"access_token": PASSWORD}


async def test_a_new_password_signs_other_browsers_out_and_removing_it_opens_the_dashboard(app_state, repo, tmp_path):
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("open")
    async with _client() as ac:
        first = _cookie(await ac.put("/api/access/password", json={"password": PASSWORD}))
        ac.cookies.clear()
        second = await ac.put("/api/access/password", json={"password": "a-second-password"}, headers=first)
        ac.cookies.clear()
        assert second.status_code == 200
        assert (await ac.get("/api/status", headers=first)).status_code == 401  # re-keyed: the old sign-in is gone
        assert (await ac.get("/api/status", headers=_cookie(second))).status_code != 401
        assert (await ac.delete("/api/access/password", headers=_cookie(second))).json() == {"mode": "open"}
        assert (await ac.get("/api/status")).status_code != 401
        assert (await ac.get("/api/access")).json()["mode"] == "open"
    assert access._close_live.closed == 3  # every change closed the live connections first
    assert not (tmp_path / "secrets.json").exists() or "access_token" not in (tmp_path / "secrets.json").read_text()
    assert await repo.get_meta(SETUP_KEY) == "open"


async def test_a_damaged_secrets_file_keeps_a_password_protected_dashboard_locked(app_state, repo, tmp_path):
    secrets_file = tmp_path / "secrets.json"
    await repo.set_meta(SETUP_KEY, "password")
    secrets_file.write_text('{"access_token": "half-written', encoding="utf-8")
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        assert (await ac.get("/api/status")).status_code == 401
        assert (await ac.post("/login", data={"token": ""})).status_code == 401
    assert access.source == "locked" and len(security_config.access_token) >= 32 and not access.pending
    assert secrets_file.read_text(encoding="utf-8") == '{"access_token": "half-written'  # never written over
    secrets_file.write_text("{}", encoding="utf-8")  # readable, without the password: removed by hand, the way back in
    assert (await _started(app_state, repo, tmp_path)).source is None and security_config.access_token == ""


async def test_a_missing_secrets_file_keeps_a_password_protected_dashboard_locked(app_state, repo, tmp_path):
    """A lost data volume or a wrong data folder: the password cannot be read, so nobody signs in (not everyone)."""
    secrets_file = tmp_path / "secrets.json"
    await repo.set_meta(SETUP_KEY, "password")
    access = await _started(app_state, repo, tmp_path)  # no secrets.json at all
    async with _client() as ac:
        assert (await ac.get("/api/status")).status_code == 401
        assert (await ac.post("/login", data={"token": ""})).status_code == 401
    assert access.source == "locked" and len(security_config.access_token) >= 32 and not access.pending
    with pytest.raises(AccessRefused) as refused:
        await access.set_password("taken-over-by-anyone")
    assert refused.value.status == 409
    assert not secrets_file.exists() and await repo.get_meta(SETUP_KEY) == "password"
    secrets_file.write_text("{}", encoding="utf-8")  # the documented way back in: the file without the password
    assert (await _started(app_state, repo, tmp_path)).source is None and security_config.access_token == ""


async def test_every_change_is_in_force_before_the_live_connections_are_closed(app_state, repo, tmp_path):
    """A page that connects while the others are being closed is checked by the new rule, not let in by the old one."""
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("password", PASSWORD)
    await access.set_password("a-second-password")
    await access.apply_viewers(True)  # viewers let in: nothing to close
    await access.apply_viewers(False)
    await access.remove_password()
    assert access._close_live.rules == [(PASSWORD, False), ("a-second-password", False), ("a-second-password", False),
                                        ("", False)]


async def test_turning_viewers_off_closes_every_live_connection(repo, tmp_path):
    from pengumon.live.ws import ConnectionManager
    live = ConnectionManager()
    viewer = Socket()
    await live.connect(viewer)
    access = AccessManager(security_config, SecretStore(tmp_path / "secrets.json"), repo,
                           environ={"PENGUMON_ACCESS_TOKEN": "env-token-example"}, close_live=live.close_all)
    await access.load()
    await access.apply_viewers(True)
    assert viewer.closed_with is None and live.active_connections == [viewer]
    await access.apply_viewers(False)
    assert viewer.closed_with == (1012, False) and live.active_connections == []  # closed once viewers were out


async def test_the_password_is_never_sent_again(app_state, repo, tmp_path):
    """Only the answer that set it carries the password (shown once, with Copy); there is no way to ask for it again."""
    await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        assert (await ac.post("/api/access/password/show", headers=me)).status_code in (404, 405)
        state = await ac.get("/api/access", headers=me)
        assert state.status_code == 200 and PASSWORD not in state.text and "can_show" not in state.json()


# --- HTTPS with your own certificate -------------------------------------------------------------------------------

def _self_signed(folder: Path):
    """A throwaway certificate for 127.0.0.1, made by the openssl tool for this test (never kept)."""
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=30)
    return cert, key


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool to make a test certificate")
def test_with_a_certificate_the_server_speaks_https_and_the_session_cookie_is_secure(tmp_path):
    cert, key = _self_signed(tmp_path)
    with pytest.raises(ValueError, match="PENGUMON_TLS_KEY is not"):
        tls_files({"PENGUMON_TLS_CERT": str(cert)})  # one without the other stops the start
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
                      "targets:\n  - {id: lo, name: Loopback, address: 127.0.0.1, category: local}\n", encoding="utf-8")
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "PENGUMON_CONFIG": str(config), "PENGUMON_HOST": "127.0.0.1",
           "PENGUMON_PORT": str(port), "PENGUMON_DATA": str(tmp_path / "data"), "PENGUMON_TLS_CERT": str(cert),
           "PENGUMON_TLS_KEY": str(key), "PENGUMON_ACCESS_TOKEN": "tls-test-token"}
    for name in ("PENGUMON_COOKIE_SECURE", "PENGUMON_LOG_FORMAT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        env.pop(name, None)
    server = subprocess.Popen([sys.executable, "-c", "from pengumon.main import main; main()"], cwd=tmp_path, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(server.stdout), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        while not any("Application startup complete" in line for line in lines):
            assert server.poll() is None and time.monotonic() < deadline, "".join(lines)
            time.sleep(0.1)
        trusted = ssl.create_default_context(cafile=str(cert))
        conn = http.client.HTTPSConnection("127.0.0.1", port, context=trusted, timeout=10)
        conn.request("POST", "/login", body=urlencode({"token": "tls-test-token"}),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        answer = conn.getresponse()
        assert answer.status == 303
        assert "; Secure" in answer.getheader("set-cookie") and "HttpOnly" in answer.getheader("set-cookie")
        conn.close()
        plain = http.client.HTTPConnection("127.0.0.1", port, timeout=10)  # no plain HTTP beside it
        try:
            plain.request("GET", "/api/health")
            status = plain.getresponse().status
        except (http.client.HTTPException, OSError):
            status = None
        assert status != 200
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    reader.join(timeout=5)
    assert "Serving HTTPS with the certificate" in "".join(lines)
