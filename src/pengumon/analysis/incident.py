"""Incident inspection: what the probes, the modem and the router showed around one incident.

The decisions (per-hop impact, DOCSIS correlation, gateway reachability) are pure functions of the data; only
`inspect_incident` reads stored data: probes from the repository, the modem's and the router's snapshots from the
histories of the devices that report the access line and the WAN counters (pengumon.lines.docsis.DocsisHistory,
pengumon.devices.samples.RouterHistory). `IncidentInspection.to_response()` keeps the pydantic models as they are:
FastAPI encodes them, which is what the dashboard has always received.
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pengumon.devices.events import EVENT_REACH_SECONDS, events_between
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.devices.traffic import hours_explained
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.incidents.loss import LEAD_SECONDS, TAIL_SECONDS
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot

# The window shown around an incident, and the lead-in that captures the drops which triggered it.
WINDOW_PADDING_SECONDS = 45.0
CALC_LEAD_SECONDS = LEAD_SECONDS  # the episode the stored loss span is read from, too (incidents.loss)
CALC_MIN_SECONDS = 20.0
CALC_TAIL_SECONDS = TAIL_SECONDS
SNAPSHOT_PADDING_SECONDS = 120.0
GATEWAY_LOSS_PCT = 50.0
_DEFAULT_RULES = ClassifierConfig()  # the outage rules when the caller gives none
SNAPSHOT_REACH_SECONDS = 900.0  # a snapshot this far outside the window still describes it (one modem poll interval)
NO_SNR = 99.0  # sentinel while no downstream channel reported a SNR

# Up to this long the inspector sends every raw probe; beyond it, buckets of about span / INSPECT_MAX_POINTS seconds.
INSPECT_RAW_MAX_SECONDS = 1800
INSPECT_MAX_POINTS = 1800  # per target


@dataclass(frozen=True)
class HopImpact:
    target_id: str
    name: str
    category: str
    sent: int
    received: int
    loss_pct: float
    avg_ms: Optional[float]
    min_ms: Optional[float]
    max_ms: Optional[float]
    jitter_ms: Optional[float]


@dataclass(frozen=True)
class DocsisCorrelation:
    rf_correlated: bool
    active_errors_in_window: int
    nearest_snapshot: Optional[DocsisSignalSnapshot]
    worst_channel: Optional[DocsisChannel]
    lowest_snr: Optional[float]
    status: str
    gateway_offline: bool


def _shallow(obj) -> Dict[str, Any]:
    """Field name -> value, in declaration order; nested models stay models (dataclasses.asdict would copy them)."""
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


@dataclass(frozen=True)
class IncidentInspection:
    incident: Incident
    window_start: float
    window_end: float
    event_start: float
    event_end: float
    hop_impact: List[HopImpact]
    samples: List[Dict[str, Any]]
    docsis_snapshots: List[DocsisSignalSnapshot]
    docsis_correlation: DocsisCorrelation
    router_snapshots: List[RouterMetricsSnapshot]
    sample_bucket_seconds: Optional[int] = None  # None: `samples` are the raw 1 s probes
    hop_resolution: Optional[str] = None  # "minute": hop_impact comes from per-minute rollups (raw samples are gone)
    # The device events (devices/events.py) within EVENT_REACH_SECONDS of the incident, oldest first, as dicts.
    device_events: List[Dict[str, Any]] = field(default_factory=list)
    # The router's traffic in the clock hours the incident covers (devices/traffic.py hours_explained): {"hours": [...]};
    # None without a device that reports traffic.
    traffic: Optional[Dict[str, Any]] = None

    def to_response(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("sample_bucket_seconds", "hop_resolution"):
                continue
            out[f.name] = getattr(self, f.name)
            if f.name == "samples" and self.sample_bucket_seconds is not None:
                # only present when `samples` are buckets, so the response for a short window is what it always was
                out["sample_bucket_seconds"] = self.sample_bucket_seconds
        if self.hop_resolution is not None:
            out["hop_resolution"] = self.hop_resolution  # absent for raw-resolution tables, as it always was
        out["hop_impact"] = [_shallow(h) for h in self.hop_impact]
        out["docsis_correlation"] = _shallow(self.docsis_correlation)
        return out


class _TargetMeta:
    """Config lookups shared by both ways of building the per-hop table."""

    def __init__(self, targets: Sequence):
        self.order = {t.id: i for i, t in enumerate(targets)}
        self.names = {t.id: t.name for t in targets}
        self.categories = {t.id: getattr(t, "category", "dns") for t in targets}

    def sort(self, ids) -> List[str]:
        """Canonical order; ids the config does not know all sort last, in the order given."""
        ids = list(ids)
        ids.sort(key=lambda tid: self.order.get(tid, 999))
        return ids

    def hop(self, tid: str, sent: int, received: int, avg_ms: Optional[float], min_ms: Optional[float],
            max_ms: Optional[float], jitter_ms: Optional[float]) -> HopImpact:
        lost = sent - received
        return HopImpact(
            target_id=tid,
            name=self.names.get(tid, tid),
            category=self.categories.get(tid, "dns" if "dns" in tid else "custom"),
            sent=sent,
            received=received,
            loss_pct=round((lost / sent) * 100.0, 1) if sent > 0 else 0.0,
            avg_ms=avg_ms,
            min_ms=min_ms,
            max_ms=max_ms,
            jitter_ms=jitter_ms,
        )


def hop_impact(samples: List[Dict[str, Any]], calc_start: float, calc_end: float,
               targets: Sequence) -> List[HopImpact]:
    """Per-target loss and latency during the degradation episode [calc_start, calc_end], in canonical target order.
    With no samples in that range, all of them are used."""
    event_samples = [s for s in samples if calc_start <= s["timestamp"] <= calc_end]
    if not event_samples:
        event_samples = samples

    meta = _TargetMeta(targets)
    # One pass over the samples; targets in canonical order (ids the config does not know sort last, in set order)
    by_target: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in event_samples:
        by_target[s["target_id"]].append(s)
    result = []
    for tid in meta.sort(set(s["target_id"] for s in event_samples)):
        t_samples = by_target[tid]

        received = sum(1 for s in t_samples if s.get("success") == 1)
        valid_lats = [s["latency_ms"] for s in t_samples if s.get("success") == 1 and s.get("latency_ms") is not None]
        valid_jitters = [s["jitter_ms"] for s in t_samples if s.get("jitter_ms") is not None]
        result.append(meta.hop(
            tid, len(t_samples), received,
            round(sum(valid_lats) / len(valid_lats), 2) if valid_lats else None,
            round(min(valid_lats), 2) if valid_lats else None,
            round(max(valid_lats), 2) if valid_lats else None,
            round(sum(valid_jitters) / len(valid_jitters), 2) if valid_jitters else None,
        ))
    return result


def hop_impact_from_stats(stats: List[Dict[str, Any]], targets: Sequence) -> List[HopImpact]:
    """The same table from repository.get_sample_stats rows (aggregated in SQL over the whole window)."""
    meta = _TargetMeta(targets)
    by_id = {row["target_id"]: row for row in stats}
    return [
        meta.hop(tid, by_id[tid]["sent"], by_id[tid]["received"], by_id[tid]["avg_ms"], by_id[tid]["min_ms"],
                 by_id[tid]["max_ms"], by_id[tid]["jitter_ms"])
        for tid in meta.sort(by_id)
    ]


def bucketed_samples(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """History buckets in the shape of raw samples, which is what the dashboard's charts read: a bucket counts as
    answered (success 1) when any probe in it was, with the bucket's mean latency and loss percentage."""
    return [
        {
            "target_id": r["target_id"],
            "timestamp": r["timestamp"],
            "success": 1 if r["successful_count"] else 0,
            "latency_ms": r["latency_ms"],
            "packet_loss_pct": r["packet_loss_pct"],
            "jitter_ms": r["jitter_ms"],
            "error": None,
        }
        for r in sorted(rows, key=lambda r: (r["timestamp"], r["target_id"]))  # SQL leaves targets unordered
    ]


