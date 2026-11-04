"""Unit tests for the OutageClassifier diagnostic engine."""

import asyncio

import pytest
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import ProbeSample, TargetStats
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.classifier.metrics import category_metrics


@pytest.fixture
def classifier():
    config = ClassifierConfig(
        local_latency_spike_threshold_ms=35.0,
        gateway_latency_spike_threshold_ms=50.0,
        public_latency_spike_threshold_ms=120.0,
        dns_timeout_threshold_ms=400.0,
        consecutive_failures_for_incident=2,
    )
    return OutageClassifier(config=config, repository=None)


@pytest.mark.asyncio
async def test_nominal_state(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True),
    }

    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.2),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.1),
        ProbeSample(target_id="public", success=True, latency_ms=14.5),
        ProbeSample(target_id="dns", success=True, latency_ms=18.0),
    ]

    diagnosis = await classifier.evaluate(samples, stats)
    assert diagnosis.status == "healthy"
    assert diagnosis.cause_code == "nominal"



@pytest.mark.asyncio
async def test_isp_last_mile_drop(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True),
        "isp": TargetStats(target_id="isp", name="ISP Regional Hub", category="isp", address="198.51.100.1", enabled=True),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True),
    }

    # Local & Gateway are fine, but ISP & Public Internet & DNS are timing out
    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.2),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
        ProbeSample(target_id="isp", success=False, latency_ms=None, packet_loss_pct=100.0),
        ProbeSample(target_id="public", success=False, latency_ms=None, packet_loss_pct=100.0),
        ProbeSample(target_id="dns", success=False, latency_ms=None, packet_loss_pct=100.0),
    ]

    diagnosis = await classifier.evaluate(samples, stats)
    assert diagnosis.status == "down"
    assert diagnosis.cause_code == "access_line_drop"
    assert "ISP" in diagnosis.title


@pytest.mark.asyncio
async def test_dynamic_isp_name_in_diagnosis(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Custom Router", category="local", address="192.168.1.1", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="ONT Gateway", category="gateway", address="192.168.1.254", enabled=True),
        "isp": TargetStats(target_id="isp", name="Verizon Fios Headend", category="isp", address="100.64.0.1", enabled=True),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True),
    }

    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.0),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
        ProbeSample(target_id="isp", success=False, latency_ms=None, packet_loss_pct=100.0),
        ProbeSample(target_id="public", success=False, latency_ms=None, packet_loss_pct=100.0),
    ]

    diagnosis = await classifier.evaluate(samples, stats)
    assert diagnosis.status == "down"
    assert "Verizon Fios Headend" in diagnosis.title


@pytest.mark.asyncio
async def test_dns_outage_isolation(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True),
    }

    # All IP pings are low-latency and working, but DNS lookups fail
    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.2),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
        ProbeSample(target_id="public", success=True, latency_ms=14.0),
        ProbeSample(target_id="dns", success=False, latency_ms=None, packet_loss_pct=100.0),
    ]

    diagnosis = await classifier.evaluate(samples, stats)
    assert diagnosis.status == "down"
    assert diagnosis.cause_code == "dns_resolver_failure"
    assert "DNS" in diagnosis.title


@pytest.mark.asyncio
async def test_local_wifi_mesh_drop(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True),
    }

    # Hop 1 fails
    samples = [
        ProbeSample(target_id="local", success=False, latency_ms=None, packet_loss_pct=100.0),
        ProbeSample(target_id="gateway", success=False, latency_ms=None, packet_loss_pct=100.0),
        ProbeSample(target_id="public", success=False, latency_ms=None, packet_loss_pct=100.0),
    ]

    diagnosis = await classifier.evaluate(samples, stats)
    assert diagnosis.status == "down"
    assert diagnosis.cause_code == "local_wifi_mesh_drop"


@pytest.mark.asyncio
async def test_transient_latency_spike_does_not_trigger_incident(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True, recent_latencies=[1.2]*10),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True, recent_latencies=[2.0]*10),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True, recent_latencies=[14.0]*9 + [500.0]),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True, recent_latencies=[18.0]*10),
    }

    # Spike for 2 evaluations
    for _ in range(2):
        samples = [
            ProbeSample(target_id="local", success=True, latency_ms=1.2),
            ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
            ProbeSample(target_id="public", success=True, latency_ms=250.0),
            ProbeSample(target_id="dns", success=True, latency_ms=18.0),
        ]
        diagnosis = await classifier.evaluate(samples, stats)
        # Status shows degraded, but no active incident started because it's transient (< 10 seconds)
        assert diagnosis.status == "degraded"
        assert diagnosis.active_incident is None
        assert classifier.active_incident is None


