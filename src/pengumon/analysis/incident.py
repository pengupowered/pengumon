"""Incident inspection: what the probes, the modem and the router showed around one incident.

The decisions (per-hop impact, DOCSIS correlation, gateway reachability) are pure functions of the data; only
`inspect_incident` reads stored data: probes from the repository, the modem's and the router's snapshots from the
histories of the devices that report the access line and the WAN counters (pengumon.lines.docsis.DocsisHistory,
pengumon.devices.samples.RouterHistory). `IncidentInspection.to_response()` keeps the pydantic models as they are:
FastAPI encodes them, which is what the dashboard has always received.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pengumon.devices.snapshots import RouterMetricsSnapshot
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
LOCAL_LOSS_PCT = 15.0
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
                       nearest: Optional[DocsisSignalSnapshot], minute_hops: bool = False) -> DocsisCorrelation:
    """Whether the modem's RF side explains the incident, and whether the gateway itself was unreachable. The gateway
    counts as unreachable only on evidence (an error snapshot, or gateway probe loss with a clean router, or the
    classifier's own gateway_down verdict when only minute rollups remain); with none, the status is "unknown".
    `snapshots` are those in the window; `nearest` is the snapshot closest to the incident start, which may lie
    outside the window (shown for context, but only in-window snapshots are evidence or give the status).
    `minute_hops`: the hops were aggregated from per-minute rollups, which dilute a short drop over the whole
    minute (20 s of total loss reads 33%), so their loss figures are not compared with the gateway threshold;
    the classifier's own verdict from the raw data (kind "gateway_down") is the evidence instead."""
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
        if not router_impact or router_impact.loss_pct < LOCAL_LOSS_PCT:
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


async def inspect_incident(repository, targets: Sequence, incident: Incident,
                           snapshot_reach_seconds: float = SNAPSHOT_REACH_SECONDS, *, line=None,
                           router=None) -> IncidentInspection:
    """Everything stored about the minutes around one incident. `targets` is the canonical target order
    (ICMP targets, then DNS probes). `snapshot_reach_seconds` (the modem poll interval) is how far outside the window
    a modem snapshot may lie and still be shown for the incident. `line` is the line device's DocsisHistory and
    `router` the WAN device's RouterHistory; without one, that part of the inspection has no snapshots."""
    # Time window: 45 seconds before start to 45 seconds after end
    end_time = incident.end_time or (incident.start_time + (incident.duration_seconds or 10.0))
    window_start = incident.start_time - WINDOW_PADDING_SECONDS
    window_end = end_time + WINDOW_PADDING_SECONDS

    # Per-hop impact is computed specifically during the degradation episode, with a 10 s lead-in buffer to capture
    # the initial packet drops that triggered the incident
    calc_start = incident.start_time - CALC_LEAD_SECONDS
    calc_end = max(end_time + CALC_TAIL_SECONDS, calc_start + CALC_MIN_SECONDS)

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

    # DOCSIS snapshots within or surrounding the incident window, and the one nearest to the incident start
    docsis_snapshots: List[DocsisSignalSnapshot] = []
    nearest_snapshot = None
    if line is not None:
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

    correlation = docsis_correlation(incident, hops, docsis_snapshots, nearest_snapshot, minute_hops=minute_hops)

    router_snapshots: List[RouterMetricsSnapshot] = []
    if router is not None:
        router_snapshots = await router.get_router_snapshots_in_range(
            start_time=window_start, end_time=window_end, padding_seconds=SNAPSHOT_PADDING_SECONDS
        )

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
    )
