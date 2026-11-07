"""How a run of a target's probes is summed up: by its middle value, so one stalled probe cannot pass for a slow line.

`middle` is the lower median: of an even count it takes the lower of the two middle values, so a level is passed only
when more than half of the values pass it (5 slow probes of 10 are not enough, 6 are). `jitter` is the middle of the
changes from one answered probe to the next: one stalled probe makes two large changes, which stay below the middle
while fewer than half of the changes are large. The classifier judges both over the probes of its window
(classifier.window_probes); the prober's per-target jitter (the dashboard's figure) is the same over its whole history.
"""

from typing import Optional, Sequence


def middle(values: Sequence[float]) -> Optional[float]:
    """The lower median of `values`; None when there are none."""
    if not values:
        return None
    return sorted(values)[(len(values) - 1) // 2]


def jitter(latencies: Sequence[Optional[float]]) -> Optional[float]:
    """The middle change between consecutive answered probes (None, a lost probe, is skipped); None with fewer than
    two answered."""
    answered = [v for v in latencies if v is not None]
    if len(answered) < 2:
        return None
    return middle([abs(b - a) for a, b in zip(answered, answered[1:])])
