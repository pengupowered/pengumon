"""Device credentials (pengumon.secrets): every source and its precedence, writing new credentials, and never
exposing a password."""

import json
import logging
import os

import pytest
from pathlib import Path

from pengumon import secrets as secrets_module
from pengumon.devices.settings import DeviceConfig
from pengumon.secrets import (CredentialSources, Credentials, SecretsFileUnusable, SecretStore, credential_updates,
                              update_secrets)
from platform_helpers import assert_mode, posix_files

SECRET = "hunter2-very-secret"


def _device(device_id="wan", **kw) -> DeviceConfig:
    return DeviceConfig(id=device_id, driver="asuswrt", url="http://192.168.1.1", **kw)


def _sources(tmp_path, env=None, store=None, files=None, store_file=None) -> CredentialSources:
    secrets_dir = tmp_path / "run-secrets"
    secrets_dir.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (secrets_dir / name).write_bytes(text.encode())  # as written: no newline translation on Windows
    path = tmp_path / "secrets.json"
    if store is not None:
        path.write_text(json.dumps(store))
        os.chmod(path, 0o600)
    return CredentialSources(env=env or {}, secrets_dir=secrets_dir, secrets_file=store_file or path)


# --- new sources ----------------------------------------------------------------------------------------------------

def test_without_any_source_there_is_no_password_and_the_config_username(tmp_path):
    creds = _sources(tmp_path).resolve(_device(username="root"))
    assert creds == Credentials("root", None, "config", None)
    assert not creds.has_password and creds.describe() == "no password configured"


def test_the_environment_wins_over_the_secret_file_and_secrets_json(tmp_path):
    sources = _sources(
        tmp_path,
        env={"PENGUMON_DEVICE_MY_WAN_PASSWORD": "from-env", "PENGUMON_DEVICE_MY_WAN_USERNAME": "env-user"},
        files={"pengumon_my-wan_password": "from-file\n"},
        store={"devices": {"my-wan": {"username": "stored-user", "password": "from-json"}}})
    creds = sources.resolve(_device("my-wan"))
    assert (creds.username, creds.password) == ("env-user", "from-env")
    assert creds.password_source == "environment PENGUMON_DEVICE_MY_WAN_PASSWORD"
    assert creds.username_source == "environment PENGUMON_DEVICE_MY_WAN_USERNAME"


def test_the_secret_file_wins_over_secrets_json_and_loses_one_trailing_newline(tmp_path):
    sources = _sources(tmp_path, files={"pengumon_wan_password": "from file \r\n"},
                       store={"devices": {"wan": {"username": "stored-user", "password": "from-json"}}})
    creds = sources.resolve(_device())
    assert creds.password == "from file "  # inner and trailing spaces are part of the password
    assert creds.password_source.endswith(str(Path("run-secrets") / "pengumon_wan_password"))
    assert (creds.username, creds.username_source) == ("stored-user", f"{tmp_path / 'secrets.json'} devices.wan.username")
    assert creds.describe() == (f"password from {creds.password_source}, "
                                f"username from {tmp_path / 'secrets.json'} devices.wan.username")


def test_secrets_json_is_the_last_new_source(tmp_path):
    creds = _sources(tmp_path, store={"devices": {"wan": {"password": "from-json"}}}).resolve(_device())
    assert creds.password == "from-json"
    assert creds.password_source == f"{tmp_path / 'secrets.json'} devices.wan.password"
    assert creds.username_source == "config"


