"""Previewing the dashboard without a device (pengumon.preview): the cookie, the registry as a previewing browser sees
it, and every dashboard route and the live stream answering as if the router or the modem were never set up, or the
router were a basic UPnP one. Nothing stored changes; without the cookie every answer is as before. Made-up values only
(MACs 02:00:00:00:00:xx)."""

import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.capabilities import Capability, Client, ClientsReading
from pengumon.clients.settings import PrivacyConfig
from pengumon.config import AppConfig
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.settings import DeviceConfig
from pengumon.incidents.model import Incident
from pengumon.live.message import LiveStreamMessage
from pengumon.live.ws import LivePreview
from pengumon.incidents.model import SystemDiagnosis
from pengumon.main import app
from pengumon.preview import (ABSENT, BASIC, COOKIE, Preview, PreviewRegistry, is_basic, live_devices, preview_config,
                              viewer_registry)

from asuswrt_fake import HOUR, FakeAsus
from device_helpers import Clock, build_modem, build_router
from test_traffic_views import GAME, inventory, seed_traffic

ROUTER_ABSENT = {COOKIE: "router%3Aabsent"}  # (as the page writes it)
MODEM_ABSENT = {COOKIE: "modem:absent"}
ROUTER_BASIC = {COOKIE: "router:basic"}


def api(cookies=None):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies or {})


def devices(repo):
    clock = Clock(time.time())
    router = build_router(repo, username="u", password="p", clock=clock, wall_clock=clock,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(FakeAsus(clock))))
    router.driver._token = "tok"
    router.monitor.latest_clients = inventory()
    modem = build_modem(repo, password="pw")
    return DeviceRegistry([modem, router])


async def api_state(app_state, repo):
    app_state.devices = devices(repo)
    app_state.config = AppConfig(privacy=PrivacyConfig(client_details="full"))
    app_state.repository = repo
    return app_state.devices


# --- the cookie ------------------------------------------------------------------------------------------------------

def test_the_cookie_names_devices_and_modes_and_anything_else_is_left_out():
    p = Preview.parse("router:absent, modem:basic,bad,x:gone,:absent,a b:absent")
    assert p.modes == {"router": ABSENT, "modem": BASIC}
    assert Preview.parse(None).active is False and Preview.parse("").active is False
    # as the page writes it (encodeURIComponent)
    assert Preview.parse("router%3Aabsent%2Cmodem%3Abasic").modes == {"router": ABSENT, "modem": BASIC}
    many = Preview.parse(",".join(f"d{i}:absent" for i in range(30)))
    assert len(many.modes) == 10


# --- the registry as a previewing browser sees it --------------------------------------------------------------------

def test_absent_leaves_the_device_out_and_basic_keeps_only_its_wan_counters(repo):
    real = devices(repo)
    absent = PreviewRegistry(real, Preview({"router": ABSENT}))
    assert [d.id for d in absent.all()] == ["modem"] and absent.get("router") is None and len(absent) == 1
    assert absent.primary(Capability.WAN_COUNTERS) is None and absent.wan_rates_device() is None
    assert absent.primary(Capability.ACCESS_LINE).id == "modem"
    basic = PreviewRegistry(real, Preview({"router": BASIC}))
    router = basic.get("router")
    assert router.driver.offers == frozenset({Capability.WAN_COUNTERS}) and is_basic(router)
    assert basic.primary(Capability.CLIENT_INVENTORY) is None and basic.primary(Capability.TRAFFIC_USAGE) is None
    assert basic.wan_rates_device().id == "router"
    assert router.monitor is real.get("router").monitor  # reads go to the real device
    no_modem = PreviewRegistry(real, Preview({"modem": ABSENT}))
    assert no_modem.line_technology() is None and no_modem.primary(Capability.ACCESS_LINE) is None
    # basic on a device without WAN counters changes nothing
    assert PreviewRegistry(real, Preview({"modem": BASIC})).get("modem") is real.get("modem")


def test_without_a_preview_the_real_registry_is_used(app_state, repo):
    app_state.devices = devices(repo)
    request = type("R", (), {"cookies": {}})()
    assert viewer_registry(app_state, request) is app_state.devices


