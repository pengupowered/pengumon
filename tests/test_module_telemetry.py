"""The module instruments (docs/telemetry.md, "Device modules"): the three counters are recorded by the monitors, the
gauges read the monitors' latest state, and no gauge carries a label that could be an address or a name."""

import re
from types import SimpleNamespace

import obs_helpers
from device_helpers import build_modem, build_router
from pengumon.capabilities import (
    BAND_2G, BAND_5G, Capability, Client, ClientsReading, LineReading, MeshNode, MeshReading, PollResult, SystemReading,
    WanCounters,
)
from pengumon.devices.registry import DeviceRegistry
from pengumon.lines.docsis_model import DocsisChannel
from pengumon.observability.gauges import GAUGES
from pengumon.observability.module_gauges import MODULE_GAUGES

obs_helpers.install()

ALLOWED_LABELS = {"device", "direction", "stat", "grade", "connection", "band", "state"}


def _channels(corrected, uncorrectable):
    return (DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0, snr_db=38.0,
                          correcteds=corrected, uncorrectables=uncorrectable),
            DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=606.0, power_dbmv=-2.0, snr_db=34.0),
            DocsisChannel(channel_id=9, direction="upstream", frequency_mhz=30.0, power_dbmv=42.0))


def _line(corrected, uncorrectable):
    return PollResult({Capability.ACCESS_LINE: LineReading("docsis", _channels(corrected, uncorrectable))})


def _router(rx, tx):
    return PollResult({
        Capability.WAN_COUNTERS: WanCounters(rx, tx), Capability.ROUTER_SYSTEM: SystemReading(cpu_pct=12.5, mem_pct=40.0),
        Capability.CLIENT_INVENTORY: ClientsReading((
            Client(key="a", connection="wired"), Client(key="b", connection="wifi", band=BAND_5G, rssi_dbm=-58),
            Client(key="c", connection="wifi", band=BAND_2G, rssi_dbm=-71),
            Client(key="d", connection="wifi", band=BAND_2G, rssi_dbm=-80),
            Client(key="e", connection="wifi", band=BAND_5G, online=False))),
        Capability.MESH: MeshReading((MeshNode("0000a1", "Hall", clients=3, online=True),
                                      MeshNode("0000a2", "Study", clients=1, online=True, backhaul="wifi",
                                               backhaul_rssi_dbm=-54),
                                      MeshNode("0000a3", "Garage", clients=0, online=False)))})


def _values(samples):
    return {tuple(sorted(labels.items())): value for labels, value in samples}


async def test_the_counters_are_recorded_and_the_gauges_read_the_monitors():
    modem, router = build_modem(id="tmod"), build_router(id="trtr")
    before = {k: obs_helpers.total(name, device=d, **extra) for k, (name, d, extra) in {
        "unc": ("pengumon.line.errors", "tmod", {"kind": "uncorrectable"}),
        "cor": ("pengumon.line.errors", "tmod", {"kind": "corrected"}),
        "line": ("pengumon.line.restarts", "tmod", {}), "router": ("pengumon.router.restarts", "trtr", {})}.items()}
    for ts, corrected, uncorrectable in ((1000.0, 100, 10), (1900.0, 140, 40), (2800.0, 5, 0)):  # the last: a restart
        await modem.monitor.on_result(ts, _line(corrected, uncorrectable))
    for ts, rx in ((1000.0, 5_000_000), (1010.0, 100), (1020.0, 12_500_100)):  # the second: counters reset
        await router.monitor.on_result(ts, _router(rx, rx // 10))

    def added(name, **wanted):
        return obs_helpers.total(name, **wanted)
    assert added("pengumon.line.errors", device="tmod", kind="uncorrectable") - before["unc"] == 30
    assert added("pengumon.line.errors", device="tmod", kind="corrected") - before["cor"] == 40
    assert added("pengumon.line.restarts", device="tmod") - before["line"] == 1
    assert added("pengumon.router.restarts", device="trtr") - before["router"] == 1

    state = SimpleNamespace(devices=DeviceRegistry([modem, router]))
    got = {g.otel: _values(g.collect(state)) for g in MODULE_GAUGES}
    assert got["pengumon.line.docsis.power"][(("device", "tmod"), ("direction", "downstream"), ("stat", "min"))] == -2.0
    assert got["pengumon.line.docsis.power"][(("device", "tmod"), ("direction", "upstream"), ("stat", "avg"))] == 42.0
    assert got["pengumon.line.docsis.snr"][(("device", "tmod"), ("stat", "avg"))] == 36.0
    assert sum(got["pengumon.line.docsis.channels"].values()) == 3
    assert got["pengumon.wan.rate"][(("device", "trtr"), ("direction", "down"))] == 10.0
    assert got["pengumon.router.cpu"] == {(("device", "trtr"),): 12.5}
    assert got["pengumon.router.memory"] == {(("device", "trtr"),): 40.0}
    clients = got["pengumon.clients"]
    assert clients[(("band", "none"), ("connection", "wired"), ("device", "trtr"))] == 1
    assert clients[(("band", "2g"), ("connection", "wifi"), ("device", "trtr"))] == 2
    assert clients[(("band", "5g"), ("connection", "wifi"), ("device", "trtr"))] == 1  # (the offline one is left out)
    assert got["pengumon.clients.wifi.signal"][(("band", "2g"), ("device", "trtr"), ("stat", "p10"))] == -80
    assert got["pengumon.clients.wifi.weak"][(("band", "2g"), ("device", "trtr"))] == 1
    assert got["pengumon.mesh.nodes"][(("device", "trtr"), ("state", "offline"))] == 1
    assert got["pengumon.mesh.node.clients"][(("device", "trtr"), ("stat", "max"))] == 3
    assert got["pengumon.mesh.backhaul.signal"] == {(("device", "trtr"), ("stat", "min")): -54}
    for gauge in MODULE_GAUGES:  # every module gauge has samples here, labeled by ids and small enums only
        assert gauge.collect(state), gauge.otel
        for labels, value in gauge.collect(state):
            assert set(labels) <= ALLOWED_LABELS
            assert not any(re.search(r"\d+\.\d+\.\d+\.\d+|[0-9a-f]{2}(:[0-9a-f]{2}){5}", v, re.I) for v in labels.values())


def test_a_service_without_the_modules_has_no_module_samples():
    for state in (SimpleNamespace(devices=None), SimpleNamespace(devices=DeviceRegistry([]))):
        for gauge in MODULE_GAUGES:
            assert gauge.collect(state) == [], gauge.otel
    assert {g.otel for g in MODULE_GAUGES} <= {g.otel for g in GAUGES}