def test_an_empty_or_unreadable_secret_file_falls_through_with_a_warning(tmp_path, caplog):
    sources_dir = tmp_path / "run-secrets"
    sources_dir.mkdir()
    (sources_dir / "pengumon_wan_password").write_text("\n")
    (sources_dir / "pengumon_lan_password").mkdir()  # reading a directory fails with an OSError
    store = tmp_path / "secrets.json"
    store.write_text(json.dumps({"devices": {"wan": {"password": "j1"}, "lan": {"password": "j2"}}}))
    os.chmod(store, 0o600)
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"):
        sources = CredentialSources(env={}, secrets_dir=sources_dir, secrets_file=store)
        assert sources.resolve(_device("wan")).password == "j1"
        assert sources.resolve(_device("lan")).password == "j2"
    assert [r.getMessage() for r in caplog.records] == [
        "Secret file is empty; trying the next credential source",
        "Secret file is not a regular file; trying the next credential source"]


def test_the_environment_is_used_only_for_its_own_device(tmp_path):
    sources = _sources(tmp_path, env={"PENGUMON_DEVICE_OTHER_PASSWORD": "x"})
    assert sources.resolve(_device("wan")).password is None


@pytest.mark.parametrize("name", ["PENGUMON_ROUTER_PASSWORD", "ROUTER_PASSWORD", "PENGUMON_ROUTER_USERNAME",
                                  "PENGUMON_MODEM_PASSWORD", "GATEWAY_PASSWORD", "PENGUMON_MODEM_USERNAME"])
def test_the_names_of_the_builds_before_devices_are_not_read(tmp_path, name):
    sources = _sources(tmp_path, env={name: "old"})
    for device_id in ("modem", "router"):
        assert sources.resolve(_device(device_id)) == Credentials("admin", None, "config", None)


# --- secrets.json problems ------------------------------------------------------------------------------------------

def test_an_unparsable_secrets_file_is_reported_without_its_content(tmp_path, caplog):
    path = tmp_path / "secrets.json"
    path.write_text('{"devices": {"router": {"password": "' + SECRET + '"')
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"):
        creds = CredentialSources(env={}, secrets_dir=None, secrets_file=path).resolve(_device("router"))
    assert creds.password is None
    assert "not valid JSON" in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize("save", [
    lambda store: store.write(credential_updates("router", "root", "new-pw")),  # the device dialog
    lambda store: store.change({"ntfy": {"token": "new-token"}}),  # a Settings save
], ids=["write", "change"])
def test_a_save_never_overwrites_an_unparsable_secrets_file(tmp_path, caplog, save):
    """A typo in a hand-edited secrets.json plus one save used to replace every credential in it with the new one."""
    path = tmp_path / "secrets.json"
    damaged = ('{"devices": {"modem": {"password": "' + SECRET + '"},, '
               '"wan": {"password": "' + SECRET + '"}}}')  # a stray comma
    path.write_text(damaged)
    store = SecretStore(path)
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"), pytest.raises(SecretsFileUnusable):
        save(store)
    assert path.read_text() == damaged
    assert "nothing was saved" in caplog.text and SECRET not in caplog.text


@posix_files
def test_a_secrets_file_readable_by_others_is_reported(tmp_path, caplog):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"devices": {"wan": {"password": SECRET}}}))
    os.chmod(path, 0o644)
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"):
        creds = CredentialSources(env={}, secrets_dir=None, secrets_file=path).resolve(_device())
    assert creds.password == SECRET
    assert "readable by other users (mode 0644)" in caplog.text and SECRET not in caplog.text


def test_on_windows_a_secrets_file_mode_is_not_reported(tmp_path, caplog, monkeypatch):
    """Windows has no permission bits (every file reads as 0666), so the warning would fire on every start there."""
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"devices": {"wan": {"password": SECRET}}}))
    os.chmod(path, 0o644)
    monkeypatch.setattr(secrets_module.sys, "platform", "win32")
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"):
        creds = CredentialSources(env={}, secrets_dir=None, secrets_file=path).resolve(_device())
    assert creds.password == SECRET and "readable by other users" not in caplog.text


