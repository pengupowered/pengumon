"""ClassifierCore: the verdict logic of a round without the clock or the side effects, and the escalation callback.

The outage-rules preview replays stored samples through ClassifierCore.step, so the core must reach the live
OutageClassifier's verdicts and incidents from the round metrics, the time and the load reading alone.
"""

from pengumon.engine.classifier import ClassifierCore, OutageClassifier, StepResult
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import ProbeSample, TargetStats

from test_classifier_characterization import CONFIGS, build_golden
from test_incident_tracker import DOWN, LOSS, Clock, FakeRepository, feed, make


def _incident_key(incident):
    if incident is None:
        return None
    return (incident.start_time, incident.end_time, incident.kind, incident.severity, incident.cause_code,
            incident.title, incident.resolved)


async def test_a_separate_core_fed_only_metrics_time_and_load_matches_the_live_classifier(monkeypatch):
    """Every round of the characterization scenarios (every config) goes through a second, independent core with
    no repository; its verdicts and incidents must be the live classifier's, round for round."""
    compared = {"rounds": 0, "started": 0, "resolved": 0, "escalated": 0}

    def tap(classifier):
        twin = ClassifierCore(classifier.config, None, classifier.line)
        live_step = classifier.core.step

        async def step(m, ts, reading, testing=False):
            live = await live_step(m, ts, reading, testing)
            other = await twin.step(m, ts, reading, testing)
            assert isinstance(live, StepResult) and live.reading is reading
            assert other.verdict == live.verdict
            assert other.is_loss_or_outage == live.is_loss_or_outage
            for what in ("started", "resolved", "escalated"):
                assert _incident_key(getattr(other.outcome, what)) == _incident_key(getattr(live.outcome, what))
                compared[what] += getattr(live.outcome, what) is not None
            assert _incident_key(twin.tracker.active_incident) == _incident_key(classifier.tracker.active_incident)
            assert twin.last_upstream_issue_ts == classifier.last_upstream_issue_ts
            compared["rounds"] += 1
            return live

        classifier.core.step = step

    await build_golden(monkeypatch, tap=tap)
    assert compared["rounds"] > 1000 and compared["started"] > 20 and compared["resolved"] > 20
    assert compared["escalated"] > 0  # the scenario escalates warning incidents to critical
    assert len(CONFIGS) == 4


def _stats(*specs):
    return {tid: TargetStats(target_id=tid, name=tid, category=cat, address="192.0.2.1", enabled=True,
                             recent_latencies=list(window))
            for tid, cat, window in specs}


async def test_core_step_judges_metrics_at_the_time_it_is_given():
    core = ClassifierCore(ClassifierConfig(consecutive_failures_for_incident=2))
    lost = [None] * 10
    stats = _stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", lost))
    m = round_metrics(stats, [])
    first = await core.step(m, 500.0, None)
    assert first.verdict.kind == "isp_outage" and first.reading is None
    assert first.is_loss_or_outage and first.outcome.started is None
    second = await core.step(m, 501.0, None)
    assert second.outcome.started is not None
    assert second.outcome.started.start_time == 500.0  # backdated to the first bad round, from the given times
    assert core.last_upstream_issue_ts == 501.0


async def test_the_tracker_reports_an_escalation_once():
    repo = FakeRepository()
    tracker, clock = make(repo, failures=2), Clock()
    outcomes = await feed(tracker, LOSS, 2, clock)
    assert all(o.escalated is None for o in outcomes)
    incident = outcomes[1].started
    escalated = await feed(tracker, DOWN, 3, clock)
    assert [o.escalated for o in escalated] == [incident, None, None]
    assert incident.severity == "critical"


async def test_an_incident_that_starts_critical_is_not_an_escalation():
    tracker, clock = make(None, failures=2), Clock()
    outcomes = await feed(tracker, DOWN, 4, clock)
    assert outcomes[1].started is not None
    assert all(o.escalated is None for o in outcomes)


async def test_escalation_without_a_repository_is_reported_too():
    tracker, clock = make(None, failures=2), Clock()
    await feed(tracker, LOSS, 2, clock)
    assert (await feed(tracker, DOWN, 1, clock))[0].escalated is tracker.active_incident


def _round(local_ok: bool):
    return [ProbeSample(target_id="lan", success=local_ok, latency_ms=1.0 if local_ok else None,
                        packet_loss_pct=0.0 if local_ok else 100.0)]


async def test_the_classifier_calls_on_incident_escalated_when_severity_rises(monkeypatch):
    started, escalated, resolved = [], [], []
    classifier = OutageClassifier(
        config=ClassifierConfig(consecutive_failures_for_incident=2), repository=None,
        on_incident_started=started.append, on_incident_escalated=escalated.append,
        on_incident_resolved=resolved.append,
    )
    stats = {"lan": TargetStats(target_id="lan", name="LAN", category="local", address="192.0.2.1", enabled=True)}
    # 2 of 10 lost: local loss 20 % (degraded, warning) opens the incident ...
    stats["lan"].recent_latencies = [1.0] * 8 + [None] * 2
    await classifier.evaluate(_round(False), stats)
    await classifier.evaluate(_round(False), stats)
    assert len(started) == 1 and started[0].severity == "warning" and escalated == []
    # ... then 5 of 10 lost: down, critical
    stats["lan"].recent_latencies = [1.0] * 5 + [None] * 5
    await classifier.evaluate(_round(False), stats)
    assert escalated == [started[0]] and started[0].severity == "critical"
    await classifier.evaluate(_round(False), stats)
    assert len(escalated) == 1


async def test_an_escalation_callback_that_raises_is_contained():
    def boom(incident):
        raise RuntimeError("notifier down")

    classifier = OutageClassifier(config=ClassifierConfig(consecutive_failures_for_incident=1),
                                  on_incident_escalated=boom)
    stats = {"lan": TargetStats(target_id="lan", name="LAN", category="local", address="192.0.2.1", enabled=True)}
    stats["lan"].recent_latencies = [1.0] * 8 + [None] * 2
    await classifier.evaluate(_round(False), stats)
    stats["lan"].recent_latencies = [None] * 10
    diagnosis = await classifier.evaluate(_round(False), stats)
    assert diagnosis.status == "down" and classifier.active_incident.severity == "critical"


