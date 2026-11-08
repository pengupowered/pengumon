"""Outages: drops of the line that belong together.

An incident is the raw record of one drop. A flapping line, or one provider event, becomes several incidents: the line
drops, comes back for a few minutes (longer than "Recovery confirmed after", so the incident ends), and drops again. An
outage joins such drops. Incidents are not changed and nothing is stored: outages are computed on every read from the
incidents and the settings (incidents.group_gap_minutes), so the history regroups when the setting changes.

The rule (pure, no I/O; every function is told the time):

* Groupable: a network incident that is not `expected` and whose cause category (taxonomy.incident_cause) is one of
  the path places router, gateway, line, isp, internet, or dns. The rest (load, unknown, service) is never grouped.
* Places are linked when neighbors on the path (router gateway line isp internet; indexes at most 1 apart); dns is
  linked to line, isp, internet and dns.
* In start order, an incident joins the current outage when it starts no later than `gap` after the outage's latest end
  (a member still open ends "now": it is still going) and its place is linked to the place of at least one member.
  Otherwise it starts a new outage. A gap of 0 turns grouping off: every groupable incident is an outage of one.

`OutageBuilder` is the one implementation of the joining rule; `group_outages` (reads) and the Notifier (alerts, one
outage live at a time) both use it.
"""

from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pengumon.incidents.taxonomy import incident_cause
from pengumon.timeofday import in_window, zone

PATH_PLACES = ("router", "gateway", "line", "isp", "internet")
GROUPABLE_PLACES = frozenset(PATH_PLACES) | {"dns"}
_PATH_INDEX = {p: i for i, p in enumerate(PATH_PLACES)}
_DNS_LINKS = frozenset({"line", "isp", "internet", "dns"})
_MAINTENANCE_PLACES = frozenset({"line", "isp"})
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_PLACE_ORDER = {**_PATH_INDEX, "dns": len(PATH_PLACES)}


def linked(a: str, b: str) -> bool:
    """Whether two places are neighbors on the path (so drops at both may be one event)."""
    if a == "dns":
        return b in _DNS_LINKS
    if b == "dns":
        return a in _DNS_LINKS
    return abs(_PATH_INDEX[a] - _PATH_INDEX[b]) <= 1


@dataclass(frozen=True)
class Rules:
    """How drops are joined and labeled, from the `incidents:` settings and the time zone."""
    gap_seconds: float = 600.0
    maintenance_start: Optional[str] = "00:00"
    maintenance_end: Optional[str] = "06:00"
    tz: Optional[tzinfo] = None

    @property
    def grouping(self) -> bool:
        return self.gap_seconds > 0

    @classmethod
    def from_config(cls, config) -> "Rules":
        """The rules of the config now (read on every call: both settings apply live)."""
        inc = getattr(config, "incidents", None)
        gap = (getattr(inc, "group_gap_minutes", 10) if inc is not None else 10) * 60.0
        start = getattr(inc, "maintenance_start", "00:00") if inc is not None else "00:00"
        end = getattr(inc, "maintenance_end", "06:00") if inc is not None else "06:00"
        return cls(gap, start, end, zone(getattr(getattr(config, "general", None), "timezone", None)))

    def in_maintenance_window(self, epoch: float) -> bool:
        if not self.maintenance_start or not self.maintenance_end or self.tz is None:
            return False
        return in_window(epoch, self.maintenance_start, self.maintenance_end, self.tz)


@dataclass(frozen=True)
class Drop:
    """What the rule needs of an incident. end None: still open."""
    id: int
    start: float
    end: Optional[float]
    severity: str
    place: str
    title: str
    cause_code: str


def _reader(obj: Any):
    """A function that reads a field of an Incident or of a plain row."""
    if isinstance(obj, dict):
        return obj.get
    return lambda name, default=None: getattr(obj, name, default)


def place_of(incident: Any) -> str:
    """The cause category of an incident (Incident or a plain row)."""
    get = _reader(incident)
    return incident_cause(get("kind"), get("cause_code") or "", get("description"))


def drop_of(incident: Any) -> Optional[Drop]:
    """The Drop of a groupable incident (an Incident or a row as the repository returns it), else None."""
    get = _reader(incident)
    if get("expected"):
        return None
    iid = get("id")
    place = incident_cause(get("kind"), get("cause_code") or "", get("description"))
    if place not in GROUPABLE_PLACES or iid is None:
        return None
    return Drop(iid, float(get("start_time")), get("end_time"), get("severity") or "info", place,
                get("title") or "", get("cause_code") or "")


