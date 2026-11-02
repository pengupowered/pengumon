"""Targets added from the dashboard (the ones config.yaml does not list)."""

import logging
import time
from typing import List

from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.db")

MAX_CUSTOM_TARGETS = 20  # dashboard-added targets; config.yaml targets do not count


class TargetQueries:
    async def list_custom_targets(self) -> List[TargetConfig]:
        """Targets added from the dashboard, oldest first."""
        async with self._db.read() as db:
            async with db.execute("SELECT config FROM custom_targets ORDER BY created, id") as cursor:
                rows = await cursor.fetchall()
        targets = []
        for (raw,) in rows:
            try:
                targets.append(TargetConfig.model_validate_json(raw))
            except ValueError as e:  # a row written by a newer build; skip rather than refuse to start
                logger.warning(f"Ignoring unreadable custom target: {e}")
        return targets

    async def add_custom_target(self, target: TargetConfig, limit: int = MAX_CUSTOM_TARGETS) -> bool:
        """Stores a dashboard-added target. False when `limit` custom targets already exist (the count and
        the insert are one statement, so concurrent requests cannot overshoot)."""
        async with self._db.write() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO custom_targets (id, config, created) "
                "SELECT ?, ?, ? WHERE (SELECT COUNT(*) FROM custom_targets) < ?",
                (target.id, target.model_dump_json(), time.time(), limit),
            )
            return cursor.rowcount > 0

    async def count_custom_targets(self) -> int:
        async with self._db.read() as db:
            async with db.execute("SELECT COUNT(*) FROM custom_targets") as cursor:
                return (await cursor.fetchone())[0]

    async def delete_custom_target(self, target_id: str) -> bool:
        """Removes a dashboard-added target. False if there was none with that id."""
        async with self._db.write() as db:
            cursor = await db.execute("DELETE FROM custom_targets WHERE id = ?", (target_id,))
            return cursor.rowcount > 0
