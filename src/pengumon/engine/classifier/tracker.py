"""The incident state machine: turns the per-round verdicts into incidents (onset, escalation, recovery), and brief
latency-only episodes into latency spikes (incidents.spikes)."""

from dataclasses import dataclass
from typing import Optional

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.classifier.verdict import Verdict
from pengumon.engine.load import LoadReading
from pengumon.incidents.model import Incident
from pengumon.incidents.spikes import LatencySpike
from pengumon.incidents.taxonomy import LATENCY_ONLY_KINDS, incident_cause
from pengumon.observability.telemetry import incidents_started


# What the record of an incident says when probing stopped under it (it did not recover as far as anyone knows)
MEASURING_STOPPED = ("Measuring stopped here: this computer could no longer send pings, so Pengumon cannot say whether "
                     "the network recovered.")


@dataclass
class TrackerOutcome:
    started: Optional[Incident] = None  # created (and stored) this round
    resolved: Optional[Incident] = None  # resolved this round (no longer active)
    escalated: Optional[Incident] = None  # the active incident's severity rose to critical this round
    spike: Optional[LatencySpike] = None  # a latency-only episode ended before it became an incident (stored now)


class IncidentTracker:
    """Turns the per-round verdicts into incidents: onset, escalation, recovery. Knows nothing about probes.

    Side effects that belong to the classifier (the path-trace task, log lines, callbacks) are left to the
    caller, which reads them from the returned TrackerOutcome.

    A latency-only episode (a verdict of taxonomy.LATENCY_ONLY_KINDS, no round with loss) that meets the start rule is
    `pending`, not yet an incident: nothing is stored or announced until it has lasted
    `config.latency_incident_after_seconds` (judged at each faulty round), when it becomes the incident it would have
    been, starting where the episode started. It ends by the incident end rule (healthy rounds in a row), and one that
    ends first is stored as a latency spike. A round with loss while it is pending makes it an incident at once, of that
    round's verdict (still from the episode's start): loss is never a spike, and the episode is counted once.
    """

    def __init__(self, config: ClassifierConfig, repository: Optional[DatabaseRepository] = None):
        self.config = config
        self.repository = repository
        self.active_incident: Optional[Incident] = None
        self.failure_counter: int = 0
        self.loss_failure_counter: int = 0
        self.latency_failure_counter: int = 0
        self.recovery_counter: int = 0
        self.last_failure_ts: float = 0.0
        # when the current run of loss / latency rounds began (an incident starts there, whatever the round spacing)
        self._loss_first_ts: float = 0.0
        self._latency_first_ts: float = 0.0
        self._stored_load_pct: Optional[float] = None
        # the latency-only episode waiting to become an incident (not stored), and the highest probe of the faulty
        # rounds since nothing was held and the counters were last zero (its peak)
        self.pending: Optional[Incident] = None
        self._run_peak: Optional[float] = None

    @property
    def idle(self) -> bool:
        """No incident open, no episode pending and no faulty round counted: a healthy round changes nothing."""
        return (self.active_incident is None and self.pending is None and not self.failure_counter
                and not self.loss_failure_counter and not self.latency_failure_counter)

    def _waits(self, verdict: Verdict, is_loss_or_outage: bool) -> bool:
        """Whether an episode that meets the start rule this round waits as `pending` instead of opening at once."""
        return (self.config.latency_incident_after_seconds > 0 and not is_loss_or_outage
                and verdict.kind in LATENCY_ONLY_KINDS
                and self.loss_failure_counter < self.config.consecutive_failures_for_incident)

    async def observe(
        self, verdict: Verdict, *, is_loss_or_outage: bool, reading: Optional[LoadReading], ts: float,
        peak_ms: Optional[float] = None,
    ) -> TrackerOutcome:
        """Feeds one round's verdict in. `is_loss_or_outage` is judged on the base diagnosis, before the load
        adjustment; `reading` is the line load of this round, if known; `peak_ms` the slowest answered probe of the
        verdict's stage this round (RoundMetrics.peak_latency), the peak a latency spike records."""
        outcome = TrackerOutcome()
        held = self.active_incident or self.pending
        if reading is not None and held is not None:
            held.load_pct = max(held.load_pct or 0.0, reading.pct)

        # "busy" counts like healthy: the delay is explained by the load, so it neither builds up toward an
        # incident nor keeps an active one open.
        if verdict.is_fault:
            self.recovery_counter = 0
            self.last_failure_ts = ts
            if peak_ms is not None:
                self._run_peak = peak_ms if self._run_peak is None else max(self._run_peak, peak_ms)

            if is_loss_or_outage:
                if self.loss_failure_counter == 0:
                    self._loss_first_ts = ts
                self.loss_failure_counter += 1
                self.failure_counter = self.loss_failure_counter
            else:
                if self.latency_failure_counter == 0:
                    self._latency_first_ts = ts
                self.latency_failure_counter += 1
                self.failure_counter = self.latency_failure_counter

            latency_threshold = self.config.consecutive_failures_for_latency_incident
            should_trigger = (
                self.loss_failure_counter >= self.config.consecutive_failures_for_incident
                or self.latency_failure_counter >= latency_threshold
            )

            if self.pending is not None and self.active_incident is None:
                # the episode goes on: loss makes it an incident of this round's verdict at once (loss is never a
                # spike), and so does lasting the wait (judged now: a changed setting applies from this round)
                if is_loss_or_outage:
                    self.pending = self.pending.model_copy(update={
                        "kind": verdict.kind, "cause_code": verdict.cause, "title": verdict.title,
                        "description": verdict.description, "severity": verdict.severity,
                        "affected_targets": list(verdict.affected)})
                if is_loss_or_outage or ts - self.pending.start_time >= self.config.latency_incident_after_seconds:
                    await self._open(self.pending, outcome)
            elif should_trigger:
                if not self.active_incident:
                    # Start new incident accurately timed from onset
                    first_ts = self._loss_first_ts if is_loss_or_outage else self._latency_first_ts
                    actual_start = min(round(first_ts, 2), ts)  # (rounding must not put it after this round)
                    incident = Incident(
                        start_time=actual_start,
                        kind=verdict.kind,
                        cause_code=verdict.cause,
                        title=verdict.title,
                        description=verdict.description,
                        severity=verdict.severity,
                        affected_targets=list(verdict.affected),
                        resolved=False,
                        load_pct=reading.pct if reading else None,
                    )
                    if (self._waits(verdict, is_loss_or_outage)
                            and ts - actual_start < self.config.latency_incident_after_seconds):
                        self.pending = incident  # held, not stored: it may end as a latency spike
                    else:
                        await self._open(incident, outcome)
                else:
                    # Update active incident if cause escalated
                    if verdict.severity == "critical" and self.active_incident.severity != "critical":
                        self.active_incident.severity = "critical"
                        self.active_incident.kind = verdict.kind
                        self.active_incident.cause_code = verdict.cause
                        self.active_incident.title = verdict.title
                        self.active_incident.description = verdict.description
                        if self.repository and self.active_incident.id:
                            await self.repository.update_incident_classification(
                                self.active_incident.id,
                                self.active_incident.kind,
                                self.active_incident.cause_code,
                                self.active_incident.title,
                                self.active_incident.description,
                                self.active_incident.severity,
                            )
                        outcome.escalated = self.active_incident
        else:
            self.loss_failure_counter = 0
            self.latency_failure_counter = 0
            self.failure_counter = 0
            if self.active_incident:
                self.recovery_counter += 1
                if self.recovery_counter >= self.config.consecutive_successes_for_recovery:
                    # Resolve incident accurately at the last anomalous sample
                    resolve_ts = self.last_failure_ts if self.last_failure_ts > 0 else ts
                    resolve_ts = max(resolve_ts, self.active_incident.start_time)  # a clock stepped back
                    await self._close(resolve_ts)
                    outcome.resolved = self.active_incident
                    self.active_incident = None
            elif self.pending is not None:
                self.recovery_counter += 1
                if self.recovery_counter >= self.config.consecutive_successes_for_recovery:
                    # ended before it became an incident: a latency spike, ending at its last faulty round
                    outcome.spike = await self.flush_pending()
            if self.active_incident is None and self.pending is None:
                self._run_peak = None
        return outcome

    async def _open(self, incident: Incident, outcome: TrackerOutcome) -> None:
        """Stores `incident` and makes it the active one (from pending, if it was)."""
        if self.repository:
            # Stored first: when this raises (database busy), no incident is held that was never stored, and the
            # failure counters (or the pending episode) stay, so the next faulty round tries again.
            incident.id = await self.repository.create_incident(incident)
        self.pending = None
        self.active_incident = incident
        self._stored_load_pct = incident.load_pct
        outcome.started = incident
        incidents_started.add(1, {"kind": incident.kind, "severity": incident.severity})

    def pending_spike(self) -> Optional[LatencySpike]:
        """The pending episode as a latency spike ending at its last faulty round (None: none is pending). Changes
        nothing."""
        p = self.pending
        if p is None:
            return None
        end = max(self.last_failure_ts or p.start_time, p.start_time)
        peak = None if self._run_peak is None else round(self._run_peak, 2)
        return LatencySpike(start_time=p.start_time, end_time=end, kind=p.kind or "",
                            stage=incident_cause(p.kind, p.cause_code, p.description), expected=p.expected,
                            peak_ms=peak)

    async def flush_pending(self) -> Optional[LatencySpike]:
        """Ends the pending episode as a latency spike (stored, when there is a repository), at its last faulty round:
        at its end, and when measuring stops or the app shuts down (it never became an incident while it was
        watched). Returns it, or None when none was pending. When storing raises, it stays pending."""
        spike = self.pending_spike()
        if spike is None:
            return None
        if self.repository:
            spike.id = await self.repository.create_latency_spike(spike)
        self.pending = None
        self._run_peak = None
        return spike

    async def _close(self, end_ts: float, note: str = "") -> None:
        """Ends the active incident at `end_ts` (stored, with the peak load), adding `note` to its description."""
        incident = self.active_incident
        incident.end_time = end_ts
        incident.duration_seconds = round(max(1.0, end_ts - incident.start_time), 1)
        incident.resolved = True
        if note:
            incident.description = f"{incident.description} {note}"
        if self.repository and incident.id:
            await self.repository.resolve_incident(incident.id, incident.end_time)
            if note:
                await self.repository.update_incident_classification(
                    incident.id, incident.kind, incident.cause_code, incident.title, incident.description,
                    incident.severity)
            if incident.load_pct != self._stored_load_pct:
                # Peak load over the whole incident, not just at its start.
                await self.repository.set_incident_load(incident.id, incident.load_pct)

    async def stop_measuring(self, ts: float) -> Optional[Incident]:
        """Probing became unavailable (this host cannot send pings): the open incident ends at `ts`, the moment the
        last round was measured, with a note that measuring stopped. Not a recovery: nothing says the network is
        back, so the caller sends no "resolved". A pending latency episode is stored as a latency spike
        (flush_pending). Returns the incident, or None when none was open."""
        await self.flush_pending()
        incident = self.active_incident
        self.failure_counter = self.loss_failure_counter = self.latency_failure_counter = self.recovery_counter = 0
        self._run_peak = None
        if incident is None:
            return None
        await self._close(max(ts, incident.start_time), MEASURING_STOPPED)
        self.active_incident = None
        return incident
