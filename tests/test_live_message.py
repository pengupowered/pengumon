"""The device part of the live message: summaries by device id (no channels), rebuilt only when a snapshot changes,
the live line capacity and the incidents version."""

import asyncio
import json
from types import SimpleNamespace

from pengumon.capabilities import BAND_2G, BAND_5G, Client, ClientsReading, MeshNode, MeshReading
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.summary import LiveSummaries, summarize_snapshot
from pengumon.lines import docsis
from pengumon.db import storage
from pengumon.devices import component as devices_component
from pengumon.engine.line_load import line_status, live_part as line_part
from pengumon.engine.loop import check_live_parts, probe_background_worker
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident, SystemDiagnosis
from pengumon.lines.docsis_model import DocsisChannel

from device_helpers import build_modem, build_router


def line_snapshot():
    channels = [DocsisChannel(channel_id=i, direction="downstream", frequency_mhz=600.0 + i, power_dbmv=1.0, snr_db=38.0)
                for i in range(1, 4)]
    return docsis.summarize(channels, 100.0)


def registry():
    modem, router = build_modem(password="pw"), build_router(password="pw", id="wifi")
    modem.monitor.latest_snapshot = line_snapshot()
    router.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=101.0, status="healthy", cpu_usage_pct=12.0,
                                                           grades={"cpu": "good"}, wan_rx_rate_mbps=3.0)
    return DeviceRegistry([modem, router]), modem, router


def test_a_summary_has_status_grades_and_headlines_but_no_channels():
    reg, modem, router = registry()
    summaries = LiveSummaries(reg).current()
    assert set(summaries) == {"modem", "wifi"}
    line = summaries["modem"]
    assert line["kind"] == "line" and line["status"] == "healthy" and line["channel_count"] == 3
    assert line["grades"]["ds_snr"] == "good" and line["downstream_snr_avg"] == 38.0
    assert "channels" not in line and "channel_id" not in json.dumps(summaries)
    assert summaries["wifi"]["kind"] == "router" and summaries["wifi"]["cpu_usage_pct"] == 12.0
    assert summaries["wifi"]["grades"] == {"cpu": "good"}


def test_a_router_summary_carries_the_uptime_the_temperature_and_the_wan_link():
    """The router card draws them from the live message (the card paints every summary): a summary without them would
    blank them a second after the first full reading."""
    reg, modem, router = registry()
    router.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=101.0, status="healthy", uptime_s=7234, cpu_temp_c=64.1,
                                                           wan_link_mbps=2500)
    summary = LiveSummaries(reg).current()["wifi"]
    assert (summary["uptime_s"], summary["cpu_temp_c"], summary["wan_link_mbps"]) == (7234, 64.1, 2500)
    router.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=111.0, status="healthy", wan_link_mbps=0)
    assert LiveSummaries(reg).current()["wifi"]["wan_link_mbps"] == 0  # 0 is "no link", not "not reported"


def test_a_summary_is_rebuilt_only_when_the_snapshot_changes():
    reg, modem, router = registry()
    live = LiveSummaries(reg)
    first = live.current()
    for _ in range(50):  # fifty ticks with no poll in between
        assert live.current() == first
    assert live.builds == 2
    router.monitor.latest_snapshot = RouterMetricsSnapshot(timestamp=111.0, status="warning")
    again = live.current()
    assert live.builds == 3 and again["modem"] is first["modem"] and again["wifi"]["status"] == "warning"


def test_disabled_unpolled_and_missing_devices_have_no_summary():
    modem = build_modem(password="pw")
    off = build_router(password="pw", id="off", enabled=False)
    off.monitor.latest_snapshot = RouterMetricsSnapshot(status="unconfigured")
    assert LiveSummaries(DeviceRegistry([modem, off])).current() == {}  # one not polled yet, one switched off
    assert LiveSummaries(None).current() == {}
    assert LiveSummaries(DeviceRegistry([])).current() == {}


def test_an_error_snapshot_summary_carries_the_message_not_a_credential():
    reg, modem, _ = registry()
    modem.monitor.latest_snapshot = docsis.DocsisSignalSnapshot(status="error", error_message="Failed: refused")
    summary = summarize_snapshot(modem.monitor.latest_snapshot)
    assert summary["status"] == "error" and summary["error_message"] == "Failed: refused" and summary["grades"] == {}


def clients_reading():
    return ClientsReading((
        Client(key="a", label_parts=("Ann's phone",), ip="192.0.2.12", mac="AA:AA:AA:00:00:02", connection="wifi",
               band=BAND_5G, node="0000a2"),
        Client(key="b", label_parts=("Desk PC",), ip="192.0.2.11", mac="AA:AA:AA:00:00:01", connection="wired",
               node="0000a1"),
        Client(key="c", label_parts=("Plug",), connection="wifi", band=BAND_2G, node="0000a1"),
        Client(key="d", label_parts=("Old laptop",), connection="wifi", band=BAND_5G, online=False),
    ))


