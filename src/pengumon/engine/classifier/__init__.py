"""Diagnostic classifier engine for network root-cause isolation.

OutageClassifier is the orchestrator: it owns the clock, the load reading and the callbacks, and each round it
runs metrics.round_metrics -> rules.diagnose -> rules.apply_load -> rules.apply_hop_deprioritisation ->
tracker.IncidentTracker.observe. The pieces:

* verdict.py   Verdict, the frozen result of a round; its `kind` drives control flow, the texts are for people
* metrics.py   CategoryMetrics / RoundMetrics, computed once per round (not pengumon.metrics, the Prometheus one)
* rules.py     the diagnosis as pure functions of the round metrics
* tracker.py   the incident state machine: onset, escalation, recovery

time.time() is called here and only here, and OutageClassifier must stay in this module: the characterization
golden patches `time` and `_trigger_path_trace` on pengumon.engine.classifier.
"""

import asyncio
import time
import logging
from typing import Callable, Dict, List, Optional
from pengumon.incidents.model import SystemDiagnosis, Incident
from pengumon.models.probes import ProbeSample, TargetStats
from pengumon.incidents.taxonomy import ISP_LINE_KINDS
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.load import LineLoad
from pengumon.engine.classifier.metrics import CategoryMetrics, RoundMetrics, round_metrics
from pengumon.engine.classifier.rules import apply_hop_deprioritisation, apply_load, diagnose
from pengumon.engine.classifier.tracker import IncidentTracker, TrackerOutcome
from pengumon.engine.classifier.verdict import NOMINAL, Verdict
from pengumon.lines import wording
from pengumon.observability.telemetry import tracer

logger = logging.getLogger("pengumon.classifier")

__all__ = [
    "OutageClassifier", "Verdict", "NOMINAL", "CategoryMetrics", "RoundMetrics", "IncidentTracker", "TrackerOutcome",
]


