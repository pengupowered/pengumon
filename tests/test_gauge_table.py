"""observability/gauges.py: one table behind the Prometheus text and the OTel observable gauges."""

import time
from types import SimpleNamespace

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import TargetConfig
from pengumon.devices.registry import DeviceRegistry
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.prober import NetworkProber
from pengumon.observability.prometheus import render_metrics
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.capabilities import BAND_5G, Client, ClientsReading, MeshNode, MeshReading
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.observability import export
from pengumon.observability.export import observe_state
from pengumon.observability.gauges import GAUGES
from pengumon.observability.runtime import loop_monitor, process_stats

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
    modem.monitor.latest_snapshot = DocsisSignalSnapshot(status="healthy", channels=[
        DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0, snr_db=38.0,
                      grades={"power": "good"}),
        DocsisChannel(channel_id=2, direction="upstream", frequency_mhz=30.0, power_dbmv=42.0)])
    router.monitor.latest_snapshot = RouterMetricsSnapshot(
        status="warning", wan_rx_rate_mbps=10.0, wan_tx_rate_mbps=1.0, cpu_usage_pct=20.0, mem_usage_pct=50.0)
    router.monitor.latest_clients = ClientsReading((Client(key="a", connection="wifi", band=BAND_5G, rssi_dbm=-80),))
    router.monitor.latest_mesh = MeshReading((MeshNode("0000a1", "Hall", online=True, backhaul="wifi",
                                                       backhaul_rssi_dbm=-60),))
    app_state.devices = DeviceRegistry([modem, router])
    status = SimpleNamespace(ok=True, latency_ms=200.0)
    app_state.service_checker = SimpleNamespace(services=[SimpleNamespace(id="app_a")], status={"app_a": status})
    app_state.repository = SimpleNamespace(count_network_incidents=_count_incidents, sample_buffer_size=3,
                                           file_sizes=lambda: {"main": 4096, "wal": 0})
    app_state.notifier = SimpleNamespace(pending_count=lambda: 2)
    app_state.speedtest = SimpleNamespace(last_ok={"down_mbps": 280.5, "up_mbps": 19.0, "idle_ms": 12.0,
                                                   "loaded_down_ms": 50.0, "loaded_up_ms": None})
    loop_monitor.tasks = 12  # (counted by the monitor on the loop)


# Process figures some platforms do not give (process_stats: whatever the platform's calls give; open descriptors on
# Linux, macOS and Windows): their gauges are left out there, by design (docs/telemetry.md).
PLATFORM_GAUGES = {"rss": "pengumon_process_memory_rss_bytes", "fds": "pengumon_process_open_fds"}


async def test_every_gauge_of_the_table_appears_in_the_prometheus_text_and_the_otel_observations(app_state):
    _populate(app_state)
    text = await render_metrics(app_state)
    observe_state(app_state)
    not_here = {PLATFORM_GAUGES[k] for k, v in process_stats().items() if v is None and k in PLATFORM_GAUGES}
    try:
        for gauge in GAUGES:
            if gauge.prom in not_here:
                assert f"# TYPE {gauge.prom} gauge\n" not in text, gauge.prom  # left out, not reported as 0
                continue
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


async def test_the_speed_test_gauges_report_the_last_good_test_and_nothing_before_one(app_state):
    _populate(app_state)
    text = await render_metrics(app_state)
    assert "pengumon_speedtest_download_mbps 280.5" in text and "pengumon_speedtest_upload_mbps 19.0" in text
    assert 'pengumon_speedtest_loaded_latency_ms{phase="idle"} 12.0' in text
    assert 'pengumon_speedtest_loaded_latency_ms{phase="download"} 50.0' in text
    assert 'phase="upload"' not in text                       # no loaded upload latency in that test: no sample
    app_state.speedtest.last_ok = None                          # no test has worked yet
    text = await render_metrics(app_state)
    assert "pengumon_speedtest_download_mbps" not in text and "pengumon_speedtest_loaded_latency_ms" not in text
    app_state.speedtest = None                                  # the component did not start
    assert "pengumon_speedtest" not in await render_metrics(app_state)
