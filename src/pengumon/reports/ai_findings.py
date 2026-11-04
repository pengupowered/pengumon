"""The facts of the AI troubleshooting pack: what Pengumon itself decides from the stored data, before any wording.

`collect_pack_facts` reads (the per-minute rollups, the incidents, a batch of episode losses, the path and settings
versions) and returns a `PackFacts`; the pack's renderer only formats it and the redactor only renames. Nothing here
writes sentences, imports a device or a line technology, or knows what a modem is: module contributions hand in
`ModuleEvent`s (capabilities/contrib.py, design 6.2) and the matching below is generic.

Every threshold is in `Rules` (one place, named). Every decision is one small function with its rule in the
docstring, in the order the pack uses them:

    hop_baseline  hop_window_vs_normal  silent_hops  path_shape  patterns  match_events  confidence  score

The roles-only labels (Router, Gateway, ISP hop 1, Internet, DNS lookup, Custom target) are `role_labels`, a port of
`shortLabels` in web/static/js/path.js without the names and addresses that function can put in a label.
"""

import bisect
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from statistics import median
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pengumon.engine.path_rules import ignoring_ping
from pengumon.incidents.taxonomy import current_cause, incident_cause, is_service_kind
from pengumon.reports.common import (
    _targets, find_primary_failing_hop, material_loss_points,
)
from pengumon.reports.dossier import collect_dossier_facts
from pengumon.reports.queries import MinuteRow, episode_loss, incident_rows, minute_rows

DAY = 86400.0


# --- the thresholds ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rules:
    """Every number the rules below use. Nothing else in this module decides by a literal."""
    baseline_days: int = 7                 # a hop's normal: its statistics over the days ending at the window's end
    baseline_min_minutes: int = 30         # fewer minutes with latency than this: no normal is claimed
    thin_baseline_minutes: int = 1440      # fewer minutes of history than this: the normal is called thin
    slow_factor: float = 2.0               # a slow minute: its average over this times the normal ...
    slow_extra_ms: float = 10.0            # ... and at least this much more
    notable_median_factor: float = 1.25    # a deviation is notable: window median at least this times the normal
    notable_extra_ms: float = 5.0          # ... and at least this much more (a 1.9 ms hop at 2.4 ms is not slower), or
    notable_slow_share: float = 0.02       # slow minutes at least this share of the window's minutes, or
    notable_loss_factor: float = 2.0       # window loss at least this times the normal loss and material points more
    busy_percentile: float = 0.95          # "busy" latency: this percentile of the minute averages
    # path shape, confidence and ranking
    one_hop_max_end_loss_pct: float = 5.0  # "one hop only" needs the public hops under this loss: more is real loss
    shape_weight: Mapping[str, float] = field(default_factory=lambda: {"carried": 1.0, "none": 0.3, "one_hop": 0.2})
    critical_weight: float = 1.5           # score factor for a critical incident
    latency_weight: float = 0.1            # an incident without lost probes scores this share of its duration
    latency_min_seconds: float = 60.0      # a latency-only incident shorter than this has low confidence
    # patterns
    block_hours: int = 6                   # time-of-day blocks
    duration_bands: Tuple[float, ...] = (10.0, 60.0, 300.0)   # seconds: under 10 s, 10-60 s, 1-5 min, longer
    cluster_gap_seconds: float = 900.0     # incidents starting within this of the previous one are one cluster
    regular_min_incidents: int = 4         # "about every N" needs at least this many incidents ...
    regular_max_spread: float = 0.25       # ... and the gaps' (max - min) / median no larger than this
    # events
    core_event_reach_seconds: float = 3600.0   # a route change (found later than it happened) counts this long
    #                                            either way; the window's changes are looked up this far around it
    change_reach_seconds: float = 600.0    # a path or settings change counts for incidents starting this soon after
    #                                        it was saved: it acts at once, so one saved earlier is stale
    change_slack_seconds: float = 60.0     # a change saved this soon after an incident's end still counts
    nodata_reach_seconds: float = 60.0
    nodata_min_seconds: float = 180.0      # a stretch without any probe at least this long is a no-data gap
    # caps (design 2.4), by detail
    incident_rows: Mapping[str, int] = field(default_factory=lambda: {"compact": 20, "detailed": 40})
    closeups: Mapping[str, int] = field(default_factory=lambda: {"compact": 3, "detailed": 6})
    closeup_minutes_around: int = 5        # a close-up's 1-minute rows: this many minutes before and after
    closeup_max_rows: int = 24             # more rows than this: the lossiest ones are kept
    closeup_rows_10s: int = 40             # the incident scope's 10-second rows, the lossiest kept
    closeup_columns: int = 4
    probe_interval_seconds: float = 1.0    # a lost probe stands for this long (the prober's default interval)
    episode_seconds_around_10s: float = 120.0


RULES = Rules()

PATH_CATEGORIES = ("local", "gateway", "isp", "public")      # the hops the path shape walks (DNS lookups are no hop)
# the probe role a verdict's cause category points at; causes not listed point at no place on the path. A "gateway"
# verdict is loss from the gateway's home-side address on; a "line" one (the access line or the provider's first
# router) is loss from the first ISP hop on while the gateway answers, as is an "isp" one further out.
PLACE_ROLE = {"router": "local", "gateway": "gateway", "line": "isp", "isp": "isp", "internet": "public"}
NO_PLACE_CAUSES = ("dns", "load")        # verdicts about lookups or a busy line, not about where loss starts
# the kinds raised on latency or jitter, not loss (incidents/taxonomy.py): no lost probe need place them
LATENCY_KINDS = frozenset({"local_latency", "gateway_latency", "upstream_latency", "busy_latency", "legacy_latency",
                           "dns_slow"})
KEY_ROLES = ("local", "gateway", "isp", "public")             # the "normal week" profile's hops: the first of each
SHORT_ROLE = {"local": "Router", "gateway": "Gateway", "isp": "ISP hop", "dns": "DNS lookup"}


# --- the shapes of the facts ---------------------------------------------------------------------------------------

@dataclass
class Baseline:
    """A hop's normal over the baseline window. Latencies in ms (None: no answered probe); loss_pct over every probe."""
    normal_ms: Optional[float]
    busy_ms: Optional[float]
    p90_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_pct: Optional[float]
    minutes: int                      # minutes with probes (history length)
    thin: bool                        # less than Rules.thin_baseline_minutes of history


@dataclass
class Deviation:
    """The window against the hop's own normal."""
    minutes: int
    median_ms: Optional[float]
    loss_pct: Optional[float]
    slow_minutes: int
    lossy_minutes: int
    worst_minute: Optional[Tuple[float, float, Optional[float]]]   # (timestamp, loss_pct, avg_ms) of the lossiest minute
    notable: List[str]                # "slower", "slow_minutes", "lossier"; empty: nothing worth saying


