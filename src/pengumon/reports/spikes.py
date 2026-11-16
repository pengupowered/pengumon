"""Latency spikes over a window (incidents/spikes.py): the counts GET /api/latency-spikes answers and the reliability
report, the dossier, the AI pack and the MCP `incidents` tool state. A spike counts where it started; expected ones (a
busy line explained them, as an expected incident) are left out of the counts and counted on their own, as the
incident figures leave out expected incidents."""

from bisect import bisect_right
from collections import Counter
from datetime import tzinfo
from typing import Any, Dict, List, Optional

from pengumon.windows import day_cells

DEFAULT_AFTER_SECONDS = 120.0  # classifier.latency_incident_after_seconds' default, for a caller without a config
# the stages in path order (taxonomy.CAUSE_CATEGORIES): a tie in the split is listed in this order
STAGE_ORDER = ("router", "gateway", "line", "isp", "internet", "dns", "load", "service", "unknown")
# a stage in words, as the dossier's causes are named (reports/dossier.py CAUSE_WORDS)
STAGE_WORDS = {
    "router": "home network", "gateway": "gateway", "line": "line or the provider's first router",
    "isp": "provider's network", "internet": "public servers only", "load": "busy line", "unknown": "not attributed",
}


def incident_after_seconds(config) -> float:
    """classifier.latency_incident_after_seconds of `config` (its default without one)."""
    return float(getattr(getattr(config, "classifier", None), "latency_incident_after_seconds", DEFAULT_AFTER_SECONDS))


async def spike_rows(repository, start: float, end: float) -> List[Dict[str, Any]]:
    """The spikes that started in [start, end], oldest first; none from a repository that keeps none (a test's
    stand-in)."""
    if not hasattr(repository, "latency_spikes_between"):
        return []
    return await repository.latency_spikes_between(start, end)


def by_stage(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{stage, count}] of `rows`, most first (ties in path order)."""
    counts = Counter(r["stage"] for r in rows)
    rank = {s: n for n, s in enumerate(STAGE_ORDER)}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], rank.get(kv[0], 99), kv[0]))
    return [{"stage": s, "count": n} for s, n in ranked]


def summarize_spikes(rows: List[Dict[str, Any]], start: float, end: float, tz: tzinfo,
                     after_seconds: float = DEFAULT_AFTER_SECONDS) -> Dict[str, Any]:
    """The answer of GET /api/latency-spikes for the window [start, end] from its spike rows (docs/api.md): the total
    (expected ones left out, and counted in `expected`), the split by stage, and the count per local calendar day of
    the window (windows.day_cells: the first and last day `partial`, with the `hours` they hold)."""
    shown = [r for r in rows if not r.get("expected")]
    cells = day_cells(start, end, tz)
    starts = [c["start"] for c in cells]
    per_cell = [0] * len(cells)
    for r in shown:
        i = bisect_right(starts, r["start_time"]) - 1
        if 0 <= i < len(cells):
            per_cell[i] += 1
    return {
        "window_start": start,
        "window_end": end,
        "incident_after_seconds": after_seconds,
        "count": len(shown),
        "expected": len(rows) - len(shown),
        "by_stage": by_stage(shown),
        "days": [{"day": c["day"], "start": c["start"], "end": c["end"], "partial": c["partial"], "hours": c["hours"],
                  "count": n} for c, n in zip(cells, per_cell)],
    }


async def spike_summary(repository, config, start: float, end: float, tz: tzinfo) -> Dict[str, Any]:
    """summarize_spikes over the stored spikes of [start, end], with the config's wait."""
    return summarize_spikes(await spike_rows(repository, start, end), start, end, tz, incident_after_seconds(config))


def after_words(seconds: Optional[float]) -> str:
    """"2 min", "90 s": the wait in words."""
    s = DEFAULT_AFTER_SECONDS if seconds is None else float(seconds)
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)} min"
    return f"{s:g} s"


def stage_words(split: List[Dict[str, Any]]) -> str:
    """"provider's network 9, home network 3"."""
    return ", ".join(f"{STAGE_WORDS.get(s['stage'], s['stage'])} {s['count']}" for s in split)
