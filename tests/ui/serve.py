"""Serves the real dashboard app for tests/test_ui_smoke.py, with the outside world replaced by fixed data.

Usage: python tests/ui/serve.py PORT   (PENGUMON_CONFIG names the config; run it with src on PYTHONPATH)

ICMP and DNS probes always succeed, with a latency that wiggles so sparklines change every second (nothing is sent, so the run is repeatable and opens no incidents of its own), and the
slow DOCSIS and router collectors answer with fixed snapshots instead of calling a modem and a router, graded the way the
real monitors grade them. A device with the id "router2" answers as a second, quieter router (the `tworouters`
variant); PENGUMON_SMOKE_LINE_TECHNOLOGY turns the modem driver into another line technology (the `unknownline`
variant: the page has no adapter for it).
"""
import os

import sys
import time

import uvicorn

from pengumon.devices.device import Device
from pengumon.engine.prober import NetworkProber
from pengumon.capabilities import BAND_2G, BAND_5G, Client, ClientsReading, MeshNode, MeshReading
from pengumon.drivers.xfinity_xb import XfinityXbDriver
from pengumon.lines.docsis import grade_snapshot, judge_errors
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample


def docsis_snapshot() -> DocsisSignalSnapshot:
    channels = [
        DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=531.0, power_dbmv=1.5, snr_db=38.2,
                      modulation="QAM256", uncorrectables=120),
        DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=537.0, power_dbmv=2.0, snr_db=31.5,
                      modulation="QAM256", uncorrectables=90210, uncorrectables_recent=42),
        DocsisChannel(channel_id=3, direction="downstream", frequency_mhz=543.0, power_dbmv=0.5, snr_db=37.9,
                      modulation="QAM256"),
        DocsisChannel(channel_id=1, direction="upstream", frequency_mhz=30.6, power_dbmv=44.5, modulation="SC-QAM"),
    ]
    snapshot = DocsisSignalSnapshot(
        timestamp=time.time(), downstream_power_avg=1.3, downstream_snr_avg=35.9, upstream_power_avg=44.5,
        uncorrectables_total=90330, uncorrectables_recent=42, uncorrectables_interval_seconds=1800.0,
        errors_affected_channels=1, errors_span_mhz=[537.0, 537.0], errors_probe_loss=False,
        channels=channels, status="warning")
    snapshot.errors_note = judge_errors(42, 1800.0, None, False)[1]  # a short burst: graded good, with a note
    return grade_snapshot(snapshot)


def router_snapshot(device_id: str = "router") -> RouterMetricsSnapshot:
    if device_id == "router2":  # a second router: quiet, nothing to warn about
        return RouterMetricsSnapshot(
            timestamp=time.time(), wan_rx_rate_mbps=5.2, wan_tx_rate_mbps=0.8, wan_rx_load_pct=1.0, wan_tx_load_pct=2.0,
            cpu_usage_pct=61.0, mem_usage_pct=40.0, client_count=1, status="healthy",
            grades={"cpu": "good", "mem": "good", "wan_rx": "good", "wan_tx": "good"})
    # memory above the server's 85 % warning line (pengumon.devices.monitors), so the meter shows the warn grade
    return RouterMetricsSnapshot(
        timestamp=time.time(), wan_rx_rate_mbps=123.4, wan_tx_rate_mbps=12.3, wan_rx_load_pct=24.7, wan_tx_load_pct=30.8,
        cpu_usage_pct=23.0, mem_usage_pct=91.0, client_count=17, status="healthy",
        grades={"cpu": "good", "mem": "warn", "wan_rx": "good", "wan_tx": "good"})


def clients_reading() -> ClientsReading:
    """A made-up client list (RFC 5737 addresses, MACs AA:AA:AA:...): two Wi-Fi bands, a cable, two nodes, one offline."""
    return ClientsReading((
        Client(key="1", label_parts=("Desk PC",), vendor="Acme", ip="192.0.2.11", mac="AA:AA:AA:00:00:01",
               connection="wired", node="0000a1"),
        Client(key="2", label_parts=("Ann's phone",), vendor="Example", kind="phone", ip="192.0.2.12", mac="AA:AA:AA:00:00:02",
               connection="wifi", band=BAND_5G, node="0000a2", rssi_dbm=-58, link_tx_mbps=866.0, link_rx_mbps=780.0,
               connected_s=97509),
        Client(key="3", label_parts=("Plug",), ip="192.0.2.13", mac="AA:AA:AA:00:00:03", connection="wifi", band=BAND_2G,
               node="0000a1", rssi_dbm=-71, link_tx_mbps=144.0, link_rx_mbps=130.0, connected_s=42),
        Client(key="4", label_parts=("Old laptop",), ip="192.0.2.14", mac="AA:AA:AA:00:00:04", connection="wifi",
               band=BAND_5G, online=False),
    ))


def mesh_reading() -> MeshReading:
    """Hall (the main router), Study on a 5 GHz backhaul and Garage, offline, on a cable that dropped."""
    return MeshReading((MeshNode("0000a1", "Hall", clients=2, wired=1, online=True),
                        MeshNode("0000a2", "Study", clients=1, online=True, backhaul="wifi", backhaul_band="5 GHz",
                                 backhaul_rssi_dbm=-54),
                        MeshNode("0000a3", "Garage", online=False)))


async def probe_icmp(self, target):
    if not target.enabled:
        return None
    return ProbeSample(target_id=target.id, timestamp=time.time(), success=True,
                       latency_ms=1.0 + (sum(map(ord, target.id)) % 7) / 10 + int(time.time() * 1000) % 4 / 10, jitter_ms=0.2)


async def probe_dns(self, dns_cfg):
    if not dns_cfg.enabled:
        return None
    return ProbeSample(target_id=dns_cfg.id, timestamp=time.time(), success=True, latency_ms=12.0, jitter_ms=1.0)


# ---- what one poll of a device leaves in its monitor, one function per capability (a new capability adds a function) ----

def fake_line_poll(device) -> None:
    """The `line` capability: the modem's DOCSIS snapshot."""
    device.monitor.latest_snapshot = docsis_snapshot()


def fake_router_poll(device) -> None:
    """The router's WAN/system snapshot (router and router2 differ, see router_snapshot)."""
    device.monitor.latest_snapshot = router_snapshot(device.id)


def fake_clients_poll(device) -> None:
    """The `clients` and `mesh` capabilities: what a poll that read the client list leaves in the monitor."""
    if device.id == "router":
        clients, mesh = clients_reading(), mesh_reading()
    elif device.id == "router2":  # one wired client, no mesh
        clients, mesh = ClientsReading((Client(key="9", label_parts=("Printer",), ip="192.0.2.30", connection="wired"),)), MeshReading(())
    else:
        return
    device.monitor.latest_clients, device.monitor.latest_mesh, device.monitor.clients_at = clients, mesh, time.time()


async def device_poll(self, force=False, **_):
    (fake_line_poll if self.id == "modem" else fake_router_poll)(self)
    fake_clients_poll(self)
    return self.monitor.latest_snapshot


if os.environ.get("PENGUMON_SMOKE_LINE_TECHNOLOGY"):
    XfinityXbDriver.line_technology = os.environ["PENGUMON_SMOKE_LINE_TECHNOLOGY"]
NetworkProber.probe_icmp = probe_icmp
NetworkProber.probe_dns = probe_dns
Device.poll = device_poll


async def no_restore(self):
    return 0.0  # the stored history is seeded for the charts; the cards must come from the fixed snapshots


Device._restore = no_restore


if __name__ == "__main__":
    from pengumon.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
