"""The `pengumon` command: run the server, show the version, show where files live, back the data up.

    pengumon [run]                 start the server (the default)
    pengumon version
    pengumon paths                 config file, data directory, database, secrets, listen address, and where each
                                   value came from (pengumon.paths has the rules)
    pengumon backup [--to DIR]     a consistent copy of the database (VACUUM INTO, safe while the app runs) plus
                                   secrets.json and sessions.json, in a timestamped folder (default: <data>/backups)
    pengumon service install|uninstall|status
                                   start Pengumon automatically (systemd, launchd or a logon task; pengumon.service)
    pengumon doctor                can this host probe? The ICMP backend and its resolution, an echo to 127.0.0.1,
                                   a TTL=1 probe (the first router), the gateway, IPv6 (a route, the IPv6 backend,
                                   an echo to ::1, the IPv6 default router), the data directory and the listen
                                   address; when ICMP can't work, the one step that fixes it. Exit 0 when IPv4
                                   probing works, 1 when it doesn't (IPv6 is reported, never required)

Standard library only apart from the app's own config models; the server is imported only by `run`.
"""

import argparse
import errno
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from pengumon import __version__, paths, service
from pengumon.config import DatabaseConfig, ServerConfig, read_config_file


def _file_settings():
    """(server section, database section, config file path) read from the config file."""
    path = paths.config_path()
    raw = read_config_file(path)
    return ServerConfig(**(raw.get("server") or {})), DatabaseConfig(**(raw.get("database") or {})), path


def cmd_version(_args) -> int:
    print(f"pengumon {__version__}")
    return 0


def cmd_paths(_args) -> int:
    server, database, config = _file_settings()
    host, port, host_from, port_from = paths.listen_address(server)
    rows = [
        ("config file", f"{config}{'' if config.exists() else '  (missing: defaults are used)'}", paths.config_source()),
        ("data directory", str(paths.data_dir()), paths.data_dir_source()),
        ("database", paths.db_path(database.db_path),
         "database.db_path in the config" if database.db_path else "<data directory>/pengumon.db"),
        ("secrets", str(paths.secrets_path()), "<data directory>/secrets.json"),
        ("sessions", str(paths.sessions_path()), "<data directory>/sessions.json"),
        ("backups", str(paths.backups_dir()), "<data directory>/backups"),
        ("listen host", host, host_from),
        ("listen port", str(port), port_from),
    ]
    width = max(len(r[0]) for r in rows)
    for name, value, source in rows:
        print(f"{name:<{width}}  {value}   [{source}]")
    return 0


LOG_FILE = "pengumon.log"
LOG_ROTATE_BYTES = 5_000_000


