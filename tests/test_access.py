"""The sign-in (pengumon.access, api/access.py): the first-run question, the password set on the page (kept hashed, the
current one asked again to change it), the environment's token winning, removing the password, a damaged secrets file,
the read token, and HTTPS with your own certificate. The middleware itself is homeguard's (test_security.py); these run
the real app with an AccessManager on the test's own repository and secrets file."""

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

from pengumon._vendor.homeguard.security import verify_token_hash
from pengumon.access.manager import SETUP_KEY, AccessManager, AccessRefused, retired_token
from pengumon.access.tls import tls_files
from pengumon.db.repository import DatabaseRepository
from pengumon.main import app, security_config
from pengumon.secrets import SecretStore
from pengumon.security import SESSION_COOKIE, read_token_digest

PASSWORD = "correct-horse-battery"  # made up for the tests


@pytest.fixture(autouse=True)
def _open_app(monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", []),
                        ("view_without_token", False), ("cookie_secure", False), ("access_hash", None),
                        ("session_secret", ""), ("read_token_hash", ""), ("token_generation", "")):
        monkeypatch.setattr(security_config, name, value)


def _holds(password: str) -> bool:
    """Whether `password` is the one in force (the environment's, as text, or the page's, as a hash)."""
    if security_config.access_token:
        return security_config.access_token == password
    return bool(security_config.access_hash) and verify_token_hash(security_config.access_hash, password)


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))


class Live:
    """Stands in for the live connections: counts how often they were all closed, and notes the rule in force at each
    closing (the password, whether viewers may look)."""

    def __init__(self):
        self.closed = 0
        self.rules = []

    async def __call__(self):
        self.closed += 1
        self.rules.append((security_config.access_token, security_config.access_hash, security_config.view_without_token))


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
            "mode": "password", "from_environment": False, "pending": False, "https": False, "read_token": False}
        # anyone else: the sign-in page, and nothing changes without the password
        assert (await ac.get("/api/status")).status_code == 401
        assert (await ac.get("/", headers={"Accept": "text/html"})).headers["location"] == "/login"
        assert (await ac.put("/api/access/password", json={"password": "taken-over-1"})).status_code == 401
        assert (await ac.delete("/api/access/password")).status_code == 401
        assert (await ac.post("/login", data={"token": PASSWORD})).status_code == 303
    assert _holds(PASSWORD) and access.security.access_token == "" and access._close_live.closed == 1
    stored = tmp_path / "secrets.json"
    saved = json.loads(stored.read_text(encoding="utf-8"))
    assert set(saved) == {"access_hash", "access_token", "session_secret"} and PASSWORD not in stored.read_text()
    assert verify_token_hash(saved["access_hash"], PASSWORD) and saved["access_token"] == retired_token(saved["access_hash"])
    assert len(saved["session_secret"]) >= 32 and saved["session_secret"] == security_config.session_secret
    if os.name == "posix":
        assert stored.stat().st_mode & 0o077 == 0
    monkeypatch.setattr(security_config, "access_hash", None)
    await _started(app_state, repo, tmp_path)  # restarted: the stored password is in force again
    assert _holds(PASSWORD) and _stored(tmp_path) == saved  # nothing rewritten


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
    assert security_config.access_token == "env-token-example" and security_config.access_hash is None
    saved = _stored(tmp_path)  # the stored text is replaced by its hash all the same: it does not stay on disk
    assert PASSWORD not in json.dumps(saved) and verify_token_hash(saved["access_hash"], PASSWORD)
    assert security_config.session_secret == saved["session_secret"]  # the environment's token gets the secret too


async def test_a_new_password_signs_other_browsers_out_and_removing_it_opens_the_dashboard(app_state, repo, tmp_path):
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("open")
    async with _client() as ac:
        first = _cookie(await ac.put("/api/access/password", json={"password": PASSWORD}))  # none yet: none asked
        ac.cookies.clear()
        second = await ac.put("/api/access/password", json={"password": "a-second-password", "current_password": PASSWORD},
                              headers=first)
        ac.cookies.clear()
        assert second.status_code == 200
        assert (await ac.get("/api/status", headers=first)).status_code == 401  # re-keyed: the old sign-in is gone
        assert (await ac.get("/api/status", headers=_cookie(second))).status_code != 401
        removed = await ac.request("DELETE", "/api/access/password", json={"current_password": "a-second-password"},
                                   headers=_cookie(second))
        assert removed.json() == {"mode": "open", "read_token_removed": False}
        assert (await ac.get("/api/status")).status_code != 401
        assert (await ac.get("/api/access")).json()["mode"] == "open"
    assert access._close_live.closed == 3  # every change closed the live connections first
    assert set(_stored(tmp_path)) == {"session_secret"}  # the hash and the retired text are gone
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
    rules = access._close_live.rules
    assert [(text, view) for text, _, view in rules] == [("", False)] * 4 and rules[3][1] is None
    assert verify_token_hash(rules[0][1], PASSWORD) and not verify_token_hash(rules[0][1], "a-second-password")
    assert verify_token_hash(rules[1][1], "a-second-password") and rules[2][1] == rules[1][1]


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