@dataclass
class HopFact:
    target_id: str
    category: str
    label: str                        # role words only
    ignores_ping: bool
    baseline: Baseline
    window: Deviation


@dataclass(frozen=True)
class CoreEventKind:
    """The shape of an EventKind (capabilities/contrib.py) for the core's own events: they support nothing."""
    code: str
    phrase: str
    reach_seconds: float
    supports: frozenset = frozenset()
    label: str = ""


@dataclass
class MatchedEvent:
    """Events of one code and subject that coincided with an incident. `value` is the largest, `value_total` the sum;
    the renderer fills `phrase` with `subject` (a role key, redacted) and a value."""
    code: str
    phrase: str
    subject: str
    value: Optional[float]
    value_total: Optional[float]
    count: int
    supports: frozenset
    source: str                       # "module" or "core"
    corroborates: bool                # supports the incident's cause category
    gap_s: float = 0.0                # the nearest event's distance from the incident, seconds: > 0 before it, < 0
    #                                   after it, 0 during it (or within a minute)


@dataclass
class Shape:
    kind: str                         # carried | one_hop | none
    first_hop: Optional[str]          # target id of the first failing hop
    end_loss_pct: Optional[float]     # median loss of the public hops


@dataclass
class IncidentFact:
    id: int
    start: float
    end: float
    duration: float
    lost_for: float                   # seconds of lost probes (loss_seconds), else the duration
    kind: Optional[str]
    cause: str                        # cause category (taxonomy.CAUSE_CATEGORIES)
    cause_code: str
    severity: str
    title: str
    expected: bool
    load_pct: Optional[float]
    resolution: str                   # "1s" | "minute" | "none": what the episode's hop losses were read from
    shape: Shape
    events: List[MatchedEvent]
    confidence: str                   # low | medium | high
    confidence_reason: str
    score: float
    rank: int = 0                     # 1 = most significant; expected incidents come last
    hops: Optional[List[Dict[str, Any]]] = None    # per-hop loss in the episode; set for the close-ups only
    latency: Optional[Shape] = None   # where latency rises (latency_start), for an incident with no hop isolated
    latency_normal: List[str] = field(default_factory=list)   # the hops before that start, checked and normal
    peaks: Dict[str, float] = field(default_factory=dict)     # target id -> the episode's highest answered probe, ms


@dataclass
class CloseUp:
    incident_id: int
    columns: List[str]                                  # target ids, path order, at most Rules.closeup_columns
    minutes: List[Tuple[float, Dict[str, Tuple[float, Optional[float]]]]]   # (minute start, {col: (loss %, avg ms)})
    minutes_cut: int                                    # rows left out (the lossiest were kept)
    rows_10s: Optional[List[Tuple[float, Dict[str, Tuple[float, Optional[float]]]]]] = None   # incident scope only


@dataclass
class Patterns:
    count: int
    per_day_avg: float
    blocks: List[Tuple[int, int, int]]                  # (first hour, last hour + 1, incidents) in local time
    peak_hour: Optional[int]                            # local hour with most starts, when 2 or more and no tie
    shapes: Dict[str, int]
    first_hops: List[Tuple[str, int]]                   # (target id, incidents), most often first, at most 3
    bands: List[Tuple[str, int]]                        # duration bands over lost_for
    median_lost: Optional[float]                        # over the incidents that lost probes (with_loss)
    longest_lost: Optional[float]
    median_gap: Optional[float]
    close_together: int                                 # gaps shorter than Rules.cluster_gap_seconds
    clusters: int                                       # runs of two or more such incidents
    largest_cluster: int
    about_every: Optional[float]                        # seconds, when the gaps are regular
    with_loss: int = 0                                  # incidents that lost probes (the others were about latency)
    peak_count: int = 0                                 # starts in the peak hour


@dataclass
class EventSummary:
    """A module's events of one code in the window, against the network incidents (expected ones included)."""
    code: str
    label: str
    count: int                                          # events in the window
    during: int                                         # of them, those that coincided with such an incident
    starts: List[Tuple[Optional[str], int]]             # (first failing hop of that incident or None, events)


@dataclass
class BusyLatency:
    """The first public hop's minute averages while an event that supports "load" (a busy line) ran, and otherwise."""
    target_id: str
    busy_ms: Optional[float]
    other_ms: Optional[float]
    busy_minutes: int


@dataclass
class PackFacts:
    start: float
    end: float
    now: float
    scope: str                                          # "window" | "incident"
    focus_id: Optional[int]
    detail: str
    tz_offset_minutes: int
    order: List[str]                                    # the considered targets, path order
    labels: Dict[str, str]
    categories: Dict[str, str]
    hops: List[HopFact]
    incidents: List[IncidentFact]                       # every network incident in the window, time order
    listed: List[int]                                   # ids shown in the table, time order (the cap by rank)
    more: List[Tuple[str, int]]                         # (cause, count) of the incidents not listed
    closeups: List[CloseUp]
    patterns: Patterns                                  # over the incidents that are not expected
    expected_count: int
    service_count: int                                  # host-app outages in the window (not network incidents)
    lost_minutes: float                                 # lost_for of the unexpected incidents, in minutes
    verdicts: Dict[str, Dict[str, int]]                 # cause -> {low, medium, high, total}
    coincided: Dict[str, int]                           # event code -> unexpected incidents it coincided with
    events: List[Dict[str, Any]]                        # the core's own events, with their times
    changes: List[Dict[str, Any]]                       # path / settings / route changes in the window: kind, at
    no_data: List[Tuple[float, float]]                  # (from, to) gaps with no probe at all
    profile: List[Dict[str, Any]]                       # time-of-day blocks x key hops over the baseline window
    per_day: List[Dict[str, Any]]                       # local days of the window: day, incidents, lost_minutes
    primary_hop: Optional[str]                          # the window's first hop with elevated loss (dossier rule)
    later_lossy: List[str]
    max_lan_loss: Optional[float]
    rules: Dict[str, Any]                               # the thresholds in force, for "how Pengumon measures"
    event_summaries: Dict[str, EventSummary] = field(default_factory=dict)   # by module event code
    busy_latency: Optional[BusyLatency] = None
    labels_of_events: Dict[str, str] = field(default_factory=dict)          # event code -> its label
    load_codes: Tuple[str, ...] = ()                    # the event codes whose kind supports "load" (a busy line)
    service_outages: Dict[str, Tuple[int, float]] = field(default_factory=dict)   # host app id -> (outages, seconds)
    tz: Optional[tzinfo] = None                         # the zone the days and hours are local to


def category_of(target: Any) -> str:
    """A target's probe category; a DNS probe (DnsProbeConfig) has none of its own and is "dns"."""
    return getattr(target, "category", None) or "dns"


# --- labels --------------------------------------------------------------------------------------------------------