# Error kinds that mean the modem could not be reached at all. Other errors (a refused login, a page that could not
# be parsed) say the modem answered, so they are no evidence of an outage. Rows from before schema v7 have no kind
# and keep counting, as they always did.
UNREACHABLE_KINDS = frozenset({"timeout", "unreachable"})


def _unreachable(snapshot: DocsisSignalSnapshot) -> bool:
    return snapshot.status in ("error", "offline") and (
        snapshot.error_kind is None or snapshot.error_kind in UNREACHABLE_KINDS)


def docsis_correlation(incident: Incident, hops: List[HopImpact], snapshots: List[DocsisSignalSnapshot],
                       nearest: Optional[DocsisSignalSnapshot], minute_hops: bool = False,
                       local_loss_pct: Optional[float] = None) -> DocsisCorrelation:
    """Whether the modem's RF side explains the incident, and whether the gateway itself was unreachable. The gateway
    counts as unreachable only on evidence (an error snapshot, or gateway probe loss with a clean router, or the
    classifier's own gateway_down verdict when only minute rollups remain); with none, the status is "unknown".
    `snapshots` are those in the window; `nearest` is the snapshot closest to the incident start, which may lie
    outside the window (shown for context, but only in-window snapshots are evidence or give the status).
    `minute_hops`: the hops were aggregated from per-minute rollups, which dilute a short drop over the whole
    minute (20 s of total loss reads 33%), so their loss figures are not compared with the gateway threshold;
    the classifier's own verdict from the raw data (kind "gateway_down") is the evidence instead.
    `local_loss_pct`: the router loss from which the router itself is lossy, so gateway loss is no evidence against
    the gateway (the classifier's degraded level; None: its default)."""
    if local_loss_pct is None:
        local_loss_pct = _DEFAULT_RULES.degraded_loss_pct
    active_errors_in_window = 0
    worst_channel = None
    lowest_snr = NO_SNR

    if snapshots:
        active_errors_in_window = sum(s.uncorrectables_recent for s in snapshots)

    if nearest and nearest.channels:
        # Dynamically sort all downstream channels:
        # Prioritize channels with active uncorrectable bursts, then total uncorrectables, then lowest SNR
        ds_channels = [c for c in nearest.channels if getattr(c, "direction", "downstream") == "downstream"]
        if ds_channels:
            ds_channels.sort(
                key=lambda c: (
                    c.uncorrectables_recent or 0,
                    c.uncorrectables or 0,
                    -(c.snr_db if c.snr_db is not None else NO_SNR)
                ),
                reverse=True
            )
            worst_channel = ds_channels[0]
            lowest_snr = worst_channel.snr_db if worst_channel.snr_db is not None else NO_SNR

    rf_correlated = bool(active_errors_in_window > 0 or (worst_channel and worst_channel.uncorrectables_recent > 0))

    # Detect gateway reachability / outage
    gateway_impact = next(
        (h for h in hops if h.category == "gateway"),
        None
    )
    router_impact = next(
        (h for h in hops if h.category == "local"),
        None
    )

    gateway_unreachable = False
    if any(_unreachable(s) for s in snapshots):
        gateway_unreachable = True
    elif minute_hops and incident.kind == "gateway_down":
        gateway_unreachable = True
    elif not minute_hops and gateway_impact and gateway_impact.loss_pct >= GATEWAY_LOSS_PCT:
        if not router_impact or router_impact.loss_pct < local_loss_pct:
            gateway_unreachable = True

    return DocsisCorrelation(
        rf_correlated=rf_correlated,
        active_errors_in_window=active_errors_in_window,
        nearest_snapshot=nearest,
        worst_channel=worst_channel,
        lowest_snr=lowest_snr if lowest_snr < 90 else None,
        status="error" if gateway_unreachable else (nearest.status if snapshots and nearest else "unknown"),
        gateway_offline=gateway_unreachable,
    )


