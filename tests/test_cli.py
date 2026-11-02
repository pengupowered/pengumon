"""The `pengumon` command: version, paths, backup, doctor (and that no command means run)."""

import os
import sqlite3
import stat

import pytest

from pengumon import __version__, cli
from platform_helpers import assert_mode


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
        assert_mode(os.path.join(folder, name), 0o600)
    assert_mode(folder, 0o700)
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


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs symbolic links")
@pytest.mark.parametrize("name", ["secrets.json", "sessions.json"])
def test_backup_refuses_a_symlinked_secrets_or_sessions_file(data, tmp_path, name):
    _make_db(data / "pengumon.db").close()
    elsewhere = tmp_path / "other-secret.txt"
    elsewhere.write_text("not pengumon's to copy")
    (data / name).symlink_to(elsewhere)
    with pytest.raises(SystemExit, match="symbolic link"):
        cli.main(["backup", "--to", str(tmp_path / "out")])
    assert not any((tmp_path / "out").rglob("*"))  # the half-made folder is removed, nothing was copied


def test_backup_without_a_database_fails_and_leaves_nothing(data, tmp_path):
    with pytest.raises(SystemExit, match="no database"):
        cli.main(["backup", "--to", str(tmp_path / "out")])
    assert not (tmp_path / "out").exists()
    assert not (data / "backups").exists()


def test_backup_does_not_create_the_database(data):
    with pytest.raises(SystemExit):
        cli.main(["backup"])
    assert not (data / "pengumon.db").exists()  # mode=ro: a typo'd path is not turned into an empty database


# ---- pengumon doctor ----------------------------------------------------------------------------------------------

def _doctor_with(monkeypatch, backend=None, error=None, route_table=None, tmp_path=None, ipv6_route=None,
                 ipv6_table=None):
    from pengumon.engine import icmp_select, netinfo

    monkeypatch.setattr(netinfo, "ipv6_route_check", lambda: ipv6_route)  # None: a route; else why not
    if tmp_path is not None:
        table = tmp_path / "ipv6_route"
        if ipv6_table is not None:
            table.write_text(ipv6_table)
        monkeypatch.setattr(netinfo, "PROC_NET_IPV6_ROUTE", table)

    def opener():
        if error:
            raise error
        return backend

    monkeypatch.setattr(icmp_select, "open_backend", opener)
    if tmp_path is not None:
        route = tmp_path / "route"
        if route_table is not None:
            route.write_text(route_table)
        monkeypatch.setattr(netinfo, "PROC_NET_ROUTE", route)


ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
         "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n")  # default via 192.168.1.1


