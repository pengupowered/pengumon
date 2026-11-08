"""The outage-rules replay: the stored raw probe samples run again through the real classifier with candidate rules.

`python -m pengumon.engine.replay` is the subprocess the Settings preview starts (pengumon.settings.preview): one JSON
document on stdin (see `run_replay` for its keys), JSON lines on stdout: `{"progress": 0.42}` while it runs, then one
`{"result": {...}}`. It runs at a low priority in a process of its own because a replay of a week of samples takes
about fifteen seconds of CPU (over two minutes before the calm rounds were skipped), and a thread would hold the GIL
against the probe rounds, whose timing is measured in user space.

What it reuses, so that "the same rules" give "the same incidents" (tests/test_replay.py proves it against the live
classifier on the characterization scenario):

* the prober's targets (`NetworkProber.stats`, for their order, names and categories), kept up by `_Target.add`, which
  does the part of `NetworkProber._update_target_stats` that a verdict can read: the last probes (as many as the longer
  of the two rule sets' windows, `classifier.window_probes`) and the "down" status that `ignoring_ping` reads, judged
  over each rule set's own window, as the live prober would judge it under those rules. The averages, the jitter and
  the lifetime counters, which no verdict reads, are not kept (tests/test_replay.py checks `_Target` against the prober
  probe by probe);
* `ignoring_ping` and `round_metrics` once per round per window (one call when both rule sets judge the same number
  of probes);
* `ClassifierCore.step` once per round per rule set, each with its own tracker and DNS-cooldown state and no repository;
* the load constants of engine/load.py, applied to the stored WAN rates of the router;
* the speed tests stored in the window (`speed_tests`): a round inside a test, or in the tail the judged window still
  holds of it, is judged with `testing=True`, as the live classifier is (`SpeedMask`, pengumon.speedtest.mask);
* the notifier's `AlertPolicy` (notify/policy.py) to count the alerts each rule set's incidents would have sent, with
  the drops joined into outages as the notifier joins them (`incidents.group_gap_minutes`, the running config's: the
  preview does not change it): one alert per outage.

How the samples become rounds: they are read in ts order, one hour at a time (`samples` is `(ts, target)` WITHOUT
ROWID, so an hour is one primary-key range). A new round starts when a target id repeats or the time jumps by more than
half a round. A round is classified at its latest sample's time. A gap of more than 30 s between rounds (the app was
down) resolves an open incident at its last bad round, as a shutdown does, and starts over with empty windows, as a
restart does.
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import quote

from pengumon.db.migrations.v014_path_versions import BASELINE_SUMMARY
from pengumon.engine import load as load_module
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.load import LineLoad, LoadReading, Rate
from pengumon.engine.load_settings import LineConfig
from pengumon.engine.prober import NetworkProber
from pengumon.engine.prober_settings import DEFAULT_WINDOW_PROBES, DnsProbeConfig, HopStatusConfig
from pengumon.incidents.model import Incident
from pengumon.incidents.outages import GROUPABLE_PLACES, Drop, OutageBuilder, place_of
from pengumon.incidents.settings import IncidentsConfig
from pengumon.incidents.taxonomy import kinds_in_scope
from pengumon.lines import wording
from pengumon.engine.path_rules import ignoring_ping
from pengumon.models.probes import TargetConfig
from pengumon.notify.policy import AlertPolicy
from pengumon.notify.settings import AlertsConfig
from pengumon.speedtest.mask import SpeedMask, mask_tail_seconds
from pengumon.timeofday import zone

GAP_SECONDS = 30.0  # more than this between two rounds: the app was not running
CHUNK_SECONDS = 3600.0  # the samples are read one hour at a time
MAX_CHANGES = 30  # incidents that differ, listed in the result
MISSING_LOAD_NOTE_FRACTION = 0.05  # say so when more than this share of the rounds had no load reading
RULE_SETS = ("current", "draft")


class _Sample(NamedTuple):
    """What `round_metrics` reads of a ProbeSample (a tuple: a week of rounds makes six million of them)."""
    target_id: str
    timestamp: float
    success: bool
    latency_ms: Optional[float]


class _Target:
    """What the classifier reads of a TargetStats: the last `keep` probes (`recent_latencies`, None for a lost one), the
    path flags and the identity, and the "down" status. `add` is NetworkProber._update_target_stats reduced to that:
    the window holds latency or None (a probe that succeeded without a latency counts as lost). `judge` sets the status
    over a rule set's window: "down" when more than `down_fraction` of its last probes is lost, else "up" (the
    prober's "degraded" is not "down" either, which is all `ignoring_ping` asks)."""

    __slots__ = ("target_id", "name", "category", "address", "enabled", "recent_latencies", "status", "ignores_ping",
                 "ipv6_unavailable", "hot", "calm_below", "keep")

    def __init__(self, stat, calm_below: float = 0.0, keep: int = DEFAULT_WINDOW_PROBES):
        self.calm_below = calm_below
        self.keep = keep  # the longest window a rule set judges
        self.target_id, self.name, self.category = stat.target_id, stat.name, stat.category
        self.address, self.enabled = stat.address, stat.enabled
        self.recent_latencies: List[Optional[float]] = []
        self.status = "up"
        self.ignores_ping = False
        self.ipv6_unavailable = False
        self.hot = 0  # probes still to come until the last `keep` are all answered and under the calm level

    def add(self, latency: Optional[float]) -> None:
        """Adds a probe. A latency under `calm_below` (and answered) is calm; see `_calm_below`."""
        window = self.recent_latencies
        window.append(latency)
        if len(window) > self.keep:
            del window[0]
        if latency is None or latency >= self.calm_below or latency < 0:
            self.hot = self.keep
        elif self.hot:
            self.hot -= 1

    def judge(self, window: int, down_fraction: float) -> None:
        """The status over the last `window` probes (the prober's, with a rule set's `window_probes`)."""
        judged = self.recent_latencies[-window:]
        self.status = "down" if judged and judged.count(None) / len(judged) > down_fraction else "up"


