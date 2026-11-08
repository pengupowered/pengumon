"""The AI pack's module contributions (design 9, test 6): every registered contribution and driver note meets the
contract: interface attributes, prompt texts within budget and free of addresses, sections within budget, unique event
codes, redaction coverage, and instruments that exist, are documented and record.

The fixture is made-up: a modem and a router at 192.0.2.x-style hosts with canary names, a short window of stored
history in which every event code fires once or more."""

import ast
import re
import string
from pathlib import Path
from types import SimpleNamespace

import pytest

from pengumon.devices.settings import DeviceConfig
from pengumon.capabilities import (
    BAND_2G, BAND_5G, Capability, Client, ClientsReading, LineReading, MeshNode, MeshReading, PollResult,
    SystemReading, WanCounters,
)
from pengumon.capabilities.contrib import DETAILS, HINT_ACTIONS, ContribContext, render_section
from pengumon.reports.contributions import activate, all_contributions, all_driver_notes, driver_note
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import DeviceSample, LineSample, MeshCount
from pengumon.incidents.taxonomy import CAUSE_CATEGORIES
from pengumon.lines import docsis
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_700_000_000.0
WINDOW = 7200.0
MODEM_HOST, ROUTER_HOST = "canary-modem.lan", "canary-router.lan"
ROUTER_ID, ROUTER_MODEL = "Canary-Router-Dev", "Example-Mesh X1"
NODE_A, NODE_B = "0000a1", "0000a2"
LABEL_A, LABEL_B = "Canary-Mesh-Kitchen", "Canary-Mesh-Hall"
CANARIES = (MODEM_HOST, ROUTER_HOST, ROUTER_ID, ROUTER_MODEL, NODE_A, NODE_B, LABEL_A, LABEL_B)
ROLE_KEY = re.compile(r"\b(?:line_device|router_device|mesh_node:\d+)\b")
ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b|\b[\w-]+\.(?:lan|local|net|com|org)\b",
                     re.I)

EXPECTED_CODES = {
    "line.offline", "line.restart", "line.error_burst", "line.errors", "line.signal_out_of_range", "wan.busy", "wan.stall",
    "router.restart", "router.unreachable", "router.cpu_high", "router.memory_high", "clients.drop",
    "mesh.node_empty",
}
# The contribution whose budget also pays for a driver note (design 6.2): DOCSIS + the gateway's, Router + the router's.
NOTE_OF = {"docsis": "xfinity_xb", "router_system": "asuswrt"}


def make_config(**sections) -> AppConfig:
    return AppConfig(
        line={"down_mbps": 300, "up_mbps": 20},
        devices=[
            {"id": "modem", "driver": "xfinity_xb", "url": f"http://{MODEM_HOST}", "poll_interval_seconds": 600,
             "model": "Example-Cable 3.1"},
            {"id": ROUTER_ID, "driver": "asuswrt", "url": f"http://{ROUTER_HOST}", "model": ROUTER_MODEL,
             "options": {"mesh_node_labels": {NODE_A: LABEL_A}}},
        ],
        **sections)


def channels(degraded: bool):
    ds = [DocsisChannel(channel_id=i, direction="downstream", frequency_mhz=500.0 + 6 * i,
                        power_dbmv=9.8 if degraded else 1.0, snr_db=28.0 + i / 10 if degraded else 38.0,
                        uncorrectables=0, uncorrectables_recent=100 * i if degraded else 0) for i in range(1, 33)]
    us = [DocsisChannel(channel_id=100 + i, direction="upstream", frequency_mhz=30.0 + 6 * i, power_dbmv=44.0)
          for i in range(4)]
    return ds + us


def line_row(i: int, total: int, recent: int, degraded: bool) -> LineSample:
    snapshot = docsis.summarize(channels(degraded), T0 + 600 * i)
    snapshot.uncorrectables_total, snapshot.uncorrectables_recent = total, recent
    return docsis.line_sample("modem", snapshot)