def test_doctor_when_probing_works(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend, FakePath

    backend = FakeBackend(path=FakePath(["192.168.1.1"]))
    _doctor_with(monkeypatch, backend, route_table=ROUTE, tmp_path=tmp_path)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ICMP backend    fake (1 µs resolution)" in out
    assert "ping 127.0.0.1  echo reply from 127.0.0.1 in 2.000 ms" in out
    assert "TTL=1 probe     time exceeded from 192.168.1.1 in 1.000 ms" in out
    assert f"gateway         192.168.1.1 (from {tmp_path / 'route'})" in out
    assert f"data directory  {data}" in out and "listen address  127.0.0.1:8000" in out
    assert out.rstrip().endswith("probing works")
    assert ("127.0.0.1", 64, 1.0) in backend.sent and ("1.1.1.1", 1, 1.0) in backend.sent and backend.closed


def test_doctor_without_a_route_table_uses_the_first_router(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend, FakePath

    _doctor_with(monkeypatch, FakeBackend(path=FakePath(["10.1.2.3"])), tmp_path=tmp_path)
    assert cli.main(["doctor"]) == 0
    assert "gateway         10.1.2.3 (the first router, from a TTL=1 probe)" in capsys.readouterr().out


def test_doctor_when_ttl_probes_get_no_answer(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend, FakePath

    _doctor_with(monkeypatch, FakeBackend(path=FakePath([None])), tmp_path=tmp_path)
    monkeypatch.setattr(cli, "_doctor_probes", _quick_probes)
    assert cli.main(["doctor"]) == 0  # loopback works: probing works, traceroute just sees nothing here
    out = capsys.readouterr().out
    assert "TTL=1 probe     no answer within 1 s (TTL probes need a router that answers" in out
    assert "gateway         unknown" in out


def _quick_probes(backend):
    import asyncio

    async def one(address, ttl=64):
        try:
            return await backend.probe(address, timeout=0.05, ttl=ttl)
        except OSError as exc:  # as cli._doctor_probes reports a send failure
            return exc

    async def both():
        return [await one("127.0.0.1"), await one("1.1.1.1", 1), await one("::1")]

    return asyncio.run(both())


def test_doctor_when_loopback_does_not_answer(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend
    from pengumon.engine.icmp_base import ProbeResult

    _doctor_with(monkeypatch, FakeBackend(lambda address, ttl: ProbeResult("timeout")), tmp_path=tmp_path)
    monkeypatch.setattr(cli, "_doctor_probes", _quick_probes)
    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "ping 127.0.0.1  no answer within 1 s" in captured.out
    assert "probing does not work" in captured.err


def test_doctor_without_icmp_prints_the_fix(data, monkeypatch, tmp_path, capsys):
    from pengumon.engine.icmp_base import IcmpUnavailable
    from pengumon.engine.icmp_linux import SYSCTL_FIX

    _doctor_with(monkeypatch, error=IcmpUnavailable("cannot open an ICMP socket: ping socket: Permission denied",
                                                   SYSCTL_FIX), tmp_path=tmp_path)
    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "ICMP backend    none: cannot open an ICMP socket: ping socket: Permission denied" in captured.out
    assert f"fix             {SYSCTL_FIX}" in captured.out
    assert f"data directory  {data}" in captured.out
    assert "probing does not work" in captured.err


def test_doctor_send_failure_is_reported(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend

    _doctor_with(monkeypatch, FakeBackend(lambda address, ttl: OSError("Network is unreachable")), tmp_path=tmp_path)
    assert cli.main(["doctor"]) == 1
    assert "ping 127.0.0.1  send failed: Network is unreachable" in capsys.readouterr().out


# an IPv6 default route via a link-local router on eth0, metric 1024 (/proc/net/ipv6_route's layout)
IPV6_ROUTE = ("00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
              "fe800000000000000000000000000001 00000400 00000001 00000000 00000003     eth0\n")


def test_doctor_reports_ipv6(data, monkeypatch, tmp_path, capsys):
    from icmp_fakes import FakeBackend, FakePath

    backend = FakeBackend(path=FakePath(["192.168.1.1"]))
    _doctor_with(monkeypatch, backend, route_table=ROUTE, tmp_path=tmp_path, ipv6_table=IPV6_ROUTE)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "IPv6            available (a route to 2606:4700:4700::1111)" in out
    assert "IPv6 backend    fake" in out
    assert "ping ::1        echo reply from ::1 in 2.000 ms" in out
    assert "IPv6 router     fe80::1%eth0" in out
    assert ("::1", 64, 1.0) in backend.sent


def test_doctor_without_ipv6_still_passes(data, monkeypatch, tmp_path, capsys):
    """IPv6 is reported, never required: a host without it probes IPv4 fine."""
    import errno

    from icmp_fakes import FakeBackend
    from pengumon.engine.icmp_base import ProbeResult

    def answer(address, ttl):
        if ":" in address:
            return OSError(errno.EAFNOSUPPORT, "this host has no IPv6")
        return ProbeResult("reply", address, 1.0)

    _doctor_with(monkeypatch, FakeBackend(answer), tmp_path=tmp_path, ipv6_route="Network is unreachable")
    monkeypatch.setattr(cli, "_doctor_probes", _quick_probes)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert 'IPv6            no route to the internet (Network is unreachable): IPv6 targets show "IPv6 unavailable on this host"' in out
    assert "IPv6 backend    none" in out
    assert "ping ::1        send failed: " in out and "this host has no IPv6" in out
    assert "IPv6 router     none" in out
