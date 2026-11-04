"""The settings store: what the owner set on the Settings page (pengumon.settings), in three tables and four meta keys.

- `settings_values`: {registry key: JSON value} for the keys that are set; a missing key means the default.
- `settings_items`: the items of the four lists, one row per item (its id and position, the item as JSON).
- `settings_versions`: the whole document after every save (revision 1 is the one-time import), the last
  MAX_VERSIONS kept.
- meta `settings_revision` (the saves so far), `settings_initialised` ("1": the one-time import ran, never cleared),
  `settings_lists_set` (JSON list of the list names that are set, not at their default; a set list may be empty),
  `settings_import_report` (JSON: what the import left out and why, never a value), `settings_imported_at`.

A save is one write transaction: the conflict check, the rows, the new revision and its version. Rows of keys or
lists this build does not know (written by a newer one) are kept and never touched by a save. Nothing here ever holds
a secret: the manager keeps those in secrets.json.
"""

import json
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from pengumon.settings.document import SettingsDocument

logger = logging.getLogger("pengumon.db")

SETTINGS_REVISION_KEY = "settings_revision"
SETTINGS_INITIALISED_KEY = "settings_initialised"
SETTINGS_LISTS_SET_KEY = "settings_lists_set"
SETTINGS_IMPORT_REPORT_KEY = "settings_import_report"
SETTINGS_IMPORTED_AT_KEY = "settings_imported_at"
MAX_VERSIONS = 50
SUMMARY_MAX = 300

_MISSING = object()


class SettingsConflict(Exception):
    """Parts a save touches changed since the revision it started from."""

    def __init__(self, current: int, conflicts: List[str]):
        super().__init__(f"the settings are at revision {current}")
        self.current = current
        self.conflicts = conflicts


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


async def _meta(db, key: str) -> Optional[str]:
    async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def _put_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _json_list(raw: Optional[str]) -> list:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


async def _revision(db) -> int:
    raw = await _meta(db, SETTINGS_REVISION_KEY)
    return int(raw) if raw and raw.isdigit() else 0


async def _document(db) -> SettingsDocument:
    """Every stored row as a document, unknown keys and lists included (the manager leaves those out)."""
    values: Dict[str, Any] = {}
    async with db.execute("SELECT key, value FROM settings_values ORDER BY key") as cursor:
        rows = await cursor.fetchall()
    for key, raw in rows:
        try:
            values[key] = json.loads(raw)
        except ValueError:
            logger.warning(f"Ignoring an unreadable stored setting: {key}")
    lists: Dict[str, Optional[List[dict]]] = {
        name: [] for name in _json_list(await _meta(db, SETTINGS_LISTS_SET_KEY)) if isinstance(name, str)}
    async with db.execute("SELECT list, id, config FROM settings_items ORDER BY list, position, id") as cursor:
        rows = await cursor.fetchall()
    for name, item_id, raw in rows:
        if name not in lists:
            continue  # rows of a list at its default are never written; ignore strays
        try:
            item = json.loads(raw)
        except ValueError:
            logger.warning(f"Ignoring an unreadable stored item: {name} {item_id}")
            continue
        if isinstance(item, dict):
            lists[name].append(item)
    return SettingsDocument(values=values, lists=lists)


async def _write(db, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]], now: float) -> None:
    """Writes the changed keys (None: removed, i.e. back to the default) and lists (None: back to the default)."""
    for key, value in values.items():
        if value is None:
            await db.execute("DELETE FROM settings_values WHERE key = ?", (key,))
        else:
            await db.execute("INSERT OR REPLACE INTO settings_values (key, value, updated) VALUES (?, ?, ?)",
                             (key, _dumps(value), now))
    if not lists:
        return
    lists_set = [n for n in _json_list(await _meta(db, SETTINGS_LISTS_SET_KEY)) if isinstance(n, str)]
    for name, items in lists.items():
        await db.execute("DELETE FROM settings_items WHERE list = ?", (name,))
        if items is None:
            lists_set = [n for n in lists_set if n != name]
            continue
        await db.executemany(
            "INSERT INTO settings_items (list, id, position, config, updated) VALUES (?, ?, ?, ?, ?)",
            [(name, str(item.get("id") or i), i, _dumps(item), now) for i, item in enumerate(items)])
        if name not in lists_set:
            lists_set.append(name)
    await _put_meta(db, SETTINGS_LISTS_SET_KEY, _dumps(sorted(lists_set)))


async def _store_version(db, revision: int, source: str, summary: str, document: SettingsDocument,
                         now: float) -> None:
    summary = summary if len(summary) <= SUMMARY_MAX else summary[:SUMMARY_MAX - 1].rstrip() + "…"
    await db.execute(
        "INSERT OR REPLACE INTO settings_versions (revision, applied_at, source, summary, document) "
        "VALUES (?, ?, ?, ?, ?)", (revision, now, source, summary, _dumps(document.to_json())))
    await db.execute("DELETE FROM settings_versions WHERE revision <= ?", (revision - MAX_VERSIONS,))