async def seed(repo) -> None:
    rows = [line_row(0, 1000, 0, False), line_row(1, 3310, 2310, False), line_row(2, 3400, 90, False)]
    rows.append(docsis.line_sample("modem", DocsisSignalSnapshot(timestamp=T0 + 1800, status="error",
                                                                 error_kind="timeout")))
    rows += [line_row(4, 5, 0, False), line_row(5, 15, 10, False)] + [line_row(i, 15, 0, i >= 8) for i in range(6, 12)]
    for r in rows:
        await repo.insert_line_sample(r)
    clients_at = []
    for i in range(int(WINDOW / 10)):
        ts = T0 + 10 * i
        bytes_ = i * 1_000_000
        if 200 <= i <= 210:
            bytes_ = 200 * 1_000_000          # stood still for 100 s
        elif i > 300:
            bytes_ = (i - 300) * 1_000_000    # the router restarted at i = 300
        kw = dict(device_id=ROUTER_ID, ts=ts, status="healthy", wan_rx_bytes=bytes_, wan_tx_bytes=bytes_ // 10,
                  wan_rx_mbps=280.0 if 100 <= i <= 130 else 5.0, wan_tx_mbps=1.0,
                  cpu_pct=95.0 if 500 <= i <= 502 else 5.0, mem_pct=97.0 if i == 600 else 40.0)
        if i == 400:
            kw.update(status="error", error_kind="timeout", wan_rx_bytes=None, wan_tx_bytes=None, wan_rx_mbps=None,
                      wan_tx_mbps=None, cpu_pct=None, mem_pct=None)
        elif i % 6 == 0:
            low = 660 <= i <= 666
            kw.update(clients_online=4 if low else 14, clients_wired=2, clients_wifi_2g=1 if low else 3,
                      clients_wifi_5g=1 if low else 9, clients_wifi_other=0)
            clients_at.append((ts, 660 <= i <= 666))
        await repo.insert_device_sample(DeviceSample(**kw))
    for ts, empty in clients_at:
        await repo.insert_mesh_counts([MeshCount(ROUTER_ID, ts, NODE_A, 9), MeshCount(ROUTER_ID, ts, NODE_B,
                                                                                    0 if empty else 5)])


def live_monitor():
    return SimpleNamespace(latest_mesh=MeshReading(nodes=(
        MeshNode(id=NODE_A, label=LABEL_A, clients=9, wired=3, online=True, backhaul="wired"),
        MeshNode(id=NODE_B, label=LABEL_B, clients=5, online=True, backhaul="wifi", backhaul_band=BAND_5G,
                 backhaul_rssi_dbm=-61))))


@pytest.fixture
async def world(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "pengumon.db"))
    await repo.initialize()
    await seed(repo)
    config = make_config()
    ctx = ContribContext(repository=repo, config=config, start=T0, end=T0 + WINDOW, devices=tuple(config.devices),
                         live=live_monitor())
    return SimpleNamespace(repo=repo, config=config, ctx=ctx)


async def collected(world):
    out = {}
    for cls in all_contributions():
        contribution = cls()
        out[cls.id] = (contribution, await contribution.collect(world.ctx))
    return out


CLASSES = pytest.mark.parametrize("cls", all_contributions(), ids=lambda c: c.id)


# --- the interface -----------------------------------------------------------------------------------------------

@CLASSES
def test_the_interface_is_complete(cls):
    assert cls.layer in ("technology", "capability") and cls.title and cls.budget_chars > 0
    assert cls.events and all(e.code and e.phrase and e.reach_seconds > 0 for e in cls.events)
    assert cls.instruments and len(set(cls.instruments)) == len(cls.instruments)
    assert set(cls.redaction.fields) and all(
        len(v) == 3 and set(v) <= HINT_ACTIONS for v in cls.redaction.fields.values())
    for method in ("prompt_context", "collect", "emit", "section"):
        assert callable(getattr(cls, method))


def test_event_codes_are_unique_listed_and_phrased_with_subject_and_value_only():
    kinds = [e for cls in all_contributions() for e in cls.events]
    codes = [e.code for e in kinds]
    assert len(codes) == len(set(codes)) and set(codes) == EXPECTED_CODES
    for kind in kinds:
        assert kind.supports <= set(CAUSE_CATEGORIES), kind.code
        fields = {f for _, f, _, _ in string.Formatter().parse(kind.phrase) if f}
        assert fields <= {"subject", "value"}, kind.code


