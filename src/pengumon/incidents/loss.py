"""How long probes were lost in an incident, as opposed to how long the incident was open.

An incident's start_time and end_time are detection times: the classifier opens it a few rounds after the first lost
probe and closes it once enough rounds are clean again, so the span runs a few seconds late on both ends (7 s of lost
probes are typically recorded as a 12 s incident). The loss span is read from the probes themselves: from the first lost
probe of the incident's affected targets to their last one, plus one probe interval (a probe stands for the time until
the next one). Computed once when the incident closes (and for older incidents whose raw samples are still kept) and
stored with it, so listing incidents never reads samples.

Pure: the repository hands in one aggregate row per target (repository.probe_loss_stats).
"""

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

# The window the probes are read from, around the detected incident: the lead-in that holds the drops which triggered it
# (the classifier needs a few failed rounds before it opens one), and a short tail after its last failed round. The
# inspector's per-hop table (analysis.incident) covers the same episode.
LEAD_SECONDS = 10.0
TAIL_SECONDS = 2.0
DEFAULT_INTERVAL_SECONDS = 1.0  # the prober's default, for a target with a single probe in the window


@dataclass(frozen=True)
class TargetLoss:
    """One target's probes in the window: how many were sent and answered, the first and last lost one, and the first
    and last probe at all (their spacing gives the probe interval)."""
    target_id: str
    sent: int
    received: int
    first_lost: Optional[float]
    last_lost: Optional[float]
    first_ts: float
    last_ts: float

    @property
    def interval(self) -> float:
        if self.sent < 2 or self.last_ts <= self.first_ts:
            return DEFAULT_INTERVAL_SECONDS
        return (self.last_ts - self.first_ts) / (self.sent - 1)


@dataclass(frozen=True)
class LossSpan:
    """start: the first lost probe (None when none was lost); seconds: first to last lost probe plus one interval (0.0
    when no probe was lost, e.g. an incident raised on latency)."""
    start: Optional[float]
    seconds: float


def probe_window(start_time: float, end_time: float) -> Tuple[float, float]:
    """The stretch of probes an incident's loss is read from."""
    return start_time - LEAD_SECONDS, end_time + TAIL_SECONDS


def loss_span(rows: Iterable[TargetLoss]) -> Optional[LossSpan]:
    """The loss span of an incident from its affected targets' probes in probe_window. None: no probe of those targets
    is stored for the window (samples past retention, or no probe target affected, as for a host service), so only the
    recorded duration is known.

    A target that answered no probe in the whole window is left out: it lost every probe from before the incident was
    detected to after it closed, so its loss is not this incident's (a hop that ignores ping, or one that was down
    already). Intermittent loss counts from its first lost probe to its last; the clean probes between them are inside the
    span, as they are inside the incident."""
    rows = list(rows)
    if not rows:
        return None
    lossy = [r for r in rows if r.received > 0 and r.first_lost is not None and r.last_lost is not None]
    if not lossy:
        return LossSpan(start=None, seconds=0.0)
    first = min(r.first_lost for r in lossy)
    last = max(lossy, key=lambda r: r.last_lost)
    return LossSpan(start=first, seconds=round(last.last_lost - first + last.interval, 1))