async def _episode_stats(repository, calc_start: float, calc_end: float, window_start: float,
                         window_end: float) -> Tuple[List[Dict[str, Any]], bool]:
    """Per-target totals for the degradation episode, else for the whole window, and whether they come from the
    per-minute rollups: raw samples while they are retained, otherwise the rollups (whole minutes, so wider than
    asked and diluted)."""
    for from_rollups, read in ((False, repository.get_sample_stats), (True, repository.get_rollup_stats)):
        for lo, hi in ((calc_start, calc_end), (window_start, window_end)):
            stats = await read(lo, hi)
            if stats:
                return stats, from_rollups
    return [], False


def end_of(start_time: float, end_time: Optional[float], duration_seconds: Optional[float], resolved: bool) -> float:
    """When an incident ended; one still open has lasted until now, and one with no duration at all, 10 seconds."""
    if end_time:
        return end_time
    if duration_seconds:
        return start_time + duration_seconds
    if not resolved:  # still open (no duration yet): the whole of it so far, not its first 10 seconds
        return max(start_time + 10.0, time.time())
    return start_time + 10.0


def _incident_end(incident: Incident) -> float:
    return end_of(incident.start_time, incident.end_time, incident.duration_seconds, incident.resolved)


@dataclass(frozen=True)
class InspectionWindow:
    """The times the inspector reads for one incident (the incident's evidence, db/evidence.py, keeps the same ones)."""

    end: float  # when the incident ended
    window_start: float  # the window shown: the incident +- WINDOW_PADDING_SECONDS
    window_end: float
    calc_start: float  # the degradation episode the hop table is computed over
    calc_end: float


