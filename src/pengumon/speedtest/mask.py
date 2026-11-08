"""When a speed test masks the classifier's verdicts ("busy is not broken", like the busy-line rule).

A test fills the line on purpose, so the delay and loss it causes must not open an incident. The mask covers the test and
the tail after it: the judged window (`classifier.window_probes` rounds) still holds probes sent under load until that
many rounds have passed, plus a few seconds. The live runner keeps its own running flag (runner.SpeedTestRunner.is_masking);
the outage-rules replay (engine.replay) applies the stored tests with `SpeedMask`, so the preview agrees with what happened.
"""

from bisect import bisect_right
from typing import Iterable, List, Tuple

TAIL_EXTRA_SECONDS = 5.0


def mask_tail_seconds(window_probes: int, round_seconds: float) -> float:
    """How long after a test ends its probes are still in the judged window."""
    return window_probes * round_seconds + TAIL_EXTRA_SECONDS


class SpeedMask:
    """Stored tests as (start, end) spans, each widened by `tail` seconds after its end; `covers(ts)` says whether a
    round at `ts` is under the mask."""

    def __init__(self, spans: Iterable[Tuple[float, float]] = (), tail: float = 0.0):
        merged: List[List[float]] = []
        for start, end in sorted(spans):
            end += tail
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self._starts = [m[0] for m in merged]
        self._ends = [m[1] for m in merged]

    def covers(self, ts: float) -> bool:
        i = bisect_right(self._starts, ts) - 1
        return i >= 0 and ts <= self._ends[i]