def test_the_reports_read_the_configuration_as_previewed():
    config = AppConfig(devices=[DeviceConfig(id="modem", driver="xfinity_xb", url="http://192.168.100.1"),
                                DeviceConfig(id="router", driver="asuswrt", url="http://192.168.1.1")])
    assert [d.id for d in preview_config(config, Preview({"modem": ABSENT})).devices] == ["router"]
    basic = preview_config(config, Preview({"router": BASIC}))
    assert [(d.id, d.driver) for d in basic.devices] == [("modem", "xfinity_xb"), ("router", "upnp_igd")]
    assert preview_config(config, Preview()) is config
    assert [d.driver for d in config.devices] == ["xfinity_xb", "asuswrt"]  # the real one is untouched


# --- the routes ------------------------------------------------------------------------------------------------------

async def test_without_the_cookie_every_route_answers_as_before(app_state, repo):
    await api_state(app_state, repo)
    async with api() as ac:
        ids = [d["id"] for d in (await ac.get("/api/devices")).json()]
        assert (await ac.get("/api/devices/router/clients")).status_code == 200
    assert ids == ["modem", "router"]


async def test_a_router_previewed_as_absent_is_in_no_answer(app_state, repo):
    await api_state(app_state, repo)
    newest = await seed_traffic(repo, time.time())
    async with api(ROUTER_ABSENT) as ac:
        listed = (await ac.get("/api/devices")).json()
        features = {f["feature"]: f for f in (await ac.get("/api/features")).json()}
        codes = {path: (await ac.get(path)).status_code for path in (
            "/api/devices/router", "/api/devices/router/history", "/api/devices/router/clients",
            "/api/devices/router/clients/history", "/api/devices/router/traffic", "/api/devices/router/events",
            f"/api/devices/router/traffic/span?start={newest}&end={newest + HOUR}",
            f"/api/devices/router/traffic/clients/{GAME}")}
        oldest = (await ac.get("/api/oldest")).json()
    async with api() as ac:
        oldest_real = (await ac.get("/api/oldest")).json()
    assert [d["id"] for d in listed] == ["modem"]
    assert all(code == 404 for code in codes.values()), codes
    for feature in ("traffic", "clients", "mesh", "router_system", "line_load"):
        assert features[feature]["available"] is False and features[feature]["reason_kind"] == "none", feature
    assert features["line_health"]["available"] is True
    assert oldest_real["traffic"] is not None and oldest["traffic"] is None and oldest["router"] is None


async def test_a_router_previewed_as_basic_has_only_its_wan_counters(app_state, repo):
    await api_state(app_state, repo)
    newest = await seed_traffic(repo, time.time())
    await repo.insert_device_event("router", time.time() - 60, "restarted", {"boot_at": 1})
    async with api(ROUTER_BASIC) as ac:
        router = next(d for d in (await ac.get("/api/devices")).json() if d["id"] == "router")
        features = {f["feature"]: f["available"] for f in (await ac.get("/api/features")).json()}
        clients = await ac.get("/api/devices/router/clients")
        traffic = await ac.get("/api/devices/router/traffic")
        span = await ac.get("/api/devices/router/traffic/span", params={"start": newest, "end": newest + HOUR})
        events = (await ac.get("/api/devices/router/events")).json()
        history = (await ac.get("/api/devices/router/history", params={"max_points": 50})).json()
        oldest = (await ac.get("/api/oldest")).json()
    assert router["offers"] == ["wan_counters"]
    assert (router["driver"], router["kind"]) == ("upnp_igd", "UPnP router") and "ASUS" not in router["label"]
    # a UPnP router has no sign-in: no Setup button on its card
    assert (router["needs_sign_in"], router["has_credentials"], router["sign_in"]) == (False, True, None)
    assert features["line_load"] is True and features["clients"] is False and features["traffic"] is False
    assert features["router_system"] is False and features["mesh"] is False
    assert (clients.status_code, traffic.status_code, span.status_code) == (400, 400, 400)
    assert events["events"] == []
    fields = history["fields"]
    assert all(row[fields.index(f)] is None for row in history["rows"] for f in ("cpu_avg", "mem_avg", "clients_avg"))
    assert oldest["traffic"] is None and oldest["mesh"] is None