@pytest.mark.asyncio
async def test_prolonged_latency_triggers_incident(classifier):
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True, recent_latencies=[1.2]*10),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True, recent_latencies=[2.0]*10),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True, recent_latencies=[250.0]*10),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True, recent_latencies=[18.0]*10),
    }

    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.2),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
        ProbeSample(target_id="public", success=True, latency_ms=250.0),
        ProbeSample(target_id="dns", success=True, latency_ms=18.0),
    ]

    # Evaluate 10 times to meet consecutive_failures_for_latency_incident (default 10)
    for _ in range(10):
        diagnosis = await classifier.evaluate(samples, stats)

    assert diagnosis.status == "degraded"
    assert diagnosis.active_incident is not None
    assert "Latency" in diagnosis.active_incident.title


@pytest.mark.asyncio
async def test_rolling_jitter_clears_in_10_samples(classifier):
    # Single spike at index 0, followed by 10 clean samples
    st = TargetStats(
        target_id="public",
        name="Public",
        category="public",
        address="1.1.1.1",
        enabled=True,
        recent_latencies=[14.0, 14.1, 14.0, 14.2, 14.0, 13.9, 14.1, 14.0, 14.0, 14.0],
    )
    stats = {"public": st}
    
    jitter = category_metrics("public", [st], []).jitter_ms
    assert jitter is not None
    assert jitter < 1.0  # Jitter is very low (<1ms) when samples are stable



class _IncidentRepository:
    """Just the calls the classifier makes when an incident starts (no database)."""

    async def create_incident(self, incident):
        return 42

    async def update_incident_path_trace(self, incident_id, hops):
        pass


@pytest.mark.asyncio
async def test_path_trace_task_is_referenced_until_it_finishes(monkeypatch):
    from pengumon.engine.mtr import MtrEngine

    release = asyncio.Event()
    started = []

    async def slow_trace(self):
        started.append(True)
        await release.wait()
        return []

    monkeypatch.setattr(MtrEngine, "run_trace", slow_trace)
    config = ClassifierConfig(
        local_latency_spike_threshold_ms=35.0,
        gateway_latency_spike_threshold_ms=50.0,
        public_latency_spike_threshold_ms=120.0,
        dns_timeout_threshold_ms=400.0,
        consecutive_failures_for_incident=2,
    )
    classifier = OutageClassifier(config=config, repository=_IncidentRepository())
    stats = {
        "local": TargetStats(target_id="local", name="Local", category="local", address="192.168.1.1", enabled=True, recent_latencies=[1.2]*10),
        "gateway": TargetStats(target_id="gateway", name="Gateway", category="gateway", address="192.168.100.1", enabled=True, recent_latencies=[2.0]*10),
        "public": TargetStats(target_id="public", name="Public", category="public", address="1.1.1.1", enabled=True, recent_latencies=[250.0]*10),
        "dns": TargetStats(target_id="dns", name="DNS", category="dns", address="google.com", enabled=True, recent_latencies=[18.0]*10),
    }
    samples = [
        ProbeSample(target_id="local", success=True, latency_ms=1.2),
        ProbeSample(target_id="gateway", success=True, latency_ms=2.0),
        ProbeSample(target_id="public", success=True, latency_ms=250.0),
        ProbeSample(target_id="dns", success=True, latency_ms=18.0),
    ]
    for _ in range(10):
        await classifier.evaluate(samples, stats)
    assert classifier.active_incident is not None and classifier.active_incident.id == 42

    assert len(classifier._background) == 1
    (task,) = classifier._background
    await asyncio.sleep(0)  # let the trace start
    assert started == [True]
    assert not task.done()

    release.set()
    await task
    assert task not in classifier._background
    assert classifier._background == set()


@pytest.mark.asyncio
async def test_no_pings_is_an_unknown_state_that_says_why_and_opens_no_incident(classifier):
    diagnosis = await classifier.not_probing("cannot open an ICMP socket", "echo fix")
    assert diagnosis.status == "unknown" and diagnosis.active_incident is None
    assert "cannot open an ICMP socket" in diagnosis.description and "echo fix" in diagnosis.description
    assert classifier.last_diagnosis is diagnosis


@pytest.mark.asyncio
async def test_pings_becoming_blocked_ends_an_open_incident_as_measuring_stopped_not_recovered(classifier):
    resolved = []
    classifier.on_incident_resolved = resolved.append
    stats = {c: TargetStats(target_id=c, name=c, category=c, address="192.0.2.1", enabled=True)
             for c in ("local", "gateway", "public")}
    samples = [ProbeSample(target_id=c, success=False, latency_ms=None, packet_loss_pct=100.0) for c in stats]
    for _ in range(2):
        await classifier.evaluate(samples, stats)
    incident = classifier.active_incident
    assert incident is not None and not incident.resolved
    measured = classifier._last_measured_ts

    diagnosis = await classifier.not_probing("cannot open an ICMP socket", None)
    assert classifier.active_incident is None and diagnosis.active_incident is None
    assert incident.resolved and incident.end_time == measured  # ended when probing stopped
    assert "Measuring stopped" in incident.description and "recovered" in incident.description
    assert resolved == []  # not announced as a recovery