def _union_seconds(intervals: List[Tuple[float, float]]) -> float:
    total, cur_start, cur_end = 0.0, None, None
    for a, b in sorted(intervals):
        if cur_end is None or a > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = a, b
        elif b > cur_end:
            cur_end = b
    if cur_end is not None:
        total += cur_end - cur_start
    return total


class OutageBuilder:
    """One outage under construction: add drops in start order; `joins` says whether the next one belongs."""

    def __init__(self, first: Drop):
        self.members: List[Drop] = [first]
        self.places = {first.place}
        self._closed_end = first.end if first.end is not None else float("-inf")  # latest end of the closed members
        self._open = 0 if first.end is not None else 1

    @property
    def id(self) -> int:
        return self.members[0].id

    @property
    def start(self) -> float:
        return self.members[0].start

    @property
    def ongoing(self) -> bool:
        return self._open > 0

    def latest_end(self, now: float) -> float:
        """The latest end of a member; an open one ends now."""
        return max(self._closed_end, now) if self._open else self._closed_end

    def joins(self, drop: Drop, gap_seconds: float, now: float) -> bool:
        if gap_seconds <= 0 or drop.start > self.latest_end(now) + gap_seconds:
            return False
        return drop.place in self.places or any(linked(drop.place, p) for p in self.places)

    def add(self, drop: Drop) -> None:
        self.members.append(drop)
        self.places.add(drop.place)
        if drop.end is None:
            self._open += 1
        elif drop.end > self._closed_end:
            self._closed_end = drop.end

    def replace(self, drop: Drop) -> None:
        """A member's incident changed (it ended, or escalated): the new reading takes its place."""
        for i, m in enumerate(self.members):
            if m.id == drop.id:
                self.members[i] = drop
                self.places = {x.place for x in self.members}
                ends = [x.end for x in self.members if x.end is not None]
                self._closed_end = max(ends, default=float("-inf"))
                self._open = len(self.members) - len(ends)
                return

    def summary(self, now: float, rules: Rules) -> Dict[str, Any]:
        """The outage's fields (docs/api.md, GET /api/outages)."""
        members = self.members
        ongoing = self.ongoing
        end_time = None if ongoing else max(m.end for m in members)
        stop = now if ongoing else end_time
        gap = rules.gap_seconds

        def span(m: Drop) -> Tuple[float, float]:
            return m.start, (now if m.end is None else m.end)

        critical = [m for m in members if m.severity == "critical"]
        pool = critical or members
        main = max(pool, key=lambda m: (span(m)[1] - span(m)[0], -m.start))
        places = sorted(self.places, key=_PLACE_ORDER.__getitem__)
        likely = (bool(rules.maintenance_start) and self.places <= _MAINTENANCE_PLACES
                  and rules.in_maintenance_window(self.start))
        return {
            "id": self.id,
            "start_time": self.start,
            "end_time": end_time,
            "ongoing": ongoing,
            "settling": (not ongoing) and gap > 0 and now - end_time < gap,
            "duration_seconds": max(0.0, stop - self.start),
            "down_seconds": _union_seconds([span(m) for m in critical]),
            "affected_seconds": _union_seconds([span(m) for m in members]),
            "drops": len(members),
            "critical_drops": len(critical),
            "severity": max((m.severity for m in members), key=lambda s: _SEVERITY_RANK.get(s, 0)),
            "places": places,
            "title": main.title,
            "cause_code": main.cause_code,
            "likely_maintenance": likely,
            "incident_ids": [m.id for m in members],
        }


def build_outages(incidents: Iterable[Any], rules: Rules, now: float) -> List[OutageBuilder]:
    """The outages of `incidents` (any scope, expected or not: only the groupable ones count), oldest first."""
    drops = [d for d in map(drop_of, incidents) if d is not None]
    drops.sort(key=lambda d: (d.start, d.id))
    outages: List[OutageBuilder] = []
    current: Optional[OutageBuilder] = None
    gap = rules.gap_seconds
    for d in drops:
        if current is not None and current.joins(d, gap, now):
            current.add(d)
        else:
            current = OutageBuilder(d)
            outages.append(current)
    return outages


def group_outages(incidents: Iterable[Any], rules: Rules, now: float) -> List[Dict[str, Any]]:
    """The outage summaries of `incidents`, oldest first."""
    return [o.summary(now, rules) for o in build_outages(incidents, rules, now)]


def outage_ids(incidents: Sequence[Any], rules: Rules, now: float) -> Dict[int, int]:
    """Incident id -> outage id for every groupable incident in `incidents`."""
    out: Dict[int, int] = {}
    for o in build_outages(incidents, rules, now):
        for m in o.members:
            out[m.id] = o.id
    return out
