"""The `pengumon` command: run the server, show the version, show where files live, back the data up.

    pengumon [run]                 start the server (the default)
    pengumon version
    pengumon paths                 config file, data directory, database, secrets, listen address, and where each
                                   value came from (pengumon.paths has the rules)
    pengumon backup [--to DIR]     a consistent copy of the database (VACUUM INTO, safe while the app runs) plus
                                   secrets.json and sessions.json, in a timestamped folder (default: <data>/backups)

Standard library only apart from the app's own config models; the server is imported only by `run`.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from pengumon import __version__, paths
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


def cmd_run(_args) -> int:
    from pengumon import main as app_main

    app_main.main()
    return 0


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
            if source.is_file():
                target = folder / source.name
                shutil.copyfile(source, target)
                os.chmod(target, 0o600)
    except (sqlite3.Error, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise SystemExit(f"pengumon backup failed: {exc}") from exc
    return folder


def cmd_backup(args) -> int:
    print(_backup(args.to))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pengumon", description="Home network monitor. No command: run the server.")
    sub = parser.add_subparsers(dest="command", metavar="{run,version,paths,backup}")
    sub.add_parser("run", help="start the server (the default)").set_defaults(func=cmd_run)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    sub.add_parser("paths", help="show where the config, data and database are, and the listen address"
                   ).set_defaults(func=cmd_paths)
    backup = sub.add_parser("backup", help="copy the database, secrets and sessions into a timestamped folder")
    backup.add_argument("--to", metavar="DIR", help="parent folder for the timestamped backup (default: <data>/backups)")
    backup.set_defaults(func=cmd_backup)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    func = getattr(args, "func", cmd_run)
    try:
        return func(args)
    except ValueError as exc:  # a bad config file or PENGUMON_PORT: one line, not a traceback
        print(f"pengumon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
