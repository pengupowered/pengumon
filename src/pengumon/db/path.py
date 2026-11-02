"""The probe path (targets), owned by the dashboard: path_targets plus two meta keys, the applied versions and the
path's events.

`path_revision` counts the applied edits (0: the path was never imported or applied, so the app probes config.yaml's
or the default targets without storing them); `path_initialised` is set by the first import or apply and never
cleared, so a path whose every hop was removed is not imported again; `path_devices_linked` says the automatic device
links were made (engine.device_links), so an owner's "no device" is not undone at the next start. An edit is one transaction: the revision check,
the rows, the new revision, its version (path_versions: the hops as applied and a one-line summary of the change; the
last MAX_VERSIONS are kept, revision 1 is the import) and its events (path_events: a hop whose address was replaced,
keeping its id and so its history) commit together or not at all.

`path_drift` (meta, JSON) holds the latest background route-change check (engine/discovery/drift.py).
"""

import ipaddress
import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.db")

PATH_REVISION_KEY = "path_revision"
PATH_INITIALISED_KEY = "path_initialised"
# Set once the home hops were linked to their devices by address (engine.device_links): by the one-time import, by any
# applied path, or once at the start of a path stored by an older version.
PATH_DEVICES_LINKED_KEY = "path_devices_linked"
PATH_DRIFT_KEY = "path_drift"
MAX_VERSIONS = 50
SUMMARY_MAX = 300
ADDRESS_CHANGED = "address_changed"

# The roles in the words the path editor uses for them.
ROLE_WORDS = {"local": "Your network", "gateway": "Line / gateway", "isp": "Provider", "public": "Internet",
              "dns": "DNS", "custom": "Custom"}


class PathConflict(Exception):
    """The path changed since the revision an edit started from."""

    def __init__(self, current: int):
        super().__init__(f"the path is at revision {current}")
        self.current = current


async def _meta(db, key: str) -> Optional[str]:
    async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def _put_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _address_key(address: Optional[str]) -> str:
    """An address compared by value (2001:DB8::1 is 2001:db8::1), a host name without case."""
    try:
        return str(ipaddress.ip_address((address or "").strip()))
    except ValueError:
        return (address or "").strip().lower().rstrip(".")


def _names(targets: Sequence[TargetConfig]) -> str:
    return ", ".join(t.name for t in targets)


def describe_change(old: Sequence[TargetConfig], new: Sequence[TargetConfig],
                    removed: Sequence[TargetConfig] = ()) -> Tuple[str, List[Tuple[str, str, str]]]:
    """What an apply changes, in one line ("Added Modem; removed Old hop; Provider hop 1: address 198.51.100.9 →
    198.51.100.10"), and the address changes as (id, old address, new address). `old` is the live path before it,
    `removed` the hops removed earlier (one that comes back is "restored", and its address is compared with the one
    it had)."""
    before = {t.id: t for t in old}
    earlier = {t.id: t for t in removed if t.id not in before}
    after_ids = {t.id for t in new}
    parts: List[str] = []
    added = [t for t in new if t.id not in before and t.id not in earlier]
    restored = [t for t in new if t.id in earlier]
    gone = [t for t in old if t.id not in after_ids]
    if added:
        parts.append(f"added {_names(added)}")
    if restored:
        parts.append(f"restored {_names(restored)}")
    if gone:
        parts.append(f"removed {_names(gone)}")
    moves: List[Tuple[str, str, str]] = []
    other = 0
    for t in new:
        was = before.get(t.id) or earlier.get(t.id)
        if was is None:
            continue
        if _address_key(was.address) != _address_key(t.address):
            moves.append((t.id, was.address, t.address))
            parts.append(f"{t.name}: address {was.address} → {t.address}")
        if t.id not in before:
            continue
        if was.name != t.name:
            parts.append(f"renamed {was.name} to {t.name}")
        if was.category != t.category:
            parts.append(f"{t.name}: role {ROLE_WORDS.get(was.category, was.category)} → "
                         f"{ROLE_WORDS.get(t.category, t.category)}")
        rest = {"id", "name", "category", "address"}
        if was.model_dump(exclude=rest) != t.model_dump(exclude=rest):
            other += 1
    common_before = [t.id for t in old if t.id in after_ids]
    common_after = [t.id for t in new if t.id in before]
    if common_before != common_after:
        parts.append("order changed")
    if other:
        parts.append(f"settings of {other} hop{'s' if other != 1 else ''} changed")
    if not parts:
        return "No changes", moves
    text = "; ".join(parts)
    text = text[0].upper() + text[1:]
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 1].rstrip() + "…"
    return text, moves


def _hops_json(targets: Sequence[TargetConfig]) -> str:
    return json.dumps([{**t.model_dump(), "position": i} for i, t in enumerate(targets)], separators=(",", ":"))


