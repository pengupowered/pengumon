"""GET /api/backup: a consistent copy of the database as a file download (Settings > About this install).

The database only, never secrets.json or the sessions file (`pengumon backup` copies those too). It holds the
household's whole history, so only someone who may edit gets it (a viewer and the read token are refused twice: the
path is in VIEWER_POLICY.deny, and the handler asks `can_edit`). Rate limited (2 per 10 minutes) and one at a time (429
while one is being made or sent).

The copy is made with VACUUM INTO on a read-only connection in a worker thread (db/snapshot.py) into a private
`.backup-tmp` folder in the data directory (the container's root is read-only and /tmp is memory), streamed in chunks
and deleted when the response ends, whether it was sent or the client went away. A leftover from a crash is removed
by the next request, and by the start of the storage component (db/storage.py, db/backup_temp.py). 507 when the folder's disk has less free space than the
database is large."""

import asyncio
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from pengumon import __version__
from pengumon.api.common import StateDep
from pengumon.db.snapshot import snapshot_database
from pengumon.db.backup_temp import BACKUP_TEMP_FOLDER, backup_temp_dir, remove_backup_leftovers
from pengumon.observability import get_logger
from pengumon.observability.telemetry import api_rate_limited, db_backup_duration, db_backup_size, db_backups
from pengumon.security import can_edit, rate_limit

router = APIRouter(prefix="/api")
_log = get_logger(__name__)

TEMP_FOLDER = BACKUP_TEMP_FOLDER
CHUNK = 1024 * 1024
MEDIA_TYPE = "application/vnd.sqlite3"
DELETE_TRIES = 6
_WINDOWS = sys.platform == "win32"  # tests set it to run the Windows branch on Linux

# Held from the start of the copy until the response has ended: the folder never holds more than one copy.
backup_running = asyncio.Lock()


def temp_dir() -> Path:
    return backup_temp_dir()


def _prepare_temp_dir() -> Path:
    """The private temp folder, created if need be and emptied of what an earlier crash left. Only called while
    `backup_running` is held."""
    folder = temp_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    remove_backup_leftovers(folder)
    return folder


def _read_chunk(file: Path, offset: int) -> bytes:
    with open(file, "rb") as handle:
        handle.seek(offset)
        return handle.read(CHUNK)


class TempFileResponse(StreamingResponse):
    """Streams `file` in chunks (read in a worker thread) and, however the response ends (sent, the client gone, an
    error), deletes the file and calls `done` (which lets the next backup start)."""

    def __init__(self, file: Path, *, headers, done=None):
        self.file, self._done = file, done
        super().__init__(self._chunks(), media_type=MEDIA_TYPE, headers=headers)

    async def _chunks(self):
        # Each chunk is read with the file opened and closed inside the worker call, so no handle stays open between
        # chunks: Windows cannot delete a file that is open, and a cancelled stream would leave one until it is collected.
        offset = 0
        while chunk := await asyncio.to_thread(_read_chunk, self.file, offset):
            offset += len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        try:
            self._delete()
        except OSError as exc:
            _log.warning("Could not delete a finished backup copy", file=str(self.file), error=str(exc))
        done, self._done = self._done, None
        if done is not None:
            done()

    def _delete(self) -> None:
        # On Windows a read still running in a worker thread (the stream was cancelled in the middle of one) holds the
        # file for a few milliseconds and the delete is refused (PermissionError); a few short retries cover it.
        for attempt in range(DELETE_TRIES if _WINDOWS else 1):
            try:
                self.file.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == DELETE_TRIES - 1 or not _WINDOWS:
                    raise
                time.sleep(0.05)


def _filename() -> str:
    return f"pengumon-{__version__}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"


@router.get("/backup", dependencies=[Depends(rate_limit("backup", 2, 600))])
async def get_backup(request: Request, state: StateDep):
    """A consistent copy of the database as `pengumon-<version>-<UTC stamp>.db` (`application/vnd.sqlite3`, an
    attachment). Only for someone who may edit (the middleware refuses a viewer with 401 and the read token with 403;
    this check is the second). 429 while another copy is being made or sent (and past 2 in 10 minutes), 507 when the
    disk has less free space than the database is large."""
    if not can_edit(request):
        raise HTTPException(status_code=403, detail="Sign in with the password to download a backup")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    db = Path(state.repository.db_path)
    if backup_running.locked():
        api_rate_limited.add(1, {"bucket": "backup_busy"})
        raise HTTPException(status_code=429, detail="Another backup is being made; try again in a minute",
                            headers={"Retry-After": "60"})
    await backup_running.acquire()
    response: Optional[TempFileResponse] = None
    try:
        try:
            folder = await asyncio.to_thread(_prepare_temp_dir)
        except OSError as exc:
            db_backups.add(1, {"outcome": "failed"})
            _log.warning("The backup temp folder is not usable", error=str(exc))
            raise HTTPException(status_code=500, detail="The backup temp folder is not usable; see the log") from None
        try:
            size = (await asyncio.to_thread(db.stat)).st_size
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="There is no database file to copy") from None
        free = (await asyncio.to_thread(shutil.disk_usage, folder)).free
        if free < size:
            db_backups.add(1, {"outcome": "no_space"})
            raise HTTPException(
                status_code=507,
                detail=f"Not enough free disk space for a backup: the database is {size // 1_000_000} MB and "
                       f"{free // 1_000_000} MB is free in {folder}")
        target = folder / f"backup-{secrets.token_hex(8)}.db"
        started = time.perf_counter()
        try:
            await asyncio.to_thread(snapshot_database, db, target)
            copied = (await asyncio.to_thread(target.stat)).st_size
        except Exception as exc:
            target.unlink(missing_ok=True)
            db_backups.add(1, {"outcome": "failed"})
            _log.warning("Backup copy failed", error=str(exc))
            raise HTTPException(status_code=500, detail="Could not make a copy of the database; see the log") from None
        db_backups.add(1, {"outcome": "ok"})
        db_backup_size.record(copied)
        db_backup_duration.record(time.perf_counter() - started)
        response = TempFileResponse(
            target, done=backup_running.release,
            headers={"Content-Disposition": f'attachment; filename="{_filename()}"', "Content-Length": str(copied),
                     "Content-Encoding": "identity",  # the gzip middleware must not compress hundreds of MB in the loop
                     "Cache-Control": "no-store"})
        return response
    finally:
        if response is None:
            backup_running.release()
