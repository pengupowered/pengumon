"""The `pengumon` command: version, paths, backup (and that no command means run)."""

import os
import sqlite3
import stat

import pytest

from pengumon import __version__, cli


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    for name in ("PENGUMON_CONFIG", "PENGUMON_HOST", "PENGUMON_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PENGUMON_DATA", str(folder))
    return folder


def _make_db(path, rows=3):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    return con  # left open: the "running app"


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == f"pengumon {__version__}\n"


def test_paths_shows_values_and_where_they_came_from(data, capsys):
    assert cli.main(["paths"]) == 0
    out = capsys.readouterr().out
    assert str(data / "config.yaml") in out and "missing" in out
    assert f"{data}   [PENGUMON_DATA]" in out
    assert str(data / "pengumon.db") in out and str(data / "secrets.json") in out
    assert "127.0.0.1   [default]" in out and "8000   [default]" in out


def test_paths_follows_env_and_config(data, tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("server:\n  host: 192.0.2.4\n  port: 9100\ndatabase:\n  db_path: data/legacy.db\n")
    monkeypatch.setenv("PENGUMON_CONFIG", str(cfg))
    monkeypatch.setenv("PENGUMON_PORT", "18080")
    assert cli.main(["paths"]) == 0
    out = capsys.readouterr().out
    assert f"{cfg}   [PENGUMON_CONFIG]" in out
    assert "data/legacy.db   [database.db_path in the config]" in out
    assert "192.0.2.4   [server.host in the config]" in out
    assert "18080   [PENGUMON_PORT]" in out


def test_bad_config_is_a_one_line_error(data, monkeypatch, capsys):
    (data / "config.yaml").write_text("server: [unclosed\n")
    assert cli.main(["paths"]) == 2
    assert "not valid YAML" in capsys.readouterr().err


def test_no_command_runs_the_server(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "cmd_run", lambda args: called.append(args) or 0)
    # the parser's default is looked up by name at call time
    assert cli.main([]) == 0
    assert called


def test_run_starts_the_app_main(monkeypatch):
    from pengumon import main as app_main
    called = []
    monkeypatch.setattr(app_main, "main", lambda: called.append(1))
    assert cli.main(["run"]) == 0 and called == [1]


def test_backup_copies_database_secrets_and_sessions_while_the_app_has_it_open(data, tmp_path, capsys):
    live = _make_db(data / "pengumon.db")
    (data / "secrets.json").write_text('{"k": 1}')
    (data / "sessions.json").write_text('{"s": 1}')
    live.execute("INSERT INTO t VALUES (99)")  # uncommitted write in the app: not in the backup, no lock problem
    assert cli.main(["backup"]) == 0
    folder = capsys.readouterr().out.strip()
    assert os.path.dirname(folder) == str(data / "backups")
    assert sorted(os.listdir(folder)) == ["pengumon.db", "secrets.json", "sessions.json"]
    copy = sqlite3.connect(os.path.join(folder, "pengumon.db"))
    assert copy.execute("SELECT COUNT(*) FROM t").fetchone() == (3,)
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()
    for name in os.listdir(folder):
        assert stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    live.commit()
    live.close()


def test_backup_to_another_folder_and_without_secrets(data, tmp_path, capsys):
    _make_db(data / "pengumon.db").close()
    target = tmp_path / "elsewhere"
    assert cli.main(["backup", "--to", str(target)]) == 0
    folder = capsys.readouterr().out.strip()
    assert os.path.dirname(folder) == str(target)
    assert os.listdir(folder) == ["pengumon.db"]  # no secrets file to copy: none is made up


def test_backup_uses_the_configured_legacy_database_path(data, tmp_path, monkeypatch, capsys):
    legacy = tmp_path / "work" / "data"
    legacy.mkdir(parents=True)
    _make_db(legacy / "pengumon.db").close()
    (data / "config.yaml").write_text("database:\n  db_path: data/pengumon.db\n")
    monkeypatch.chdir(tmp_path / "work")
    assert cli.main(["backup", "--to", str(tmp_path / "out")]) == 0
    folder = capsys.readouterr().out.strip()
    assert sqlite3.connect(os.path.join(folder, "pengumon.db")).execute("SELECT COUNT(*) FROM t").fetchone() == (3,)


def test_backup_without_a_database_fails_and_leaves_nothing(data, tmp_path):
    with pytest.raises(SystemExit, match="no database"):
        cli.main(["backup", "--to", str(tmp_path / "out")])
    assert not (tmp_path / "out").exists()
    assert not (data / "backups").exists()


def test_backup_does_not_create_the_database(data):
    with pytest.raises(SystemExit):
        cli.main(["backup"])
    assert not (data / "pengumon.db").exists()  # mode=ro: a typo'd path is not turned into an empty database