def test_a_stall_supports_every_place_that_can_stop_all_traffic():
    """No traffic through the router at all: the router, the gateway behind it or the line can each stop it."""
    stall = next(e for cls in all_contributions() for e in cls.events if e.code == "wan.stall")
    assert stall.supports == {"router", "gateway", "line"}


def test_the_budgets_of_the_largest_combination_stay_within_the_pack_allowance():
    assert sum(cls.budget_chars for cls in all_contributions()) <= 8000


def test_every_instrument_name_is_unique_across_the_contributions():
    names = [n for cls in all_contributions() for n in cls.instruments]
    assert len(names) == len(set(names))


# --- activation ---------------------------------------------------------------------------------------------------

def test_activation_follows_the_configured_devices_alone():
    both = activate(make_config())
    assert [c.id for c in both.contributions] == ["docsis", "wan_load", "router_system", "wifi_mesh"]
    assert [cfg.driver for cfg, _ in both.notes] == ["xfinity_xb", "asuswrt"]
    modem_only = AppConfig(devices=[make_config().devices[0]])
    assert [c.id for c in activate(modem_only).contributions] == ["docsis"]
    assert [c.id for c in activate(AppConfig()).contributions] == [] and activate(AppConfig()).notes == ()
    switched_off = make_config()
    switched_off.devices[1].enabled = False  # a device switched off still brings its history
    assert len(activate(switched_off).contributions) == 4


# --- prompt texts -------------------------------------------------------------------------------------------------

@CLASSES
def test_the_prompt_text_has_no_address_and_none_of_the_devices_names(cls):
    text = cls().prompt_context(make_config())
    assert text and not ADDRESS.search(text)
    assert not any(c.lower() in text.lower() for c in CANARIES)


def test_the_prompt_texts_fill_in_the_levels_in_force():
    config = make_config(
        line_signal={"ds_snr_warning_db": 36.5, "ds_snr_critical_db": 31.5, "ds_power_warning_abs_dbmv": 6,
                     "us_power_warning_dbmv": 47, "us_power_low_warning_dbmv": 39, "us_power_critical_dbmv": 52},
        router_levels={"cpu_warning_pct": 80, "mem_warning_pct": 70, "mem_critical_pct": 92},
        classifier={"busy_load_pct": 55})
    by_id = {cls.id: cls().prompt_context(config) for cls in all_contributions()}
    for needle in ("above 36.5 dB", "below 31.5 dB", "±6 dBmV", "from 39 to 47 dBmV", "above 52 dBmV"):
        assert needle in by_id["docsis"], needle
    for needle in ("above 80 %", "above 70 % (92 % critical)"):
        assert needle in by_id["router_system"], needle
    assert "From 55 %" in by_id["wan_load"] and "300 / 20 Mbps" in by_id["wan_load"]
    assert "every 10 s" in by_id["wan_load"]


def test_the_learned_capacity_is_named_as_learned_not_invented():
    text = {c.id: c().prompt_context(AppConfig(devices=make_config().devices)) for c in all_contributions()}["wan_load"]
    assert "learned from 7 days" in text and "Mbps here" not in text


# --- sections, events, redaction on the worst fixture ---------------------------------------------------------------

async def test_a_section_with_its_prompt_text_and_note_fits_the_budget(world):
    notes = all_driver_notes()
    configs = {c.driver: c for c in world.config.devices}
    for contribution, facts in (await collected(world)).values():
        cls = type(contribution)
        driver = NOTE_OF.get(cls.id)
        note = driver_note(configs[driver], notes[driver], "full", mesh=True) if driver else ""
        for detail in DETAILS:
            section = contribution.section(facts, detail)
            if len(section.headline or "") > 200:
                pytest.fail(f"{cls.id}: the headline is {len(section.headline)} characters")
            size = len(contribution.prompt_context(world.config)) + len(render_section(section)) + len(note or "")
            assert size <= cls.budget_chars, f"{cls.id} at {detail}: {size} > {cls.budget_chars}"
            assert len(section.day_columns) <= 2