SKIP_CALM_ROUNDS = True  # tests turn it off to prove the skipping changes nothing
CALM_MARGIN_MS = 0.01  # the metrics are rounded to 0.01 ms; stay clear of that


def _calm_below(config: ClassifierConfig) -> Dict[str, float]:
    """Per category, a latency (ms) under which probes cannot make `diagnose` find a fault: with no probe lost, the
    category's median latency, and its jitter (a mean of median differences, at most the largest latency), cannot pass
    any latency or jitter level of the config, and its loss is 0. For latencies >= 0. Categories the metrics don't
    read: no limit."""
    public = config.public_latency_spike_threshold_ms
    return {
        "local": min(config.local_latency_spike_threshold_ms, config.local_jitter_spike_threshold_ms),
        "gateway": min(config.gateway_latency_spike_threshold_ms, config.gateway_jitter_spike_threshold_ms),
        "isp": config.isp_latency_spike_threshold_ms or public,
        "public": min(public, config.public_jitter_spike_threshold_ms),
        "dns": config.dns_timeout_threshold_ms,
    }


class StoredLoad:
    """What the live `LineLoad.current` answers, from the stored WAN rates of the router: the peak over the last
    LIVE_WINDOW_SECONDS (or the latest sample, if it is at most STALE_AFTER_SECONDS old), as a share of the capacity of
    `line` (a LineLoad that holds only the capacity). The constants are read from engine/load.py at call time."""

    def __init__(self, rates: List[Rate], line: LineLoad):
        self._rates = rates  # oldest first
        self._ts = [r[0] for r in rates]
        self._line = line
        self._last: Tuple[int, int] = (-1, -1)  # the samples of the last answer; rounds a second apart share them
        self._reading: Optional[LoadReading] = None

    def current(self, ts: float) -> Optional[LoadReading]:
        end = bisect_right(self._ts, ts)
        if end == 0 or ts - self._ts[end - 1] > load_module.STALE_AFTER_SECONDS:
            return None
        start = bisect_left(self._ts, ts - load_module.LIVE_WINDOW_SECONDS, 0, end)
        if (start, end) != self._last:
            window = self._rates[start:end] or self._rates[end - 1:end]
            self._reading = self._line.reading_from(window)
            self._last = (start, end)
        return self._reading


