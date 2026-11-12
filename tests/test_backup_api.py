"""GET /api/backup (api/backup.py): a consistent copy of the database as a download, for someone who may edit only,
rate limited and one at a time, made in the data directory's private temp folder and deleted when the response ends."""

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from pengumon import __version__, security
from pengumon.api import backup
from pengumon.db import snapshot
from pengumon.main import app, security_config
from pengumon.state import AppState

TOKEN = "example-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
READ_TOKEN = "pgr_made-up-read-token-for-backups"
READER = {"Authorization": f"Bearer {READ_TOKEN}"}


POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no permission bits and no owner ids to check")


def _symlink(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system does not let the test create a symlink ({exc})")  # Windows needs a privilege


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)
    monkeypatch.setattr(security_config, "read_token_hash", "")


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setenv("PENGUMON_DATA", str(folder))
    return folder


@pytest.fixture
async def served(app_state, repo, data):
    """The app with the test database `repo` (tmp_path/repo.db) and a committed marker row in it."""
    app_state.repository = repo
    con = sqlite3.connect(repo.db_path)
    con.execute("CREATE TABLE backup_marker (note TEXT)")
    con.execute("INSERT INTO backup_marker VALUES ('example-marker')")
    con.commit()
    con.close()
    (data / "secrets.json").write_text('{"webhook": "example-secret-value"}')
    (data / "sessions.json").write_text('{"everywhere": 1}')
    return repo


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _temp_files(data: Path):
    folder = data / backup.TEMP_FOLDER
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _set_up_tokens(monkeypatch, view_without_token=False):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "read_token_hash", security.read_token_digest(READ_TOKEN))
    monkeypatch.setattr(security_config, "view_without_token", view_without_token)


