"""The probe path (targets), owned by the dashboard: path_targets plus two meta keys.

`path_revision` counts the applied edits (0: the path was never imported or applied, so the app probes config.yaml's
or the default targets without storing them); `path_initialised` is set by the first import or apply and never
cleared, so a path whose every hop was removed is not imported again. An edit is one transaction: the revision check,
the rows and the new revision commit together or not at all.
"""

import logging
import time
from typing import List, Optional, Sequence, Set, Tuple

from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.db")

PATH_REVISION_KEY = "path_revision"
PATH_INITIALISED_KEY = "path_initialised"


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


class PathQueries:
    async def path_initialised(self) -> bool:
        async with self._db.read() as db:
            return await _meta(db, PATH_INITIALISED_KEY) == "1"

    async def path_revision(self) -> int:
        async with self._db.read() as db:
            return int(await _meta(db, PATH_REVISION_KEY) or 0)

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
        self.hidden_targets = {t.id for t in removed}
        return True

    async def save_path(self, base_revision: int, targets: List[TargetConfig]) -> int:
        """Replaces the path with `targets` (in path order) if it is still at `base_revision`, else raises
        PathConflict. Hops left out are soft-deleted; a removed hop that comes back is restored with its history.
        Returns the new revision."""
        async with self._db.write() as db:
            current = int(await _meta(db, PATH_REVISION_KEY) or 0)
            if current != base_revision:
                raise PathConflict(current)
            await self._write_hops(db, targets)
            await _put_meta(db, PATH_REVISION_KEY, str(current + 1))
            await _put_meta(db, PATH_INITIALISED_KEY, "1")
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
        self.hidden_targets = self.hidden_targets - {target_id}
        return True