async def _rows(db, deleted: bool) -> List[TargetConfig]:
    async with db.execute(
        f"SELECT config FROM path_targets WHERE deleted_at IS {'NOT ' if deleted else ''}NULL ORDER BY position, id"
    ) as cursor:
        rows = await cursor.fetchall()
    out = []
    for (raw,) in rows:
        try:
            out.append(TargetConfig.model_validate_json(raw))
        except ValueError:
            pass
    return out


async def _store_version(db, revision: int, targets: Sequence[TargetConfig], summary: str, now: float,
                         moves: Sequence[Tuple[str, str, str]] = ()) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES (?, ?, ?, ?, ?)",
        (revision, now, len(targets), summary, _hops_json(targets)))
    await db.execute("DELETE FROM path_versions WHERE revision <= ?", (revision - MAX_VERSIONS,))
    if moves:
        await db.executemany(
            "INSERT INTO path_events (at, kind, target_id, old, new, revision) VALUES (?, ?, ?, ?, ?, ?)",
            [(now, ADDRESS_CHANGED, tid, old, new, revision) for tid, old, new in moves])


class PathQueries:
    async def path_initialised(self) -> bool:
        async with self._db.read() as db:
            return await _meta(db, PATH_INITIALISED_KEY) == "1"

    async def path_revision(self) -> int:
        async with self._db.read() as db:
            return int(await _meta(db, PATH_REVISION_KEY) or 0)

    async def path_devices_linked(self) -> bool:
        async with self._db.read() as db:
            return await _meta(db, PATH_DEVICES_LINKED_KEY) == "1"

    async def mark_path_devices_linked(self) -> None:
        async with self._db.write() as db:
            await _put_meta(db, PATH_DEVICES_LINKED_KEY, "1")

    async def drop_stored_ignores_ping(self) -> int:
        """Removes the "ignores ping" answers an older version stored with the hops (the state is observed, never set:
        engine.path_rules decides it alone). The number of hops changed."""
        async with self._db.write() as db:
            cursor = await db.execute("UPDATE path_targets SET config = json_remove(config, '$.ignores_ping') "
                                      "WHERE json_type(config, '$.ignores_ping') IS NOT NULL")
            return cursor.rowcount

    async def load_path(self) -> Tuple[int, List[TargetConfig]]:
        """The revision and the hops on the path (not the removed ones), in path order."""
        async with self._db.read() as db:
            revision = int(await _meta(db, PATH_REVISION_KEY) or 0)
            async with db.execute(
                "SELECT config FROM path_targets WHERE deleted_at IS NULL ORDER BY position, id"
            ) as cursor:
                rows = await cursor.fetchall()
        hops = []
        for (raw,) in rows:
            try:
                hops.append(TargetConfig.model_validate_json(raw))
            except ValueError as e:  # a row written by a newer build; skip it rather than refuse to start
                logger.warning(f"Ignoring an unreadable path hop: {e}")
        return revision, hops

    async def removed_path_hops(self) -> List[TargetConfig]:
        """The hops removed from the path whose history is kept, as they were when removed (discovery gives one back
        its id when it finds its address again)."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT config FROM path_targets WHERE deleted_at IS NOT NULL ORDER BY position, id"
            ) as cursor:
                rows = await cursor.fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(TargetConfig.model_validate_json(raw))
            except ValueError as e:
                logger.warning(f"Ignoring an unreadable removed hop: {e}")
        return out

    async def removed_path_ids(self) -> Set[str]:
        """The hops removed from the path whose history is kept (and hidden)."""
        async with self._db.read() as db:
            async with db.execute("SELECT id FROM path_targets WHERE deleted_at IS NOT NULL") as cursor:
                return {row[0] for row in await cursor.fetchall()}

    async def import_path(self, targets: List[TargetConfig], removed: Sequence[TargetConfig] = ()) -> bool:
        """The one-time import (config.yaml's targets and the custom targets, ids kept, in this order); `removed`
        are imported as hops removed from the path (history kept, the owner can restore them). False, and nothing
        written, when the path was imported or applied before."""
        async with self._db.write() as db:
            if await _meta(db, PATH_INITIALISED_KEY) == "1":
                return False
            await self._write_hops(db, targets)
            if removed:
                now = time.time()
                await db.executemany(
                    "INSERT INTO path_targets (id, position, config, deleted_at, updated) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    [(t.id, len(targets) + i, t.model_dump_json(), now, now) for i, t in enumerate(removed)])
            await _put_meta(db, PATH_REVISION_KEY, "1")
            await _put_meta(db, PATH_INITIALISED_KEY, "1")
            summary = f"Imported {len(targets)} hop{'s' if len(targets) != 1 else ''} (config.yaml's targets and " \
                      "the ones added in the dashboard)"
            await _store_version(db, 1, targets, summary, time.time())
            await _put_meta(db, PATH_DEVICES_LINKED_KEY, "1")
        self.hidden_targets = {t.id for t in removed}
        return True

    async def save_path(self, base_revision: int, targets: List[TargetConfig]) -> int:
        """Replaces the path with `targets` (in path order) if it is still at `base_revision`, else raises
        PathConflict. Hops left out are soft-deleted; a removed hop that comes back is restored with its history. The
        applied path is stored as the new revision's version, and a hop whose address changed (same id) gets an
        "address_changed" event. Returns the new revision."""
        async with self._db.write() as db:
            current = int(await _meta(db, PATH_REVISION_KEY) or 0)
            if current != base_revision:
                raise PathConflict(current)
            summary, moves = describe_change(await _rows(db, False), targets, await _rows(db, True))
            await self._write_hops(db, targets)
            await _put_meta(db, PATH_REVISION_KEY, str(current + 1))
            await _put_meta(db, PATH_INITIALISED_KEY, "1")
            await _store_version(db, current + 1, targets, summary, time.time(), moves)
            await _put_meta(db, PATH_DEVICES_LINKED_KEY, "1")
            async with db.execute("SELECT id FROM path_targets WHERE deleted_at IS NOT NULL") as cursor:
                hidden = {row[0] for row in await cursor.fetchall()}
        self.hidden_targets = hidden
        return current + 1

    @staticmethod
    async def _write_hops(db, targets: List[TargetConfig]) -> None:
        now = time.time()
        await db.executemany(
            "INSERT INTO path_targets (id, position, config, deleted_at, updated) VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT(id) DO UPDATE SET position = excluded.position, config = excluded.config, deleted_at = NULL, "
            "updated = excluded.updated",
            [(t.id, i, t.model_dump_json(), now) for i, t in enumerate(targets)],
        )
        keep = [t.id for t in targets]
        marks = ",".join("?" * len(keep))
        await db.execute(
            f"UPDATE path_targets SET deleted_at = ?, updated = ? WHERE deleted_at IS NULL"
            f"{f' AND id NOT IN ({marks})' if keep else ''}",
            (now, now, *keep),
        )

    async def delete_path_history(self, target_id: str) -> bool:
        """Deletes the stored probes of a hop removed from the path, and forgets the hop. False (nothing deleted)
        when `target_id` is not a removed hop: the history of a hop on the path is never deleted here."""
        await self.flush_samples()
        async with self._db.write() as db:
            async with db.execute(
                "SELECT 1 FROM path_targets WHERE id = ? AND deleted_at IS NOT NULL", (target_id,)
            ) as cursor:
                if await cursor.fetchone() is None:
                    return False
            key = self._target_ids.get(target_id)
            if key is not None:  # the targets row stays: the id map is cached, and the id may come back later
                await db.execute("DELETE FROM samples WHERE target = ?", (key,))
                await db.execute("DELETE FROM rollup_1m WHERE target = ?", (key,))
            await db.execute("DELETE FROM path_targets WHERE id = ?", (target_id,))
            await db.execute("DELETE FROM path_events WHERE target_id = ?", (target_id,))
        self.hidden_targets = self.hidden_targets - {target_id}
        return True

    # --- versions and events ---------------------------------------------------------------------------------------

    async def path_versions(self) -> List[Dict[str, Any]]:
        """The kept versions, newest first: revision, applied_at, hop_count, summary."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT revision, applied_at, hop_count, summary FROM path_versions ORDER BY revision DESC LIMIT ?",
                (MAX_VERSIONS,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [{"revision": r, "applied_at": at, "hop_count": n, "summary": text} for r, at, n, text in rows]

    async def path_version(self, revision: int) -> Optional[List[dict]]:
        """The hops of a kept version as applied (with their positions), or None when it is not kept."""
        async with self._db.read() as db:
            async with db.execute("SELECT hops FROM path_versions WHERE revision = ?", (revision,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            hops = json.loads(row[0])
        except ValueError:
            return None
        return [h for h in hops if isinstance(h, dict)] if isinstance(hops, list) else None

    async def path_events(self, target_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """The path's events, oldest first (of one hop when `target_id` is given)."""
        where, params = ("WHERE target_id = ?", (target_id,)) if target_id else ("", ())
        async with self._db.read() as db:
            async with db.execute(
                f"SELECT at, kind, target_id, old, new, revision FROM path_events {where} ORDER BY at DESC, id DESC "
                "LIMIT ?", (*params, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [{"at": at, "kind": kind, "target_id": tid, "old": old, "new": new, "revision": rev}
                for at, kind, tid, old, new, rev in reversed(rows)]

    # --- the latest route-change check ----------------------------------------------------------------------------

    async def load_path_drift(self) -> Optional[Dict[str, Any]]:
        async with self._db.read() as db:
            raw = await _meta(db, PATH_DRIFT_KEY)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def save_path_drift(self, drift: Dict[str, Any]) -> None:
        async with self._db.write() as db:
            await _put_meta(db, PATH_DRIFT_KEY, json.dumps(drift, separators=(",", ":")))