@dataclass
class _RuleSet:
    name: str
    config: ClassifierConfig
    load: StoredLoad
    technology: Optional[str]
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    core: Any = None
    incidents: List[Incident] = field(default_factory=list)
    first_severity: Dict[int, str] = field(default_factory=dict)  # id(incident) -> its severity when it started
    escalated_at: Dict[int, float] = field(default_factory=dict)  # id(incident) -> when it rose to critical
    busy_runs: int = 0
    in_busy: bool = False
    rounds_without_load: int = 0
    speed_mask: SpeedMask = field(default_factory=SpeedMask)  # the stored speed tests, masked as the live classifier does

    def reset(self) -> None:
        from pengumon.engine.classifier import ClassifierCore
        self.core = ClassifierCore(self.config, None, wording(self.technology))
        self.in_busy = False

    def close_open(self) -> None:
        """The incident still open when the app stopped: ended at its last bad round."""
        tracker = self.core.tracker
        incident = tracker.active_incident
        if incident is None:
            return
        end = tracker.last_failure_ts if tracker.last_failure_ts > 0 else incident.start_time
        incident.end_time = end
        incident.duration_seconds = round(max(1.0, end - incident.start_time), 1)
        incident.resolved = True
        tracker.active_incident = None


def _day(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d %b").lstrip("0")


def _incident_key(incident: Incident) -> Tuple[float, Optional[float], str, str]:
    return (round(incident.start_time, 2), None if incident.end_time is None else round(incident.end_time, 2),
            incident.kind or "", incident.severity)


def _announce_time(rule_set: _RuleSet, incident: Incident, policy: AlertPolicy) -> Optional[float]:
    """When the notifier would have announced this incident (Notifier.on_incident_started / _escalated): at its start
    plus the delay, moved out of quiet hours unless exempt; with `min_severity: critical` a warning is considered when
    it rises to critical, as if it started then. None: never (filtered out, or resolved before its time came)."""
    if incident.expected:
        return None
    first = rule_set.first_severity.get(id(incident), incident.severity)
    escalated = rule_set.escalated_at.get(id(incident))
    if policy.passes_severity(first):
        considered, severity = incident.start_time, first
    elif escalated is not None and policy.passes_severity("critical"):
        considered, severity = escalated, "critical"
    else:
        return None
    at = policy.incident_time(considered, severity).at
    if escalated is not None and severity != "critical" and escalated < at:
        # still waiting when it rose to critical: timed again, now perhaps exempt from quiet hours
        at = max(escalated, policy.incident_time(considered, "critical").at)
    if incident.end_time is not None and incident.end_time < at:
        return None  # resolved while it waited: nothing is sent, not even "resolved"
    return at


def _outages(incidents: List[Incident], policy: AlertPolicy, last_ts: float) -> List[List[Incident]]:
    """The incidents grouped as the notifier groups them (incidents.outages.OutageBuilder, the same joining rule): the
    drops of one outage together in start order, every other incident (expected, not a place of the path, or grouping
    off) alone. An incident still open ends at the end of the window."""
    rules = policy.outage_rules
    groups: List[List[Incident]] = []
    current: Optional[Tuple[OutageBuilder, List[Incident]]] = None
    for number, incident in enumerate(sorted(incidents, key=lambda i: i.start_time)):
        place = place_of(incident)
        if incident.expected or not rules.grouping or place not in GROUPABLE_PLACES:
            groups.append([incident])
            continue
        drop = Drop(number, incident.start_time, incident.end_time, incident.severity, place, incident.title, incident.cause_code)
        if current is not None and current[0].joins(drop, rules.gap_seconds, last_ts):
            current[0].add(drop)
            current[1].append(incident)
        else:
            current = (OutageBuilder(drop), [incident])
            groups.append(current[1])
    return groups


def _alerts(rule_set: _RuleSet, timezone_name: Optional[str], last_ts: float,
            incidents: Optional[IncidentsConfig] = None) -> int:
    """The incident alerts this rule set's incidents would have sent in the window (the "resolved" messages aren't
    counted), with the repeat window judged at send time per cause, as the notifier does. With `incidents` (the
    running config's section) grouping on, the drops of an outage send one alert: at the earliest time one of them
    would have been announced alone (a drop that joins asks again when the first was not announced, as the notifier
    does), counted under the cause of its first drop."""
    policy = AlertPolicy(rule_set.alerts, timezone_name or "UTC", incidents)
    due = []
    for group in _outages(rule_set.incidents, policy, last_ts):
        times = [at for i in group if (at := _announce_time(rule_set, i, policy)) is not None and at <= last_ts]
        if times:
            due.append((min(times), group[0].cause_code))
    last_sent: Dict[str, float] = {}
    sent = 0
    for at, cause in sorted(due):
        if policy.repeat_allowed(last_sent.get(cause), at):
            last_sent[cause] = at
            sent += 1
    return sent


def _counts(rule_set: _RuleSet, last_ts: float, timezone_name: Optional[str] = None,
            incidents: Optional[IncidentsConfig] = None) -> Dict[str, Any]:
    critical = sum(1 for i in rule_set.incidents if i.severity == "critical")
    warning = sum(1 for i in rule_set.incidents if i.severity == "warning")
    seconds = sum(((i.end_time if i.end_time is not None else last_ts) - i.start_time) for i in rule_set.incidents)
    return {"incidents": len(rule_set.incidents), "critical": critical, "warning": warning,
            "busy_hidden": rule_set.busy_runs, "total_seconds": round(seconds, 1),
            "alerts": _alerts(rule_set, timezone_name, last_ts, incidents)}


def _overlap(a: Tuple[float, Optional[float]], b: Tuple[float, Optional[float]]) -> bool:
    inf = float("inf")
    return a[0] <= (inf if b[1] is None else b[1]) and b[0] <= (inf if a[1] is None else a[1])


def _changes(current: _RuleSet, draft: _RuleSet) -> List[Dict[str, Any]]:
    """The incidents only one rule set had (same start, end, kind and severity counts as the same), by start time.
    An incident of one that overlaps one of the other of the same kind is the same outage seen differently (a rule
    that delays or shortens it): one "changed" entry carrying the draft's start, end and severity and the current ones
    as was_*, not a "no longer open" plus a "newly open"."""
    have = Counter(_incident_key(i) for i in current.incidents)
    want = Counter(_incident_key(i) for i in draft.incidents)
    gone = sorted((have - want).elements())
    new = sorted((want - have).elements())
    rows: List[Tuple[Tuple[float, Optional[float], str, str], str, Optional[Tuple[float, Optional[float], str, str]]]] = []
    for key in new:
        match = next((g for g in gone if g[2] == key[2] and _overlap(g[:2], key[:2])), None)
        if match is not None:
            gone.remove(match)
            rows.append((key, "changed", match))
        else:
            rows.append((key, "draft", None))
    rows += [(key, "current", None) for key in gone]
    rows.sort(key=lambda row: (row[0][0], row[1]))
    out = []
    for k, side, was in rows[:MAX_CHANGES]:
        row = {"start": k[0], "end": k[1], "kind": k[2], "severity": k[3], "in": side}
        if was is not None:
            row.update({"was_start": was[0], "was_end": was[1], "was_severity": was[3]})
        out.append(row)
    return out


def _recorded(conn: sqlite3.Connection, start: float, end: float) -> Optional[int]:
    """Network incidents the app stored in the window (the load-explained ones left out, as the page leaves them out)."""
    kinds = kinds_in_scope("network")
    marks = ",".join("?" * len(kinds))
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM incidents WHERE start_time >= ? AND start_time <= ? AND COALESCE(expected, 0) = 0 "
            f"AND (kind IS NULL OR kind IN ({marks}))", (start, end, *kinds)).fetchone()[0]
    except sqlite3.Error:
        return None