def _quiet_streams() -> None:
    """`pengumonw` (Windows, no console) has no stdout or stderr, and the logging handler and uvicorn write to
    them. They go to <data directory>/pengumon.log (the previous one is kept as pengumon.log.1 once it passes 5 MB),
    or nowhere if that file can't be opened."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        folder = paths.ensure_data_dir()
        log = folder / LOG_FILE
        if log.exists() and log.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log, folder / (LOG_FILE + ".1"))
        stream = open(log, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, stream)


def cmd_run(args) -> int:
    _quiet_streams()
    # environment variables rank above the config file (pengumon.paths), so the options are set as such
    if getattr(args, "host", None):
        os.environ["PENGUMON_HOST"] = args.host
    if getattr(args, "port", None):
        os.environ["PENGUMON_PORT"] = str(args.port)
    from pengumon import main as app_main

    app_main.main()
    return 0


def _copy_private_file(source: Path, target: Path) -> None:
    """Copies a regular file without following a symbolic link (a link in the data directory could point at any file
    this account can read). The source is opened with O_NOFOLLOW where the OS has it; a link is refused everywhere."""
    if source.is_symlink():
        raise OSError(f"{source} is a symbolic link; refusing to back up what it points to")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) == getattr(errno, "ELOOP", None):
            raise OSError(f"{source} is a symbolic link; refusing to back up what it points to") from exc
        raise
    with os.fdopen(fd, "rb") as src, open(target, "xb") as dst:
        shutil.copyfileobj(src, dst)


def _backup(to: Optional[str]) -> Path:
    server, database, _ = _file_settings()
    db = Path(paths.db_path(database.db_path))
    if not db.is_file():
        raise SystemExit(f"pengumon backup: no database at {db} (nothing to back up)")
    parent = Path(to).expanduser() if to else paths.backups_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = parent / stamp
    if folder.exists():
        raise SystemExit(f"pengumon backup: {folder} already exists; try again in a second")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700)
    try:
        # Read-only connection + VACUUM INTO: a consistent snapshot even while the app writes (WAL), never a lock
        # on the live file beyond a read transaction.
        con = sqlite3.connect(f"file:{quote(str(db.resolve()))}?mode=ro", uri=True, timeout=30)
        try:
            con.execute("VACUUM INTO ?", (str(folder / "pengumon.db"),))
        finally:
            con.close()
        os.chmod(folder / "pengumon.db", 0o600)
        for source in (paths.secrets_path(), paths.sessions_path()):
            if source.is_symlink() or source.is_file():
                target = folder / source.name
                _copy_private_file(source, target)
                os.chmod(target, 0o600)
    except (sqlite3.Error, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise SystemExit(f"pengumon backup failed: {exc}") from exc
    return folder


def cmd_backup(args) -> int:
    print(_backup(args.to))
    return 0


def _doctor_probes(backend):
    """(loopback result, TTL=1 result, ::1 result) from one event loop; a send failure comes back as its OSError."""
    import asyncio

    from pengumon.engine.netinfo import GATEWAY_PROBE_ADDRESS

    async def both():
        out = []
        for address, ttl in (("127.0.0.1", 64), (GATEWAY_PROBE_ADDRESS, 1), ("::1", 64)):
            try:
                out.append(await backend.probe(address, timeout=1.0, ttl=ttl))
            except OSError as exc:
                out.append(exc)
        return out

    return asyncio.run(both())


def _describe(result) -> str:
    if isinstance(result, OSError):
        return f"send failed: {result}"
    if result.kind == "timeout":
        return "no answer within 1 s"
    words = {"reply": "echo reply", "ttl_exceeded": "time exceeded", "unreachable": "unreachable"}[result.kind]
    rtt = f" in {result.rtt_ms:.3f} ms" if result.rtt_ms is not None else ""
    return f"{words} from {result.responder or 'an unknown address'}{rtt}"


def _ipv6_rows(backend, loopback6) -> List[tuple]:
    """What doctor says about IPv6: a route to the internet or why not, the IPv6 backend, the ::1 echo, the default
    router (Linux)."""
    from pengumon.engine.netinfo import IPV6_CHECK_ADDRESS, get_ipv6_default_router, ipv6_route_check

    no_route = ipv6_route_check()
    rows = [("IPv6", f"no route to the internet ({no_route}): IPv6 targets show \"IPv6 unavailable on this host\""
             if no_route else f"available (a route to {IPV6_CHECK_ADDRESS})")]
    name = getattr(backend, "ipv6_name", None) or (backend.name if not isinstance(loopback6, OSError) else None)
    rows.append(("IPv6 backend", name or "none"))
    rows.append(("ping ::1", _describe(loopback6)))
    router = get_ipv6_default_router()
    rows.append(("IPv6 router", router or "none (no IPv6 default route, or no /proc/net/ipv6_route on this OS)"))
    return rows


def cmd_doctor(_args) -> int:
    from pengumon.engine.icmp_base import IcmpUnavailable
    from pengumon.engine.icmp_select import describe_resolution, open_backend
    from pengumon.engine.netinfo import PROC_NET_ROUTE, is_rfc1918, parse_default_gateway

    rows = []
    ok = False
    fix = None
    try:
        backend = open_backend()
    except IcmpUnavailable as exc:
        backend = None
        rows.append(("ICMP backend", f"none: {exc.reason}"))
        fix = exc.fix
    if backend is not None:
        try:
            loopback, first_hop, loopback6 = _doctor_probes(backend)
        finally:
            backend.close()
        rows.append(("ICMP backend", f"{backend.name} ({describe_resolution(backend.resolution_ms)})"))
        ok = not isinstance(loopback, OSError) and loopback.kind == "reply"
        rows.append(("ping 127.0.0.1", _describe(loopback)))
        ttl_note = ""
        if not isinstance(first_hop, OSError) and first_hop.kind == "timeout":
            ttl_note = " (TTL probes need a router that answers; some networks and cloud runners drop them)"
        rows.append(("TTL=1 probe", _describe(first_hop) + ttl_note))
        try:
            from_routes = parse_default_gateway(PROC_NET_ROUTE.read_text(encoding="ascii", errors="replace"))
            gateway = f"{from_routes or 'none'} (from {PROC_NET_ROUTE})"
        except OSError:  # no /proc/net/route (macOS, Windows): what the app uses there, the TTL=1 answer
            responder = None
            if not isinstance(first_hop, OSError) and first_hop.kind == "ttl_exceeded" and is_rfc1918(first_hop.responder):
                responder = first_hop.responder
            gateway = f"{responder} (the first router, from a TTL=1 probe)" if responder else "unknown"
        rows.append(("gateway", gateway))
        rows += _ipv6_rows(backend, loopback6)
    server, _database, config = _file_settings()
    host, port, _host_from, _port_from = paths.listen_address(server)
    rows += [
        ("config file", f"{config}{'' if config.exists() else '  (missing: defaults are used)'}"),
        ("data directory", str(paths.data_dir())),
        ("listen address", f"{host}:{port}"),
    ]
    width = max(len(r[0]) for r in rows)
    for name, value in rows:
        print(f"{name:<{width}}  {value}")
    if fix:
        print(f"{'fix':<{width}}  {fix}")
    print("probing works" if ok else "probing does not work", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pengumon", description="Home network monitor. No command: run the server.")
    sub = parser.add_subparsers(dest="command", metavar="{run,version,paths,backup,doctor,service}")
    run = sub.add_parser("run", help="start the server (the default)")
    run.add_argument("--host", help="listen address (same as PENGUMON_HOST)")
    run.add_argument("--port", type=int, help="listen port (same as PENGUMON_PORT)")
    run.set_defaults(func=cmd_run)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    sub.add_parser("paths", help="show where the config, data and database are, and the listen address"
                   ).set_defaults(func=cmd_paths)
    backup = sub.add_parser("backup", help="copy the database, secrets and sessions into a timestamped folder")
    backup.add_argument("--to", metavar="DIR", help="parent folder for the timestamped backup (default: <data>/backups)")
    backup.set_defaults(func=cmd_backup)
    sub.add_parser("doctor", help="check that this host can probe (ICMP backend, loopback, first router) and how to fix it"
                   ).set_defaults(func=cmd_doctor)
    service.add_parser(sub)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    func = getattr(args, "func", cmd_run)
    try:
        return func(args)
    except service.ServiceError as exc:
        print(f"pengumon: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # a bad config file or PENGUMON_PORT: one line, not a traceback
        print(f"pengumon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
