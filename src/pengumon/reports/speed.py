"""The speed tests of a period, summarized for the reports (the dossier, the AI pack, the MCP tool). Pure: it takes the
stored rows (db.speedtests) and the plan speed (Settings > Your line) and decides the figures; the callers only word them.
"""

import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from pengumon.speedtest.grade import added_latency_ms, grade_for_added
from pengumon.speedtest.quality import HOUSEHOLD_SHARE, headline, trusted

UNDER_PCT = 50.0      # a test under this share of the plan speed (download or upload) counts as "under"


@dataclass(frozen=True)
class SpeedSummary:
    total: int
    ok: int
    partial: int
    failed: int
    skipped: int
    down_median: Optional[float]
    down_low: Optional[float]
    up_median: Optional[float]
    up_low: Optional[float]
    plan_down: Optional[float]
    plan_up: Optional[float]
    measured: int                       # tests with a trusted speed in at least one direction
    under: int                          # of those, the ones under UNDER_PCT of the plan (0 without a plan)
    added_ms: Optional[float]           # median latency a loaded line added, over the tests that have it
    grade: Optional[str]                # the bufferbloat grade of that median
    too_short: int = 0                  # tests with a direction too short to measure the line: left out of the speeds
    by_router: int = 0                  # of the measured ones, those whose speeds are the line's, from the router
    shared: int = 0                     # of those, the ones where the test itself got far less: household traffic

    def share(self, median: Optional[float], plan: Optional[float]) -> Optional[float]:
        return None if median is None or not plan else median / plan * 100.0


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 1) if values else None


def _under(row: Dict[str, Any], plan_down: Optional[float], plan_up: Optional[float]) -> bool:
    for value, plan in ((row.get("down_mbps"), plan_down), (row.get("up_mbps"), plan_up)):
        if value is not None and plan and value < plan * UNDER_PCT / 100.0:
            return True
    return False


def summarize(rows: Iterable[Dict[str, Any]], plan_down: Optional[float] = None,
              plan_up: Optional[float] = None) -> Optional[SpeedSummary]:
    """None when there are no tests at all."""
    rows = list(rows)
    if not rows:
        return None
    count = {s: sum(1 for r in rows if r["status"] == s) for s in ("ok", "partial", "failed", "skipped")}
    # A direction that lasted too short to measure the line (speedtest/quality.py) is left out of the speeds: a 25 MB test
    # on a gigabit line reads whatever the first answers' waits allow, not the line.
    speeds = [(r, trusted(r)) for r in rows]
    downs = [d for _, (d, _u) in speeds if d is not None]
    ups = [u for _, (_d, u) in speeds if u is not None]
    measured = [{**r, "down_mbps": d, "up_mbps": u} for r, (d, u) in speeds if d is not None or u is not None]
    too_short = sum(1 for r, (d, u) in speeds
                    if (r.get("down_mbps") is not None and d is None) or (r.get("up_mbps") is not None and u is None))
    # The speeds are the headline's (speedtest/quality.headline): the line's rate the router measured, else the test's
    best = [(r, headline(r)) for r, (d, u) in speeds if d is not None or u is not None]
    by_router = sum(1 for _, h in best if h["measured_by"] == "router")
    shared = sum(1 for _, h in best if h["note"])
    added = [a for a in (added_latency_ms(r.get("idle_ms"), r.get("loaded_down_ms"), r.get("loaded_up_ms"))
                         for r in rows) if a is not None]
    added_ms = _median(added)
    return SpeedSummary(
        total=len(rows), ok=count["ok"], partial=count["partial"], failed=count["failed"], skipped=count["skipped"],
        down_median=_median(downs), down_low=round(min(downs), 1) if downs else None,
        up_median=_median(ups), up_low=round(min(ups), 1) if ups else None,
        plan_down=plan_down or None, plan_up=plan_up or None, measured=len(measured),
        under=sum(1 for r in measured if _under(r, plan_down, plan_up)),
        added_ms=added_ms, grade=grade_for_added(added_ms), too_short=too_short, by_router=by_router, shared=shared)


def _mbps(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def direction_text(label: str, median: Optional[float], low: Optional[float], plan: Optional[float]) -> Optional[str]:
    """"Download: median 212 of 300 Mbps (71 %), lowest 80 Mbps", or without a plan "Download: median 212 Mbps, lowest 80
    Mbps"; None without a measurement."""
    if median is None:
        return None
    if plan:
        text = f"{label}: median {_mbps(median)} of {_mbps(plan)} Mbps ({median / plan * 100:.0f} %)"
    else:
        text = f"{label}: median {_mbps(median)} Mbps"
    return text + (f", lowest {_mbps(low)} Mbps" if low is not None else "")


def counts_text(s: SpeedSummary) -> str:
    """"30 tests (27 ok, 1 partial, 1 failed, 1 skipped)"; zero counts left out."""
    parts = [f"{n} {w}" for n, w in ((s.ok, "ok"), (s.partial, "partial"), (s.failed, "failed"), (s.skipped, "skipped")) if n]
    text = f"{s.total} test{'s' if s.total != 1 else ''}" + (f" ({', '.join(parts)})" if parts else "")
    if s.too_short:
        text += (f"; {s.too_short} too short to measure the line, left out of the speeds (raise Data per test: "
                 "Settings > Speed test)")
    return text


def under_text(s: SpeedSummary) -> Optional[str]:
    """"4 of 30 tests under 50 % of the plan speed", only with a plan and a measurement."""
    if not (s.plan_down or s.plan_up) or not s.measured:
        return None
    return f"{s.under} of {s.measured} tests under {UNDER_PCT:.0f} % of the plan speed"


def method_text(s: SpeedSummary) -> Optional[str]:
    """How the speeds were measured: "Measured by the router (what the line carried during the test): 4 of 5 tests;
    the others by the test alone", and when the household's traffic shared the line, how often; None without a
    measured test."""
    if not s.measured:
        return None
    if s.by_router == s.measured:
        text = "Measured by the router (what the line carried during each test, all traffic)"
    elif s.by_router:
        text = (f"Measured by the router (what the line carried during the test, all traffic): {s.by_router} of "
                f"{s.measured} tests; the others by the test alone (one server)")
    else:
        text = "Measured by the test alone (one server; no router reading of the line)"
    if s.shared:
        text += (f"; in {s.shared} the test itself got under {HOUSEHOLD_SHARE * 100:.0f} % of what the line carried: "
                 "other traffic on the network, not a slow line")
    return text


def latency_text(s: SpeedSummary) -> Optional[str]:
    if s.added_ms is None:
        return None
    return f"Latency under load: median {s.added_ms:.0f} ms added, bufferbloat grade {s.grade}"