# --- the password kept as a hash ---------------------------------------------------------------------------------

async def test_a_stored_password_in_text_is_replaced_by_its_hash_at_the_next_start(app_state, repo, tmp_path, caplog):
    """secrets.json as versions before this one wrote it: the text is converted once, and the same password works."""
    (tmp_path / "secrets.json").write_text(json.dumps({"access_token": PASSWORD, "devices": {"modem": {"password": "pw"}}}),
                                           encoding="utf-8")
    await repo.set_meta(SETUP_KEY, "password")
    with caplog.at_level("INFO", logger="pengumon.access"):
        access = await _started(app_state, repo, tmp_path)
    saved = _stored(tmp_path)
    assert PASSWORD not in (tmp_path / "secrets.json").read_text() and saved["devices"] == {"modem": {"password": "pw"}}
    assert verify_token_hash(saved["access_hash"], PASSWORD) and saved["access_token"] == retired_token(saved["access_hash"])
    assert access.source == "stored" and _holds(PASSWORD) and security_config.access_token == ""
    assert caplog.text.count("replaced by its hash") == 1
    async with _client() as ac:
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {PASSWORD}"})).status_code != 401
        signed_in = await ac.post("/login", data={"token": PASSWORD})
        ac.cookies.clear()
        assert signed_in.status_code == 303 and _cookie(signed_in)["Cookie"].startswith(f"{SESSION_COOKIE}=3.")
        # the retired text is no password
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {saved['access_token']}"})).status_code == 401
    caplog.clear()
    with caplog.at_level("INFO", logger="pengumon.access"):
        await _started(app_state, repo, tmp_path)  # the next start: nothing to convert
    assert "replaced by its hash" not in caplog.text and _stored(tmp_path) == saved and _holds(PASSWORD)


async def test_a_password_an_older_version_wrote_beside_the_hash_wins(app_state, repo, tmp_path):
    """Rolled back, the older version found a password nobody knows (the retired text); its owner put one in by hand
    (or the older version set one): that one is in force at the next start, converted."""
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("password", PASSWORD)
    saved = _stored(tmp_path)
    (tmp_path / "secrets.json").write_text(json.dumps({**saved, "access_token": "set-by-an-older-version"}), encoding="utf-8")
    await _started(app_state, repo, tmp_path)
    assert _holds("set-by-an-older-version") and not _holds(PASSWORD)
    assert "set-by-an-older-version" not in (tmp_path / "secrets.json").read_text()


async def test_an_access_hash_that_is_no_hash_keeps_the_dashboard_locked(app_state, repo, tmp_path):
    secrets_file = tmp_path / "secrets.json"
    await repo.set_meta(SETUP_KEY, "password")
    content = json.dumps({"access_hash": {"hash": "not-one"}, "access_token": "hashed:0123"})
    secrets_file.write_text(content, encoding="utf-8")
    access = await _started(app_state, repo, tmp_path)
    assert access.source == "locked" and len(security_config.access_token) >= 32
    assert secrets_file.read_text(encoding="utf-8") == content  # never converted or written over
    async with _client() as ac:
        assert (await ac.get("/api/status", headers={"Authorization": "Bearer hashed:0123"})).status_code == 401


async def test_the_placeholder_left_without_its_hash_keeps_the_dashboard_locked_and_says_why(app_state, repo, tmp_path,
                                                                                               caplog):
    """The hash removed by hand without the access_token beside it (docs/security.md says both). The placeholder
    is no password anyone knows: locked, as before, but no longer taken for a password and hashed, and the log says
    what to remove."""
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("password", PASSWORD)
    saved = _stored(tmp_path)
    content = json.dumps({"access_token": saved["access_token"], "session_secret": saved["session_secret"]})
    (tmp_path / "secrets.json").write_text(content, encoding="utf-8")
    with caplog.at_level("ERROR", logger="pengumon.access"):
        access = await _started(app_state, repo, tmp_path)
    assert access.source == "locked" and (tmp_path / "secrets.json").read_text(encoding="utf-8") == content
    assert "without its access_hash (remove access_token too)" in caplog.text
    (tmp_path / "secrets.json").write_text(json.dumps({"session_secret": saved["session_secret"]}), encoding="utf-8")
    assert (await _started(app_state, repo, tmp_path)).source is None  # both removed: open, the documented way back


