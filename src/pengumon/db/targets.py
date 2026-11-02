"""Targets added from the dashboard before the probe path moved into the database (custom_targets, schema v3).

Read once, by the one-time import of the path (pengumon.engine.path); nothing writes the table any more. It is kept,
unchanged, so an older build started on this database still finds the targets it had.
"""

import logging
from typing import List

from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.db")


class TargetQueries:
    async def list_custom_targets(self) -> List[TargetConfig]:
        """Targets added from the dashboard by an older build, oldest first."""
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