async def test_every_event_code_fires_on_the_fixture_with_role_keys_and_renderable_phrases(world):
    seen = set()
    for contribution, facts in (await collected(world)).values():
        kinds = {k.code: k for k in type(contribution).events}
        for event in contribution.emit(facts):
            assert event.code in kinds and event.start <= event.end
            assert event.subject == "" or ROLE_KEY.fullmatch(event.subject), event
            text = kinds[event.code].render(event)
            assert text and "None" not in text, text
            seen.add(event.code)
    assert seen == EXPECTED_CODES


async def test_nothing_a_section_says_names_a_device_and_every_role_key_resolves(world):
    for contribution, facts in (await collected(world)).values():
        known = set(type(contribution).redaction.identifiers(facts).values())
        assert known, contribution.id
        for detail in DETAILS:
            section = contribution.section(facts, detail)
            text = "\n".join([render_section(section), contribution.prompt_context(world.config)]
                             + [f"{c} {d}" for c, days in section.day_columns.items() for d in days.values()])
            for event in contribution.emit(facts):
                text += "\n" + {k.code: k for k in type(contribution).events}[event.code].render(event)
            for canary in CANARIES:
                assert canary.lower() not in text.lower(), f"{contribution.id} at {detail} shows {canary}"
            assert not ADDRESS.search(text), f"{contribution.id} at {detail}"
            assert set(ROLE_KEY.findall(text)) <= known | {"line_device", "router_device"}, contribution.id


async def test_the_identifiers_cover_what_the_devices_are_called(world):
    ids = {}
    for contribution, facts in (await collected(world)).values():
        ids.update(type(contribution).redaction.identifiers(facts))
    assert ids[MODEM_HOST] == "line_device" and ids["modem"] == "line_device"
    assert ids[ROUTER_HOST] == ids[ROUTER_ID] == "router_device" and ROUTER_MODEL not in ids
    assert ids[NODE_A] == ids[NODE_B] == "never" and ids[LABEL_A] != ids[LABEL_B]
    assert re.fullmatch(r"mesh_node:\d+", ids[LABEL_A]) and re.fullmatch(r"mesh_node:\d+", ids[LABEL_B])
    models = {}
    for contribution, facts in (await collected(world)).values():
        models.update(type(contribution).redaction.field_values(facts))
    assert models == {ROUTER_MODEL: ("keep", "keep", "drop"), "Example-Cable 3.1": ("keep", "keep", "drop")}


async def test_the_section_numbers_follow_the_stored_history(world):
    facts = dict((await collected(world)))
    docs = facts["docsis"][0].section(facts["docsis"][1], "full")
    assert "2,410 new uncorrectable codewords in 3 of 11 readings: +" in " ".join(docs.findings)
    # the restarts and the unanswered readings are in the headline, which no trim cuts
    assert "; 1 modem restart; not answering in 1 reading; " in docs.headline
    assert docs.table and len(docs.table[1]) == 6 and docs.headline.startswith("Cable line:")
    # a channel's Finding names every way it is outside the levels, worst first
    assert docs.table[1][0][5] == "Low SNR; high power"
    load = facts["wan_load"][0].section(facts["wan_load"][1], "full")
    assert any("Counters stood still 1 time; router restarts 1." in f for f in load.findings)
    assert any("Peak load 93 %" in f for f in load.findings)
    router = facts["router_system"][0].section(facts["router_system"][1], "full")
    assert "highest 95 %" in router.findings[0] and "(3 readings at 90 % or more)" in router.findings[0]
    wifi = facts["wifi_mesh"][0].section(facts["wifi_mesh"][1], "full")
    assert "sharp drops 1" in wifi.findings[0] and wifi.table
    rows = {r[0]: r for r in wifi.table[1]}
    assert rows["mesh_node:1"][1] == "wired" and rows["mesh_node:2"][1] == "Wi-Fi 5 GHz, -61 dBm (good)"
    # falls to 0 clients and the minutes at 0 are two columns; each fall is listed with its time and length
    assert wifi.table[0][4:] == ["Falls to 0", "Minutes at 0"] and rows["mesh_node:2"][4:] == ["1", "2"]
    fell_at = (T0 + 6600) // 60 * 60                   # the minute the node's count went to 0
    assert f"mesh_node:2 fell to 0 clients once (2 min in all): {{minute:{fell_at:.0f}}} (2 min)." in wifi.findings
    assert "1 sharp drop in online clients, at the time a node fell to 0 clients." in wifi.findings