def role_labels(targets: Sequence[Any]) -> Dict[str, str]:
    """Role words for the enabled targets in path order (path.js shortLabels): Router, Gateway, ISP hop N, Internet,
    DNS lookup, "Custom target". A number only when the role has several targets. Never a name or an address."""
    live = [t for t in targets if getattr(t, "enabled", True) is not False]
    by_role: Dict[str, list] = defaultdict(list)
    for t in live:
        by_role[category_of(t)].append(t)
    out = {}
    for t in live:
        category = category_of(t)
        group = by_role[category]
        n = group.index(t) + 1
        many = len(group) > 1
        if category == "public":
            out[t.id] = f"Internet {n}" if many else "Internet"
        elif category == "isp":
            out[t.id] = f"ISP hop {n}"
        elif category in SHORT_ROLE:
            out[t.id] = f"{SHORT_ROLE[category]} {n}" if many else SHORT_ROLE[category]
        else:
            out[t.id] = f"Custom target {n}" if many else "Custom target"
    return out


# --- rule: a hop's normal, and the window against it ----------------------------------------------------------------

def _percentile(sorted_values: List[float], q: float) -> Optional[float]:
    """Nearest rank."""
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def hop_baseline(rows: Sequence[MinuteRow], rules: Rules = RULES) -> Baseline:
    """The hop's normal from its minute rows over the baseline window: the median of the minute averages, their 95th
    percentile ("busy"), the median of the minute p90s, the median jitter and the loss over every probe. Without
    `baseline_min_minutes` minutes that have a latency, no latency normal is claimed (None)."""
    sent, ok_total = 0, 0
    avgs: List[float] = []
    p90s: List[float] = []
    jits: List[float] = []
    if rows:   # a week is 70k+ rows per hop: the columns are read once each, not attribute by attribute
        _, _, n_col, ok_col, avg_col, p90_col, jit_col = zip(*rows)   # MinuteRow's fields in order
        sent, ok_total = sum(n_col), sum(ok_col)
        avgs = [v for v in avg_col if v is not None]
        p90s = [v for v in p90_col if v is not None]
        jits = [v for v in jit_col if v is not None]
    lost = sent - ok_total
    avgs.sort()
    enough = len(avgs) >= rules.baseline_min_minutes
    return Baseline(
        normal_ms=median(avgs) if enough else None,
        busy_ms=_percentile(avgs, rules.busy_percentile) if enough else None,
        p90_ms=median(p90s) if enough and p90s else None,
        jitter_ms=median(jits) if enough and jits else None,
        loss_pct=(lost * 100.0 / sent) if sent else None,
        minutes=len(rows),
        thin=len(rows) < rules.thin_baseline_minutes,
    )


def hop_window_vs_normal(rows: Sequence[MinuteRow], base: Baseline, material: float,
                         rules: Rules = RULES) -> Deviation:
    """The window's rows against the hop's normal.
    Slow minute: average over `slow_factor` x normal and at least `slow_extra_ms` more.
    Lossy minute: any probe lost.
    Notable (any one): window median >= `notable_median_factor` x normal and `notable_extra_ms` more ("slower"); slow minutes >=
    `notable_slow_share` of the minutes with a latency ("slow_minutes"); window loss >= `notable_loss_factor` x the
    normal loss and at least `material` points more ("lossier"). Nothing is notable without a normal."""
    if rows:   # the columns once each (MinuteRow's fields in order), not attribute by attribute
        _, _, n_col, ok_col, avg_col, _, _ = zip(*rows)
    else:
        n_col = ok_col = avg_col = ()
    lat = [a for a in avg_col if a is not None]   # the average of each minute that has a latency
    sent = sum(n_col)
    lost = sent - sum(ok_col)
    loss = (lost * 100.0 / sent) if sent else None
    med = median(lat) if lat else None
    normal = base.normal_ms
    slow = 0
    if normal is not None:
        slow = sum(1 for a in lat if a > rules.slow_factor * normal and a - normal >= rules.slow_extra_ms)
    lossy = [r for r in rows if r[2] > r[3]]
    worst = None
    if lossy:
        w = max(lossy, key=lambda r: ((r.n - r.ok) / r.n, r.avg or 0.0))
        worst = (w.minute * 60.0, round((w.n - w.ok) * 100.0 / w.n, 1), w.avg)
    notable: List[str] = []
    if (normal is not None and med is not None and med >= rules.notable_median_factor * normal
            and med - normal >= rules.notable_extra_ms):
        notable.append("slower")
    if normal is not None and lat and slow / len(lat) >= rules.notable_slow_share:
        notable.append("slow_minutes")
    if (loss is not None and base.loss_pct is not None and loss >= rules.notable_loss_factor * base.loss_pct
            and loss - base.loss_pct >= material):
        notable.append("lossier")
    return Deviation(minutes=len(rows), median_ms=med, loss_pct=None if loss is None else round(loss, 2),
                     slow_minutes=slow, lossy_minutes=len(lossy), worst_minute=worst, notable=notable)


# --- rule: where loss starts along the path ------------------------------------------------------------------------

def silent_hops(path: Sequence[Tuple[str, str]], hops: Mapping[str, Tuple[int, int]]) -> set:
    """The hops that answered nothing in the episode while a later stage of the path did: they ignore ping
    (engine.path_rules.ignoring_ping, applied to the episode's own probes), so their loss is no loss on the path."""
    stats = [SimpleNamespace(target_id=t, category=c, enabled=True, ipv6_unavailable=False,
                             status="down" if hops[t][0] and not hops[t][1] else "up")
             for t, c in path if t in hops and hops[t][0]]
    return ignoring_ping(stats)


def path_shape(path: Sequence[Tuple[str, str]], hops: Mapping[str, Tuple[int, int]], ignoring: Iterable[str],
               material: float, degraded_loss_pct: float, rules: Rules = RULES) -> Shape:
    """Where loss starts, from the episode's per-hop probes (path: [(target id, category)] in path order; hops:
    {target id: (sent, answered)}). Only local, gateway, ISP and public hops with probes count, without the hops
    that ignore ping (`ignoring` and silent_hops). The first failing hop is find_primary_failing_hop (its loss at
    least `material` points above every hop before it).
      none     no hop isolated;
      one_hop  the public hops' median loss is under `degraded_loss_pct` and under half the first hop's (the
               intermediate-hop rule: engine/classifier/rules.py apply_hop_deprioritisation), and under
               `one_hop_max_end_loss_pct`: a few per cent lost at the Internet is real loss the user had, whatever
               the hop before it lost;
      carried  otherwise: it carries on to the later hops. (Without a public hop to look at, carried.)"""
    walk = [(t, c) for t, c in path if c in PATH_CATEGORIES and hops.get(t, (0, 0))[0]]
    skip = set(ignoring) | silent_hops(walk, hops)
    kept = [(t, c) for t, c in walk if t not in skip]
    rows = [{"target_id": t, "sent": hops[t][0], "loss_pct": (hops[t][0] - hops[t][1]) * 100.0 / hops[t][0]}
            for t, _ in kept]
    first = find_primary_failing_hop(rows, (), material)
    public = [r["loss_pct"] for r, (_, c) in zip(rows, kept) if c == "public"]
    end_loss = median(public) if public else None
    if first is None:
        return Shape("none", None, end_loss)
    if (end_loss is not None and end_loss < degraded_loss_pct and end_loss < first["loss_pct"] / 2
            and end_loss < rules.one_hop_max_end_loss_pct):
        return Shape("one_hop", first["target_id"], end_loss)
    return Shape("carried", first["target_id"], end_loss)


