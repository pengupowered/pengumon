"""`pengumon settings check-import`: a dry run of the one-time import, for the deploy (design 9.3 / 9.4).

    pengumon settings check-import --config C --db D [--secrets S]   the import, run on copies of D and S
    pengumon settings check-import --config C --db D [--secrets S] --live
                                                                      an imported database, read on a copy

The database and secrets.json are copied into a private temporary folder first (the database read-only, with
SQLite's backup API, so the original is never written, its WAL included) and the copies are deleted at the end. The
copy is migrated and imported exactly as the app does at its first start (without --live), then the effective
settings are compared with what the build before the settings store ran on (`load_config`: the file as it is).

The output names keys, lists and counts only: `same`, `differs`, `skipped (<reason>)` per key the file sets, the
item count and `same` / `differs` per item of each list, the notes, what the import wrote to the copy of secrets.json
(entry counts), and the migration's time. Never a value, so it is safe to paste. Exit code 0 when everything came
across unchanged, 1 when a key or item differs or was skipped, 2 when the check could not run.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from pengumon.config import AppConfig, load_config, read_config_file
from pengumon.notify.channels import resolve_headers
from pengumon.secrets import SecretStore, resolve_slot
from pengumon.settings import lenient, registry
from pengumon.settings.dotted import present
from pengumon.settings.importer import RECOVERY_KEY, get_path, jsonable


def _copy_db(source: Path, target: Path) -> None:
    """A consistent copy of the database through a read-only connection: the source (and its WAL) is not written.
    Without a WAL next to it (a backup, a stopped app) the file is opened as immutable, so not even the -shm / -wal
    side files are created in its folder; with one (a running app's database, --live) it is read with its WAL."""
    mode = "mode=ro" if source.with_name(source.name + "-wal").exists() else "mode=ro&immutable=1"
    src = sqlite3.connect(f"file:{quote(str(source.resolve()))}?{mode}", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)


def _entries(path: Path) -> Dict[str, int]:
    """{top-level key: number of entries} of a secrets.json (names and counts only)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: (len(v) if isinstance(v, dict) else 1) for k, v in data.items()}


def _resolved_headers(headers: Dict[str, str], store: SecretStore) -> Dict[str, Optional[str]]:
    """A webhook's headers with each `secret:<name>` replaced by that secret (to compare, never to print)."""
    return resolve_headers(headers, lambda name: resolve_slot(f"webhook_header.{name}", store, env={},
                                                              secrets_dir=None)[0])


def _hooks(config: AppConfig, store: SecretStore) -> List[dict]:
    return [{**hook.model_dump(mode="json"), "headers": _resolved_headers(hook.headers, store)}
            for hook in config.notifications.webhooks]


def _old_config(config_path: Path, out: Callable[[str], None]) -> Tuple[AppConfig, List[str]]:
    """The config the build before the settings store ran on: the whole file validated (with this build's ranges;
    a file they refuse is read leniently, and the refused keys show as skipped anyway)."""
    try:
        config, refused = load_config(str(config_path)), []
    except ValueError:
        config, dropped = lenient.validate_leniently(
            AppConfig, {k: v for k, v in read_config_file(config_path).items() if k != "targets"})
        out(f"note: this build's ranges refuse {len(dropped)} part(s) of the file as a whole; compared leniently")
        refused = lenient.removed_paths(dropped)
    if not present(read_config_file(config_path), RECOVERY_KEY):
        # the earlier builds confirmed recovery after the onset count (there was no recovery setting)
        config.classifier.consecutive_successes_for_recovery = config.classifier.consecutive_failures_for_incident
    return config, refused


def _compare_values(raw: dict, file_keys: List[str], old: AppConfig, new: AppConfig, report: Dict[str, str],
                    out: Callable[[str], None]) -> int:
    """Each setting the file sets, then the others, old build against new; returns the number of problems."""
    problems = 0
    out(f"values: {len(file_keys)} set in config.yaml")
    for key in file_keys:
        if key in report:
            out(f"  {key}: skipped ({report[key]})")
            problems += 1
        elif jsonable(get_path(old, key)) == jsonable(get_path(new, key)):
            out(f"  {key}: same")
        else:
            out(f"  {key}: differs")
            problems += 1
    other = [s.key for s in registry.SETTINGS if s.key not in file_keys
             and jsonable(get_path(old, s.key)) != jsonable(get_path(new, s.key))]
    out(f"  other keys ({len(registry.SETTINGS) - len(file_keys)}, at their defaults): "
        + (f"differ: {', '.join(other)}" if other else "same"))
    return problems + len(other)


def _compare_lists(raw: dict, old: AppConfig, new: AppConfig, store: SecretStore, out: Callable[[str], None]) -> int:
    """Each list item by item, old build against new; returns the number of problems."""
    problems = 0
    for name, spec in registry.LISTS.items():
        if name == "webhooks":
            before_items, after_items = _hooks(old, store), _hooks(new, store)
        else:
            before_items = jsonable(list(get_path(old, spec.path)))
            after_items = jsonable(list(get_path(new, spec.path)))
        in_file = present(raw, spec.path)
        state = "set in config.yaml" if in_file else "default"
        out(f"list {name} ({state}): {len(before_items)} item(s) before, {len(after_items)} after")
        if len(before_items) != len(after_items):
            problems += 1
        for i in range(max(len(before_items), len(after_items))):
            same = i < len(before_items) and i < len(after_items) and before_items[i] == after_items[i]
            out(f"  [{i}]: {'same' if same else 'differs'}")
            problems += 0 if same else 1
    return problems


async def _run(config_path: Path, db_copy: Path, secrets_copy: Path, live: bool, out: Callable[[str], None]) -> int:
    from pengumon.db.migrations import migrate
    from pengumon.db.repository import DatabaseRepository
    from pengumon.settings.manager import SettingsManager

    probe = sqlite3.connect(str(db_copy))
    try:
        before = probe.execute("PRAGMA user_version").fetchone()[0]
    finally:
        probe.close()
    started = time.monotonic()
    version = await migrate(str(db_copy))
    out(f"migration: schema v{before} -> v{version} on the copy in {time.monotonic() - started:.1f} s")
    raw = read_config_file(config_path)
    repository = DatabaseRepository(db_path=str(db_copy))
    try:
        if live and not await repository.settings_initialized():
            out("the database has no imported settings yet (nothing to check with --live)")
            return 2
        secrets_before = _entries(secrets_copy)
        store = SecretStore(secrets_copy)
        live_config = load_config(str(config_path), lenient_rest=True)
        manager = SettingsManager(live_config, repository, store, file_raw=raw, environ={}, secrets_dir=None)
        await manager.load()
        old, refused = _old_config(config_path, out)
        new = manager.config
        report = {r.get("key"): r.get("reason") for r in manager.import_report()}

        file_keys = [s.key for s in registry.SETTINGS if present(raw, s.key)]
        problems = _compare_values(raw, file_keys, old, new, report, out)
        problems += _compare_lists(raw, old, new, store, out)
        for key, why in report.items():
            if key not in file_keys:
                out(f"skipped: {key} ({why})")
                problems += 1
        if refused:
            out(f"refused by this build when the file is read as a whole: {', '.join(refused)}")
        for note in _notes(raw):
            out(f"note: {note}")
        secrets_after = _entries(secrets_copy)
        if secrets_after == secrets_before:
            out("secrets.json (copy): unchanged")
        else:
            changed = {k: secrets_after.get(k, 0) - secrets_before.get(k, 0) for k in sorted(secrets_after)}
            out("secrets.json (copy): " + ", ".join(f"{k} {n:+d} entries" for k, n in changed.items() if n))
        out(f"import report: {len(manager.import_report())} entr{'y' if len(manager.import_report()) == 1 else 'ies'}")
        if live:
            out(f"store at revision {manager.revision}")
        else:
            out(f"version 1: {(await repository.settings_versions())[-1]['summary']}")
        out("result: " + ("every value came across unchanged" if not problems else f"{problems} problem(s)"))
        return 0 if not problems else 1
    finally:
        await repository.close()


def _notes(raw: dict) -> List[str]:
    from pengumon.settings.importer import read_settings
    return read_settings(raw).notes


def check_import(config: str, db: str, secrets: Optional[str] = None, *, live: bool = False,
                 out: Callable[[str], None] = print) -> int:
    config_path, db_path = Path(config), Path(db)
    if not config_path.is_file():
        out(f"no config file at {config_path}")
        return 2
    if not db_path.is_file():
        out(f"no database at {db_path}")
        return 2
    folder = Path(tempfile.mkdtemp(prefix="pengumon-check-import-"))
    try:
        os.chmod(folder, stat.S_IRWXU)
        db_copy = folder / "pengumon.db"
        _copy_db(db_path, db_copy)
        secrets_copy = folder / "secrets.json"
        if secrets:
            shutil.copyfile(secrets, secrets_copy)
            os.chmod(secrets_copy, 0o600)
        return asyncio.run(_run(config_path, db_copy, secrets_copy, live, out))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