async def test_a_modem_previewed_as_absent_takes_its_line_with_it(app_state, repo):
    await api_state(app_state, repo)
    async with api(MODEM_ABSENT) as ac:
        listed = [d["id"] for d in (await ac.get("/api/devices")).json()]
        features = {f["feature"]: f["available"] for f in (await ac.get("/api/features")).json()}
        codes = [(await ac.get(p)).status_code for p in ("/api/devices/modem", "/api/devices/modem/channels/history")]
    assert listed == ["router"] and features["line_health"] is False and codes == [404, 404]


async def test_speed_tests_without_the_router_give_the_tests_own_speeds(app_state, repo):
    await api_state(app_state, repo)
    await repo.insert_speed_test({"ts": time.time() - 600, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok", "duration_s": 12.0, "down_mbps": 300.0, "up_mbps": 20.0,
                                  "wan_down_mbps": 900.0, "wan_up_mbps": 40.0})
    async with api() as ac:
        real = (await ac.get("/api/speedtests")).json()["tests"][0]
    async with api(ROUTER_ABSENT) as ac:
        previewed = (await ac.get("/api/speedtests")).json()["tests"][0]
    async with api(ROUTER_BASIC) as ac:
        basic = (await ac.get("/api/speedtests")).json()["tests"][0]
    assert (real["headline_down_mbps"], real["down_measured_by"]) == (900.0, "router")
    assert (previewed["headline_down_mbps"], previewed["down_measured_by"], previewed["wan_down_mbps"]) == (300.0, "test", None)
    assert basic["headline_down_mbps"] == 900.0  # a basic router still counts the WAN bytes


async def test_the_inspector_leaves_out_what_the_previewed_router_told(app_state, repo):
    await api_state(app_state, repo)
    newest = await seed_traffic(repo, time.time())
    start = newest - 2 * HOUR + 900
    await repo.insert_device_event("router", start + 30, "wan_link", {"old": 2500, "new": 0})
    incident_id = await repo.create_incident(Incident(start_time=start, end_time=start + 300, duration_seconds=300,
                                                      cause_code="load_induced_latency", title="t", description="d",
                                                      severity="warning", affected_targets=[], resolved=True))
    async with api() as ac:
        real = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
    async with api(ROUTER_ABSENT) as ac:
        absent = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
    async with api(ROUTER_BASIC) as ac:
        basic = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
    assert [e["kind"] for e in real["device_events"]] == ["wan_link"] and real["traffic"] is not None
    assert absent["device_events"] == [] and absent["traffic"] is None and absent["router_snapshots"] == []
    assert basic["device_events"] == [] and basic["traffic"] is None
    assert all(s["cpu_usage_pct"] is None and s["client_count"] is None for s in basic["router_snapshots"])


# --- the live stream -------------------------------------------------------------------------------------------------

def message():
    return LiveStreamMessage(timestamp=1.0, diagnosis=SystemDiagnosis(load_pct=40.0), targets=[],
                             devices={"modem": {"status": "healthy", "downstream_snr_avg": 38.0},
                                      "router": {"status": "healthy", "wan_rx_rate_mbps": 12.0, "cpu_usage_pct": 9.0,
                                                 "client_count": 23, "clients": {"online": 23}}},
                             line={"load_pct": 40.0, "down_mbps": 940.0, "up_mbps": 40.0})


def test_the_live_message_leaves_out_an_absent_device_and_its_line_load():
    shaped = LivePreview(Preview({"router": ABSENT}), basic_ids=set(), wan_hidden=True).shape(message())
    assert list(shaped.devices) == ["modem"] and shaped.line is None and shaped.diagnosis.load_pct is None