async def test_a_contribution_with_no_readings_says_so_in_one_line(world):
    ctx = ContribContext(repository=world.repo, config=world.config, start=T0 + 10 * WINDOW,
                         end=T0 + 11 * WINDOW, devices=tuple(world.config.devices))
    for cls in all_contributions():
        contribution = cls()
        section = contribution.section(await contribution.collect(ctx), "full")
        assert section.table is None and not section.day_columns and len(section.findings) == 1
        assert section.findings[0].startswith("Set up, but no ")
    # a modem whose last poll was refused
    await world.repo.insert_line_sample(docsis.line_sample("modem", DocsisSignalSnapshot(
        timestamp=T0 + 10 * WINDOW + 5, status="error", error_kind="auth")))
    contribution = all_contributions()[0]()
    section = contribution.section(await contribution.collect(ctx), "full")
    assert section.findings == ["Set up, but no readings in this window (last poll: auth refused)."]


# --- driver notes -------------------------------------------------------------------------------------------------

def test_driver_notes_state_the_configured_intervals_and_hide_the_vendor_when_anonymized():
    notes, config = all_driver_notes(), make_config()
    configs = {c.driver: c for c in config.devices}
    configs["upnp_igd"] = DeviceConfig(id="gw", driver="upnp_igd", url="http://192.168.88.1")
    assert set(notes) == {"xfinity_xb", "asuswrt", "upnp_igd"}
    for name, note in notes.items():
        full = driver_note(configs[name], note, "full", mesh=True)
        vague = driver_note(configs[name], note, "vague", mesh=True)
        anonymous = driver_note(configs[name], note, "anonymized", mesh=True)
        assert full == vague and "{" not in full and "{" not in anonymous
        assert not re.search(r"Xfinity|ASUS|AiMesh|\bXB\d?\b", anonymous), anonymous
    modem = driver_note(configs["xfinity_xb"], notes["xfinity_xb"], "full")
    assert "Xfinity gateway (XB6, XB7 or XB8)" in modem and "never more often than every 10 minutes, because" in modem
    router = driver_note(configs["asuswrt"], notes["asuswrt"], "full")
    assert "(AsusWRT). Its counters are read every 10 s, its client list every 60 s" in router
    assert ", on an AiMesh network" in driver_note(configs["asuswrt"], notes["asuswrt"], "full", mesh=True)
    assert "AiMesh" not in router
    upnp = driver_note(configs["upnp_igd"], notes["upnp_igd"], "full")
    assert "read every 30 s" in upnp and "32-bit" in upnp and "UPnP" in upnp


# --- telemetry ----------------------------------------------------------------------------------------------------

ALLOWED_LABELS = {"device", "device.id", "direction", "stat", "grade", "kind", "connection", "band", "state"}


def _counter_names() -> set:
    tree = ast.parse((ROOT / "src" / "pengumon" / "observability" / "telemetry.py").read_text(encoding="utf-8"))
    return {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("create_")}


def _fixture_state():
    """The two devices of the telemetry fixture, polled: a modem whose errors rise and whose counters then start
    again, and a router with WAN counters that start again, system readings, clients and a two-node mesh."""
    from device_helpers import build_modem, build_router
    from pengumon.devices.registry import DeviceRegistry

    modem, router = build_modem(id="tm-modem"), build_router(id="tm-router")
    return SimpleNamespace(
        devices=DeviceRegistry([modem, router]), modem=modem, router=router, repository=None, prober=None,
        classifier=SimpleNamespace(last_diagnosis=SimpleNamespace(load_pct=12.0, status="healthy"),
                                   load=SimpleNamespace(down_mbps=300.0, up_mbps=20.0)))