def test_secret_files_are_opened_without_newline_translation(tmp_path, monkeypatch):
    """os.open is text mode on Windows unless O_BINARY is given (CRLF translation, ^Z ends the file): the secret file
    read and the secrets.json write both ask for it. O_BINARY is 0x8000 on Windows and absent elsewhere; a stand-in
    bit shows here that it is passed."""
    o_binary = 0x40000000  # no Linux open flag uses it
    real_open = os.open
    seen = {}

    def recording_open(path, flags, *args):
        seen[Path(path).name] = bool(flags & o_binary)
        return real_open(path, flags & ~o_binary, *args)

    monkeypatch.setattr(secrets_module.os, "O_BINARY", o_binary, raising=False)
    monkeypatch.setattr(secrets_module.os, "open", recording_open)
    sources = _sources(tmp_path, files={"pengumon_wan_password": "pw\r\n"})
    assert sources.resolve(_device()).password == "pw"
    secrets_module.write_secrets({"devices": {}}, tmp_path / "written.json")
    assert seen == {"pengumon_wan_password": True, "written.json.tmp": True}


def test_non_string_values_are_ignored(tmp_path):
    store = {"devices": {"wan": {"password": 1234, "username": ["x"]}}, "devices_extra": None}
    assert _sources(tmp_path, store=store).resolve(_device()) == Credentials("admin", None, "config", None)
    assert _sources(tmp_path, store={"devices": "nonsense"}).resolve(_device()).password is None


def test_the_secrets_file_default_is_looked_up_when_the_sources_are_made(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere.json"
    path.write_text(json.dumps({"devices": {"wan": {"password": "p"}}}))
    os.chmod(path, 0o600)
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", path)
    assert CredentialSources(env={}, secrets_dir=None).resolve(_device()).password == "p"


# --- never exposed --------------------------------------------------------------------------------------------------

def test_credentials_never_show_the_password():
    creds = Credentials("admin", SECRET, "config", "environment X")
    assert SECRET not in repr(creds) and SECRET not in str(creds) and SECRET not in creds.describe()
    assert creds.describe() == "password from environment X, username from config"


# --- writing --------------------------------------------------------------------------------------------------------

def test_new_credentials_are_written_under_the_device_only():
    assert credential_updates("router", "admin", "pw") == {"devices": {"router": {"username": "admin", "password": "pw"}}}
    assert credential_updates("upstairs", "u", "p") == {"devices": {"upstairs": {"username": "u", "password": "p"}}}


def test_written_credentials_are_read_back(tmp_path):
    path = tmp_path / "secrets.json"
    update_secrets(credential_updates("router", "root", "router-pw"), path)
    creds = CredentialSources(env={}, secrets_dir=None, secrets_file=path).resolve(_device("router"))
    assert (creds.username, creds.password) == ("root", "router-pw")
    assert creds.password_source == f"{path} devices.router.password"


# --- /run/secrets files ---------------------------------------------------------------------------------------------

def _from_file(tmp_path, make, caplog):
    secrets_dir = tmp_path / "run"
    secrets_dir.mkdir()
    make(secrets_dir / "pengumon_wan_password")
    with caplog.at_level(logging.WARNING, logger="pengumon.secrets"):
        creds = CredentialSources(env={}, secrets_dir=secrets_dir, store={}).resolve(_device())
    return creds, [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("content, expected", [
    ("pw\r\n", "pw"),
    ("pw\n", "pw"),
    ("pw\n\n", "pw\n"),          # exactly one newline is removed
    ("pw\r\n\r\n", "pw\r\n"),
    ("pw\r", "pw\r"),           # a lone CR is part of the password
    ("  pw  ", "  pw  "),
    ("x" * 4096, "x" * 4096),   # the limit itself is fine
])
def test_a_secret_file_loses_exactly_one_trailing_newline(tmp_path, caplog, content, expected):
    creds, warnings = _from_file(tmp_path, lambda p: p.write_bytes(content.encode()), caplog)
    assert creds.password == expected and warnings == []


@pytest.mark.parametrize("make, problem", [
    (lambda p: p.mkdir(), "is not a regular file"),
    pytest.param(lambda p: os.mkfifo(p), "is not a regular file", marks=posix_files),
    (lambda p: p.write_bytes(b"x" * 4097), "is larger than 4096 bytes"),
    (lambda p: p.write_bytes(b"\xff\xfe pw"), "is not UTF-8 text"),
    (lambda p: p.write_bytes(b"\r\n"), "is empty"),  # bytes: write_text would make it \r\r\n on Windows
], ids=["directory", "fifo", "oversized", "binary", "only-a-newline"])
def test_a_secret_file_that_is_not_a_small_text_file_is_skipped_by_path(tmp_path, caplog, make, problem):
    creds, warnings = _from_file(tmp_path, make, caplog)  # a FIFO must not block: it is never opened
    assert creds.password is None
    assert warnings == [f"Secret file {problem}; trying the next credential source"]
    (record,) = caplog.records
    assert record.pengumon_context["path"].endswith(str(Path("run") / "pengumon_wan_password"))


def test_a_secret_file_is_read_once(tmp_path):
    secrets_dir = tmp_path / "run"
    secrets_dir.mkdir()
    (secrets_dir / "pengumon_wan_password").write_text("first")
    sources = CredentialSources(env={}, secrets_dir=secrets_dir, store={})
    assert sources.resolve(_device()).password == "first"
    (secrets_dir / "pengumon_wan_password").write_text("second")
    assert sources.resolve(_device()).password == "first"


# --- SecretStore ----------------------------------------------------------------------------------------------------

def test_the_store_reads_once_and_writes_through(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"note": "a"}))
    os.chmod(path, 0o600)
    store = SecretStore(path)
    assert store.data() == {"note": "a"}
    path.write_text(json.dumps({"note": "changed behind our back"}))
    assert store.data() == {"note": "a"}  # read once
    store.write({"devices": {"wan": {"username": "u", "password": "p"}}})
    assert store.data()["devices"] == {"wan": {"username": "u", "password": "p"}}
    on_disk = json.loads(path.read_text())
    assert on_disk["devices"] == {"wan": {"username": "u", "password": "p"}}
    assert_mode(path, 0o600)
    assert store.label("devices.wan.password") == f"{path} devices.wan.password"


