"""Diagnostic classifier engine for network root-cause isolation.

OutageClassifier is the orchestrator: it owns the clock, the load reading and the callbacks. Each round it computes
metrics.round_metrics and hands them to ClassifierCore.step, which runs rules.diagnose -> rules.apply_speedtest ->
rules.apply_load -> rules.apply_hop_deprioritization -> tracker.IncidentTracker.observe; the side effects (path
trace, log lines, callbacks) stay in OutageClassifier. The outage-rules preview (engine.replay) drives a ClassifierCore of its own with
the timestamps of stored samples. The pieces:

* verdict.py   Verdict, the frozen result of a round; its `kind` drives control flow, the texts are for people
* metrics.py   CategoryMetrics / RoundMetrics, computed once per round
* rules.py     the diagnosis as pure functions of the round metrics
* tracker.py   the incident state machine: onset, escalation, recovery

time.time() is called here and only here, and OutageClassifier must stay in this module: the characterization
golden patches `time` and `_trigger_path_trace` on pengumon.engine.classifier.
"""

import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from pengumon.incidents.model import SystemDiagnosis, Incident
from pengumon.models.probes import ProbeSample, TargetStats
from pengumon.incidents.taxonomy import ISP_LINE_KINDS
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.load import LineLoad, LoadReading
from pengumon.engine.classifier.metrics import CategoryMetrics, RoundMetrics, round_metrics
from pengumon.engine.classifier.rules import apply_hop_deprioritization, apply_load, apply_speedtest, diagnose
from pengumon.engine.classifier.tracker import IncidentTracker, TrackerOutcome
from pengumon.engine.classifier.verdict import NOMINAL, Verdict
from pengumon.lines import GENERIC, LineWording, wording
from pengumon.observability.telemetry import tracer

logger = logging.getLogger("pengumon.classifier")

PATH_TRACE_MAX_HOPS = 15  # the traceroute when an incident starts

__all__ = [
    "OutageClassifier", "ClassifierCore", "StepResult", "Verdict", "NOMINAL", "CategoryMetrics", "RoundMetrics",
    "IncidentTracker", "TrackerOutcome",
]


@dataclass
class StepResult:
    """What one ClassifierCore.step concluded. `is_loss_or_outage` is judged on the base diagnosis (before the load
    adjustment); `reading` is the load reading the step was given; `outcome` says which incident started, escalated
    or resolved this round."""

    verdict: Verdict
    is_loss_or_outage: bool
    reading: Optional[LoadReading]
    outcome: TrackerOutcome