def inspection_window(start_time: float, end_time: Optional[float], duration_seconds: Optional[float],
                      resolved: bool) -> InspectionWindow:
    end = end_of(start_time, end_time, duration_seconds, resolved)
    # Per-hop impact is computed specifically during the degradation episode, with a lead-in buffer to capture the
    # initial packet drops that triggered the incident
    calc_start = start_time - CALC_LEAD_SECONDS
    return InspectionWindow(end=end, window_start=start_time - WINDOW_PADDING_SECONDS,
                            window_end=end + WINDOW_PADDING_SECONDS, calc_start=calc_start,
                            calc_end=max(end + CALC_TAIL_SECONDS, calc_start + CALC_MIN_SECONDS))


async def _hops_and_samples(repository, targets: Sequence, window_start: float, window_end: float, calc_start: float,
                            calc_end: float):
    """The per-hop impact and the samples for the charts: raw 1 s samples when the window is short and they are still
    stored, else per-minute rollups. Returns the hops, the samples, the bucket width of the samples (None: raw) and
    whether the hops come from the rollups."""
    sample_bucket_seconds = None
    minute_hops = False
    short_window = window_end - window_start <= INSPECT_RAW_MAX_SECONDS
    samples: List[Dict[str, Any]] = []
    if short_window:
        # Every raw 1 s sample during this window
        samples = await repository.get_raw_samples(start_time=window_start, end_time=window_end)
    if samples:
        hops = hop_impact(samples, calc_start, calc_end, targets)
    else:
        # A long incident, or one whose raw samples are past retention (only the per-minute rollups remain): about
        # INSPECT_MAX_POINTS buckets per target for the charts, and the hop table aggregated in SQL over the whole
        # episode (shipping every raw probe would be megabytes and is capped by the repository)
        bucket = math.ceil((window_end - window_start) / INSPECT_MAX_POINTS)
        if short_window:
            bucket = max(bucket, 60)  # no raw samples to bucket: ask for the rollups' minute resolution
        rows, sample_bucket_seconds = await repository.get_history_buckets(
            window_start, window_end, bucket_seconds=bucket)
        samples = bucketed_samples(rows)
        stats, minute_hops = await _episode_stats(repository, calc_start, calc_end, window_start, window_end)
        hops = hop_impact_from_stats(stats, targets)
        if short_window and not samples:
            sample_bucket_seconds = None  # nothing stored at all: the plain short-window shape
    return hops, samples, sample_bucket_seconds, minute_hops