async def test_an_editor_downloads_a_consistent_copy_of_the_database_only(served, data, tmp_path, monkeypatch):
    counted = []
    monkeypatch.setattr(backup.db_backups, "add", lambda n, attrs=None: counted.append(attrs))
    async with _client() as ac:
        res = await ac.get("/api/backup", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/vnd.sqlite3"
    assert re.fullmatch(rf'attachment; filename="pengumon-{re.escape(__version__)}-\d{{8}}T\d{{6}}Z\.db"',
                        res.headers["content-disposition"])
    assert res.headers["content-encoding"] == "identity" and int(res.headers["content-length"]) == len(res.content)
    assert res.content.startswith(b"SQLite format 3\x00")  # not gzip: the middleware leaves it alone
    copy = tmp_path / "downloaded.db"
    copy.write_bytes(res.content)
    con = sqlite3.connect(copy)
    assert con.execute("SELECT note FROM backup_marker").fetchall() == [("example-marker",)]
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    con.close()
    assert b"example-secret-value" not in res.content and b"everywhere" not in res.content  # the database only
    assert counted == [{"outcome": "ok"}]
    assert _temp_files(data) == [] and not backup.backup_running.locked()  # deleted, and the next one may start


@pytest.mark.parametrize("leftovers", [False, True])
async def test_the_temp_folder_is_private_and_a_crashs_leftovers_are_removed(leftovers, served, data, monkeypatch):
    folder = data / backup.TEMP_FOLDER
    if leftovers:
        folder.mkdir(mode=0o700)
        (folder / "old-dir").mkdir()
        (folder / "old-dir" / "x").write_text("x")
        (folder / "backup-old.db").write_text("left by a crash")
    seen = []
    original = backup.snapshot_database

    def watching(source, target):
        seen.append(sorted(p.name for p in folder.iterdir()))
        if sys.platform != "win32":  # Windows reports every folder as 0o777
            seen.append(folder.stat().st_mode & 0o777)
        return original(source, target)

    monkeypatch.setattr(backup, "snapshot_database", watching)
    async with _client() as ac:
        assert (await ac.get("/api/backup")).status_code == 200
    assert seen == ([[]] if sys.platform == "win32" else [[], 0o700]) and _temp_files(data) == []


async def test_the_password_holder_downloads_a_viewer_and_the_read_token_are_refused(served, data, monkeypatch):
    """The middleware refuses them first (the path is in VIEWER_POLICY.deny); the handler's own check is tested below."""
    _set_up_tokens(monkeypatch, view_without_token=True)
    async with _client() as ac:
        viewer = await ac.get("/api/backup")
        reader = await ac.get("/api/backup", headers=READER)
        assert (await ac.get("/api/status", headers=READER)).status_code != 403  # the read token reads other things
        owner = await ac.get("/api/backup", headers=BEARER)
    assert viewer.status_code == 401 and viewer.headers.get("content-type", "").startswith("application/json")
    assert reader.status_code == 403 and "read token can only read" in reader.json()["detail"]
    assert b"SQLite format 3" not in viewer.content + reader.content
    assert owner.status_code == 200 and owner.content.startswith(b"SQLite format 3\x00")
    assert _temp_files(data) == []


async def test_without_viewing_a_visitor_with_no_token_is_refused_too(served, monkeypatch):
    _set_up_tokens(monkeypatch)
    async with _client() as ac:
        assert (await ac.get("/api/backup")).status_code == 401


def test_the_viewer_policy_denies_the_backup_to_a_viewer_and_to_the_read_token():
    policy = security.VIEWER_POLICY
    assert not policy.permits("GET", "/api/backup") and not policy.permits_reader("GET", "/api/backup")
    assert policy.permits("GET", "/api/settings") and policy.permits_reader("GET", "/api/settings")


@pytest.mark.parametrize("auth", ["viewer", "reader", "public", None])
async def test_the_handler_itself_refuses_whoever_may_not_edit(auth, served, data):
    """Not relying on the middleware's deny: a request that got through as a viewer or a reader is refused here."""
    scope = {"type": "http", "method": "GET", "path": "/api/backup", "headers": [], "app": app,
             "homeguard": {"auth": auth} if auth else {}}
    with pytest.raises(backup.HTTPException) as refused:
        await backup.get_backup(Request(scope), _state(served))
    assert refused.value.status_code == 403
    assert _temp_files(data) == [] and not backup.backup_running.locked()


def _state(repo):
    return AppState(repository=repo)


@pytest.mark.parametrize("auth", ["open", "bearer", "cookie"])
async def test_the_handler_serves_whoever_may_edit(auth, served, data):
    scope = {"type": "http", "method": "GET", "path": "/api/backup", "headers": [], "app": app, "homeguard": {"auth": auth}}
    response = await backup.get_backup(Request(scope), _state(served))
    assert isinstance(response, backup.TempFileResponse)
    response.cleanup()  # nobody is going to send it
    assert _temp_files(data) == [] and not backup.backup_running.locked()


async def test_two_backups_in_ten_minutes_then_429(served):
    async with _client() as ac:
        codes = [(await ac.get("/api/backup")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_a_second_backup_while_one_is_being_made_gets_429_and_leaves_the_first_alone(served, data, monkeypatch):
    refused = []
    monkeypatch.setattr(backup.api_rate_limited, "add", lambda n, attrs=None: refused.append(attrs))
    await backup.backup_running.acquire()  # the one being made
    try:
        async with _client() as ac:
            res = await ac.get("/api/backup")
        assert res.status_code == 429 and res.headers["retry-after"] == "60" and "being made" in res.json()["detail"]
        assert refused == [{"bucket": "backup_busy"}]
        assert backup.backup_running.locked() and _temp_files(data) == []
    finally:
        backup.backup_running.release()


async def test_a_full_disk_is_507_and_leaves_nothing(served, data, monkeypatch):
    counted = []
    monkeypatch.setattr(backup.db_backups, "add", lambda n, attrs=None: counted.append(attrs))
    free = Path(served.db_path).stat().st_size - 1
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: type("U", (), {"free": free})())
    async with _client() as ac:
        res = await ac.get("/api/backup")
    assert res.status_code == 507 and "Not enough free disk space" in res.json()["detail"]
    assert counted == [{"outcome": "no_space"}]
    assert _temp_files(data) == [] and not backup.backup_running.locked()


async def test_a_failed_copy_is_500_without_the_reason_and_leaves_nothing(served, data, monkeypatch):
    def failing(source, target):
        Path(target).write_bytes(b"half a copy")
        raise sqlite3.OperationalError("disk I/O error at /private/path")

    monkeypatch.setattr(backup, "snapshot_database", failing)
    async with _client() as ac:
        res = await ac.get("/api/backup")
        again = await ac.get("/api/backup")  # the lock was released
    assert res.status_code == 500 and "/private/path" not in res.text
    assert again.status_code == 500
    assert _temp_files(data) == [] and not backup.backup_running.locked()


async def test_no_database_file_is_404(app_state, data):
    from types import SimpleNamespace

    # a repository that points at a file that is not there (the real one keeps its file open, and Windows cannot rename that)
    app_state.repository = SimpleNamespace(db_path=str(data / "not-there.db"))
    async with _client() as ac:
        res = await ac.get("/api/backup")
    assert res.status_code == 404 and not backup.backup_running.locked()


async def test_the_copy_runs_in_a_worker_thread_not_on_the_event_loop(served, monkeypatch):
    import threading
    where = []
    original = backup.snapshot_database

    def watching(source, target):
        where.append(threading.current_thread())
        return original(source, target)

    monkeypatch.setattr(backup, "snapshot_database", watching)
    async with _client() as ac:
        assert (await ac.get("/api/backup")).status_code == 200
    assert where and where[0] is not threading.main_thread()


# ---- the response: deleted however it ends ----

def _file(tmp_path, size=3 * backup.CHUNK + 5):
    path = tmp_path / "copy.db"
    path.write_bytes(b"x" * size)
    return path


def _scope(spec=None):
    scope = {"type": "http", "method": "GET", "headers": []}
    if spec:
        scope["asgi"] = {"spec_version": spec}
    return scope


async def test_the_response_sends_the_file_in_chunks_then_deletes_it_and_lets_the_next_one_start(tmp_path):
    path, done, sent = _file(tmp_path), [], []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    response = backup.TempFileResponse(path, headers={}, done=lambda: done.append(1))
    await response(_scope(), receive, send)
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body" and m["body"]]
    assert [len(b) for b in bodies] == [backup.CHUNK] * 3 + [5]
    assert not path.exists() and done == [1]


async def test_the_response_deletes_the_file_when_the_client_goes_away(tmp_path):
    """Both ways Starlette notices it: a disconnect message (older ASGI servers) and a failed send (spec 2.4)."""
    path, done = _file(tmp_path), []
    stall = asyncio.Event()

    async def gone():
        return {"type": "http.disconnect"}

    async def slow_send(message):
        if message["type"] == "http.response.body":
            await stall.wait()  # the socket is not draining

    response = backup.TempFileResponse(path, headers={}, done=lambda: done.append(1))
    await asyncio.wait_for(response(_scope(), gone, slow_send), 5)
    assert not path.exists() and done == [1]

    path, done = _file(tmp_path), []
    calls = []

    async def broken_send(message):
        calls.append(message["type"])
        if message["type"] == "http.response.body":
            raise OSError("connection reset")

    async def never():
        await asyncio.sleep(3600)

    response = backup.TempFileResponse(path, headers={}, done=lambda: done.append(1))
    with pytest.raises(Exception):
        await response(_scope("2.4"), never, broken_send)
    assert not path.exists() and done == [1] and calls.count("http.response.body") == 1


async def test_the_response_deletes_the_file_when_the_send_is_cancelled_or_fails_before_the_body(tmp_path):
    path, done = _file(tmp_path), []

    async def refusing(message):
        raise RuntimeError("the server stopped")

    async def never():
        await asyncio.sleep(3600)

    response = backup.TempFileResponse(path, headers={}, done=lambda: done.append(1))
    with pytest.raises(RuntimeError):
        await response(_scope("2.4"), never, refusing)
    assert not path.exists() and done == [1]
    response.cleanup()  # a second call does not release twice
    assert done == [1]


# ---- the shared snapshot ----

def test_the_snapshot_is_private_and_consistent_while_the_source_is_open_in_wal_mode(tmp_path):
    source = tmp_path / "live.db"
    live = sqlite3.connect(source)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t (n INTEGER)")
    live.execute("INSERT INTO t VALUES (1)")
    live.commit()
    live.execute("INSERT INTO t VALUES (2)")  # not committed: not in the copy
    target = tmp_path / "copy.db"
    snapshot.snapshot_database(source, target)
    if sys.platform != "win32":  # Windows has no permission bits
        assert (target.stat().st_mode & 0o777) == 0o600
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT n FROM t").fetchall() == [(1,)]
    copy.close()
    live.close()


async def test_the_storage_component_empties_a_crashs_leftovers_when_it_starts(data):
    """A crash in the middle of a download left a copy of the database in the temp folder; it used to stay until
    someone asked for another backup."""
    from types import SimpleNamespace

    from pengumon.db import storage

    class Repo:
        incidents_version = 0

        async def initialize(self):
            pass

        async def close(self):
            pass

    folder = data / backup.TEMP_FOLDER
    folder.mkdir(mode=0o700)
    (folder / "old-dir").mkdir()
    (folder / "old-dir" / "x").write_text("x")
    (folder / "backup-old.db").write_text("left by a crash")
    component = await storage.register(SimpleNamespace(state=SimpleNamespace(repository=Repo())))
    assert component.name == "storage"
    assert _temp_files(data) == [] and folder.is_dir()  # emptied, the folder itself stays


async def test_the_storage_component_starts_without_a_temp_folder(data):
    from types import SimpleNamespace

    from pengumon.db import storage

    class Repo:
        incidents_version = 0

        async def initialize(self):
            pass

        async def close(self):
            pass

    await storage.register(SimpleNamespace(state=SimpleNamespace(repository=Repo())))
    assert not (data / backup.TEMP_FOLDER).exists()  # starting creates nothing


def _a_target_folder_with_a_file(tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("precious")
    (target / "sub").mkdir()
    (target / "sub" / "keep2.txt").write_text("precious")
    return target


async def test_a_symlinked_temp_folder_is_refused_and_its_target_is_left_alone(served, data, tmp_path):
    target = _a_target_folder_with_a_file(tmp_path)
    _symlink(data / backup.TEMP_FOLDER, target)
    async with _client() as ac:
        res = await ac.get("/api/backup")
    assert res.status_code == 500 and not backup.backup_running.locked()
    assert (target / "keep.txt").read_text() == "precious" and (target / "sub" / "keep2.txt").exists()


async def test_the_storage_component_does_not_follow_a_symlinked_temp_folder(data, tmp_path):
    from types import SimpleNamespace

    from pengumon.db import storage

    class Repo:
        incidents_version = 0

        async def initialize(self):
            pass

        async def close(self):
            pass

    target = _a_target_folder_with_a_file(tmp_path)
    _symlink(data / backup.TEMP_FOLDER, target)
    await storage.register(SimpleNamespace(state=SimpleNamespace(repository=Repo())))  # logs, does not stop the start
    assert (target / "keep.txt").exists() and (target / "sub" / "keep2.txt").exists()


def test_leftover_links_inside_the_temp_folder_are_removed_as_links(data, tmp_path):
    from pengumon.db.backup_temp import remove_backup_leftovers

    target = _a_target_folder_with_a_file(tmp_path)
    folder = data / backup.TEMP_FOLDER
    folder.mkdir(mode=0o700)
    _symlink(folder / "link", target)
    remove_backup_leftovers(folder)
    assert list(folder.iterdir()) == [] and (target / "keep.txt").exists()


def test_a_temp_path_that_is_a_file_is_refused(data):
    from pengumon.db.backup_temp import remove_backup_leftovers

    path = data / backup.TEMP_FOLDER
    path.write_text("not a folder")
    with pytest.raises(OSError):
        remove_backup_leftovers(path)


@POSIX_ONLY
def test_an_existing_temp_folder_with_a_wide_mode_is_set_to_0700(data):
    from pengumon.db.backup_temp import remove_backup_leftovers

    folder = data / backup.TEMP_FOLDER
    folder.mkdir()
    folder.chmod(0o755)
    remove_backup_leftovers(folder)
    assert folder.stat().st_mode & 0o777 == 0o700


@POSIX_ONLY
def test_a_temp_folder_owned_by_someone_else_is_refused(data, monkeypatch):
    import os

    from pengumon.db.backup_temp import remove_backup_leftovers

    folder = data / backup.TEMP_FOLDER
    folder.mkdir(mode=0o700)
    (folder / "x").write_text("x")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(folder).st_uid + 1)
    with pytest.raises(PermissionError):
        remove_backup_leftovers(folder)
    assert (folder / "x").exists()


# ---- the Windows branches, run here by telling the module it is on Windows ----

@pytest.fixture
def as_windows(monkeypatch):
    """The Windows paths of db/backup_temp.py and api/backup.py, executed on this system. What they call (lstat,
    scandir, unlink, rmdir, rmtree) exists everywhere; only the POSIX-only calls are made to fail loudly."""
    from pengumon.db import backup_temp

    monkeypatch.setattr(backup_temp, "_WINDOWS", True)
    monkeypatch.setattr(backup, "_WINDOWS", True)
    for name in ("geteuid", "fchmod"):  # what the POSIX branch uses and Windows does not have
        if hasattr(backup_temp.os, name):
            monkeypatch.setattr(backup_temp.os, name, lambda *a, **k: pytest.fail(f"os.{name} used on Windows"))
    monkeypatch.delattr(backup_temp.os, "O_DIRECTORY", raising=False)
    monkeypatch.delattr(backup_temp.os, "O_NOFOLLOW", raising=False)


def test_windows_empties_the_temp_folder_by_path_and_leaves_its_mode_alone(data, tmp_path, as_windows):
    from pengumon.db.backup_temp import remove_backup_leftovers

    folder = data / backup.TEMP_FOLDER
    folder.mkdir()
    folder.chmod(0o755)
    mode_before = folder.stat().st_mode & 0o777  # 0755 where there are modes; whatever Windows reports there
    (folder / "old-dir").mkdir()
    (folder / "old-dir" / "x").write_text("x")
    (folder / "backup-old.db").write_text("left by a crash")
    target = _a_target_folder_with_a_file(tmp_path)
    _symlink(folder / "link", target)
    (folder / "file-link").symlink_to(target / "keep.txt")
    remove_backup_leftovers(folder)
    assert list(folder.iterdir()) == [] and (target / "keep.txt").exists() and (target / "sub" / "keep2.txt").exists()
    assert folder.stat().st_mode & 0o777 == mode_before  # no mode is set where the system has none


def test_windows_refuses_a_symlinked_folder_and_a_file(data, tmp_path, as_windows):
    from pengumon.db.backup_temp import remove_backup_leftovers

    target = _a_target_folder_with_a_file(tmp_path)
    link = data / backup.TEMP_FOLDER
    _symlink(link, target)
    with pytest.raises(NotADirectoryError):
        remove_backup_leftovers(link)
    assert (target / "keep.txt").exists() and (target / "sub" / "keep2.txt").exists()
    link.unlink()
    link.write_text("not a folder")
    with pytest.raises(OSError):
        remove_backup_leftovers(link)


def test_windows_treats_a_reparse_point_like_a_link(data, tmp_path, as_windows, monkeypatch):
    """A junction is not a symlink to lstat, but carries the reparse-point attribute; this system has no junctions,
    so the attribute is faked on the folder's lstat."""
    import os
    import stat as stat_module

    from pengumon.db import backup_temp

    folder = data / backup.TEMP_FOLDER
    folder.mkdir()
    real = os.lstat

    class Junction:
        def __init__(self, st):
            self.st_mode, self.st_file_attributes = st.st_mode, stat_module.FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(backup_temp.os, "lstat", lambda p: Junction(real(p)) if Path(p) == folder else real(p))
    with pytest.raises(NotADirectoryError):
        backup_temp.remove_backup_leftovers(folder)


async def test_windows_download_works_end_to_end(served, data, as_windows):
    async with _client() as ac:
        res = await ac.get("/api/backup")
    assert res.status_code == 200 and res.content.startswith(b"SQLite format 3\x00")
    assert _temp_files(data) == [] and not backup.backup_running.locked()


async def test_no_file_handle_stays_open_between_chunks(tmp_path, monkeypatch):
    """Windows cannot delete a file that is open: the copy must be closed whenever the stream is waiting."""
    path, open_now, seen = _file(tmp_path), [], []
    real_open = open

    class Tracked:
        def __init__(self, handle):
            self.handle = handle
            open_now.append(self)

        def __enter__(self):
            return self.handle

        def __exit__(self, *exc):
            self.handle.close()
            open_now.remove(self)

    monkeypatch.setattr(backup, "open", lambda *a, **k: Tracked(real_open(*a, **k)), raising=False)

    async def send(message):
        if message["type"] == "http.response.body":
            seen.append(len(open_now))

    async def never():
        await asyncio.sleep(3600)

    await backup.TempFileResponse(path, headers={}, done=lambda: None)(_scope(), never, send)
    assert seen and set(seen) == {0} and not path.exists()


def test_windows_retries_the_delete_while_a_worker_read_still_holds_the_file(tmp_path, as_windows, monkeypatch):
    path, done, calls = _file(tmp_path), [], []
    real_unlink = Path.unlink

    def refusing_twice(self, missing_ok=False):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_twice)
    monkeypatch.setattr(backup.time, "sleep", lambda s: None)
    backup.TempFileResponse(path, headers={}, done=lambda: done.append(1)).cleanup()
    assert len(calls) == 3 and not path.exists() and done == [1]


def test_a_delete_that_keeps_failing_is_logged_and_still_lets_the_next_backup_start(tmp_path, as_windows, monkeypatch):
    path, done = _file(tmp_path), []
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError(32, "in use")))
    monkeypatch.setattr(backup.time, "sleep", lambda s: None)
    backup.TempFileResponse(path, headers={}, done=lambda: done.append(1)).cleanup()
    assert done == [1]


def test_the_read_only_uri_is_the_documented_windows_form_there(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_WINDOWS", True)
    uri = snapshot._read_only_uri(tmp_path / "a b.db")
    assert uri.startswith("file:///") and uri.endswith("a%20b.db?mode=ro")