class ClassifierCore:
    """The verdict logic of one round, without a clock, callbacks or background tasks: diagnose, the load and
    hop-deprioritization adjustments, and the incident tracker. Owns the tracker and the time of the last upstream
    issue (the DNS cooldown). `config` is read at every step, so a change to it applies from the next round.

    The live OutageClassifier and the outage-rules replay both run their rounds through `step`, so the replay's
    verdicts are the live code's."""

    def __init__(self, config: ClassifierConfig, repository: Optional[DatabaseRepository] = None,
                 line: LineWording = GENERIC):
        self.config = config
        self.line = line  # how access-line faults are described (pengumon.lines)
        self.tracker = IncidentTracker(config, repository)
        self.last_upstream_issue_ts: float = 0.0

    async def step(self, m: RoundMetrics, ts: float, reading: Optional[LoadReading],
                   testing: bool = False) -> StepResult:
        """Judges one round's metrics at time `ts`, with the line load `reading` of this round (None: unknown).
        `testing`: a speed test is running (or its probes are still in the judged window): a fault that is not a hard
        outage is "busy_speedtest", and the tracker holds still for that round (see below)."""
        config = self.config
        previous_issue_ts = self.last_upstream_issue_ts
        if m.upstream_issue(config):
            self.last_upstream_issue_ts = ts

        # Classify root cause based on sustained rolling metrics
        verdict = diagnose(m, config, ts, self.last_upstream_issue_ts, self.line)

        # Determine if degradation is packet loss / hard outage vs pure latency / jitter
        is_loss_or_outage = m.is_loss_or_outage(verdict.status, config)

        if testing and verdict.is_fault:
            verdict = apply_speedtest(verdict)
            if verdict.kind == "busy_speedtest":
                # A round under the test's own load says nothing about the line either way, so the tracker does not
                # see it: it neither builds toward an incident nor counts as a recovery (an incident already open
                # stays open across the test instead of being resolved by it and opened again after). Nor does the
                # test's own load start the DNS cooldown, which would hide a real DNS fault that follows it.
                self.last_upstream_issue_ts = previous_issue_ts
                return StepResult(verdict, is_loss_or_outage, reading, TrackerOutcome())
        if verdict.is_fault and reading is not None:
            verdict = apply_load(verdict, m, reading, config, is_loss_or_outage)
        if verdict.is_fault and verdict.kind in ISP_LINE_KINDS:
            verdict = apply_hop_deprioritization(verdict, m, config)
        outcome = await self.tracker.observe(verdict, is_loss_or_outage=is_loss_or_outage, reading=reading, ts=ts)
        return StepResult(verdict, is_loss_or_outage, reading, outcome)


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
        on_incident_escalated: Optional[Callable[[Incident], None]] = None,
    ):
        # Plain synchronous callbacks that must return quickly (the notifier only schedules a task).
        self.on_incident_started = on_incident_started
        self.on_incident_resolved = on_incident_resolved
        self.on_incident_escalated = on_incident_escalated  # an active incident's severity rose to critical
        self.repository = repository
        self.known_targets = known_targets if known_targets is not None else []  # shared: a path edit updates it
        self.load = load
        self.core = ClassifierCore(config, repository, wording(line_technology))
        self.last_diagnosis: SystemDiagnosis = SystemDiagnosis()
        self.last_verdict: Verdict = NOMINAL
        # Whether a speed test is masking the verdicts now; set by the speedtest component (runner.is_masking)
        self.speedtest_active: Optional[Callable[[], bool]] = None
        self._last_measured_ts: float = 0.0  # when evaluate last ran: the time probing stopped, if it does
        # Strong references to background tasks: the event loop holds tasks only weakly.
        self._background: set = set()

    # The core's state, under the names callers and tests have always used.
    @property
    def config(self) -> ClassifierConfig:
        return self.core.config

    @property
    def line(self) -> LineWording:
        return self.core.line

    @line.setter
    def line(self, line: LineWording) -> None:
        self.core.line = line

    @property
    def tracker(self) -> IncidentTracker:
        return self.core.tracker

    @property
    def last_upstream_issue_ts(self) -> float:
        return self.core.last_upstream_issue_ts

    # State that lives in the tracker, under the names callers and tests have always used.
    @property
    def active_incident(self) -> Optional[Incident]:
        return self.tracker.active_incident

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

    async def not_probing(self, reason: str, fix: Optional[str]) -> SystemDiagnosis:
        """The diagnosis while this host cannot send pings (Prober.probing_problem): nothing was measured, so no
        verdict about the network and no new incident. An incident that is open ends at the last round measured, with a
        note that measuring stopped; the callbacks hear nothing (it is not a recovery, so no "resolved" goes out). Says
        why, and the one step that fixes it."""
        if self.tracker.active_incident is not None:
            ended = await self.tracker.stop_measuring(self._last_measured_ts or time.time())
            logger.warning(f"Network incident #{ended.id} ended: measuring stopped ({reason}); not a recovery")
        text = ("Pengumon is running but cannot send pings from this computer, so it is measuring nothing and reports no "
                f"outage. Why: {reason}.")
        if fix:
            text += f" To fix it, run this once: {fix}"
        self.last_diagnosis = SystemDiagnosis(
            status="unknown", cause_code="nominal", title="Not probing: pings are not allowed", description=text,
            active_incident=self.tracker.active_incident, timestamp=time.time())
        return self.last_diagnosis

    async def evaluate(
        self,
        samples: List[ProbeSample],
        stats: Dict[str, TargetStats]
    ) -> SystemDiagnosis:
        """Evaluates the latest probe samples against the diagnostic matrix."""
        ts = self._last_measured_ts = time.time()

        # Each target's window (its last classifier.window_probes probes), summed up once for the round
        m = round_metrics(stats, samples, self.config.window_probes)
        reading = self.load.current(ts) if self.load else None
        result = await self.core.step(m, ts, reading, testing=bool(self.speedtest_active and self.speedtest_active()))
        verdict, outcome = result.verdict, result.outcome
        self.last_verdict = verdict
        if outcome.started is not None:
            if self.repository:
                task = asyncio.create_task(self._trigger_path_trace(outcome.started.id))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            logger.warning(f"🚨 Network Incident Started: {outcome.started.title} (ID: {outcome.started.id})")
            self._notify(self.on_incident_started, outcome.started)
        if outcome.escalated is not None:
            logger.warning(f"🚨 Network Incident Escalated to critical: {outcome.escalated.title} "
                           f"(ID: {outcome.escalated.id})")
            self._notify(self.on_incident_escalated, outcome.escalated)
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
            engine = MtrEngine(target_host=self.config.path_trace_target, max_hops=PATH_TRACE_MAX_HOPS, timeout=1.5,
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