async def _nearby_snapshots(line, incident: Incident, window_start: float, window_end: float,
                            snapshot_reach_seconds: float, evidence=None, repository=None):
    """The modem's snapshots within or surrounding the incident window, and the one nearest to the incident start.
    With the incident's evidence, from the readings it kept (the same rule over them)."""
    docsis_snapshots: List[DocsisSignalSnapshot] = []
    nearest_snapshot = None
    if line is not None and evidence is not None and getattr(line, "device_id", None) is not None:
        from pengumon.lines.docsis import TECHNOLOGY, snapshot_from_sample
        kept = [snapshot_from_sample(s) for s in await repository.evidence_line_samples(incident.id, line.device_id)
                if s.technology == TECHNOLOGY]
        pad = SNAPSHOT_PADDING_SECONDS
        docsis_snapshots = [s for s in kept if window_start - pad <= s.timestamp <= window_end + pad]
        reach = docsis_snapshots or [s for s in kept if window_start - snapshot_reach_seconds <= s.timestamp
                                     <= window_end + snapshot_reach_seconds]
        if reach:
            nearest_snapshot = min(reach, key=lambda s: abs(s.timestamp - incident.start_time))
    elif line is not None:
        docsis_snapshots = await line.get_docsis_snapshots_in_range(
            start_time=window_start, end_time=window_end, padding_seconds=SNAPSHOT_PADDING_SECONDS
        )
        if docsis_snapshots:
            nearest_snapshot = min(docsis_snapshots, key=lambda s: abs(s.timestamp - incident.start_time))
        else:
            # None in the window: the nearest one within a poll interval of it still describes the incident,
            # anything older or newer is a reading of some other time and is not shown
            reach = await line.get_docsis_snapshots_in_range(
                start_time=window_start, end_time=window_end, padding_seconds=snapshot_reach_seconds
            )
            nearest_snapshot = min(reach, key=lambda s: abs(s.timestamp - incident.start_time)) if reach else None
    return docsis_snapshots, nearest_snapshot


async def _evidence_of(repository, incident: Incident):
    """The evidence a closed incident kept, when it holds anything (resolution other than "none"); else None."""
    if incident.id is None or not incident.resolved or not hasattr(repository, "get_incident_evidence"):
        return None
    evidence = await repository.get_incident_evidence(incident.id)
    return evidence if evidence is not None and evidence.resolution != "none" else None


async def _evidence_hops_and_samples(repository, targets: Sequence, incident: Incident, evidence,
                                     win: InspectionWindow):
    """_hops_and_samples from the incident's evidence: its raw probes (resolution "raw"), or buckets of the probes it
    kept ("mixed": 10-second buckets and raw ends; "minute": the per-minute rollups), with the hop table summed over
    the episode, else over the whole window. Same return."""
    if evidence.resolution == "raw":
        samples = await repository.evidence_raw_samples(incident.id, win.window_start, win.window_end)
        return hop_impact(samples, win.calc_start, win.calc_end, targets), samples, None, False
    minute = evidence.resolution == "minute"
    width = await repository.evidence_bucket_width(incident.id, win.window_end - win.window_start,
                                                   60 if minute else 10)
    samples = bucketed_samples(await repository.evidence_buckets(incident.id, win.window_start, win.window_end, width))
    stats: List[Dict[str, Any]] = []
    for lo, hi in ((win.calc_start, win.calc_end), (win.window_start, win.window_end)):
        stats = await repository.evidence_stats(incident.id, lo, hi)
        if stats:
            break
    return hop_impact_from_stats(stats, targets), samples, width, minute