def latency_start(path: Sequence[Tuple[str, str]], peaks: Mapping[str, float], normals: Mapping[str, Optional[float]],
                  ignoring: Iterable[str], rules: Rules = RULES) -> Tuple[Shape, List[str]]:
    """Where latency rises in an episode with no hop isolated by loss, from each hop's peak (its highest answered
    probe in the episode) against its own normal. A hop is raised when its peak is over `slow_factor` x its normal
    and at least `slow_extra_ms` more (the slow-minute rule, applied to the peak). Local, gateway, ISP and public
    hops with a peak and a normal count, in path order, without the hops that ignore ping.
      none     no hop raised;
      one_hop  the first raised hop is not public and no public hop is raised (a provider router slow to answer
               pings, which is harmless);
      carried  otherwise: it carries on to the Internet hops.
    Returns the shape (first_hop: the first raised hop; end_loss_pct: unused, None) and the hops before it that were
    checked and normal."""
    skip = set(ignoring)
    walk = [(t, c) for t, c in path if c in PATH_CATEGORIES and t not in skip and peaks.get(t) is not None
            and normals.get(t) is not None]

    def raised(t: str) -> bool:
        peak, normal = peaks[t], normals[t]
        return peak > rules.slow_factor * normal and peak - normal >= rules.slow_extra_ms

    first = next((k for k, (t, _) in enumerate(walk) if raised(t)), None)
    if first is None:
        return Shape("none", None, None), [t for t, _ in walk]
    start, category = walk[first]
    public = [t for t, c in walk[first:] if c == "public"]
    kind = "one_hop" if category != "public" and public and not any(raised(t) for t in public) else "carried"
    return Shape(kind, start, None), [t for t, _ in walk[:first]]


# --- rule: patterns -------------------------------------------------------------------------------------------------

