"""`pengumon settings check-import` (settings/check.py): the one-time import as a dry run on copies, compared key by
key with the old load, printing names and counts only and never writing the originals."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from pengumon import cli
from pengumon.db.migrations import LATEST_VERSION
from pengumon.db.repository import DatabaseRepository

from settings_helpers import OWNER_SHAPED, WEBHOOKS, make_manager, write_config

PLANTED = ("Example ISP", "192.168.88.2", "path-token-abc", "inline-secret-xyz", "Example-Mesh", "app.example.com",
           "ha-secret-1")


async def _backup(tmp_path: Path) -> tuple:
    """A database as the build before the settings store left it (never initialized) and a secrets.json."""
    db = tmp_path / "backup" / "pengumon.db"
    db.parent.mkdir()
    repo = DatabaseRepository(db_path=str(db))
    await repo.initialize()
    await repo.close()
    secrets = tmp_path / "backup" / "secrets.json"
    secrets.write_text(json.dumps({"devices": {"router": {"password": "Dev-Pass-1"}},
                                   "webhooks": {"ha_token": "ha-secret-1"}}), encoding="utf-8")
    return db, secrets


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _run(capsys, *args) -> tuple:
    """The command in a thread, as on a console (it runs its own event loop)."""
    code = await asyncio.to_thread(cli.main, ["settings", "check-import", *args])
    return code, capsys.readouterr().out


async def test_a_clean_config_comes_across_unchanged_and_the_originals_are_untouched(tmp_path, capsys, monkeypatch):
    # The command makes its copies in a folder of its own (tempfile.mkdtemp), so two runs never share one; this test
    # looks for leftovers in a temp directory of its own too, not in the machine's, where a parallel worker's run
    # (or anything else) makes and removes folders of the same name pattern.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    made, real_mkdtemp = [], tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: made.append(real_mkdtemp(*a, **k)) or made[-1])
    db, secrets = await _backup(tmp_path)
    config = write_config(tmp_path, OWNER_SHAPED + WEBHOOKS)
    before = (_digest(db), _digest(secrets), sorted(p.name for p in db.parent.iterdir()))
    temp_before = set(scratch.glob("pengumon-check-import-*"))
    code, out = await _run(capsys, "--config", str(config), "--db", str(db), "--secrets", str(secrets))
    assert code == 0, out
    assert "result: every value came across unchanged" in out
    assert "classifier.busy_load_pct: same" in out and "list devices (set in config.yaml): 2 item(s)" in out
    assert "list webhooks (set in config.yaml): 1 item(s) before, 1 after" in out
    assert "note: prober.ping_count_per_probe: does nothing; dropped" in out
    assert "secrets.json (copy): webhook_urls +1 entries, webhooks +1 entries" in out
    assert f"migration: schema v{LATEST_VERSION} -> v{LATEST_VERSION}" in out and "import report: 0 entries" in out
    for value in PLANTED:
        assert value not in out, value
    assert (_digest(db), _digest(secrets), sorted(p.name for p in db.parent.iterdir())) == before
    assert set(scratch.glob("pengumon-check-import-*")) == temp_before == set()  # copies deleted
    assert len(made) == 1 and Path(made[0]).parent == scratch and not Path(made[0]).exists()  # it did make one there
    repo = DatabaseRepository(db_path=str(db))
    await repo.initialize()
    assert not await repo.settings_initialized()


async def test_a_value_out_of_the_new_ranges_is_reported_skipped_by_key(tmp_path, capsys):
    db, secrets = await _backup(tmp_path)
    config = write_config(tmp_path, OWNER_SHAPED.replace("busy_load_pct: 70", "busy_load_pct: 5"))
    code, out = await _run(capsys, "--config", str(config), "--db", str(db), "--secrets", str(secrets))
    assert code == 1
    assert "classifier.busy_load_pct: skipped (" in out and "import report: 1 entry" in out


async def test_an_onset_count_copied_into_recovery_counts_as_the_same_behavior(tmp_path, capsys):
    db, secrets = await _backup(tmp_path)
    config = write_config(tmp_path, OWNER_SHAPED.replace("consecutive_failures_for_incident: 3",
                                                         "consecutive_failures_for_incident: 5"))
    code, out = await _run(capsys, "--config", str(config), "--db", str(db), "--secrets", str(secrets))
    assert code == 0, out
    assert "note: classifier.consecutive_successes_for_recovery: set to" in out


async def test_live_reads_an_imported_database_and_refuses_one_that_is_not(tmp_path, capsys):
    db, secrets = await _backup(tmp_path)
    config = write_config(tmp_path, OWNER_SHAPED)
    code, out = await _run(capsys, "--config", str(config), "--db", str(db), "--live")
    assert code == 2 and "no imported settings yet" in out
    manager, _ = await make_manager(tmp_path / "backup", OWNER_SHAPED)  # the app's first start on that database
    await manager.repository.close()
    digest = _digest(db)
    code, out = await _run(capsys, "--config", str(config), "--db", str(db), "--live")
    assert code == 0, out
    assert "store at revision 1" in out and _digest(db) == digest


async def test_missing_files_exit_2(tmp_path, capsys):
    code, out = await _run(capsys, "--config", str(tmp_path / "none.yaml"), "--db", str(tmp_path / "none.db"))
    assert code == 2 and "no config file" in out