async def test_the_environments_token_stays_text_but_gets_a_lasting_session_secret(app_state, repo, tmp_path):
    env = {"PENGUMON_ACCESS_TOKEN": "env-token-example"}
    await _started(app_state, repo, tmp_path, env)
    secret = security_config.session_secret
    assert security_config.access_token == "env-token-example" and len(secret) >= 32
    assert _stored(tmp_path) == {"session_secret": secret}
    async with _client() as ac:
        cookie = _cookie(await ac.post("/login", data={"token": "env-token-example"}))
        ac.cookies.clear()
        assert cookie["Cookie"].startswith(f"{SESSION_COOKIE}=3.")
    await _started(app_state, repo, tmp_path, env)  # restarted: the same secret, so the sign-in survives
    async with _client() as ac:
        assert security_config.session_secret == secret
        assert (await ac.get("/api/status", headers=cookie)).status_code != 401


async def test_with_a_damaged_secrets_file_the_environments_token_works_and_nothing_is_written(app_state, repo, tmp_path):
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text('{"devices": ', encoding="utf-8")
    await _started(app_state, repo, tmp_path, {"PENGUMON_ACCESS_TOKEN": "env-token-example"})
    assert len(security_config.session_secret) >= 32  # in memory only: sign-ins end at the next start
    assert secrets_file.read_text(encoding="utf-8") == '{"devices": '
    async with _client() as ac:
        assert (await ac.post("/login", data={"token": "env-token-example"})).status_code == 303


# --- the current password, asked again ---------------------------------------------------------------------------

async def test_changing_or_removing_the_password_needs_the_current_one(app_state, repo, tmp_path):
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        for body, detail in (({"password": "taken-over-1"}, "Enter the current password"),
                             ({"password": "taken-over-1", "current_password": ""}, "Enter the current password"),
                             ({"password": "taken-over-1", "current_password": "a-guess-1"}, "not right")):
            res = await ac.put("/api/access/password", json=body, headers=me)
            assert res.status_code == 403 and detail in res.json()["detail"], body
        res = await ac.request("DELETE", "/api/access/password", headers=me)  # no body at all
        assert res.status_code == 403 and "Enter the current password" in res.json()["detail"]
        res = await ac.request("DELETE", "/api/access/password", json={"current_password": "a-guess-2"}, headers=me)
        assert res.status_code == 403 and "not right" in res.json()["detail"]
        assert _holds(PASSWORD) and access.source == "stored" and access._close_live.closed == 1
        assert (await ac.get("/api/status", headers=me)).status_code != 401  # this browser is still signed in
        changed = await ac.put("/api/access/password", json={"password": "a-second-password", "current_password": PASSWORD},
                               headers=me)
        assert changed.status_code == 200 and _holds("a-second-password")


async def test_wrong_current_passwords_are_throttled_like_failed_sign_ins(app_state, repo, tmp_path):
    await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        for i in range(5):
            res = await ac.put("/api/access/password", json={"password": "taken-over-1", "current_password": f"guess-{i}"},
                               headers=me)
            assert res.status_code == 403
        refused = await ac.put("/api/access/password", json={"password": "taken-over-1", "current_password": PASSWORD},
                               headers=me)  # the right one, but not compared now
        assert refused.status_code == 429 and "retry-after" in refused.headers
        assert (await ac.post("/login", data={"token": PASSWORD})).status_code == 429  # the same count
        assert (await ac.get("/api/status", headers=me)).status_code != 401  # the signed-in browser itself still works
    assert _holds(PASSWORD)


# --- the read token -----------------------------------------------------------------------------------------------