def test_a_router_that_lists_clients_sends_the_counts_and_never_a_client():
    reg, modem, router = registry()
    router.monitor.latest_clients, router.monitor.clients_at = clients_reading(), 105.0
    router.monitor.latest_mesh = MeshReading((MeshNode("0000a1", "Hall", clients=2, wired=1), MeshNode("0000a2", "Study", clients=1)))
    summaries = LiveSummaries(reg).current()
    assert summaries["wifi"]["clients"] == {
        "ts": 105.0, "online": 3, "wired": 1, "by_band": {BAND_5G: 1, BAND_2G: 1}, "by_node": {"0000a2": 1, "0000a1": 2},
        "nodes": [{"id": "0000a1", "label": "Hall", "clients": 2, "wired": 1, "online": None, "backhaul": None,
                   "backhaul_band": None, "backhaul_rssi_dbm": None},
                  {"id": "0000a2", "label": "Study", "clients": 1, "wired": 0, "online": None, "backhaul": None,
                   "backhaul_band": None, "backhaul_rssi_dbm": None}]}
    text = json.dumps(summaries)
    for private in ("Ann", "Desk PC", "Plug", "Old laptop", "192.0.2.", "AA:AA:AA"):  # no name, address or MAC
        assert private not in text
    assert "clients" not in summaries["modem"]


def test_a_new_client_list_rebuilds_the_summary_and_none_read_means_no_counts():
    reg, modem, router = registry()
    live = LiveSummaries(reg)
    assert "clients" not in live.current()["wifi"]  # no list read yet
    builds = live.builds
    router.monitor.latest_clients = clients_reading()
    assert live.current()["wifi"]["clients"]["online"] == 3 and live.builds == builds + 1
    assert live.current()["wifi"]["clients"]["nodes"] == [] and live.builds == builds + 1  # unchanged: no rebuild
    router.monitor.latest_clients = ClientsReading((Client(key="z", connection="wired"),))
    assert live.current()["wifi"]["clients"]["online"] == 1 and live.builds == builds + 2


def test_the_line_part_reports_the_load_and_the_capacity():
    assert line_status(SimpleNamespace(down_mbps=1100.0, up_mbps=40.0), 12.5) == {
        "load_pct": 12.5, "down_mbps": 1100.0, "up_mbps": 40.0}
    assert line_status(None, None) == {"load_pct": None, "down_mbps": None, "up_mbps": None}


async def test_the_worker_publishes_devices_line_and_the_incidents_version(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "l.db"))
    await repo.initialize()
    await repo.create_incident(Incident(start_time=1.0, cause_code="unknown_outage", title="t", description="d",
                                        severity="warning", affected_targets=[]))
    reg, _, _ = registry()
    published = []

    class Spy:
        active_connections = [object()]

        def publish(self, msg):
            published.append(msg)

    class Prober:
        stats = {}

        def probing_problem(self):
            return None

        async def execute_probe_round(self, targets, dns_probes):
            return []

    class Classifier:
        async def evaluate(self, samples, stats):
            return SystemDiagnosis(load_pct=7.5)

    cfg = AppConfig()
    cfg.prober.interval_seconds = 0.05
    parts = (devices_component.live_part(reg), storage.live_part(repo),
             line_part(SimpleNamespace(down_mbps=1100.0, up_mbps=40.0)))
    task = asyncio.create_task(probe_background_worker(cfg, Prober(), Classifier(), repo, ws=Spy(), live_parts=parts))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    msg = published[0]
    assert set(msg.devices) == {"modem", "wifi"} and msg.line == {"load_pct": 7.5, "down_mbps": 1100.0, "up_mbps": 40.0}
    assert msg.incidents_version == repo.incidents_version > 0
    await repo.close()


async def test_the_worker_does_not_classify_while_no_pings_can_be_sent(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "n.db"))
    await repo.initialize()
    published = []

    class Spy:
        active_connections = [object()]

        def publish(self, msg):
            published.append(msg)

    class Prober:
        stats = {}

        def probing_problem(self):
            return ("no ICMP", "echo fix")

        async def execute_probe_round(self, targets, dns_probes):
            return []

    class Classifier:
        async def evaluate(self, samples, stats):
            raise AssertionError("a verdict about the network, with nothing measured")

        async def not_probing(self, reason, fix):
            return SystemDiagnosis(status="unknown", title=reason, description=fix)

    cfg = AppConfig()
    cfg.prober.interval_seconds = 0.05
    task = asyncio.create_task(probe_background_worker(cfg, Prober(), Classifier(), repo, ws=Spy()))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert published and published[0].diagnosis.status == "unknown" and published[0].diagnosis.title == "no ICMP"
    await repo.close()


def test_a_live_part_must_name_a_field_the_round_leaves_to_the_components():
    import pytest
    from pengumon.components import LivePart
    check_live_parts([LivePart("devices", dict), LivePart("line", dict), LivePart("incidents_version", int)])
    for bad in ([LivePart("nope", dict)], [LivePart("diagnosis", dict)], [LivePart("line", dict), LivePart("line", dict)]):
        with pytest.raises(ValueError):
            check_live_parts(bad)


def test_the_live_message_and_the_router_snapshot_have_only_the_canonical_fields():
    from pengumon.live.message import LiveStreamMessage
    assert set(LiveStreamMessage.model_fields) == {"timestamp", "diagnosis", "targets", "devices", "line", "incidents_version",
                                                    "settings_revision"}
    router = set(RouterMetricsSnapshot.model_fields)
    assert {"wan_rx_rate_mbps", "wan_tx_rate_mbps", "client_count"} <= router
    assert not {"wan_rx_mbps", "wan_tx_mbps", "active_clients_count"} & router
