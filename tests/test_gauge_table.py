"""observability/gauges.py: one table behind the Prometheus text and the OTel observable gauges."""

import time
from types import SimpleNamespace

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import TargetConfig
from pengumon.devices.registry import DeviceRegistry
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.prober import NetworkProber
from pengumon.metrics import render_metrics
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.observability import export
from pengumon.observability.export import observe_state
from pengumon.observability.gauges import GAUGES

from device_helpers import build_modem, build_router

# The OTel names the Grafana dashboard (ops/observability/dashboards/pengumon.json) queries, as Prometheus names them.
DASHBOARD_OTEL_NAMES = {"pengumon.target.status", "pengumon.system.status", "pengumon.device.status",
                        "pengumon.incident.active", "pengumon.probe.loop.age"}


async def _count_incidents(**kwargs):
    return 3


def _populate(app_state):
    prober = NetworkProber()
    prober.register_target(TargetConfig(id="r", name="Router", address="192.168.1.1", category="local"))
    for stats in prober.stats.values():
        stats.last_ms, stats.avg_ms = 1.5, 2.5
    prober.last_round_ts = time.time()
    app_state.prober, app_state.classifier = prober, OutageClassifier(config=ClassifierConfig())
    app_state.classifier.last_diagnosis.load_pct = 12.0
    app_state.classifier.load = SimpleNamespace(down_mbps=100.0, up_mbps=10.0)
    modem, router = build_modem(password="pw"), build_router()
    modem.monitor.latest_snapshot = DocsisSignalSnapshot(status="healthy")
    router.monitor.latest_snapshot = RouterMetricsSnapshot(status="warning")
    app_state.devices = DeviceRegistry([modem, router])
    status = SimpleNamespace(ok=True, latency_ms=200.0)
    app_state.service_checker = SimpleNamespace(services=[SimpleNamespace(id="app_a")], status={"app_a": status})
    app_state.repository = SimpleNamespace(count_network_incidents=_count_incidents)


async def test_every_gauge_of_the_table_appears_in_the_prometheus_text_and_the_otel_observations(app_state):
    _populate(app_state)
    text = await render_metrics(app_state)
    observe_state(app_state)
    try:
        for gauge in GAUGES:
            assert f"# TYPE {gauge.prom} gauge\n" in text, gauge.prom
            if gauge.otel is not None:
                assert export._guard(gauge)(None), gauge.otel
    finally:
        observe_state(None)


async def test_prometheus_labels_a_device_gauge_by_device_id_like_the_otel_one(app_state):
    _populate(app_state)
    text = await render_metrics(app_state)
    assert 'pengumon_collector_status{device="modem",status="healthy"} 1.0' in text
    assert 'pengumon_collector_status{device="router",status="warning"} 1.0' in text
    assert "collector=" not in text


def test_the_table_has_unique_names_and_the_dashboard_gauges():
    assert len({g.prom for g in GAUGES}) == len(GAUGES)
    otel = [g.otel for g in GAUGES if g.otel]
    assert len(set(otel)) == len(otel) and DASHBOARD_OTEL_NAMES <= set(otel)