def _lower_first(text: str) -> str:
    """"The path ..." as the middle of a sentence; an acronym or a name ("DNS", "Wi-Fi") keeps its capital."""
    return text[:1].lower() + text[1:] if text[:1].isupper() and text[1:2].islower() else text


def _path_notes(conn: sqlite3.Connection, start: float, end: float, tz) -> List[str]:
    try:
        rows = conn.execute("SELECT applied_at, summary, revision FROM path_versions WHERE applied_at >= ? AND "
                            "applied_at <= ? ORDER BY revision", (start, end)).fetchall()
    except sqlite3.Error:
        return []
    # the first version only records the path as it was (a baseline, or the import of config.yaml); it is not a change.
    # Changes on one day are one note.
    by_day: Dict[str, List[str]] = {}
    for at, summary, revision in rows:
        if summary == BASELINE_SUMMARY or revision == 1:
            continue
        by_day.setdefault(_day(at, tz), []).append(_lower_first(summary.rstrip(". ")))
    return [f"The path was changed on {day} ({'; '.join(parts)}). The replay uses today's path for the whole window."
            for day, parts in by_day.items()]


def _read_rates(conn: sqlite3.Connection, device_id: Optional[str], start: float, end: float) -> List[Rate]:
    if device_id is None:
        return []
    lead = max(load_module.STALE_AFTER_SECONDS, load_module.LIVE_WINDOW_SECONDS)
    try:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT ts, wan_rx_mbps, wan_tx_mbps FROM device_samples WHERE device_id = ? AND ts >= ? AND ts <= ? "
            "AND wan_rx_mbps IS NOT NULL AND wan_tx_mbps IS NOT NULL ORDER BY ts", (device_id, start - lead, end))]
    except sqlite3.Error:
        return []