def test_the_sources_and_the_store_share_one_file(tmp_path):
    store = SecretStore(tmp_path / "s.json", data={"devices": {"wan": {"password": "p"}}})
    sources = CredentialSources(env={}, secrets_dir=None, store=store)
    assert sources.store is store
    assert sources.resolve(_device()).password_source == f"{tmp_path / 's.json'} devices.wan.password"


def test_the_default_store_path_is_config_secrets_path(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", tmp_path / "x.json")
    assert SecretStore().path == tmp_path / "x.json"


# --- what outranks the store ----------------------------------------------------------------------------------------


def test_outranking_names_the_source_that_would_override_saved_credentials(tmp_path):
    secrets_dir = tmp_path / "run"
    secrets_dir.mkdir()
    (secrets_dir / "pengumon_router_password").write_text("file")
    router = _device("router")

    def outranking(env=None, secrets_dir_=None):
        store = {"devices": {"router": {"password": "s"}}}
        return CredentialSources(env=env or {}, secrets_dir=secrets_dir_, store=store).outranking(router)

    assert outranking(env={"PENGUMON_DEVICE_ROUTER_PASSWORD": "e"}, secrets_dir_=secrets_dir) == \
        "environment PENGUMON_DEVICE_ROUTER_PASSWORD"
    assert outranking(secrets_dir_=secrets_dir) == str(secrets_dir / "pengumon_router_password")
    assert outranking() is None  # only secrets.json: nothing outranks it
    assert outranking(env={"PENGUMON_DEVICE_ROUTER_USERNAME": "root"}) == "environment PENGUMON_DEVICE_ROUTER_USERNAME"
