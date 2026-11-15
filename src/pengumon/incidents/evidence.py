"""The incident-evidence component: keeps each resolved incident's own copy of what the inspector shows (db/evidence.py).

At start and then every minute it writes the evidence of resolved incidents that have none, a few per pass, once their
tail has passed (db.evidence.settle_seconds: the probe buffer is flushed and the modem's next reading has arrived). An
incident still open gets nothing until it closes; the inspector reads the shared tables for it. A failure here costs
only the evidence (OPTIONAL): the inspector falls back to the shared tables for an incident without it."""

import asyncio
import time
from typing import Optional

from pengumon.analysis.incident import SNAPSHOT_REACH_SECONDS
from pengumon.capabilities import Capability
from pengumon.components import Component, Context
from pengumon.db.evidence import settle_seconds
from pengumon.observability import get_logger

__all__ = ["OPTIONAL", "register", "write_pending"]

OPTIONAL = True
INTERVAL_SECONDS = 60.0
PER_PASS = 5  # incidents written per pass: each is one write transaction, and a backlog is worked off over a few minutes
_log = get_logger("pengumon")


def _reach(state) -> float:
    """The modem's poll interval: how far from an incident its nearest reading may lie and still be shown."""
    line = state.devices.primary(Capability.ACCESS_LINE) if getattr(state, "devices", None) is not None else None
    return float(line.interval_seconds) if line is not None and line.interval_seconds else SNAPSHOT_REACH_SECONDS


async def write_pending(repository, reach: float = SNAPSHOT_REACH_SECONDS, limit: int = PER_PASS,
                        now: Optional[float] = None) -> int:
    """One pass: the evidence of up to `limit` settled incidents without any. Returns how many were written."""
    settled_before = (time.time() if now is None else now) - settle_seconds(reach)
    written = 0
    for incident_id, *_ in await repository.incidents_needing_evidence(settled_before, limit):
        if await repository.write_incident_evidence(incident_id, reach=reach) is not None:
            written += 1
    return written


async def _worker(state) -> None:
    repository = state.repository
    while True:
        try:
            await write_pending(repository, _reach(state))
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"Writing incident evidence failed: {e}", exc_info=True)
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break


async def register(ctx: Context) -> Component:
    async def start() -> None:
        ctx.spawn(_worker(ctx.state))

    return Component("incident_evidence", start=start)
