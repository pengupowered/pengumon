"""Reading outages: the incidents an outage needs, widened by time until the grouping is exact.

An outage is a chain of drops, so the page of incidents or the period asked for may start in the middle of one. The
chain is read from a time `since` on, and `since` moves back (twice as far each time) while the earliest outage found
starts within one gap of it, since an earlier drop could still belong to it. History is never read by a fixed row count.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from pengumon.incidents.outages import OutageBuilder, Rules, build_outages, drop_of

_MIN_STEP = 3600.0


async def outages_since(repository, rules: Rules, since: float, now: Optional[float] = None) -> List[OutageBuilder]:
    """Every outage that ends at or after `since` or is still open (oldest first), each with all its members."""
    now = time.time() if now is None else now
    step = max(rules.gap_seconds * 2, _MIN_STEP)
    start = since - rules.gap_seconds - 1
    while True:
        outages = build_outages(await repository.get_incidents_touching(start), rules, now)
        if not outages or outages[0].start - start > rules.gap_seconds or start <= 0:
            break
        start -= step
        step *= 2
    return [o for o in outages if o.ongoing or o.latest_end(now) >= since]


async def outage_ids_since(repository, rules: Rules, since: float, now: Optional[float] = None) -> Dict[int, int]:
    """Incident id -> outage id for the incidents of the outages that reach `since` or later."""
    ids: Dict[int, int] = {}
    for o in await outages_since(repository, rules, since, now):
        for m in o.members:
            ids[m.id] = o.id
    return ids


async def with_outage_ids(repository, rules: Rules, incidents: List[Any], now: Optional[float] = None) -> List[Any]:
    """`incidents` (Incident models, newest first, as get_recent_incidents returns) as copies that carry their
    outage_id. The cached models are not touched."""
    if not incidents:
        return incidents
    if not rules.grouping:  # every groupable incident is an outage of one: its own id
        return [i.model_copy(update={"outage_id": i.id}) if drop_of(i) is not None else i for i in incidents]
    ids = await outage_ids_since(repository, rules, min(i.start_time for i in incidents), now)
    return [i.model_copy(update={"outage_id": ids.get(i.id)}) for i in incidents]