def _differs(a: SettingsDocument, b: SettingsDocument, keys: Iterable[str], lists: Iterable[str]) -> List[str]:
    out = [k for k in keys if a.values.get(k, _MISSING) != b.values.get(k, _MISSING)]
    out += [n for n in lists if a.lists.get(n) != b.lists.get(n)]
    return out


class SettingsQueries:
    async def settings_initialised(self) -> bool:
        async with self._db.read() as db:
            return await _meta(db, SETTINGS_INITIALISED_KEY) == "1"

    async def settings_revision(self) -> int:
        async with self._db.read() as db:
            return await _revision(db)

    async def load_settings(self) -> Tuple[int, SettingsDocument]:
        """The revision and the stored document (every row, unknown keys and lists included)."""
        async with self._db.read() as db:
            return await _revision(db), await _document(db)

    async def settings_import_info(self) -> Dict[str, Any]:
        """{"report": [{key, reason}], "imported_at": float | None}: what the one-time import left out, and when."""
        async with self._db.read() as db:
            report = _json_list(await _meta(db, SETTINGS_IMPORT_REPORT_KEY))
            at = await _meta(db, SETTINGS_IMPORTED_AT_KEY)
        try:
            imported_at = float(at) if at else None
        except ValueError:
            imported_at = None
        return {"report": [r for r in report if isinstance(r, dict)], "imported_at": imported_at}

    async def initialise_settings(self, document: SettingsDocument, summary: str,
                                  report: List[Dict[str, str]]) -> bool:
        """The one-time import: `document` as revision 1 (source "import"). False, and nothing written, when the
        store was initialised before."""
        now = time.time()
        async with self._db.write() as db:
            if await _meta(db, SETTINGS_INITIALISED_KEY) == "1":
                return False
            await _write(db, document.values, {n: v for n, v in document.lists.items() if v is not None}, now)
            await _put_meta(db, SETTINGS_REVISION_KEY, "1")
            await _store_version(db, 1, "import", summary, await _document(db), now)
            await _put_meta(db, SETTINGS_INITIALISED_KEY, "1")
            await _put_meta(db, SETTINGS_IMPORT_REPORT_KEY, _dumps(report))
            await _put_meta(db, SETTINGS_IMPORTED_AT_KEY, repr(now))
        return True

    async def settings_conflicts(self, base_revision: int, keys: Iterable[str],
                                 lists: Iterable[str]) -> Tuple[int, List[str]]:
        """(current revision, the touched parts that changed between `base_revision` and now). Every touched part
        is a conflict when the base version is no longer kept."""
        async with self._db.read() as db:
            return await self._conflicts(db, base_revision, list(keys), list(lists))

    @staticmethod
    async def _conflicts(db, base_revision: int, keys: List[str], lists: List[str]) -> Tuple[int, List[str]]:
        current = await _revision(db)
        if current == base_revision:
            return current, []
        base = await _version(db, base_revision) if 0 < base_revision < current else None
        if base is None:
            return current, sorted(set(keys)) + sorted(set(lists))
        return current, _differs(base, await _document(db), keys, lists)

    async def save_settings(self, base_revision: int, values: Mapping[str, Any],
                            lists: Mapping[str, Optional[List[dict]]], source: str,
                            describe: Callable[[SettingsDocument, SettingsDocument], str]
                            ) -> Tuple[int, SettingsDocument, SettingsDocument, str]:
        """Applies a change (values[key] None: back to the default; lists[name] None: back to the default) as the
        next revision, unless a part it touches changed since `base_revision` (SettingsConflict). `describe(old, new)`
        gives the version's summary line. Returns (revision, document before, document after, summary)."""
        now = time.time()
        async with self._db.write() as db:
            current, conflicts = await self._conflicts(db, base_revision, list(values), list(lists))
            if conflicts:
                raise SettingsConflict(current, conflicts)
            old = await _document(db)
            await _write(db, values, lists, now)
            new = await _document(db)
            summary = describe(old, new)
            await _put_meta(db, SETTINGS_REVISION_KEY, str(current + 1))
            await _put_meta(db, SETTINGS_INITIALISED_KEY, "1")
            await _store_version(db, current + 1, source, summary, new, now)
        return current + 1, old, new, summary

    async def settings_versions(self) -> List[Dict[str, Any]]:
        """The kept versions, newest first: revision, applied_at, source, summary."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT revision, applied_at, source, summary FROM settings_versions ORDER BY revision DESC LIMIT ?",
                (MAX_VERSIONS,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [{"revision": r, "applied_at": at, "source": source, "summary": text} for r, at, source, text in rows]

    async def settings_version(self, revision: int) -> Optional[SettingsDocument]:
        """The document of a kept version, or None when it is not kept."""
        async with self._db.read() as db:
            return await _version(db, revision)


async def _version(db, revision: int) -> Optional[SettingsDocument]:
    async with db.execute("SELECT document FROM settings_versions WHERE revision = ?", (revision,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except ValueError:
        return None
    return SettingsDocument.from_json(data) if isinstance(data, dict) else None
