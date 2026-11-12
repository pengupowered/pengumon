"""The incident state machine: turns the per-round verdicts into incidents (onset, escalation, recovery)."""

from dataclasses import dataclass
from typing import Optional

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.classifier.verdict import Verdict
from pengumon.engine.load import LoadReading
from pengumon.incidents.model import Incident
from pengumon.observability.telemetry import incidents_started


# What the record of an incident says when probing stopped under it (it did not recover as far as anyone knows)
MEASURING_STOPPED = ("Measuring stopped here: this computer could no longer send pings, so Pengumon cannot say whether "
                     "the network recovered.")


@dataclass
class TrackerOutcome:
    started: Optional[Incident] = None  # created (and stored) this round
    resolved: Optional[Incident] = None  # resolved this round (no longer active)
    escalated: Optional[Incident] = None  # the active incident's severity rose to critical this round


class IncidentTracker:
    """Turns the per-round verdicts into incidents: onset, escalation, recovery. Knows nothing about probes.

    Side effects that belong to the classifier (the path-trace task, log lines, callbacks) are left to the
    caller, which reads them from the returned TrackerOutcome.
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

    async def observe(
        self, verdict: Verdict, *, is_loss_or_outage: bool, reading: Optional[LoadReading], ts: float
    ) -> TrackerOutcome:
        """Feeds one round's verdict in. `is_loss_or_outage` is judged on the base diagnosis, before the load
        adjustment; `reading` is the line load of this round, if known."""
        outcome = TrackerOutcome()
        if reading is not None and self.active_incident is not None:
            self.active_incident.load_pct = max(self.active_incident.load_pct or 0.0, reading.pct)

        # "busy" counts like healthy: the delay is explained by the load, so it neither builds up toward an
        # incident nor keeps an active one open.
        if verdict.is_fault:
            self.recovery_counter = 0
            self.last_failure_ts = ts

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

            if should_trigger:
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
                    if self.repository:
                        # Stored first: when this raises (database busy), no incident is held that was never
                        # stored, and the failure counters stay, so the next faulty round tries again.
                        incident.id = await self.repository.create_incident(incident)
                    self.active_incident = incident
                    self._stored_load_pct = incident.load_pct
                    outcome.started = incident
                    incidents_started.add(1, {"kind": verdict.kind, "severity": verdict.severity})
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
        return outcome

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
        back, so the caller sends no "resolved". Returns the incident, or None when none was open."""
        incident = self.active_incident
        self.failure_counter = self.loss_failure_counter = self.latency_failure_counter = self.recovery_counter = 0
        if incident is None:
            return None
        await self._close(max(ts, incident.start_time), MEASURING_STOPPED)
        self.active_incident = None
        return incident
