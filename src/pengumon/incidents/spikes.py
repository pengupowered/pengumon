"""Latency spikes: brief latency or jitter problems with no loss, counted instead of recorded as incidents.

A latency-only episode (a kind in taxonomy.LATENCY_ONLY_KINDS, no round with loss) that reaches the incident start rule
(`classifier.consecutive_failures_for_latency_incident` faulty rounds) is held by the incident tracker
(engine/classifier/tracker.py) until it has lasted `classifier.latency_incident_after_seconds`: then it becomes an
incident that starts where the episode started. One that ends sooner (by the incident end rule,
`consecutive_successes_for_recovery` healthy rounds) is stored as a LatencySpike in the `latency_spikes` table
(db/spikes.py): never announced, never in the incident list, its counts, the timeline or the outages, and kept and
pruned like the incidents (`database.incident_retention_days`). The stored history from before them was converted
by the migration that created the table (db/migrations/v0*_latency_spikes.py) with the same rule.
"""

from typing import Dict, Optional, Tuple

from pydantic import BaseModel

# The probe categories whose latency a spike's stage is about: the peak it records is the slowest answered probe of
# these categories during the spike (a stage not listed has no peak).
STAGE_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "router": ("local",), "gateway": ("gateway",), "line": ("isp", "public"), "isp": ("isp", "public"),
    "internet": ("public",), "load": ("isp", "public"),
}


class LatencySpike(BaseModel):
    """A latency-only episode that ended before it became an incident. `kind` is the incident kind it would have had
    (upstream_latency, local_latency, ...), `stage` that kind's cause category (taxonomy.incident_cause: router,
    gateway, line, isp, internet, load), `expected` whether a busy line explained it (as an incident's), `peak_ms` the
    slowest answered probe at the stage's hops during it (None: not known)."""

    id: Optional[int] = None
    start_time: float
    end_time: float
    kind: str
    stage: str
    expected: bool = False
    peak_ms: Optional[float] = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_time - self.start_time)