class OutageClassifier:
    """Evaluates multi-target probe samples to isolate failure root causes."""

    def __init__(
        self,
        config: ClassifierConfig,
        repository: Optional[DatabaseRepository] = None,
        known_targets: Optional[List] = None,
        load: Optional[LineLoad] = None,
        on_incident_started: Optional[Callable[[Incident], None]] = None,
        on_incident_resolved: Optional[Callable[[Incident], None]] = None,
        line_technology: Optional[str] = None,
    ):
        # Plain synchronous callbacks that must return quickly (the notifier only schedules a task).
        self.on_incident_started = on_incident_started
        self.on_incident_resolved = on_incident_resolved
        self.config = config
        self.repository = repository
        self.known_targets = known_targets if known_targets is not None else []  # shared: a path edit updates it
        self.load = load
        self.line = wording(line_technology)  # how access-line faults are described (pengumon.lines)
        self.tracker = IncidentTracker(config, repository)
        self.last_diagnosis: SystemDiagnosis = SystemDiagnosis()
        self.last_verdict: Verdict = NOMINAL
        self.last_upstream_issue_ts: float = 0.0
        # Strong references to background tasks: the event loop holds tasks only weakly.
        self._background: set = set()

    # State that lives in the tracker, under the names callers and tests have always used.
    @property
    def active_incident(self) -> Optional[Incident]:
        return self.tracker.active_incident

    @active_incident.setter
    def active_incident(self, incident: Optional[Incident]) -> None:
        self.tracker.active_incident = incident

    @property
    def failure_counter(self) -> int:
        return self.tracker.failure_counter

    @property
    def loss_failure_counter(self) -> int:
        return self.tracker.loss_failure_counter

    @property
    def latency_failure_counter(self) -> int:
        return self.tracker.latency_failure_counter

    @property
    def recovery_counter(self) -> int:
        return self.tracker.recovery_counter

    @property
    def last_failure_ts(self) -> float:
        return self.tracker.last_failure_ts

    async def evaluate(
        self,
        samples: List[ProbeSample],
        stats: Dict[str, TargetStats]
    ) -> SystemDiagnosis:
        """Evaluates the latest probe samples against the diagnostic matrix."""
        ts = time.time()
        
        # Rolling metrics across the last 10 samples of each category, computed once for the round
        m = round_metrics(stats, samples)

        # Track upstream issues
        if m.upstream_issue(self.config.public_latency_spike_threshold_ms):
            self.last_upstream_issue_ts = ts

        # Classify root cause based on sustained rolling metrics
        verdict = diagnose(m, self.config, ts, self.last_upstream_issue_ts, self.line)

        # Determine if degradation is packet loss / hard outage vs pure latency / jitter
        is_loss_or_outage = m.is_loss_or_outage(verdict.status)

        reading = self.load.current(ts) if self.load else None
        if verdict.is_fault and reading is not None:
            verdict = apply_load(
                verdict, m, reading, self.config.busy_load_pct, is_loss_or_outage)
        if verdict.is_fault and verdict.kind in ISP_LINE_KINDS:
            verdict = apply_hop_deprioritisation(verdict, m)
        self.last_verdict = verdict
        outcome = await self.tracker.observe(verdict, is_loss_or_outage=is_loss_or_outage, reading=reading, ts=ts)
        if outcome.started is not None:
            if self.repository:
                task = asyncio.create_task(self._trigger_path_trace(outcome.started.id))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            logger.warning(f"🚨 Network Incident Started: {outcome.started.title} (ID: {outcome.started.id})")
            self._notify(self.on_incident_started, outcome.started)
        if outcome.resolved is not None:
            logger.info(
                f"✅ Network Incident Resolved: {outcome.resolved.title} (Duration: {outcome.resolved.duration_seconds}s)")
            self._notify(self.on_incident_resolved, outcome.resolved)

        diagnosis = SystemDiagnosis(
            status=verdict.status,
            cause_code=verdict.cause,
            title=verdict.title,
            description=verdict.description,
            active_incident=self.tracker.active_incident,
            timestamp=ts,
            load_pct=reading.pct if reading else None,
        )
        self.last_diagnosis = diagnosis
        return diagnosis

    @staticmethod
    def _notify(callback: Optional[Callable[[Incident], None]], incident: Incident) -> None:
        if callback is None:
            return
        try:
            callback(incident)
        except Exception as e:
            logger.error(f"Incident callback failed: {e}", exc_info=True)

    async def cancel_background(self) -> None:
        """Cancels the background tasks (path traces) and waits for them; call before the repository closes."""
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _trigger_path_trace(self, incident_id: int):
        """Asynchronously runs MTR path trace when an incident starts."""
        with tracer.start_as_current_span("incident.path_trace", attributes={"incident.id": incident_id}) as span:
            await self._path_trace(incident_id, span)

    async def _path_trace(self, incident_id: int, span) -> None:
        try:
            from pengumon.engine.mtr import MtrEngine
            from pengumon.models.probes import PathTraceHop
            engine = MtrEngine(target_host=self.config.path_trace_target, max_hops=15, timeout=1.5,
                               known_targets=self.known_targets)
            hops = await engine.run_trace()
            span.set_attribute("trace.hops", len(hops or []))
            if hops and self.repository:
                pt_hops = [
                    PathTraceHop(
                        hop=h.hop_number,
                        ip=h.ip or "*",
                        hostname=h.hostname or h.label,
                        latency_ms=h.rtt_ms,
                        loss_pct=100.0 if not h.ip else 0.0
                    )
                    for h in hops
                ]
                await self.repository.update_incident_path_trace(incident_id, pt_hops)
                if self.active_incident and self.active_incident.id == incident_id:
                    self.active_incident.path_trace = pt_hops
                logger.info(f"📍 Captured automated path trace for incident #{incident_id} ({len(pt_hops)} hops)")
        except Exception as ex:
            span.record_exception(ex)
            logger.warning(f"Error capturing path trace for incident {incident_id}: {ex}")