def _read_speed_tests(conn: sqlite3.Connection, start: float, end: float) -> List[Tuple[float, float]]:
    """(start, end) of the speed tests that ran in the window (any that moved data: a skipped one has no duration)."""
    try:
        return [(r[0], r[0] + r[1]) for r in conn.execute(
            "SELECT ts, duration_s FROM speed_tests WHERE duration_s IS NOT NULL AND ts <= ? AND ts + duration_s >= ? "
            "ORDER BY ts", (end, start - 3600.0))]
    except sqlite3.Error:
        return []


def _line_load(spec: Dict[str, Any], rules: Dict[str, Any]) -> LineLoad:
    line = rules.get("line") or {}
    learned = spec.get("learned") or {}
    out = LineLoad(LineConfig(down_mbps=line.get("down_mbps"), up_mbps=line.get("up_mbps")))
    out.learned_down_mbps, out.learned_up_mbps = learned.get("down_mbps"), learned.get("up_mbps")
    return out


async def run_replay(spec: Dict[str, Any], conn: sqlite3.Connection,
                     emit: Callable[[Dict[str, Any]], None] = lambda message: None) -> Dict[str, Any]:
    """The result document of `replay` (see there for the spec)."""
    return (await replay(spec, conn, emit))[0]


async def replay(spec: Dict[str, Any], conn: sqlite3.Connection,
                 emit: Callable[[Dict[str, Any]], None] = lambda message: None) -> Tuple[Dict[str, Any], List[_RuleSet]]:
    """Replays the samples of [spec["start"], spec["end"]] in `conn` and returns the result document.

    spec: start, end (ts), round_seconds, targets (TargetConfig dicts, in path order), dns_probes (DnsProbeConfig dicts),
    hop_status (HopStatusConfig dict), rules {"current"|"draft": {"classifier": {...}, "line": {down_mbps, up_mbps},
    "alerts": {...} (AlertsConfig; the defaults when left out)}},
    incidents (IncidentsConfig; left out: no grouping),
    learned {down_mbps, up_mbps}, wan_device_id, line_technology, timezone. Returns the result document and the two rule
    sets (with their incidents, for tests)."""
    start, end = float(spec["start"]), float(spec["end"])
    half_round = float(spec.get("round_seconds", 1.0)) / 2.0
    targets = [TargetConfig.model_validate(t) for t in spec["targets"]]
    dns_probes = [DnsProbeConfig.model_validate(d) for d in spec.get("dns_probes", [])]
    hop_status = HopStatusConfig.model_validate(spec.get("hop_status") or {})
    tz = zone(spec.get("timezone"))
    # how drops are joined into outages (the running config's `incidents` section; left out: grouping off, as before outages)
    incidents = IncidentsConfig.model_validate(spec["incidents"]) if spec.get("incidents") else None

    rates = _read_rates(conn, spec.get("wan_device_id"), start, end)
    rule_sets = [
        _RuleSet(name, ClassifierConfig.model_validate(spec["rules"][name]["classifier"]),
                 StoredLoad(rates, _line_load(spec, spec["rules"][name])), spec.get("line_technology"),
                 AlertsConfig.model_validate(spec["rules"][name].get("alerts") or {}))
        for name in RULE_SETS
    ]
    speed_tests = _read_speed_tests(conn, start, end)
    for rule_set in rule_sets:
        rule_set.speed_mask = SpeedMask(speed_tests, mask_tail_seconds(rule_set.config.window_probes,
                                                                       float(spec.get("round_seconds", 1.0))))
    capacity_known = any(r.load._line.down_mbps and r.load._line.up_mbps for r in rule_sets)

    down_fraction = hop_status.down_loss_fraction
    levels = [_calm_below(r.config) for r in rule_sets]
    calm_below = {c: min(level[c] for level in levels) - CALM_MARGIN_MS for c in levels[0]}
    keep = max(r.config.window_probes for r in rule_sets)
    windows = sorted({r.config.window_probes for r in rule_sets})

    def new_stats() -> Dict[str, _Target]:
        prober = NetworkProber(status_thresholds=hop_status)
        for target in targets:
            prober.register_target(target)
        for probe in dns_probes:
            prober.register_dns_target(probe)
        return {tid: _Target(stat, calm_below.get(stat.category, float('inf')), keep)
                for tid, stat in prober.stats.items()}

    for rule_set in rule_sets:
        rule_set.reset()
    keys = {row[0]: row[1] for row in conn.execute("SELECT id, key FROM targets")}

    # flagged: per window, whether a target was found ignoring ping in the last round that looked
    state = {"rounds": 0, "prev_ts": None, "last_ts": start, "stats": new_stats(), "flagged": set()}
    gaps: List[Dict[str, float]] = []

    async def run_round(samples: List[_Sample]) -> None:
        ts = max(s.timestamp for s in samples)
        prev = state["prev_ts"]
        if prev is not None and ts - prev > GAP_SECONDS:
            gaps.append({"start": prev, "end": ts})
            for rule_set in rule_sets:
                rule_set.close_open()
                rule_set.reset()
            state["stats"], state["flagged"] = new_stats(), set()
        state["prev_ts"] = state["last_ts"] = ts
        state["rounds"] += 1
        stats = state["stats"]
        for sample in samples:
            target = stats.get(sample.target_id)
            if target is not None:
                target.add(sample.latency_ms if sample.success else None)
        # A target is hot while one of its last `keep` probes was lost or slow. With none hot, a round cannot be a fault
        # (_calm_below), nobody is down, and `observe` of a healthy round changes nothing in a tracker that has no
        # incident open and no failures counted, which is then the whole step.
        hot = any(t.hot for t in stats.values())
        calm = SKIP_CALM_ROUNDS and not hot
        # the flags (NetworkProber.update_path_flags), per window: they only change while a target is down (hot) or one
        # is flagged
        quiet: Dict[int, set] = {}
        for window in windows:
            quiet[window] = set()
            if hot or window in state["flagged"]:
                for target in stats.values():
                    target.judge(window, down_fraction)
                quiet[window] = ignoring_ping(stats.values())
                state["flagged"].discard(window)
                if quiet[window]:
                    state["flagged"].add(window)
        metrics: Dict[int, Any] = {}  # by window
        for rule_set in rule_sets:
            reading = rule_set.load.current(ts)
            if reading is None:
                rule_set.rounds_without_load += 1
            if calm:
                tracker = rule_set.core.tracker
                if (tracker.active_incident is None and not tracker.failure_counter
                        and not tracker.loss_failure_counter and not tracker.latency_failure_counter):
                    rule_set.in_busy = False
                    continue
            window = rule_set.config.window_probes
            if window not in metrics:
                for tid, target in stats.items():
                    target.ignores_ping = tid in quiet[window]
                metrics[window] = round_metrics(stats, samples, window)
            result = await rule_set.core.step(metrics[window], ts, reading, testing=rule_set.speed_mask.covers(ts))
            if result.verdict.status == "busy":
                if not rule_set.in_busy:
                    rule_set.busy_runs += 1
                rule_set.in_busy = True
            else:
                rule_set.in_busy = False
            if result.outcome.started is not None:
                rule_set.incidents.append(result.outcome.started)
                rule_set.first_severity[id(result.outcome.started)] = result.outcome.started.severity
            if result.outcome.escalated is not None:
                rule_set.escalated_at.setdefault(id(result.outcome.escalated), ts)

    pending: List[_Sample] = []
    pending_ids: set = set()
    last_sample_ts = 0.0
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + CHUNK_SECONDS, end)
        last_chunk = chunk_end >= end
        upper = "ts <= ?" if last_chunk else "ts < ?"
        rows = conn.execute(f"SELECT ts, target, ok, latency_ms FROM samples WHERE ts >= ? AND {upper} "
                            "ORDER BY ts, target", (chunk_start, chunk_end)).fetchall()
        for ts, target, ok, latency in rows:
            key = keys.get(target)
            if key is None:
                continue
            sample = _Sample(key, ts, bool(ok), latency)
            if pending and (key in pending_ids or ts - last_sample_ts > half_round):
                await run_round(pending)
                pending, pending_ids = [], set()
            pending.append(sample)
            pending_ids.add(key)
            last_sample_ts = ts
        chunk_start = chunk_end
        emit({"progress": round(min(1.0, (chunk_start - start) / max(1.0, end - start)), 3)})
    if pending:
        await run_round(pending)

    current, draft = rule_sets
    last_ts = state["last_ts"]
    notes = _path_notes(conn, start, end, tz)
    rounds = state["rounds"]
    if rounds and not capacity_known:
        notes.append("The line capacity isn't known yet (set it, or wait for it to be learned), so the replay could not "
                     "take the line load into account.")
    elif rounds and max(r.rounds_without_load for r in rule_sets) > rounds * MISSING_LOAD_NOTE_FRACTION:
        share = round(100 * max(r.rounds_without_load for r in rule_sets) / rounds)
        notes.append(f"No router load reading was stored for {share}% of the window, so a busy line could not be "
                     f"recognized there.")
    if not rounds:
        notes.append("No probe samples are stored for this window.")
    return {
        "window": {"start": start, "end": end, "rounds": rounds, "gaps": gaps},
        "current": _counts(current, last_ts, spec.get("timezone"), incidents),
        "draft": _counts(draft, last_ts, spec.get("timezone"), incidents),
        "recorded": _recorded(conn, start, end),
        "changes": _changes(current, draft), "notes": notes,
    }, rule_sets


def _watch_parent() -> None:
    """Ends this process when the app that started it is gone (a crash, a kill): nobody would read the result."""
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(2.0)
            if os.getppid() != parent:
                os._exit(1)

    threading.Thread(target=watch, daemon=True, name="replay-parent-watch").start()


def main() -> int:
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    logging.disable(logging.WARNING)  # the load code warns on every impossible rate; nobody reads this process's log
    _watch_parent()
    spec = json.load(sys.stdin)

    def emit(message: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    uri = f"file:{quote(str(spec['db_path']))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        emit({"result": asyncio.run(run_replay(spec, conn, emit))})
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