async def test_the_read_token_is_shown_once_reads_only_and_can_be_replaced_or_removed(app_state, repo, tmp_path):
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        refused = await ac.post("/api/access/read-token")  # no password yet: nothing to protect
        assert refused.status_code == 409 and "Set a password first" in refused.json()["detail"]
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        assert (await ac.get("/api/access", headers=me)).json()["read_token"] is False
        made = await ac.post("/api/access/read-token", json={"current_password": PASSWORD}, headers=me)
        token = made.json()["read_token"]
        assert made.status_code == 200 and token.startswith("pgr_") and len(token) >= 40
        assert _stored(tmp_path)["read_token_hash"] == read_token_digest(token) and token not in json.dumps(_stored(tmp_path))
        assert access._close_live.closed == 1  # a first read token closes nothing
        reader = {"Authorization": f"Bearer {token}"}
        state = await ac.get("/api/access", headers=reader)
        assert state.status_code == 200 and state.json()["read_token"] is True and token not in state.text
        assert (await ac.get("/api/status", headers=reader)).status_code not in (401, 403)  # 503: an empty test state
        assert (await ac.get("/api/info", headers=reader)).json()["can_edit"] is False
        for method, path, body in (("PUT", "/api/access/password", {"password": "taken-over-1", "current_password": PASSWORD}),
                                   ("DELETE", "/api/access/read-token", None), ("POST", "/api/access/read-token", None),
                                   ("GET", "/api/settings/export", None), ("POST", "/api/mtr/trace", {"target_host": "x"})):
            res = await ac.request(method, path, json=body, headers=reader)
            assert res.status_code == 403 and "read token can only read" in res.json()["detail"], (method, path)
        assert (await ac.post("/login", data={"token": token})).status_code == 401  # not a password
        replaced = (await ac.post("/api/access/read-token", json={"current_password": PASSWORD},
                                  headers=me)).json()["read_token"]
        assert replaced != token and access._close_live.closed == 2  # live connections under the old one are closed
        assert (await ac.get("/api/status", headers=reader)).status_code == 401
        assert (await ac.get("/api/access", headers={"Authorization": f"Bearer {replaced}"})).status_code == 200
        removed = await ac.request("DELETE", "/api/access/read-token", json={"current_password": PASSWORD}, headers=me)
        assert removed.json() == {"read_token": False}
        assert (await ac.get("/api/status", headers={"Authorization": f"Bearer {replaced}"})).status_code == 401
    assert "read_token_hash" not in _stored(tmp_path) and access._close_live.closed == 3
    await _started(app_state, repo, tmp_path)
    assert security_config.read_token_hash == ""


async def test_a_signed_in_browser_alone_can_neither_make_nor_remove_the_read_token(app_state, repo, tmp_path):
    """A browser left signed in, or a copied cookie, must not leave a read token behind that outlives a new
    password and signing out everywhere, nor end the owner's: the current password is asked, as for changing it."""
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        for body, detail in ((None, "Enter the current password"), ({}, "Enter the current password"),
                             ({"current_password": ""}, "Enter the current password"),
                             ({"current_password": "a-guess-1"}, "not right")):
            res = await ac.post("/api/access/read-token", json=body, headers=me)
            assert res.status_code == 403 and detail in res.json()["detail"], body
            assert "read token" in res.json()["detail"] or detail == "not right"
        assert not access.read_token_set and "read_token_hash" not in _stored(tmp_path)
        token = (await ac.post("/api/access/read-token", json={"current_password": PASSWORD},
                               headers=me)).json()["read_token"]
        reader = {"Authorization": f"Bearer {token}"}
        for body in (None, {"current_password": "a-guess-2"}):
            res = await ac.request("DELETE", "/api/access/read-token", json=body, headers=me)
            assert res.status_code == 403, body
        assert (await ac.get("/api/access", headers=reader)).status_code == 200  # still the owner's
        replaced = await ac.post("/api/access/read-token", json={"current_password": "a-guess-3"}, headers=me)
        assert replaced.status_code == 403 and (await ac.get("/api/access", headers=reader)).status_code == 200
    assert _stored(tmp_path)["read_token_hash"] == read_token_digest(token)


async def test_with_the_environments_password_the_read_token_needs_that_one(app_state, repo, tmp_path):
    """The page cannot change a password from PENGUMON_ACCESS_TOKEN, but it can check it (the middleware holds it as
    text): the read token needs it as the current password, like a stored one."""
    await _started(app_state, repo, tmp_path, {"PENGUMON_ACCESS_TOKEN": "env-token-example"})
    async with _client() as ac:
        me = _cookie(await ac.post("/login", data={"token": "env-token-example"}))
        ac.cookies.clear()
        refused = await ac.post("/api/access/read-token", headers=me)
        assert refused.status_code == 403 and "Enter the current password" in refused.json()["detail"]
        wrong = await ac.post("/api/access/read-token", json={"current_password": "a-guess-1"}, headers=me)
        assert wrong.status_code == 403 and "not right" in wrong.json()["detail"]
        made = await ac.post("/api/access/read-token", json={"current_password": "env-token-example"}, headers=me)
        assert made.status_code == 200 and made.json()["read_token"].startswith("pgr_")