async def inspect_incident(repository, targets: Sequence, incident: Incident,
                           snapshot_reach_seconds: float = SNAPSHOT_REACH_SECONDS, *, line=None,
                           router=None, classifier: Optional[ClassifierConfig] = None, traffic=None,
                           tz=None) -> IncidentInspection:
    """Everything stored about the minutes around one incident. `targets` is the canonical target order
    (ICMP targets, then DNS probes). `snapshot_reach_seconds` (the modem poll interval) is how far outside the window
    a modem snapshot may lie and still be shown for the incident. `line` is the line device's DocsisHistory and
    `router` the WAN device's RouterHistory; without one, that part of the inspection has no snapshots.
    `classifier`: the outage rules in force (their degraded loss level judges the router); None: the defaults.
    `traffic`: (device id, devices.traffic.Namer) of the device that reports traffic per client: the hours the incident
    covers get their top clients by download. `tz`: the zone the traffic texts give clock times in (None: local).
    An incident still open has lasted until now."""
    # Time window: 45 seconds before start to 45 seconds after end
    win = inspection_window(incident.start_time, incident.end_time, incident.duration_seconds, incident.resolved)
    end_time, window_start, window_end = win.end, win.window_start, win.window_end

    # A closed incident keeps its own evidence (db/evidence.py): read first, the shared tables only without it
    evidence = await _evidence_of(repository, incident)
    if evidence is not None:
        hops, samples, sample_bucket_seconds, minute_hops = await _evidence_hops_and_samples(
            repository, targets, incident, evidence, win)
    else:
        hops, samples, sample_bucket_seconds, minute_hops = await _hops_and_samples(
            repository, targets, window_start, window_end, win.calc_start, win.calc_end)

    docsis_snapshots, nearest_snapshot = await _nearby_snapshots(line, incident, window_start, window_end,
                                                                 snapshot_reach_seconds, evidence, repository)

    correlation = docsis_correlation(incident, hops, docsis_snapshots, nearest_snapshot, minute_hops=minute_hops,
                                     local_loss_pct=(classifier or _DEFAULT_RULES).degraded_loss_pct)

    router_snapshots: List[RouterMetricsSnapshot] = []
    if router is not None:
        router_samples = None
        if evidence is not None and getattr(router, "device_id", None) is not None:
            router_samples = await repository.evidence_device_samples(incident.id, router.device_id)
        if router_samples is not None:
            from pengumon.devices.samples import router_snapshot
            pad = SNAPSHOT_PADDING_SECONDS
            router_snapshots = [router_snapshot(s) for s in router_samples
                                if window_start - pad <= s.ts <= window_end + pad]
        else:
            router_snapshots = await router.get_router_snapshots_in_range(
                start_time=window_start, end_time=window_end, padding_seconds=SNAPSHOT_PADDING_SECONDS
            )

    device_events = [e.as_dict(tz) for e in await events_between(
        repository, None, incident.start_time - EVENT_REACH_SECONDS, end_time + EVENT_REACH_SECONDS)
    ] if hasattr(repository, "get_device_events") else []
    traffic_hours = None
    if traffic is not None:
        device_id, namer = traffic
        traffic_hours = {"hours": await hours_explained(repository, device_id, incident.start_time, end_time, namer,
                                                         tz=tz)}

    return IncidentInspection(
        incident=incident,
        window_start=window_start,
        window_end=window_end,
        event_start=incident.start_time,
        event_end=end_time,
        hop_impact=hops,
        samples=samples,
        docsis_snapshots=docsis_snapshots,
        docsis_correlation=correlation,
        router_snapshots=router_snapshots,
        sample_bucket_seconds=sample_bucket_seconds,
        hop_resolution="minute" if minute_hops else None,
        device_events=device_events,
        traffic=traffic_hours,
    )