def _line_poll(uncorrectables: int, corrected: int) -> PollResult:
    ch = [DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=500.0, power_dbmv=1.0, snr_db=38.0,
                        correcteds=corrected, uncorrectables=uncorrectables),
          DocsisChannel(channel_id=2, direction="upstream", frequency_mhz=30.0, power_dbmv=44.0)]
    return PollResult({Capability.ACCESS_LINE: LineReading("docsis", tuple(ch))})


def _router_poll(rx: int) -> PollResult:
    clients = ClientsReading((
        Client(key="a", connection="wired"),
        Client(key="b", connection="wifi", band=BAND_5G, rssi_dbm=-80, node=NODE_B),
        Client(key="c", connection="wifi", band=BAND_2G, rssi_dbm=-55, node=NODE_A)))
    mesh = MeshReading((MeshNode(id=NODE_A, label="Node A", clients=1, online=True, backhaul="wired"),
                        MeshNode(id=NODE_B, label="Node B", clients=1, online=True, backhaul="wifi",
                                 backhaul_band=BAND_5G, backhaul_rssi_dbm=-70)))
    return PollResult({Capability.WAN_COUNTERS: WanCounters(rx, rx // 10), Capability.ROUTER_SYSTEM:
                       SystemReading(cpu_pct=12.0, mem_pct=40.0, client_count=3),
                       Capability.CLIENT_INVENTORY: clients, Capability.MESH: mesh})


async def test_every_instrument_exists_is_documented_records_and_carries_only_allowed_labels():
    """Written against the names of design 7.2 (workstream T builds them): skipped until `module_gauges` exists."""
    pytest.importorskip("pengumon.observability.module_gauges")
    import obs_helpers
    from pengumon.observability.gauges import GAUGES
    from test_telemetry_docs import DOC, _prometheus_name

    obs_helpers.install()
    state = _fixture_state()
    for ts, uncorrectables in ((100.0, 100), (700.0, 150), (1300.0, 10)):  # +50 new, then the counters start again
        await state.modem.monitor.on_result(ts, _line_poll(uncorrectables, 1000))
    # the counters start again at 120 s (a router restart), and a rate is known again from the next poll
    for ts, rx in ((100.0, 10_000_000), (110.0, 20_000_000), (120.0, 1_000), (130.0, 5_000_000)):
        await state.router.monitor.on_result(ts, _router_poll(rx))

    gauges = {g.otel: g for g in GAUGES if g.otel}
    counters = _counter_names()
    ids = {"tm-modem", "tm-router"}
    for cls in all_contributions():
        for name in cls.instruments:
            if name in gauges:
                gauge = gauges[name]
                assert gauge.prom in DOC, name
                samples = [(labels, value) for labels, value in gauge.collect(state) if value is not None]
                assert samples, f"{name} reports nothing on the fixture state"
                labeled = [labels for labels, _ in samples]
            else:
                assert name in counters, f"{name} is in neither GAUGES nor telemetry.py"
                kind = "counter"
                assert _prometheus_name(name, "", kind) in DOC or _prometheus_name(name, "{codeword}", kind) in DOC, name
                points = obs_helpers.points(name)
                assert any(p.value > 0 for a, p in points if {a.get("device"), a.get("device.id")} & ids), \
                    f"{name} did not record"
                labeled = [a for a, _ in points if {a.get("device"), a.get("device.id")} & ids]
            for labels in labeled:
                assert set(labels) <= ALLOWED_LABELS, (name, labels)
                for key, value in labels.items():
                    if key in ("device", "device.id"):
                        assert value in ids, (name, labels)
                    else:
                        assert re.fullmatch(r"[a-z0-9_]+", str(value)), (name, labels)
                        assert not ADDRESS.search(str(value)), (name, labels)


def test_the_modem_itself_supports_the_gateway_and_its_codewords_the_line():
    """The modem not answering or restarting supports a fault at the Gateway; codeword bursts and a signal out of range
    one on the line (loss from ISP hop 1 on while the Gateway answers)."""
    from pengumon.lines.docsis_contrib import DocsisContribution
    assert {k.code: set(k.supports) for k in DocsisContribution.events} == {
        "line.offline": {"gateway"}, "line.restart": {"gateway"}, "line.error_burst": {"line"},
        "line.errors": set(), "line.signal_out_of_range": {"line"}}
