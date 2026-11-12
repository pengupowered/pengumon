"""pengumon.paths: where the files live and which address the server listens on."""

from pathlib import Path

import pytest

from pengumon import paths
from pengumon.config import AppConfig, DatabaseConfig, ServerConfig, load_config, load_server_config
from platform_helpers import assert_mode


def test_data_dir_prefers_pengumon_data():
    assert paths.data_dir({"PENGUMON_DATA": "/srv/pg"}) == Path("/srv/pg")
    assert paths.secrets_path({"PENGUMON_DATA": "/srv/pg"}) == Path("/srv/pg/secrets.json")
    assert paths.sessions_path({"PENGUMON_DATA": "/srv/pg"}) == Path("/srv/pg/sessions.json")
    assert paths.backups_dir({"PENGUMON_DATA": "/srv/pg"}) == Path("/srv/pg/backups")


# An absolute path on the OS running the test ("/x/state" has no drive on Windows, so it is not absolute there)
XDG = Path("/x/state").resolve()


def test_os_defaults(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.default_data_dir({}, "linux") == Path("/home/u/.local/state/pengumon")
    assert paths.default_data_dir({"XDG_STATE_HOME": str(XDG)}, "linux") == XDG / "pengumon"
    assert paths.default_data_dir({"XDG_STATE_HOME": "relative"}, "linux") == Path("/home/u/.local/state/pengumon")
    assert paths.default_data_dir({}, "darwin") == Path("/home/u/Library/Application Support/Pengumon")
    win = paths.default_data_dir({"LOCALAPPDATA": "C:/Users/u/AppData/Local"}, "win32")
    assert win == Path("C:/Users/u/AppData/Local") / "Pengumon"


def test_data_dir_uses_the_platform_default_when_unset(monkeypatch):
    monkeypatch.delenv("PENGUMON_DATA", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(XDG))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.data_dir() == XDG / "pengumon"
    assert paths.data_dir_source() == "default for this system"


def test_ensure_data_dir_creates_it_private(tmp_path):
    target = tmp_path / "a" / "pg"
    assert paths.ensure_data_dir({"PENGUMON_DATA": str(target)}) == target
    assert_mode(target, 0o700)


def test_config_defaults_to_the_data_directory():
    assert paths.config_path({"PENGUMON_DATA": "/srv/pg"}) == Path("/srv/pg/config.yaml")
    assert paths.config_path({"PENGUMON_DATA": "/srv/pg", "PENGUMON_CONFIG": "/etc/pg.yaml"}) == Path("/etc/pg.yaml")


def test_load_config_without_a_path_follows_the_environment(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("line:\n  isp_name: From Data Dir\n")
    monkeypatch.delenv("PENGUMON_CONFIG", raising=False)
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path))
    assert load_config().line.isp_name == "From Data Dir"
    other = tmp_path / "other.yaml"
    other.write_text("line:\n  isp_name: From Env\n")
    monkeypatch.setenv("PENGUMON_CONFIG", str(other))
    assert load_config().line.isp_name == "From Env"
    monkeypatch.setenv("PENGUMON_CONFIG", str(tmp_path / "absent.yaml"))  # missing file: defaults, as before
    assert load_config().line.isp_name != "From Env"


def test_db_path_unset_is_in_the_data_directory():
    assert paths.db_path(None, {"PENGUMON_DATA": "/srv/pg"}) == str(Path("/srv/pg") / "pengumon.db")
    assert DatabaseConfig().db_path is None


def test_a_configured_relative_db_path_keeps_its_old_meaning():
    """Existing configs say `data/pengumon.db` and run with the working directory /app: the same file as before."""
    assert paths.db_path("data/pengumon.db", {"PENGUMON_DATA": "/elsewhere"}) == "data/pengumon.db"
    assert paths.db_path("/abs/p.db", {"PENGUMON_DATA": "/elsewhere"}) == "/abs/p.db"


def test_repository_default_database_is_in_the_data_directory(tmp_path, monkeypatch):
    from pengumon.db.repository import DatabaseRepository
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)  # a relative path is made under the working directory, which is read-only in the test container
    assert DatabaseRepository().db_path == str(tmp_path / "state" / "pengumon.db")
    assert DatabaseRepository(db_path="data/pengumon.db").db_path == "data/pengumon.db"


def test_listen_default_is_loopback_not_all_interfaces():
    assert paths.listen_address(None, {})[:2] == ("127.0.0.1", 8000)
    assert paths.listen_address(ServerConfig(), {})[:2] == ("127.0.0.1", 8000)  # the model's own default
    assert AppConfig().server.host == "127.0.0.1"


def test_listen_precedence_env_then_config_then_default():
    configured = ServerConfig(host="192.0.2.7", port=9001)
    assert paths.listen_address(configured, {}) == ("192.0.2.7", 9001, "server.host in the config",
                                                     "server.port in the config")
    env = {"PENGUMON_HOST": "0.0.0.0", "PENGUMON_PORT": "18080"}
    assert paths.listen_address(configured, env) == ("0.0.0.0", 18080, "PENGUMON_HOST", "PENGUMON_PORT")
    only_port = ServerConfig(port=9001)  # host not in the file: default, not the model's value
    assert paths.listen_address(only_port, {})[:3] == ("127.0.0.1", 9001, "default")


def test_a_bad_port_is_a_clear_error():
    with pytest.raises(ValueError, match="PENGUMON_PORT"):
        paths.listen_address(None, {"PENGUMON_PORT": "http"})


def test_server_section_is_read_from_the_file(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("server:\n  host: 192.0.2.9\n")
    server = load_server_config(str(cfg))
    assert paths.listen_address(server, {})[:3] == ("192.0.2.9", 8000, "server.host in the config")
    assert load_server_config(str(tmp_path / "absent.yaml")).host == "127.0.0.1"


def test_main_listens_where_the_paths_say(tmp_path, monkeypatch):
    from pengumon import main as app_main
    cfg = tmp_path / "c.yaml"
    cfg.write_text("server:\n  host: 192.0.2.9\n  port: 9002\n")
    calls = []
    monkeypatch.setattr(app_main.uvicorn, "run", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(app_main, "setup_logging", lambda: None)
    monkeypatch.setenv("PENGUMON_CONFIG", str(cfg))
    monkeypatch.delenv("PENGUMON_HOST", raising=False)
    monkeypatch.delenv("PENGUMON_PORT", raising=False)
    app_main.main()
    assert (calls[-1]["host"], calls[-1]["port"]) == ("192.0.2.9", 9002)
    monkeypatch.delenv("PENGUMON_CONFIG")
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path / "nothing-here"))
    app_main.main()
    assert (calls[-1]["host"], calls[-1]["port"]) == ("127.0.0.1", 8000)
    monkeypatch.setenv("PENGUMON_HOST", "0.0.0.0")
    app_main.main()
    assert calls[-1]["host"] == "0.0.0.0"


def test_module_level_secret_and_session_paths_come_from_paths():
    from pengumon import secrets, security
    assert secrets.SECRETS_PATH == paths.secrets_path()
    assert security.SESSIONS_PATH == paths.sessions_path()


def test_main_caps_the_size_of_a_websocket_message(tmp_path, monkeypatch):
    from pengumon import main as app_main
    calls = []
    monkeypatch.setattr(app_main.uvicorn, "run", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(app_main, "setup_logging", lambda: None)
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path / "nothing-here"))
    app_main.main()
    assert calls[-1]["ws_max_size"] == 4096