async def test_a_new_password_or_removing_it_removes_the_read_token(app_state, repo, tmp_path):
    """Changing the password is a complete recovery, whoever made the read token; and a password set after a
    removal does not bring back one made under an earlier password."""
    access = await _started(app_state, repo, tmp_path)
    async with _client() as ac:
        me = _cookie(await ac.post("/api/access/setup", json={"choice": "password", "password": PASSWORD}))
        ac.cookies.clear()
        token = (await ac.post("/api/access/read-token", json={"current_password": PASSWORD},
                               headers=me)).json()["read_token"]
        reader = {"Authorization": f"Bearer {token}"}
        assert (await ac.get("/api/access", headers=reader)).status_code == 200
        changed = await ac.put("/api/access/password", json={"password": "a-second-password",
                                                             "current_password": PASSWORD}, headers=me)
        ac.cookies.clear()
        assert changed.status_code == 200 and changed.json()["read_token_removed"] is True
        assert (await ac.get("/api/access", headers=reader)).status_code == 401
        assert "read_token_hash" not in _stored(tmp_path) and security_config.read_token_hash == ""
        me = _cookie(changed)
        assert (await ac.get("/api/access", headers=me)).json()["read_token"] is False
        again = await ac.put("/api/access/password", json={"password": "a-third-password",
                                                           "current_password": "a-second-password"}, headers=me)
        ac.cookies.clear()
        assert again.json()["read_token_removed"] is False  # there was none
        me = _cookie(again)
        second = (await ac.post("/api/access/read-token", json={"current_password": "a-third-password"},
                                headers=me)).json()["read_token"]
        removed = await ac.request("DELETE", "/api/access/password", json={"current_password": "a-third-password"},
                                   headers=me)
        assert removed.json() == {"mode": "open", "read_token_removed": True}
        assert "read_token_hash" not in _stored(tmp_path) and security_config.read_token_hash == ""
        fourth = await ac.put("/api/access/password", json={"password": "a-fourth-password"})  # none now: none asked
        ac.cookies.clear()
        assert fourth.status_code == 200 and fourth.json()["read_token_removed"] is False
        assert (await ac.get("/api/access", headers={"Authorization": f"Bearer {second}"})).status_code == 401
    await _started(app_state, repo, tmp_path)  # and not at the next start either
    assert security_config.read_token_hash == "" and access.read_token_set is False


async def test_a_read_token_left_in_the_file_is_removed_by_the_next_password(app_state, repo, tmp_path):
    """A read token in secrets.json while there is no password (one made before this version, then the password
    removed by hand as docs/security.md says): setting a password removes it rather than putting it back in force."""
    (tmp_path / "secrets.json").write_text(json.dumps({"read_token_hash": read_token_digest("pgr_left-over")}),
                                           encoding="utf-8")
    access = await _started(app_state, repo, tmp_path)
    await access.answer_setup("open")
    await access.set_password(PASSWORD)
    assert "read_token_hash" not in _stored(tmp_path) and security_config.read_token_hash == ""
    async with _client() as ac:
        assert (await ac.get("/api/access", headers={"Authorization": "Bearer pgr_left-over"})).status_code == 401


async def test_the_read_token_survives_a_restart_and_works_with_the_environments_token(app_state, repo, tmp_path):
    env = {"PENGUMON_ACCESS_TOKEN": "env-token-example"}
    access = await _started(app_state, repo, tmp_path, env)
    token = await access.new_read_token()
    await _started(app_state, repo, tmp_path, env)
    async with _client() as ac:
        assert (await ac.get("/api/access", headers={"Authorization": f"Bearer {token}"})).json()["read_token"] is True


async def test_the_read_token_calls_the_mcp_endpoint(mcp_state, tmp_path, monkeypatch):
    access = AccessManager(security_config, SecretStore(tmp_path / "secrets.json"), mcp_state.repository,
                           environ={"PENGUMON_ACCESS_TOKEN": "env-token-example"}, close_live=Live())
    await access.load()
    mcp_state.access = access
    token = await access.new_read_token()
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    async with _client() as ac:
        for path in ("/mcp", "/mcp/"):
            res = await ac.post(path, content=ping, headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"})
            assert res.status_code == 200 and res.json()["id"] == 1, path
        assert (await ac.post("/mcp", content=ping)).status_code == 401


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
        assert answer.getheader("strict-transport-security") == "max-age=31536000"  # no plain HTTP for a year
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