def test_the_live_message_cuts_a_basic_router_to_its_wan_figures():
    shaped = LivePreview(Preview({"router": BASIC}), basic_ids={"router"}, wan_hidden=False).shape(message())
    assert shaped.devices["router"] == {"status": "healthy", "wan_rx_rate_mbps": 12.0}
    assert shaped.line is not None and shaped.devices["modem"]["downstream_snr_avg"] == 38.0
    original = message()
    assert live_devices(original.devices, Preview(), set()) is original.devices  # no preview: untouched


# --- found in review (2026-11-17) -------------------------------------------------------------------------------------

def test_a_router_previewed_as_basic_is_configured_as_a_upnp_router_without_its_own_options():
    """Its options are the ASUS driver's; the UPnP driver takes none (extra=forbid), and the AI pack validates them."""
    from pengumon.drivers import DRIVERS
    config = AppConfig(devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.168.1.1",
                                             options={"mesh_node_names": False})])
    basic = preview_config(config, Preview({"router": BASIC})).devices[0]
    assert (basic.driver, basic.options) == ("upnp_igd", {})
    DRIVERS["upnp_igd"].options_model.model_validate(basic.options)
    assert config.devices[0].options == {"mesh_node_names": False}  # the real one is untouched


async def test_the_dossier_leaves_out_the_events_of_a_router_previewed_as_absent(app_state, repo):
    await api_state(app_state, repo)
    await repo.insert_device_event("router", time.time() - 600, "restarted", {"boot_at": 1})
    async with api() as ac:
        real = (await ac.get("/api/incidents/dossier")).text
    async with api(ROUTER_ABSENT) as ac:
        absent = (await ac.get("/api/incidents/dossier")).text
    assert "restarted" in real and "restarted" not in absent


async def test_the_editors_get_the_devices_as_configured(app_state, repo):
    """Settings and the path editor work on the configuration, which a preview does not change: a hop must never
    be linked to the wrong device because the right one is previewed as absent."""
    await api_state(app_state, repo)
    async with api(ROUTER_ABSENT) as ac:
        shown = [d["id"] for d in (await ac.get("/api/devices")).json()]
        configured = [(d["id"], d["driver"]) for d in (await ac.get("/api/devices", params={"configured": 1})).json()]
    async with api(ROUTER_BASIC) as ac:
        basic = [(d["id"], d["driver"]) for d in (await ac.get("/api/devices", params={"configured": 1})).json()]
    assert shown == ["modem"]
    assert configured == basic == [("modem", "xfinity_xb"), ("router", "asuswrt")]


def test_a_basic_router_keeps_its_wan_figures_and_grades_only():
    from pengumon.devices.snapshots import RouterMetricsSnapshot
    from pengumon.preview import basic_snapshot, basic_summary
    snap = RouterMetricsSnapshot(wan_rx_rate_mbps=12.0, cpu_usage_pct=95.0, mem_usage_pct=40.0, client_count=9,
                                 grades={"cpu": "crit", "wan_rx": "good", "wan_tx": "good"}, status="warning")
    cut = basic_snapshot(snap)
    assert (cut.wan_rx_rate_mbps, cut.cpu_usage_pct, cut.mem_usage_pct, cut.client_count) == (12.0, None, None, None)
    assert cut.grades == {"wan_rx": "good", "wan_tx": "good"} and cut.status == "healthy"  # the warning was the CPU's
    assert basic_snapshot(None) is None
    summary = basic_summary({"status": "warning", "grades": {"cpu": "warn", "wan_rx": "good"}, "cpu_usage_pct": 95.0,
                             "clients": [1], "mesh": {}, "wan_rx_rate_mbps": 12.0})
    assert summary == {"status": "healthy", "grades": {"wan_rx": "good"}, "wan_rx_rate_mbps": 12.0}


def test_a_preview_registry_works_its_devices_out_once(repo):
    registry = PreviewRegistry(devices(repo), Preview({"router": BASIC}))
    assert registry.get("router") is registry.get("router") is registry.all()[1]
    assert is_basic(registry.get("router")) and not is_basic(registry.get("modem")) and not is_basic(None)
    assert registry.wan_rates_device() is registry.get("router")