class LocalClock:
    """The wall clock of a zone (a named one follows its daylight-saving changes): the local hour and day of a time.
    The offset is looked up once per quarter hour of UTC (every zone's offset changes on a quarter hour), so a week of
    minute rows costs a few hundred lookups, not one per row."""

    def __init__(self, tz: tzinfo):
        self.tz = tz
        self._offsets: Dict[int, float] = {}

    def offset(self, ts: float) -> float:
        q = int(ts // 900)
        off = self._offsets.get(q)
        if off is None:
            off = (datetime.fromtimestamp(q * 900, self.tz).utcoffset() or timedelta(0)).total_seconds()
            self._offsets[q] = off
        return off

    def hour(self, ts: float) -> int:
        return int(((ts + self.offset(ts)) % DAY) // 3600)

    def day(self, ts: float) -> int:
        """The local calendar day as a day number (days since 1970-01-01 on the local calendar)."""
        return int((ts + self.offset(ts)) // DAY)


def _clock(tz) -> LocalClock:
    """A LocalClock for a tzinfo or, as before, a fixed offset in minutes east of UTC."""
    if isinstance(tz, LocalClock):
        return tz
    if tz is None or isinstance(tz, (int, float)):
        return LocalClock(timezone(timedelta(minutes=int(tz or 0))))
    return LocalClock(tz)


def day_name(day: int) -> str:
    """'YYYY-MM-DD' of a local day number."""
    return (date(1970, 1, 1) + timedelta(days=day)).isoformat()


def patterns(incidents: Sequence[IncidentFact], window_seconds: float, tz: Any,
             rules: Rules = RULES) -> Patterns:
    """Pure functions over the incidents given (the caller passes the ones that are not expected; `tz`: a tzinfo, a
    LocalClock or minutes east of UTC):
    time of day in blocks of `block_hours` and the peak hour (by start, local time; only an hour with at least two
    starts and more than any other); duration bands over lost_for, its median and longest over the incidents that
    lost probes;
    the median gap between starts; incidents closer than `cluster_gap_seconds` to the previous one, and the runs of
    them (clusters); 'about every N' when there are `regular_min_incidents` or more and the gaps' (max - min) /
    median is at most `regular_max_spread`."""
    items = sorted(incidents, key=lambda i: i.start)
    clock = _clock(tz)
    hours = Counter(clock.hour(i.start) for i in items)
    blocks = [(h, h + rules.block_hours, sum(n for hh, n in hours.items() if h <= hh < h + rules.block_hours))
              for h in range(0, 24, rules.block_hours)]
    peak = min(hours, key=lambda h: (-hours[h], h)) if hours else None
    if peak is not None and (hours[peak] < 2 or sum(1 for n in hours.values() if n == hours[peak]) > 1):
        peak = None                    # one incident, or a tie: no hour stands out
    lost = sorted(i.lost_for for i in items)
    lossy = [v for v in lost if v > 0]
    edges = rules.duration_bands
    names = [f"under {edges[0]:g} s"] + [f"{a:g}-{b:g} s" for a, b in zip(edges, edges[1:])] + [f"{edges[-1]:g} s or more"]
    counts = [0] * len(names)
    for v in lost:
        counts[bisect.bisect_right(edges, v)] += 1
    gaps = [b.start - a.start for a, b in zip(items, items[1:])]
    close = [g < rules.cluster_gap_seconds for g in gaps]
    clusters, run, largest = 0, 1, 1 if items else 0
    for flag in close + [False]:
        if flag:
            run += 1
        else:
            if run >= 2:
                clusters += 1
            largest = max(largest, run)
            run = 1
    med_gap = median(gaps) if gaps else None
    regular = None
    if len(items) >= rules.regular_min_incidents and med_gap and (max(gaps) - min(gaps)) / med_gap <= rules.regular_max_spread:
        regular = med_gap
    firsts = Counter(i.shape.first_hop for i in items if i.shape.first_hop)
    return Patterns(
        count=len(items), per_day_avg=len(items) / max(1.0, window_seconds / DAY), blocks=blocks, peak_hour=peak,
        shapes=dict(Counter(i.shape.kind for i in items)),
        first_hops=sorted(firsts.items(), key=lambda kv: (-kv[1], kv[0]))[:3],
        bands=list(zip(names, counts)),
        median_lost=median(lossy) if lossy else None, longest_lost=lossy[-1] if lossy else None,
        median_gap=med_gap, close_together=sum(close), clusters=clusters, largest_cluster=largest,
        about_every=regular, with_loss=len(lossy), peak_count=hours[peak] if peak is not None else 0,
    )


# --- rule: events that coincided with an incident -------------------------------------------------------------------

def match_events(span: Tuple[float, float], events: Sequence[Any], kinds: Mapping[str, Any], cause: str,
                 starts: Sequence[float], max_length: float, max_reach: float,
                 sources: Mapping[str, str]) -> List[MatchedEvent]:
    """The events (ModuleEvent shape: code, start, end, subject, value; in start order, `starts` their starts) that
    coincide with an incident spanning `span`: an event counts when event.start <= span end + reach and event.end >=
    span start - reach, with the reach of its kind (reach_seconds). Events whose code has no kind are ignored (they
    cannot say how far they reach). One MatchedEvent per (code, subject), those that corroborate the incident's
    cause (kind.supports holds the cause category) first, then the most frequent, then by code."""
    lo, hi = span
    first = bisect.bisect_left(starts, lo - max_length - max_reach)
    last = bisect.bisect_right(starts, hi + max_reach)
    groups: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    for ev in events[first:last]:
        kind = kinds.get(ev.code)
        if kind is None:
            continue
        if ev.start <= hi + kind.reach_seconds and ev.end >= lo - kind.reach_seconds:
            groups[(ev.code, ev.subject or "")].append(ev)
    out = []
    for (code, subject), evs in groups.items():
        kind = kinds[code]
        values = [e.value for e in evs if e.value is not None]
        out.append(MatchedEvent(
            code=code, phrase=kind.phrase, subject=subject, value=max(values) if values else None,
            value_total=sum(values) if values else None, count=len(evs), supports=frozenset(kind.supports),
            source=sources.get(code, "module"), corroborates=cause in kind.supports,
            gap_s=min((event_gap(e, span) for e in evs), key=abs)))
    out.sort(key=lambda m: (not m.corroborates, -m.count, m.code, m.subject))
    return out


def event_gap(event: Any, span: Tuple[float, float]) -> float:
    """How far an event lies from an incident spanning `span`, in seconds: > 0 before it, < 0 after it, 0 when they
    overlap or are less than a minute apart. A change (an event with `at`, the moment it was saved or found) is
    measured from that moment, not from the hour it counts for."""
    lo, hi = span
    at = getattr(event, "at", None)
    first, last = (at, at) if at is not None else (event.start, event.end)
    gap = lo - last if last < lo else (hi - first if first > hi else 0.0)
    return gap if abs(gap) >= 60.0 else 0.0


def event_summaries(events: Sequence[Any], kinds: Mapping[str, Any], sources: Mapping[str, str],
                    spans: Sequence[Tuple[float, float, Optional[str]]], start: float, end: float
                    ) -> Dict[str, "EventSummary"]:
    """Per module event code, its events in [start, end] and how many coincided with an incident (by the rule of
    match_events), with where loss started in those incidents. `spans`: (from, to, first failing hop) of the network
    incidents, expected ones included (a busy line coincides with the incidents it explains)."""
    out: Dict[str, Any] = {}
    for ev in events:
        if sources.get(ev.code) != "module" or ev.end < start or ev.start > end:
            continue
        kind = kinds[ev.code]
        reach = kind.reach_seconds
        acc = out.setdefault(ev.code, [0, 0, Counter()])
        acc[0] += 1
        for lo, hi, first in spans:
            if ev.start <= hi + reach and ev.end >= lo - reach:
                acc[1] += 1
                acc[2][first] += 1
                break
    return {code: EventSummary(code, getattr(kinds[code], "label", "") or code, n, k,
                               sorted(c.items(), key=lambda kv: (-kv[1], kv[0] or "")))
            for code, (n, k, c) in out.items()}


def busy_latency(events: Sequence[Any], kinds: Mapping[str, Any], target_id: Optional[str],
                 rows: Sequence[MinuteRow]) -> Optional["BusyLatency"]:
    """The median of `rows`' minute averages (the first public hop, in the window) in the minutes an event whose kind
    supports "load" ran, and in the others. None without such an event or such a hop."""
    spans = [(e.start, e.end) for e in events if "load" in getattr(kinds.get(e.code), "supports", ())]
    if not spans or target_id is None:
        return None
    busy = {m for a, b in spans for m in range(int(a // 60), int(b // 60) + 1)}
    inside = [r.avg for r in rows if r.avg is not None and r.minute in busy]
    other = [r.avg for r in rows if r.avg is not None and r.minute not in busy]
    if not inside:
        return None
    return BusyLatency(target_id, median(inside), median(other) if other else None, len(inside))


# --- rule: confidence and score ------------------------------------------------------------------------------------

def confidence(*, resolution: str, expected: bool, shape: Shape, first_category: Optional[str], cause: str,
               corroborated: bool, kind: Optional[str] = None, duration: Optional[float] = None,
               latency: Optional[Shape] = None, latency_category: Optional[str] = None,
               rules: Rules = RULES) -> Tuple[str, str]:
    """(level, reason). Checked in this order:
    low     because the episode was read from minute data or nothing ("minute_data"); or the incident is expected
            ("expected");
    then, for a latency verdict (LATENCY_KINDS) with no hop isolated by loss, the rule for latency (`latency`: where
    it rises, latency_start; `latency_category`: that hop's role):
    low     it lasted under `latency_min_seconds` ("brief"); no hop's peak was clearly raised ("no_rise", not for a DNS
            or load verdict, which do not rest on the path); only one provider hop shows it ("one_hop"); or it rises
            from a hop whose role is not the place the verdict points at ("place");
    and for the others, the rule for loss:
    low     only one hop shows it ("one_hop"); or the shape does not agree with the verdict: loss
            starts at a hop whose role is not the place the verdict points at ("place": PLACE_ROLE; a dns, load or
            latency verdict has no such place, so loss carried along the path disagrees with it), or no hop is
            isolated for a verdict about loss ("no_hop");
    high    1 s data, the shape agrees with the verdict, and a coinciding event supports the cause ("corroborated");
    medium  otherwise ("uncorroborated").
    A verdict that does not rest on where loss starts (DNS, load, latency: NO_PLACE_CAUSES, LATENCY_KINDS) agrees with
    "no hop isolated": the pings along the path were fine, which is what it says."""
    if resolution != "1s":
        return "low", "minute_data"
    if expected:
        return "low", "expected"
    if kind in LATENCY_KINDS and shape.first_hop is None:
        if duration is not None and duration < rules.latency_min_seconds:
            return "low", "brief"
        if cause not in NO_PLACE_CAUSES:
            if latency is None or latency.first_hop is None:
                return "low", "no_rise"
            if latency.kind == "one_hop":
                return "low", "one_hop"
            if PLACE_ROLE.get(cause) is not None and PLACE_ROLE[cause] != latency_category:
                return "low", "place"
        return ("high", "corroborated") if corroborated else ("medium", "uncorroborated")
    if shape.kind == "one_hop":
        return "low", "one_hop"
    if shape.first_hop is None:
        if not (cause in NO_PLACE_CAUSES or kind in LATENCY_KINDS):
            return "low", "no_hop"
    elif PLACE_ROLE.get(cause) != first_category:
        return "low", "place"
    if corroborated:
        return "high", "corroborated"
    return "medium", "uncorroborated"


def score(lost_for: float, shape: Shape, severity: str, rules: Rules = RULES, duration: float = 0.0) -> float:
    """Probes lost for x the shape's weight (carried 1.0, none 0.3, one hop 0.2) x `critical_weight` if critical.
    An incident without lost probes (raised on latency) counts `latency_weight` x its duration instead, so a long
    latency incident is not ranked with nothing. Ranking puts expected incidents last and breaks ties by the newest
    (see rank_incidents)."""
    base = lost_for if lost_for > 0 else rules.latency_weight * max(0.0, duration)
    return base * rules.shape_weight[shape.kind] * (rules.critical_weight if severity == "critical" else 1.0)


def rank_incidents(incidents: Sequence[IncidentFact]) -> None:
    """Sets `rank` (1 = most significant): not expected first, then the higher score, then the newer."""
    ordered = sorted(incidents, key=lambda i: (i.expected, -i.score, -i.start))
    for n, inc in enumerate(ordered, 1):
        inc.rank = n


# --- the core's own events ------------------------------------------------------------------------------------------

def core_kinds(rules: Rules = RULES) -> Dict[str, CoreEventKind]:
    """A path or settings change matters from when it was saved on: its event spans the `change_reach_seconds` after
    it, so it coincides with the incidents of the minutes after it, not with the ones before or the ones much later
    (it acts at once: one saved 40 minutes before an incident did not cause it). A route change is found later than
    it happened, so it reaches `core_event_reach_seconds` either way."""
    return {
        "path.changed": CoreEventKind("path.changed", "path changed", rules.change_slack_seconds,
                                      label="path changes"),
        "settings.changed": CoreEventKind("settings.changed", "settings changed", rules.change_slack_seconds,
                                          label="settings changes"),
        "route.changed": CoreEventKind("route.changed", "route changed", rules.core_event_reach_seconds,
                                       label="route changes"),
        "nodata": CoreEventKind("nodata", "no data recorded", rules.nodata_reach_seconds, label="no-data gaps"),
    }


def no_data_gaps(minutes: Iterable[int], end: float, rules: Rules = RULES) -> List[Tuple[float, float]]:
    """Stretches of at least `nodata_min_seconds` between two minutes that have a probe (of any target), and from the
    last such minute to `end`: the monitoring computer was off or the app stopped. Not before the first probe."""
    ordered = sorted(set(minutes))
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        if (b - a - 1) * 60.0 >= rules.nodata_min_seconds:
            gaps.append(((a + 1) * 60.0, b * 60.0))
    if ordered and end - (ordered[-1] + 1) * 60.0 >= rules.nodata_min_seconds:
        gaps.append(((ordered[-1] + 1) * 60.0, end))
    return gaps


async def _change_events(repository, start: float, end: float, rules: Rules) -> List[Dict[str, Any]]:
    """Path and settings versions applied (not revision 1, the import) and a route change found, as events. Names and
    summaries are left out: when and what kind only."""
    out = []
    for kind, versions in (("path.changed", await repository.path_versions()),
                           ("settings.changed", await repository.settings_versions())):
        for v in versions:
            if v["revision"] > 1:
                out.append({"code": kind, "at": float(v["applied_at"])})
    drift = await repository.load_path_drift() or {}
    if drift.get("state") == "checked" and drift.get("confirmed") and isinstance(drift.get("good_at"), (int, float)):
        out.append({"code": "route.changed", "at": float(drift["good_at"])})   # found then; the change itself is older
    reach = rules.core_event_reach_seconds
    return sorted((e for e in out if start - reach <= e["at"] <= end + reach), key=lambda e: e["at"])


# --- collecting ----------------------------------------------------------------------------------------------------

def _service_outages(rows: Sequence[Mapping[str, Any]], now: float) -> Dict[str, Tuple[int, float]]:
    """Host-app outages (the services feature's incidents) per app id: how many started in the window, and how long
    they lasted in all."""
    out: Dict[str, List[float]] = {}
    for r in rows:
        try:
            ids = [t for t in json.loads(r["affected_targets"] or "[]") if isinstance(t, str)]
        except (TypeError, ValueError):
            ids = []
        seconds = r["duration_seconds"] if r["duration_seconds"] is not None else max(0.0, now - r["start_time"])
        for sid in ids:
            acc = out.setdefault(sid, [0, 0.0])
            acc[0] += 1
            acc[1] += seconds
    return {k: (int(n), secs) for k, (n, secs) in out.items()}


def _kinds_map(kinds: Any) -> Dict[str, Any]:
    if not kinds:
        return {}
    return dict(kinds) if isinstance(kinds, Mapping) else {k.code: k for k in kinds}


class _LazyMinutes:
    """{target: {minute: MinuteRow}} built per target when first asked for (a week of rows is 100k dict entries, and
    most packs have no close-up that reads them)."""

    def __init__(self, per_target: Mapping[str, List[MinuteRow]]):
        self._rows, self._built = per_target, {}

    def get(self, target: str, default: Any = None) -> Any:
        if target not in self._rows:
            return default
        if target not in self._built:
            self._built[target] = {r.minute: r for r in self._rows[target]}
        return self._built[target]


def _closeup(inc: IncidentFact, rows_by_target: Mapping[str, Mapping[int, MinuteRow]], order: List[str],
             categories: Mapping[str, str], rules: Rules) -> CloseUp:
    """The columns: the hop before the first failing hop (none isolated: the first hop of the verdict's place), that
    hop, the first public and the first DNS probe (then the first hops, up to `closeup_columns`). The rows: the minutes from `closeup_minutes_around` before
    to after the incident; more than `closeup_max_rows`: the lossiest are kept, in time order."""
    cols: List[str] = []
    first = inc.shape.first_hop
    if first is None:     # no hop isolated (latency, DNS): the first hop of the place the verdict points at
        first = next((t for t in order if categories[t] == PLACE_ROLE.get(inc.cause)), None)
    if first in order:
        i = order.index(first)
        cols += ([order[i - 1]] if i else []) + [first]
    for role in ("public", "dns"):
        pick = next((t for t in order if categories[t] == role), None)
        if pick and pick not in cols:
            cols.append(pick)
    cols = (cols + [t for t in order if t not in cols])[:rules.closeup_columns]
    cols.sort(key=order.index)
    pad = rules.closeup_minutes_around
    m0, m1 = int(inc.start // 60) - pad, int(inc.end // 60) + pad
    minutes = []
    for m in range(m0, m1 + 1):
        cells = {}
        for t in cols:
            r = rows_by_target.get(t, {}).get(m)
            if r is not None:
                cells[t] = (round((r.n - r.ok) * 100.0 / r.n, 1) if r.n else 0.0, r.avg)
        if cells:
            minutes.append((m * 60.0, cells))
    cut = max(0, len(minutes) - rules.closeup_max_rows)
    if cut:
        keep = sorted(sorted(range(len(minutes)), key=lambda k: (-max(c[0] for c in minutes[k][1].values()), k))
                      [:rules.closeup_max_rows])
        minutes = [minutes[k] for k in keep]
    return CloseUp(incident_id=inc.id, columns=cols, minutes=minutes, minutes_cut=cut)


async def _rows_10s(repository, inc: IncidentFact, cols: List[str], rules: Rules):
    """The incident scope's 10-second rows (loss %, average ms per column) around the incident, the lossiest kept."""
    pad = rules.episode_seconds_around_10s
    rows, _ = await repository.get_history_buckets(inc.start - pad, inc.end + pad, cols, 10)
    by: Dict[float, Dict[str, Tuple[float, Optional[float]]]] = defaultdict(dict)
    for r in rows:
        by[r["timestamp"]][r["target_id"]] = (r.get("packet_loss_pct") or 0.0, r.get("latency_ms"))
    stamps = sorted(by)
    if len(stamps) > rules.closeup_rows_10s:
        keep = sorted(sorted(stamps, key=lambda s: (-max(c[0] for c in by[s].values()), s))[:rules.closeup_rows_10s])
        stamps = keep
    return [(s, by[s]) for s in stamps]


async def collect_pack_facts(repository, config, *, start: float, end: float, events: Sequence[Any] = (),
                             event_kinds: Any = (), ignoring: Iterable[str] = (), detail: str = "compact",
                             scope: str = "window", incident_id: Optional[int] = None, tz_offset_minutes: int = 0,
                             now: Optional[float] = None, rules: Rules = RULES,
                             tz: Optional[tzinfo] = None) -> PackFacts:
    """Everything the pack's core states, decided from the stored data for the window [start, end].

    `events`: the modules' ModuleEvents for the window (objects with code, start, end, subject, value);
    `event_kinds`: their EventKinds (objects with code, phrase, supports, reach_seconds), a mapping by code or an
    iterable. Both are only read, by attribute: this module imports nothing from the modules.
    `ignoring`: the hops that ignore ping now (engine.path_rules); `detail`: "compact" or "detailed" (the caps);
    `scope`: "window", or "incident" with `incident_id` (that incident is first in the close-ups and gets the 10-second
    rows; `start`..`end` is the window around it). `tz`: the local time zone (a named zone follows its daylight-saving
    changes); without it, the fixed offset `tz_offset_minutes`, east positive."""
    now = time.time() if now is None else now
    tz = tz or timezone(timedelta(minutes=tz_offset_minutes))
    clock = LocalClock(tz)
    ignoring = frozenset(ignoring)
    material = material_loss_points(config)
    classifier = getattr(config, "classifier", None)
    degraded_pct = getattr(classifier, "degraded_loss_pct", 15.0)
    busy_pct = getattr(classifier, "busy_load_pct", 60.0)

    cfg_targets = [t for t in _targets(config) if getattr(t, "enabled", True) is not False]
    labels = role_labels(cfg_targets)
    order = [t.id for t in cfg_targets]
    categories = {t.id: category_of(t) for t in cfg_targets}
    path = [(t, categories[t]) for t in order]

    # 1. the hops: the baseline window's minute rows, once; the window's are a slice of them
    base_start = end - rules.baseline_days * DAY
    rows = await minute_rows(repository, base_start, end)
    per_target: Dict[str, List[MinuteRow]] = defaultdict(list)
    for r in rows:
        if r[1] in categories:
            per_target[r[1]].append(r)
    w0, w1 = int(start // 60), int(end // 60)
    hops = []
    for t in order:
        rs = per_target.get(t)
        if not rs:
            continue
        base = hop_baseline(rs, rules)
        win = hop_window_vs_normal([r for r in rs if w0 <= r[0] <= w1], base, material, rules)
        hops.append(HopFact(t, categories[t], labels[t], t in ignoring, base, win))
    by_minute = _LazyMinutes(per_target)   # only the incidents' close-ups look minutes up

    # 2. the incidents and their episodes (one batched query)
    raw = await incident_rows(repository, start, end)
    # host-app outages are not the network's; by kind once the services feature registered it, by cause always
    network = [r for r in raw if not is_service_kind(r["kind"])
               and incident_cause(r["kind"], r["cause_code"], r.get("description")) != "service"]
    peaks: Dict[Any, Dict[str, float]] = {}
    episodes = await episode_loss(repository, [(r["id"], r["start_time"], r["end_time"] if r["end_time"] is not None
                                                else now) for r in network], peaks)
    normals = {h.target_id: h.baseline.normal_ms for h in hops}

    # 3. events: the modules', the core's changes and its no-data gaps, in start order
    kinds = core_kinds(rules)
    sources = {c: "core" for c in kinds}
    kinds.update(_kinds_map(event_kinds))
    for code in list(kinds):
        sources.setdefault(code, "module")
    changes = await _change_events(repository, start, end, rules)
    gaps = [g for g in no_data_gaps((r.minute for r in rows), end, rules) if g[1] > start and g[0] < end]
    all_events = list(events) + [SimpleNamespace(
        code=c["code"], start=c["at"], at=c["at"], subject="", value=None,
        end=c["at"] + (rules.change_reach_seconds if c["code"] != "route.changed" else 0.0)) for c in changes]
    all_events += [SimpleNamespace(code="nodata", start=a, end=b, subject="", value=None) for a, b in gaps]
    all_events = [e for e in all_events if e.code in kinds]
    all_events.sort(key=lambda e: e.start)
    starts = [e.start for e in all_events]
    max_len = max((e.end - e.start for e in all_events), default=0.0)
    max_reach = max((k.reach_seconds for k in kinds.values()), default=0.0)

    # 4. one fact per incident
    facts: List[IncidentFact] = []
    spans: Dict[int, Tuple[float, float]] = {}
    for r in network:
        inc_end = r["end_time"] if r["end_time"] is not None else now
        resolution, hop_losses = episodes[r["id"]]
        shape = path_shape(path, hop_losses, ignoring, material, degraded_pct, rules)
        cause = incident_cause(r["kind"], r["cause_code"], r.get("description"))
        lo = min(r["start_time"], r["loss_start"]) if r["loss_start"] is not None else r["start_time"]
        hi = inc_end
        if r["loss_start"] is not None and r["loss_seconds"]:
            hi = max(inc_end, r["loss_start"] + r["loss_seconds"])
        matched = match_events((lo, hi), all_events, kinds, cause, starts, max_len, max_reach, sources)
        spans[r["id"]] = (lo, hi)
        duration = r["duration_seconds"] if r["duration_seconds"] is not None else max(0.0, now - r["start_time"])
        lost_for = r["loss_seconds"]
        if lost_for is None:   # not stored (an incident raised on latency names no lost target): from its episode,
            #                    the probes its first failing hop lost (none isolated: none lost on the path)
            sent, ok = hop_losses.get(shape.first_hop, (0, 0)) if shape.first_hop else (0, 0)
            lost_for = (sent - ok) * rules.probe_interval_seconds if resolution == "1s" else duration
        latency, normal_before = (latency_start(path, peaks.get(r["id"], {}), normals, ignoring, rules)
                                  if shape.kind == "none" else (None, []))
        level, why = confidence(resolution=resolution, expected=bool(r["expected"]), shape=shape,
                                first_category=categories.get(shape.first_hop), cause=cause,
                                corroborated=any(m.corroborates for m in matched), kind=r["kind"], duration=duration,
                                latency=latency, rules=rules,
                                latency_category=categories.get(latency.first_hop) if latency else None)
        facts.append(IncidentFact(
            id=r["id"], start=r["start_time"], end=inc_end, duration=duration, lost_for=lost_for, kind=r["kind"],
            cause=cause, cause_code=current_cause(r["cause_code"]), severity=r["severity"], title=r["title"] or "",
            expected=bool(r["expected"]), load_pct=r["load_pct"], resolution=resolution, shape=shape, events=matched,
            confidence=level, confidence_reason=why, score=score(lost_for, shape, r["severity"], rules, duration),
            latency=latency, latency_normal=normal_before, peaks=peaks.get(r["id"], {})))
    rank_incidents(facts)
    real = [i for i in facts if not i.expected]

    # 5. caps, close-ups
    n_rows, n_close = rules.incident_rows[detail], rules.closeups[detail]
    by_rank = sorted(facts, key=lambda i: i.rank)
    focus = next((i for i in facts if i.id == incident_id), None) if scope == "incident" else None
    if focus:
        n_close = 1
        by_rank = [focus] + [i for i in by_rank if i is not focus]
    shown = by_rank[:n_rows]
    listed = [i.id for i in sorted(shown, key=lambda i: i.start)]
    more = Counter(i.cause for i in by_rank[n_rows:])
    closeups = []
    for inc in by_rank[:n_close]:
        inc.hops = [{"target_id": t, "label": labels[t], "category": categories[t], "sent": s,
                     "lost": s - ok, "loss_pct": round((s - ok) * 100.0 / s, 1) if s else 0.0}
                    for t in order for s, ok in [episodes[inc.id][1].get(t, (0, 0))] if s]
        cu = _closeup(inc, by_minute, order, categories, rules)
        if scope == "incident" and inc is focus:
            cu.rows_10s = await _rows_10s(repository, inc, cu.columns, rules)
        closeups.append(cu)

    # 6. summaries over the unexpected incidents
    verdicts: Dict[str, Dict[str, int]] = {}
    coincided: Counter = Counter()
    for i in real:
        v = verdicts.setdefault(i.cause, {"low": 0, "medium": 0, "high": 0, "total": 0})
        v[i.confidence] += 1
        v["total"] += 1
        coincided.update({m.code for m in i.events})
    key_ids = [next((t for t in order if categories[t] == role and t in per_target), None) for role in KEY_ROLES]
    profile = []
    for t in filter(None, key_ids):
        acc = [[[], 0, 0] for _ in range(24 // rules.block_hours)]   # per block: averages, probes sent, probes lost
        for r in per_target[t]:
            a = acc[clock.hour(r.minute * 60.0) // rules.block_hours]
            if r.avg is not None:
                a[0].append(r.avg)
            a[1] += r.n
            a[2] += r.n - r.ok
        profile.append({"target_id": t, "label": labels[t], "blocks": [
            {"hours": (k * rules.block_hours, (k + 1) * rules.block_hours), "median_ms": median(a[0]) if a[0] else None,
             "loss_pct": round(a[2] * 100.0 / a[1], 2) if a[1] else None} for k, a in enumerate(acc)]})
    days = range(clock.day(start), clock.day(max(start, end - 1e-6)) + 1)
    per_day = [{"day": day_name(d),
                "incidents": sum(1 for i in real if clock.day(i.start) == d),
                "lost_minutes": round(sum(i.lost_for for i in real if clock.day(i.start) == d) / 60.0, 1)}
               for d in days]

    # 7. the window's first elevated hop (the dossier's rule, device-free)
    dossier = await collect_dossier_facts(repository, config, max(1, math.ceil((end - start) / 3600.0)), end,
                                          None, ignoring, start=start)
    first_public = next((t for t in order if categories[t] == "public" and t in per_target), None)
    incident_spans = sorted(((spans[i.id][0], spans[i.id][1], i.shape.first_hop) for i in facts), key=lambda t: t[0])
    summaries = event_summaries(all_events, kinds, sources, incident_spans, start, end)
    loaded = busy_latency(all_events, kinds, first_public,
                          [r for r in per_target.get(first_public, ()) if w0 <= r.minute <= w1])
    return PackFacts(
        start=start, end=end, now=now, scope=scope, focus_id=focus.id if focus else None, detail=detail,
        tz_offset_minutes=tz_offset_minutes, order=[h.target_id for h in hops], labels=labels, categories=categories,
        hops=hops, incidents=sorted(facts, key=lambda i: i.start), listed=listed, more=sorted(more.items()),
        closeups=closeups, patterns=patterns(real, end - start, clock, rules),
        expected_count=len(facts) - len(real), service_count=len(raw) - len(network),
        lost_minutes=round(sum(i.lost_for for i in real) / 60.0, 1), verdicts=verdicts, coincided=dict(coincided),
        events=[{"code": e["code"], "at": e["at"]} for e in changes] + [{"code": "nodata", "at": a, "until": b} for a, b in gaps],
        changes=[{"kind": e["code"].split(".")[0], "at": e["at"]} for e in changes if start <= e["at"] <= end],
        no_data=gaps, profile=profile, per_day=per_day,
        primary_hop=dossier.primary["target_id"] if dossier.primary else None,
        later_lossy=[h["target_id"] for h in dossier.later_lossy], max_lan_loss=dossier.max_lan_loss,
        rules={"slow_factor": rules.slow_factor, "slow_extra_ms": rules.slow_extra_ms,
               "notable_median_factor": rules.notable_median_factor, "notable_slow_share": rules.notable_slow_share,
               "notable_loss_factor": rules.notable_loss_factor, "material_loss_points": material,
               "degraded_loss_pct": degraded_pct, "busy_load_pct": busy_pct, "baseline_days": rules.baseline_days,
               "cluster_gap_seconds": rules.cluster_gap_seconds},
        event_summaries=summaries, busy_latency=loaded,
        labels_of_events={c: getattr(k, "label", "") or c for c, k in kinds.items()},
        service_outages=_service_outages([r for r in raw if r not in network], now),
        load_codes=tuple(sorted(c for c, k in kinds.items() if "load" in getattr(k, "supports", ()))),
        tz=tz,
    )
