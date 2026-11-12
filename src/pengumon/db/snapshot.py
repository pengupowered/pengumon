"""A consistent copy of the database file: `pengumon backup` (cli.py) and `GET /api/backup` (api/backup.py) both make
theirs here."""

import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


_WINDOWS = sys.platform == "win32"  # tests set it to check the Windows form here


def _read_only_uri(source: Path) -> str:
    path = Path(source).resolve()
    if _WINDOWS:
        return path.as_uri() + "?mode=ro"  # file:///C:/dir/file.db, the form SQLite documents for a Windows drive path
    return f"file:{quote(str(path))}?mode=ro"


def snapshot_database(source: Path, target: Path) -> None:
    """Writes a consistent copy of the database at `source` to `target` (a file that does not exist yet; mode 0600
    where the system has modes).
    It reads through a read-only connection with VACUUM INTO, so the copy is one point in time even while the app
    writes (WAL), and the live file is locked for no longer than a read transaction. Blocking: call it from a worker
    thread inside the app. Raises sqlite3.Error or OSError; a partly written target is left for the caller to remove."""
    con = sqlite3.connect(_read_only_uri(source), uri=True, timeout=30)
    try:
        con.execute("VACUUM INTO ?", (str(target),))
    finally:
        con.close()
    os.chmod(target, 0o600)
