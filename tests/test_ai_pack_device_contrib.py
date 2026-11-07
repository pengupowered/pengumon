"""The device contributions to the AI pack (line load, router, Wi-Fi) on stored polls: one run with a stall, a restart, a
busy stretches split by a silence, an unanswered poll, a CPU burst, a single high CPU reading and a drop in clients, so a rewrite of how the polls are read keeps the
same facts. Example values only."""

from pengumon.capabilities.contrib import ContribContext
from pengumon.config import AppConfig
from pengumon.db.samples import DeviceSample
from pengumon.capabilities import BAND_5G, MeshNode
from pengumon.devices.contrib import (
    LineLoadContribution, NodeFacts, RouterContribution, WifiFacts, WifiMeshContribution, _backhaul_words, _descriptors,
)
from pengumon.devices.settings import DeviceConfig

T0 = 100000.0


def poll(i: int) -> DeviceSample:
    """The i-th poll, 10 s apart; i < 0 is history before the window."""
    rx = tx = 1_000_000 + 1000 * i
    if 20 <= i <= 30:
        rx = tx = 1_000_000 + 1000 * 20          # counters stood still for 100 s
    if i >= 50:
        rx = tx = 1000 * (i - 50)                # the counters started again: a restart
    s = DeviceSample(device_id="r", ts=T0 + 10 * i, status="healthy", wan_rx_bytes=rx, wan_tx_bytes=tx,
                     wan_rx_mbps=80.0 if 100 <= i <= 109 or i in (168, 169, 183, 184) else 10.0, wan_tx_mbps=1.0,
                     cpu_pct=95.0 if 120 <= i <= 122 or i == 125 else 20.0, mem_pct=40.0,
                     clients_online=5 if 150 <= i <= 152 else 20, clients_wired=2, clients_wifi_2g=3,
                     clients_wifi_5g=15)
    if i == 70:
        return DeviceSample(device_id="r", ts=s.ts, status="error", error_kind="timeout")
    return s


async def test_the_facts_of_a_run_of_polls(repo):
    for i in [i for i in range(-60, 200) if not 170 <= i <= 182]:   # 140 s of silence between 169 and 183
        await repo.insert_device_sample(poll(i))
    config = AppConfig()
    config.line.down_mbps = config.line.up_mbps = 100.0
    device = DeviceConfig(id="r", driver="asuswrt", url="http://192.0.2.1")
    ctx = ContribContext(repo, config, T0, T0 + 1990, (device,))
    load = await LineLoadContribution().collect(ctx)
    router = await RouterContribution().collect(ctx)
    wifi = await WifiMeshContribution().collect(ctx)

    assert [(e.code, e.start, e.end, e.value) for e in load.events] == [
        ("wan.stall", T0 + 200, T0 + 300, 100.0), ("router.restart", T0 + 500, T0 + 500, None),
        ("wan.busy", T0 + 1000, T0 + 1090, 80.0)]
    assert (load.stalls, load.restarts, load.busy_seconds, load.peak_pct, load.readings) == (1, 1, 140.0, 80.0, 186)
    assert [(e.code, e.start, e.end, e.value) for e in router.events] == [
        ("router.unreachable", T0 + 700, T0 + 700, None), ("router.cpu_high", T0 + 1200, T0 + 1220, 95.0)]
    assert (router.readings, router.unreachable, router.cpu_over, router.mem) == (186, 1, 4, (40.0, 40.0))
    assert [(e.code, e.start, e.end, e.value) for e in wifi.events] == [("clients.drop", T0 + 1500, T0 + 1520, 5.0)]
    assert (wifi.readings, wifi.drops, wifi.online, wifi.wifi_5g) == (186, 1, 20.0, 15.0)


def wifi_facts(node: NodeFacts) -> WifiFacts:
    return WifiFacts(identifiers={}, readings=10, online=9.0, wired=4.0, wifi_2g=1.0, wifi_5g=4.0, nodes=(node,))


def test_a_node_that_falls_to_0_on_a_wifi_backhaul_near_weak_says_so_in_the_findings_line():
    near = MeshNode(id="000003", label="x", backhaul="wifi", backhaul_band=BAND_5G, backhaul_rssi_dbm=-74)
    fair = MeshNode(id="000003", label="x", backhaul="wifi", backhaul_band=BAND_5G, backhaul_rssi_dbm=-72)
    assert _backhaul_words(near) == "Wi-Fi 5 GHz, -74 dBm (fair, near weak: a backhaul this weak often drops the node)"
    assert _backhaul_words(fair) == "Wi-Fi 5 GHz, -72 dBm (fair)"
    node = NodeFacts("mesh_node:3", _backhaul_words(near), 2.0, 0.0, ((T0, 5), (T0 + 7200, 8)), 13, "near weak", -74)
    assert WifiMeshContribution().section(wifi_facts(node), "full").headline == (
        "Wi-Fi and mesh: mesh_node:3 fell to 0 clients 2 times (13 min in all; Wi-Fi backhaul near weak, -74 dBm).")


def test_a_node_is_told_apart_without_its_name_where_its_backhaul_is_the_only_one_of_its_kind():
    nodes = {"000002": MeshNode(id="000002", label="x", backhaul="wired"),
             "000003": MeshNode(id="000003", label="y", backhaul="wifi")}
    assert _descriptors(["000001", "000002", "000003"], nodes) == {
        "000002": "the only node on wired backhaul", "000003": "the only node on Wi-Fi backhaul"}
    assert _descriptors(["000001", "000002", "000003", "000004"], nodes) == {}   # two not known: either may be wired
    node = NodeFacts("mesh_node:3", "Wi-Fi", 2.0, 0.0, ((T0, 5),), 5, descriptor="the only node on Wi-Fi backhaul")
    contribution = WifiMeshContribution()
    assert contribution.section(wifi_facts(node), "vague").findings[1].startswith(
        "mesh_node:3 (the only node on Wi-Fi backhaul) fell to 0 clients once (5 min in all): ")
    assert "(the only node" not in contribution.section(wifi_facts(node), "full").headline   # Full names the node

