"""The router's traffic, uptime, WAN link and temperature once read (schema v19): storage, the device events, the routes
(/traffic, /traffic/clients/{client}, /events, the hint in /clients), the explanations in the incident inspector, the
dossier and the AI pack, the MCP tool and the retention. Made-up values only (MACs 02:00:00:00:00:xx)."""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.capabilities import (
    MAX_UPTIME_SECONDS, Capability, Client, ClientsReading, PollResult, SystemReading, TrafficApps, TrafficHour,
    TrafficReading,
)
from pengumon.capabilities.contrib import ContribContext, render_section
from pengumon.clients.settings import PrivacyConfig
from pengumon.config import AppConfig
from pengumon.settings.general import GeneralConfig
from pengumon.db import migrations
from pengumon.devices import traffic
from pengumon.devices.contrib import LineLoadContribution, RouterContribution
from pengumon.devices.device import Device
from pengumon.devices.events import DeviceEvent, EventTracker, event_text
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.settings import DeviceConfig
from pengumon.incidents.model import Incident
from pengumon.main import app
from pengumon.mcp.tools import TOOLS
from pengumon.reports.dossier import build_dossier

from asuswrt_fake import HOUR, T0, UPTIME_S, FakeAsus
from device_helpers import Clock, build_router

GAME, LAPTOP, PHONE = "02:00:00:00:00:03", "02:00:00:00:00:01", "02:00:00:00:00:02"


def api():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def router_device(repo, clock, fake=None, **options):
    fake = fake or FakeAsus(clock)
    device = build_router(repo, username="u", password="p", client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
                          clock=clock, wall_clock=clock, options=options or {})
    device.driver._token = "tok"
    return device, fake


def inventory():
    return ClientsReading((
        Client(key=GAME, label_parts=("Den console",), vendor="Example Games", kind="game", mac=GAME),
        Client(key=LAPTOP, label_parts=("laptop",), vendor="Example PC", kind="computer", mac=LAPTOP),
    ))


async def seed_traffic(repo, now, device_id="router"):
    """The last 6 complete hours: the game console downloads 75 GB in the hour 3 hours ago, the laptop a little every
    hour; the newest hour is not published yet (no row). The app windows end at the newest stored hour's end."""
    newest = traffic.hour_floor(now) - 2 * HOUR  # the hour before the last one is the newest the router published
    rows = []
    for h in range(newest - 5 * HOUR, newest + HOUR, HOUR):
        game = 75 * 10**9 if h == newest - 2 * HOUR else 0
        rows += [TrafficHour("", h, 10**8, game + 10**9), TrafficHour(LAPTOP, h, 5 * 10**7, 10**9)]
        if game:
            rows.append(TrafficHour(GAME, h, 10**9, game))
    await repo.upsert_traffic_hours(device_id, rows, now - 60)
    # yesterday as a whole day (UTC, the zone of a configuration without one): the game console's row and everyone's
    yesterday = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    day_end = traffic.midnight(yesterday + timedelta(days=1), timezone.utc)
    await repo.store_traffic_apps(device_id, [
        TrafficApps(GAME, day_end, (("Sony PlayStation", 10**9, 20 * 10**9),)),
        TrafficApps("", day_end, (("Sony PlayStation", 10**9, 20 * 10**9),)),
    ], now - 60, lambda w: yesterday.isoformat())
    window = newest + HOUR
    await repo.store_traffic_apps(device_id, [
        TrafficApps(GAME, window, (("Sony PlayStation", 10**9, 75 * 10**9), ("General", 1, 10**8))),
        TrafficApps(LAPTOP, window, (("YouTube", 10**7, 3 * 10**9), ("Netflix", 10**7, 3 * 10**9))),
        TrafficApps("", window, (("Sony PlayStation", 10**9, 75 * 10**9), ("YouTube", 10**7, 3 * 10**9))),
    ], now - 60, lambda w: None)
    return newest


async def api_state(app_state, repo, privacy="full"):
    clock = Clock(time.time())
    device, _ = router_device(repo, clock)
    device.monitor.latest_clients = inventory()
    app_state.devices = DeviceRegistry([device])
    app_state.config = AppConfig(privacy=PrivacyConfig(client_details=privacy))
    app_state.repository = repo
    return device


# --- storage ---------------------------------------------------------------------------------------------------------

async def test_v19_adds_the_columns_and_tables_and_runs_once(tmp_path):
    path = str(tmp_path / "v.db")
    assert await migrations.migrate(path) == migrations.LATEST_VERSION >= 19
    con = sqlite3.connect(path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(device_samples)")}
        assert {"uptime_s", "cpu_temp_c", "wan_link_mbps"} <= cols
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"device_events", "traffic_hourly", "traffic_apps"} <= tables
    finally:
        con.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION


async def test_a_round_is_stored_through_the_monitor_hours_replaced_and_app_days_kept(repo):
    clock = Clock(T0 + 10 * 60)
    device, fake = router_device(repo, clock)
    for _ in range(12):
        await device.poll(force=True)
        clock.now += 10
    rows = await repo.get_traffic_hours("router", T0 - 24 * HOUR, T0 + HOUR)
    assert len([r for r in rows if r[1] == ""]) == 24 and (T0 - 11 * HOUR, "", 72794439, 40_000_000_000) in rows
    assert (T0 - 10 * HOUR, "02:00:00:00:00:03", 1360000000, 68000000000) in rows
    assert device.monitor.traffic_read_at is not None and device.monitor.traffic_reason is None
    latest = {c: (w, apps) for c, _, w, _, apps in await repo.get_traffic_apps("router")}
    assert latest[GAME] == (T0, [["Sony PlayStation", 4000000000, 196000000000], ["General", 0, 0], ["SSL/TLS", 0, 0]])
    # a later read of the same hour replaces it; the latest app window too
    await repo.upsert_traffic_hours("router", [TrafficHour("", T0 - HOUR, 1, 2)], clock.now)
    assert (T0 - HOUR, "", 1, 2) in await repo.get_traffic_hours("router", T0 - HOUR, T0)
    await repo.store_traffic_apps("router", [TrafficApps(GAME, T0 + HOUR, (("Netflix", 1, 2),))], clock.now,
                                  device.monitor._day_of)
    assert (await repo.get_traffic_apps("router", client=GAME))[0][4] == [["Netflix", 1, 2]]
    # T0 is 09:00 UTC: no window of this round is a whole day, so no day row was kept
    assert device.monitor._day_of(T0) is None
    assert await repo.get_traffic_app_day_range("router", "2000-01-01", "2100-01-01") == []
    # a window ending at a midnight is that day's row; a second read of the same day keeps the first
    midnight = traffic.hour_floor(T0 + 86400 - T0 % 86400)  # the next 00:00 UTC
    day = datetime.fromtimestamp(T0, timezone.utc).date().isoformat()
    assert device.monitor._day_of(midnight) == day and device.monitor._day_of(midnight + HOUR) is None
    await repo.store_traffic_apps("router", [TrafficApps(GAME, midnight, (("Netflix", 1, 2),))], clock.now,
                                  device.monitor._day_of)
    await repo.store_traffic_apps("router", [TrafficApps(GAME, midnight, (("YouTube", 3, 4),))], clock.now + 60,
                                  device.monitor._day_of)
    assert (await repo.get_traffic_apps("router", client=GAME, day=day))[0][4] == [["Netflix", 1, 2]]


async def test_the_uptime_link_and_temperature_are_stored_when_read_and_the_snapshot_keeps_them(repo):
    clock = Clock(T0 + 10 * 60)
    device, _ = router_device(repo, clock, traffic_usage=False)
    first = await device.poll(force=True)
    assert (first.uptime_s, first.wan_link_mbps, first.cpu_temp_c) == (UPTIME_S, 2500, 64.1)
    clock.now += 10
    second = await device.poll(force=True)  # not read at this poll: the snapshot keeps them, the uptime counts on
    assert (second.uptime_s, second.wan_link_mbps, second.cpu_temp_c) == (UPTIME_S + 10, 2500, 64.1)
    samples = await repo.get_device_samples("router", 0, clock.now + 1)
    assert [(s.uptime_s, s.wan_link_mbps, s.cpu_temp_c) for s in samples] == [(UPTIME_S, 2500, 64.1),
                                                                            (None, None, None)]
    rows = (await repo.get_device_samples_bucketed("router", 0, clock.now + 1, 3600))[0]
    assert rows[-3:] == (64.1, 64.1, 2500)


async def test_a_capability_said_missing_once_does_not_log_a_recovery_at_the_next_poll(caplog):
    class Once:
        name, host, offers = "fake", "192.0.2.1", frozenset({Capability.ROUTER_SYSTEM, Capability.TRAFFIC_USAGE})

        def __init__(self):
            self.n = 0

        def missing_configuration(self):
            return None

        async def poll(self):
            self.n += 1
            missing = {Capability.TRAFFIC_USAGE: "no Traffic Analyzer"} if self.n == 1 else {}
            return PollResult({Capability.ROUTER_SYSTEM: SystemReading(cpu_pct=1.0)}, missing)

    from pengumon.devices.monitors import RouterMonitor, UnconfiguredMessages
    device = Device("r", Once(), RouterMonitor(messages=UnconfiguredMessages("off", "no password")), interval_seconds=10)
    with caplog.at_level(logging.INFO, logger="pengumon.devices"):
        for _ in range(3):
            await device.poll(force=True)
    messages = [r.getMessage() for r in caplog.records]
    assert sum("no Traffic Analyzer" in m for m in messages) == 1 and not any("recovered" in m for m in messages)
    assert device.monitor.traffic_reason == "no Traffic Analyzer"


# --- device events --------------------------------------------------------------------------------------------------

def test_a_restart_is_the_boot_time_moving_on_not_the_clock_drifting():
    tracker = EventTracker("r")
    assert tracker.check(1000.0, 500, 2500) == []
    assert tracker.check(1060.0, 559, 2500) == []                   # a second slow: drift, not a restart
    events = tracker.check(5000.0, 30, 2500)
    assert [(e.kind, e.detail["boot_at"]) for e in events] == [("restarted", 4970)]
    assert tracker.check(5060.0, 90, 0)[0].detail == {"old": 2500, "new": 0}   # the link went down
    assert tracker.check(5120.0, 150, 1000)[0].detail == {"old": 0, "new": 1000}
    assert tracker.check(5180.0, 210, 1000) == []


async def test_a_restart_while_pengumon_was_stopped_is_found_from_the_stored_reading(repo):
    clock = Clock(T0)
    device, fake = router_device(repo, clock, traffic_usage=False)
    await device.poll(force=True)
    assert await repo.get_device_events("router", 0, T0 + HOUR) == []
    # a new start (a new device, nothing in memory); the router restarted meanwhile: its uptime is 2 minutes
    clock.now += 600
    device, fake = router_device(repo, clock, traffic_usage=False)
    from asuswrt_fake import APPGET
    old = APPGET["uptime()"]
    APPGET["uptime()"] = {"uptime": "Tue, 10 Nov 2026 13:10:00 -0500(120 secs since boot)"}
    try:
        await device.poll(force=True)
    finally:
        APPGET["uptime()"] = old
    events = await repo.get_device_events("router", 0, clock.now + 1)
    assert [(e[2], e[3]["boot_at"]) for e in events] == [("restarted", int(clock.now) - 120)]


async def test_the_events_route_lists_them_with_text(app_state, repo):
    await api_state(app_state, repo)
    now = time.time()
    await repo.insert_device_event("router", now - 100, "wan_link", {"old": 2500, "new": 0})
    await repo.insert_device_event("router", now - 50, "restarted", {"boot_at": now - 60, "uptime_s": 10})
    async with api() as ac:
        body = (await ac.get("/api/devices/router/events")).json()
        assert (await ac.get("/api/devices/nope/events")).status_code == 404
    assert [(e["kind"], e["text"].split(" (")[0]) for e in body["events"]] == [
        ("wan_link", "WAN link went down"), ("restarted", "restarted")]


# --- the routes -----------------------------------------------------------------------------------------------------

async def test_the_traffic_route_ranks_clients_by_download_with_names_apps_and_hints(app_state, repo):
    await api_state(app_state, repo, privacy="full")
    newest = await seed_traffic(repo, time.time())
    async with api() as ac:
        body = (await ac.get("/api/devices/router/traffic", params={"hours": 6})).json()
    assert body["available"] is True and body["reason"] is None and body["interval_seconds"] == 3600
    assert body["privacy"] == "full" and body["window_end"] == newest + HOUR
    assert len(body["wan"]) == 6 and body["wan"][-1] == {"hour": body["end"] - HOUR, "up": None, "down": None}
    assert body["totals"]["down"] == 75 * 10**9 + 5 * 10**9
    game, laptop = body["clients"]
    assert (game["label"], game["mac"], game["kind"], game["down"]) == ("Den console", GAME, "game", 75 * 10**9)
    assert game["traffic_hint"] == "Sony PlayStation" and game["apps"][0]["app"] == "Sony PlayStation"
    assert laptop["traffic_hint"] is None  # two apps half each: no hint
    assert [a["app"] for a in body["apps"]] == ["Sony PlayStation", "YouTube"]
    assert len(game["key"]) == 12 and GAME not in game["key"]


async def test_the_traffic_routes_mask_clients_as_the_clients_list_does(app_state, repo):
    await api_state(app_state, repo, privacy="masked")
    await seed_traffic(repo, time.time())
    async with api() as ac:
        masked = await ac.get("/api/devices/router/traffic", params={"hours": 6})
        key = masked.json()["clients"][0]["key"]
        one = await ac.get(f"/api/devices/router/traffic/clients/{key}", params={"hours": 48})
        by_mac = await ac.get(f"/api/devices/router/traffic/clients/{GAME}")
        clients = await ac.get("/api/devices/router/clients")
        app_state.config = AppConfig(privacy=PrivacyConfig(client_details="full"))
        full = await ac.get(f"/api/devices/router/traffic/clients/{GAME}", params={"hours": 6})
    assert masked.json()["privacy"] == "masked"
    assert GAME not in masked.text and "Den console" not in masked.text and "Example Games 0003" in masked.text
    assert one.status_code == 200 and GAME not in one.text and one.json()["client"]["label"] == "Example Games 0003"
    assert sum(p["down"] or 0 for p in one.json()["series"]) == 75 * 10**9
    # 48 hours: the apps are the whole day stored (yesterday's 20 GB), not the latest 24-hour window (75 GB)
    assert one.json()["apps"][0]["down"] == 20 * 10**9 and one.json()["apps_days"]["stored"] == 1
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert [d["day"] for d in one.json()["days"]] == [yesterday]
    assert by_mac.status_code == 404  # a MAC only for a request that may see them
    assert full.status_code == 200 and full.json()["client"]["mac"] == GAME
    rows = {c["key"]: c for c in clients.json()["clients"]}
    assert rows[key]["traffic_hint"] == "Sony PlayStation"  # the same key in both answers


async def test_a_device_without_traffic_is_a_400_and_a_switched_off_one_says_why(app_state, repo):
    clock = Clock(time.time())
    off, _ = router_device(repo, clock, traffic_usage=False)
    app_state.devices, app_state.repository, app_state.config = DeviceRegistry([off]), repo, AppConfig()
    async with api() as ac:
        body = (await ac.get("/api/devices/router/traffic")).json()
        assert (await ac.get("/api/devices/router/traffic/clients/abc")).status_code == 404
    assert body["available"] is False and "traffic_usage: false" in body["reason"]
    from device_helpers import build_modem
    app_state.devices = DeviceRegistry([build_modem(password="pw")])
    async with api() as ac:
        assert (await ac.get("/api/devices/modem/traffic")).status_code == 400


# --- explaining incidents and busy spells ---------------------------------------------------------------------------

async def test_the_hours_of_an_incident_name_the_top_downloader_or_say_not_yet(repo):
    now = time.time()
    newest = await seed_traffic(repo, now)
    namer = traffic.Namer("full", inventory())
    hours = await traffic.hours_explained(repo, "router", newest - 2 * HOUR + 600, newest - 2 * HOUR + 900, namer)
    assert len(hours) == 1 and hours[0]["clients"][0]["mac"] == GAME
    assert hours[0]["text"].startswith(f"Den console ({GAME}) downloaded 75 GB in ")
    later = await traffic.hours_explained(repo, "router", newest + HOUR + 60, newest + HOUR + 120, namer)
    assert later[0]["available"] is False and later[0]["state"] == "not yet available"
    generic = await traffic.hours_explained(repo, "router", newest - 2 * HOUR, newest - 2 * HOUR + 1,
                                            traffic.Namer("generic", inventory()))
    assert generic[0]["text"].startswith("a game console (Sony PlayStation) downloaded 75 GB")
    assert GAME not in json.dumps(generic) and "Den console" not in json.dumps(generic)


async def test_the_inspector_carries_the_events_and_the_traffic_of_its_hours(app_state, repo):
    await api_state(app_state, repo, privacy="full")
    now = time.time()
    newest = await seed_traffic(repo, now)
    start = newest - 2 * HOUR + 900
    await repo.insert_device_event("router", start + 30, "wan_link", {"old": 2500, "new": 0})
    await repo.insert_device_event("router", start - 3 * 3600, "restarted", {"boot_at": 1})  # far before: not shown
    incident_id = await repo.create_incident(Incident(start_time=start, end_time=start + 300, duration_seconds=300,
                                                      cause_code="load_induced_latency", title="t", description="d",
                                                      severity="warning", affected_targets=[], resolved=True))
    async with api() as ac:
        body = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
    assert [e["kind"] for e in body["device_events"]] == ["wan_link"]
    assert body["traffic"]["hours"][0]["clients"][0]["label"] == "Den console"


async def test_the_dossier_explains_busy_hours_by_device_kind_only(repo):
    now = time.time()
    newest = await seed_traffic(repo, now)
    start = newest - 2 * HOUR + 600
    await repo.create_incident(Incident(start_time=start, end_time=start + 120, duration_seconds=120,
                                        cause_code="load_induced_latency", title="Line busy", description="d", severity="warning",
                                        affected_targets=[], resolved=True, load_pct=95.0))
    await repo.insert_device_event("router", start, "restarted", {"boot_at": start - 30, "uptime_s": 30})
    source = ("router", traffic.Namer("generic", inventory()))
    text = (await build_dossier(repo, AppConfig(), 24, now=now, traffic=source))["formatted_markdown"]
    assert "a game console (Sony PlayStation) downloaded 75 GB in" in text
    assert "### Router events" in text and "router restarted" in text
    assert GAME not in text and "Den console" not in text and "02:00:00" not in text


async def test_the_ai_pack_gives_the_busy_hours_and_the_router_events_without_naming_a_client(repo):
    now = time.time()
    newest = await seed_traffic(repo, now)
    busy = newest - 2 * HOUR
    config = AppConfig(devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.168.88.1")],
                       line={"down_mbps": 100, "up_mbps": 10})
    from pengumon.db.samples import DeviceSample
    for i in range(12):  # a busy stretch in that hour
        await repo.insert_device_sample(DeviceSample("router", busy + 600 + 10 * i, "healthy", wan_rx_mbps=95.0,
                                                     wan_tx_mbps=1.0))
    await repo.insert_device_event("router", busy + 700, "wan_link", {"old": 2500, "new": 1000})
    ctx = ContribContext(repository=repo, config=config, start=busy, end=busy + HOUR, devices=tuple(config.devices),
                         live=SimpleNamespace(latest_clients=inventory()))
    load = LineLoadContribution()
    text = render_section(load.section(await load.collect(ctx), "full"))
    assert f"Router-measured traffic: 76 GB down, {{counted:{busy}:{busy + HOUR}}}" in text
    assert "most of it by a game console (Sony PlayStation): 75 GB" in text
    assert GAME not in text and "Den console" not in text
    router = RouterContribution()
    facts = await router.collect(ctx)
    assert [e.code for e in router.emit(facts)] == ["router.wan_link"]
    assert "WAN port link: 1 Gb/s at {when:" in render_section(router.section(facts, "full"))


# --- MCP and retention ----------------------------------------------------------------------------------------------

async def test_the_mcp_tool_ranks_the_devices_by_kind_never_by_name_or_mac(app_state, repo):
    await api_state(app_state, repo, privacy="full")  # even when the dashboard shows names: the tools never do
    await seed_traffic(repo, time.time())
    tool = next(t for t in TOOLS if t.name == "traffic")
    out = await tool.handler(app_state, {"hours": 6, "top": 5})
    assert out["available"] is True
    assert out["clients"][0] == {"device": "game console", "vendor": "Example Games", "download_bytes": 75 * 10**9,
                                 "upload_bytes": 10**9, "main_app": "Sony PlayStation",
                                 "top_apps": [{"app": "Sony PlayStation", "download_bytes": 75 * 10**9,
                                               "upload_bytes": 10**9},
                                              {"app": "General", "download_bytes": 10**8, "upload_bytes": 1}]}
    assert GAME not in json.dumps(out) and "Den console" not in json.dumps(out)
    assert out["busiest_hours"][0]["download_bytes"] == 76 * 10**9 and out["totals"]["download_bytes"] == 80 * 10**9


async def test_the_hourly_cleanup_prunes_traffic_by_its_own_setting(repo):
    now = time.time()
    old = traffic.hour_floor(now - 20 * 86400)
    await repo.upsert_traffic_hours("router", [TrafficHour("", old, 1, 2),
                                               TrafficHour("", traffic.hour_floor(now) - HOUR, 3, 4)], now)
    await repo.store_traffic_apps("router", [TrafficApps(GAME, old, ())], now, lambda w: "2026-10-20")
    await repo.insert_device_event("router", now - 500 * 86400, "restarted", {})
    await repo.insert_device_event("router", now - 86400, "restarted", {})
    repo.traffic_retention_days = 10
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=400)
    assert (removed["traffic_hourly"], removed["traffic_apps"], removed["device_events"]) == (1, 2, 1)
    assert len(await repo.get_traffic_hours("router", 0, now + HOUR)) == 1


def test_the_retention_setting_is_validated():
    from pengumon.config import DatabaseConfig
    assert DatabaseConfig().traffic_retention_days == 400
    with pytest.raises(ValueError):
        DatabaseConfig(traffic_retention_days=0)


# --- hostile values (0.27 security review) --------------------------------------------------------------------------

def test_an_uptime_that_is_not_one_is_ignored_and_never_becomes_a_boot_time():
    """A bad uptime (negative, over 20 years, not a number) is left out; before 0.27's fix it set a boot time decades
    off, and the next good reading was a "restart" (with a boot time no clock can show)."""
    tracker = EventTracker("r")
    assert tracker.check(1000.0, 500, None) == []
    for bad in (10**15, -5, MAX_UPTIME_SECONDS + 1, float("nan"), True, "600"):
        assert tracker.check(1060.0, bad, None) == []
    assert tracker.check(1120.0, 620, None) == []  # the same boot as before the bad ones
    fresh = EventTracker("r")
    fresh.restore((1000.0, 10**15), None)  # a stored row from before the fix
    assert fresh.check(1060.0, 560, None) == [] and fresh.check(1120.0, 620, None) == []


def test_an_uptime_that_wraps_is_not_a_restart_but_a_restart_after_a_wrap_still_is():
    """SNMP sysUpTime wraps every 2^32 hundredths of a second (about 497 days)."""
    wrap = 2**32 / 100
    tracker = EventTracker("r")
    t = 1_800_000_000.0
    assert tracker.check(t, int(wrap) - 30, None, wrap) == []
    assert tracker.check(t + 60, 29, None, wrap) == []        # wrapped: 30 s past 2^32 ticks
    assert tracker.check(t + 120, 89, None, wrap) == []       # and on from there
    assert tracker.check(t + 86400 * 30, 86400 * 30 - 31, None, wrap) == []  # a month on: still the same boot
    events = tracker.check(t + 86400 * 31, 50, None, wrap)    # a real restart
    assert [(e.kind, e.detail["boot_at"], e.detail["previous_boot_at"]) for e in events] == [
        ("restarted", round(t + 86400 * 31 - 50), round(t - int(wrap) + 30))]
    other = EventTracker("r")  # without wrap_s (AsusWRT: seconds since boot, no wrap) the drop is a restart
    other.check(t, int(wrap) - 30, None)
    assert [e.kind for e in other.check(t + 60, 29, None)] == ["restarted"]


async def test_the_snmp_uptime_wrap_reaches_the_events_through_the_monitor(repo):
    from pengumon.devices.monitors import RouterMonitor, UnconfiguredMessages
    from pengumon.drivers.snmp import UPTIME_WRAP_SECONDS
    assert UPTIME_WRAP_SECONDS == 2**32 / 100
    monitor = RouterMonitor(repo, messages=UnconfiguredMessages("off", "no password"), device_id="r")
    t = 1_800_000_000.0
    for ts, uptime in ((t, int(UPTIME_WRAP_SECONDS) - 30), (t + 60, 29), (t + 120, 89)):
        reading = SystemReading(cpu_pct=1.0, uptime_s=uptime, uptime_wrap_s=UPTIME_WRAP_SECONDS)
        await monitor.on_result(ts, PollResult({Capability.ROUTER_SYSTEM: reading}, {}))
    assert await repo.get_device_events("r", 0, t + 3600) == []


def test_an_event_text_from_a_bad_stored_row_is_plain_words():
    for boot in (1e300, -1e300, float("nan"), float("inf"), True, "08:14", None, 10**30):
        assert event_text(DeviceEvent("r", 1.0, "restarted", {"boot_at": boot})) == "restarted"
    assert event_text(DeviceEvent("r", 1.0, "wan_link", {"old": "fast", "new": [1]})) == (
        "WAN link changed from unknown to unknown")
    assert event_text(DeviceEvent("r", 1.0, "wan_link", {"old": {}, "new": 0})) == "WAN link went down (was unknown)"


async def test_a_bad_stored_event_breaks_no_reader(app_state, repo):
    """The events route, the incident inspector and the dossier all read stored events: a row an older version stored
    (a boot time far off, NaN that no JSON answer may carry) gives "restarted", never a 500."""
    await api_state(app_state, repo, privacy="full")
    now = time.time()
    await repo.insert_device_event("router", now - 120, "restarted", {"boot_at": 1e300, "uptime_s": 10**15})
    await repo.insert_device_event("router", now - 110, "restarted", {"boot_at": float("nan")})
    await repo.insert_device_event("router", now - 100, "wan_link", {"old": "x", "new": None})
    incident_id = await repo.create_incident(Incident(start_time=now - 130, end_time=now - 90, duration_seconds=40,
                                                      cause_code="load_induced_latency", title="t", description="d",
                                                      severity="warning", affected_targets=[], resolved=True))
    async with api() as ac:
        events = await ac.get("/api/devices/router/events")
        inspect = await ac.get(f"/api/incidents/{incident_id}/inspect")
    assert events.status_code == 200 and inspect.status_code == 200
    assert [e["text"] for e in events.json()["events"]] == ["restarted", "restarted", "WAN link changed from unknown "
                                                                                         "to unknown"]
    assert events.json()["events"][1]["detail"] == {}  # NaN is not JSON: the row's detail is dropped
    assert [e["kind"] for e in inspect.json()["device_events"]] == ["restarted", "restarted", "wan_link"]
    text = (await build_dossier(repo, AppConfig(), 24, now=now))["formatted_markdown"]
    assert "### Router events" in text and "router restarted" in text


async def test_device_events_are_kept_as_long_as_the_quarter_hour_rollups(repo):
    now = time.time()
    await repo.insert_device_event("router", now - 500 * 86400, "restarted", {})
    await repo.insert_device_event("router", now - 100 * 86400, "restarted", {})  # older than the per-minute rollups
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=30, long_rollup_retention_days=400)
    assert removed["device_events"] == 1
    assert [round((now - e[1]) / 86400) for e in await repo.get_device_events("router", 0, now)] == [100]


# An app name the router (or whatever answers at its address) chose to look like the AI pack's time tokens: the pack
# float()s what follows "{when:", so "1.2.3" made building the pack raise (a 500) before 0.27's fix.
TOKEN_APP = "{when:1.2.3}"


def test_router_names_cannot_carry_curly_brackets():
    from pengumon.drivers import asuswrt_traffic as stats
    from pengumon.drivers.common import clean_text
    assert clean_text(TOKEN_APP) == "(when:1.2.3)"
    assert stats.app_rows([[TOKEN_APP, 1, 2]]) == (("(when:1.2.3)", 1, 2),)
    # a name stored before the fix: the hint built from it has none either
    assert traffic.hint_of([[TOKEN_APP, 10**9, 10**9]], 1000.0, 1000.0) == "(when:1.2.3)"


async def test_a_stored_app_name_like_a_time_token_reaches_no_finding_as_one(repo):
    now = time.time()
    newest = await seed_traffic(repo, now)
    busy = newest - 2 * HOUR
    await repo.store_traffic_apps("router", [TrafficApps(GAME, newest + HOUR, ((TOKEN_APP, 10**9, 75 * 10**9),))],
                                  now - 30, lambda w: "2026-11-09")
    config = AppConfig(devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.168.88.1")],
                       line={"down_mbps": 100, "up_mbps": 10})
    from pengumon.db.samples import DeviceSample
    for i in range(12):
        await repo.insert_device_sample(DeviceSample("router", busy + 600 + 10 * i, "healthy", wan_rx_mbps=95.0,
                                                     wan_tx_mbps=1.0))
    ctx = ContribContext(repository=repo, config=config, start=busy, end=busy + HOUR, devices=tuple(config.devices),
                         live=SimpleNamespace(latest_clients=inventory()))
    load = LineLoadContribution()
    section = load.section(await load.collect(ctx), "full")
    text = render_section(section)
    assert "a game console ((when:1.2.3)): 75 GB" in text and TOKEN_APP not in text


def test_the_ai_pack_renders_a_finding_whose_time_token_is_not_a_time():
    """The renderer's own guard: a token that is not a time stays text (without its brackets)."""
    from ai_pack_helpers import Section, canary_facts
    from pengumon.reports.ai_pack import ModuleInput, render_pack
    import dataclasses
    facts = canary_facts(week=False, with_modules=False)
    module = ModuleInput("line_load", "Line load", "capability", 1200, "How to read it.",
                         Section(findings=[f"Most of it by a game console ({TOKEN_APP}).",
                                           "Then {when:99999999999999999999}, {minute:1e5.5} and {counted:1.2.3:4}."]))
    for level in ("full", "vague", "anonymized"):
        md = render_pack(dataclasses.replace(facts, modules=(module,)), level).markdown
        assert "(when:1.2.3)" in md and "when:99999999999999999999" in md


@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
@pytest.mark.parametrize("name", ["start_time", "end_time"])
async def test_the_events_route_refuses_a_time_that_is_not_a_finite_number(app_state, repo, name, value):
    await api_state(app_state, repo)
    async with api() as ac:
        res = await ac.get(f"/api/devices/router/events?{name}={value}")
    assert res.status_code == 422


# --- ranges longer than a day: the apps are the sum of whole local days ---------------------------------------------------

NY = ZoneInfo("America/New_York")
NOW_NY = datetime(2026, 11, 12, 13, 30, tzinfo=NY).timestamp()  # a Thursday afternoon, 11 days after daylight saving ended


async def seed_days(repo, tz=NY, now=NOW_NY):
    """Whole days (midnight to midnight in `tz`) for the 3 days before `now`'s: the console played 10 GB of Sony
    PlayStation a day, the laptop watched 2 GB of YouTube a day; the day before those has a row that is not a whole day
    (it ends at 09:00, as Pengumon before 0.27.0 kept one). Every hour of the last 5 days has a WAN row and the console
    and the laptop download in each. The latest 24-hour windows say Netflix only."""
    today = datetime.fromtimestamp(now, tz).date()
    for back in (1, 2, 3):
        day = today - timedelta(days=back)
        end = traffic.midnight(day + timedelta(days=1), tz)
        await repo.store_traffic_apps("router", [
            TrafficApps(GAME, end, (("Sony PlayStation", 10**8, 10 * 10**9),)),
            TrafficApps(LAPTOP, end, (("YouTube", 10**7, 2 * 10**9),)),
            TrafficApps("", end, (("Sony PlayStation", 10**8, 10 * 10**9), ("YouTube", 10**7, 2 * 10**9))),
        ], now, lambda w, d=day: d.isoformat())
    legacy = today - timedelta(days=4)
    await repo.store_traffic_apps("router", [
        TrafficApps(GAME, traffic.midnight(legacy + timedelta(days=1), tz) + 9 * HOUR, (("Old Game", 1, 99 * 10**9),)),
        TrafficApps("", traffic.midnight(legacy + timedelta(days=1), tz) + 9 * HOUR, (("Old Game", 1, 99 * 10**9),)),
    ], now, lambda w: legacy.isoformat())
    latest = traffic.hour_floor(now)
    await repo.store_traffic_apps("router", [TrafficApps(c, latest, (("Netflix", 1, 5 * 10**9),)) for c in ("", GAME, LAPTOP)],
                                  now, lambda w: None)
    rows = []
    for h in range(traffic.hour_floor(now) - 5 * 24 * HOUR, traffic.hour_floor(now), HOUR):
        rows += [TrafficHour("", h, 10**6, 3 * 10**8), TrafficHour(GAME, h, 10**6, 2 * 10**8),
                 TrafficHour(LAPTOP, h, 10**6, 10**8)]
    await repo.upsert_traffic_hours("router", rows, now)


async def test_seven_days_count_the_apps_of_the_whole_days_in_them_not_the_latest_window(repo):
    await seed_days(repo)
    namer = traffic.Namer("full", inventory())
    week = await traffic.summary(repo, "router", 168, namer, now=NOW_NY, tz=NY)
    assert week["apps_days"] == {"first": "2026-11-05", "last": "2026-11-11", "wanted": 7, "stored": 3,
                                 "days": ["2026-11-09", "2026-11-10", "2026-11-11"]}
    # three whole days; the row that is not a whole day (Old Game) and the latest window (Netflix) are not counted
    assert week["apps"] == [{"app": "Sony PlayStation", "up": 3 * 10**8, "down": 30 * 10**9},
                            {"app": "YouTube", "up": 3 * 10**7, "down": 6 * 10**9}]
    assert week["apps_window_end"] == datetime(2026, 11, 12, tzinfo=NY).timestamp()
    game, laptop = week["clients"]
    assert game["apps"] == [{"app": "Sony PlayStation", "up": 3 * 10**8, "down": 30 * 10**9}]
    assert game["traffic_hint"] == "Sony PlayStation" and laptop["traffic_hint"] == "YouTube"
    # a day or less: the router's latest 24-hour window, as before
    day = await traffic.summary(repo, "router", 24, namer, now=NOW_NY, tz=NY)
    assert day["apps_days"] is None and [a["app"] for a in day["apps"]] == ["Netflix"]
    assert day["clients"][0]["apps"][0]["app"] == "Netflix"
    # the days are cut on the zone: in UTC none of the stored rows ends at a midnight
    utc = await traffic.summary(repo, "router", 168, namer, now=NOW_NY, tz=timezone.utc)
    assert utc["apps"] == [] and utc["apps_days"]["stored"] == 0 and utc["apps_window_end"] is None
    assert utc["clients"][0]["apps"] == [] and utc["clients"][0]["traffic_hint"] is None


async def test_thirty_days_are_whole_local_days_up_to_the_last_midnight_with_a_total_per_day(repo):
    await seed_days(repo)
    body = await traffic.summary(repo, "router", 24, traffic.Namer("full", inventory()), now=NOW_NY, tz=NY, days=30)
    assert body["days"] == 30 and body["apps_days"]["wanted"] == 30 and body["apps_days"]["stored"] == 3
    assert body["start"] == datetime(2026, 10, 13, tzinfo=NY).timestamp()
    assert body["end"] == datetime(2026, 11, 12, tzinfo=NY).timestamp()  # today, still under way, is not in it
    assert body["hours"] == 30 * 24 + 1  # 1 November had 25 hours (daylight saving ended)
    assert body["wan"] == []  # per day only: the hours of 30 days would be most of the answer
    daily = body["daily"]
    assert [d["day"] for d in daily][:2] == ["2026-10-13", "2026-10-14"] and daily[-1]["day"] == "2026-11-11"
    assert next(d for d in daily if d["day"] == "2026-11-01")["hours"] == 25
    # the WAN rows cover the last 5 days up to now: 4 whole days and today's hours (not in the range)
    full = [d for d in daily if d["stored"]]
    assert [d["day"] for d in full] == ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"]
    assert full[0]["stored"] < 24 and all(d["stored"] == d["hours"] == 24 for d in full[1:])
    assert all(d["down"] == 24 * 3 * 10**8 for d in full[1:]) and daily[0]["down"] is None
    assert body["totals"]["down"] == sum(d["down"] for d in full)
    assert body["apps"][0] == {"app": "Sony PlayStation", "up": 3 * 10**8, "down": 30 * 10**9}


async def test_the_traffic_route_takes_days_in_the_zone_set_in_settings(app_state, repo):
    await api_state(app_state, repo, privacy="full")
    app_state.config = AppConfig(privacy=PrivacyConfig(client_details="full"),
                                 general=GeneralConfig(timezone="America/New_York"))
    await seed_days(repo, now=time.time())
    async with api() as ac:
        month = await ac.get("/api/devices/router/traffic", params={"days": 30})
        week = await ac.get("/api/devices/router/traffic", params={"hours": 168})
        bad = [await ac.get("/api/devices/router/traffic", params={"days": d}) for d in (0, traffic.MAX_DAYS + 1, "x")]
    assert month.status_code == 200 and len(month.json()["daily"]) == 30 and month.json()["days"] == 30
    assert month.json()["apps_days"]["stored"] == 3 and month.json()["apps"][0]["app"] == "Sony PlayStation"
    assert week.json()["days"] is None and week.json()["daily"] is None and week.json()["apps_days"]["stored"] == 3
    assert [r.status_code for r in bad] == [422, 422, 422]


async def test_a_day_row_is_kept_only_for_a_window_that_ends_at_a_midnight_in_the_zone_set_in_settings(repo):
    clock = Clock(NOW_NY)
    device, _ = router_device(repo, clock)
    device.monitor._config = AppConfig(general=GeneralConfig(timezone="America/New_York"))
    assert device.monitor._day_of(int(datetime(2026, 11, 12, tzinfo=NY).timestamp())) == "2026-11-11"
    assert device.monitor._day_of(int(datetime(2026, 11, 12, 1, tzinfo=NY).timestamp())) is None
    assert device.monitor._day_of(int(datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp())) is None
    # the day daylight saving ended is a whole day too (25 hours)
    assert device.monitor._day_of(int(datetime(2026, 11, 2, tzinfo=NY).timestamp())) == "2026-11-01"


async def test_a_device_over_7_and_30_days_has_the_apps_and_hint_of_the_whole_days_and_a_total_per_day(repo):
    await seed_days(repo)
    namer = traffic.Namer("full", inventory())
    day = await traffic.client_detail(repo, "router", GAME, 24, namer, now=NOW_NY, tz=NY)
    assert day["apps_days"] is None and [a["app"] for a in day["apps"]] == ["Netflix"] and day["daily"] is None
    week = await traffic.client_detail(repo, "router", GAME, 168, namer, now=NOW_NY, tz=NY)
    assert week["apps"] == [{"app": "Sony PlayStation", "up": 3 * 10**8, "down": 30 * 10**9}]
    assert week["client"]["traffic_hint"] == "Sony PlayStation" and week["apps_days"]["stored"] == 3
    assert week["apps_window_end"] == datetime(2026, 11, 12, tzinfo=NY).timestamp() and week["range_days"] is None
    month = await traffic.client_detail(repo, "router", GAME, 24, namer, now=NOW_NY, tz=NY, days=30)
    assert month["range_days"] == 30 and len(month["daily"]) == 30 and month["hours"] == 30 * 24 + 1
    assert month["series"] == []
    assert month["start"] == datetime(2026, 10, 13, tzinfo=NY).timestamp()
    assert month["daily"][-1] == {"day": "2026-11-11", "start": int(datetime(2026, 11, 11, tzinfo=NY).timestamp()),
                                  "end": int(datetime(2026, 11, 12, tzinfo=NY).timestamp()), "up": 24 * 10**6,
                                  "down": 24 * 2 * 10**8, "hours": 24, "stored": 24}
    assert month["totals"]["down"] == sum(d["down"] or 0 for d in month["daily"])
    assert month["apps"][0]["down"] == 30 * 10**9 and [d["day"] for d in month["days"]] == ["2026-11-09", "2026-11-10",
                                                                                             "2026-11-11"]
    # a device without a whole day of apps in the range: no apps and no hint, not its latest window
    laptop_none = await traffic.client_detail(repo, "router", PHONE, 168, namer, now=NOW_NY, tz=NY)
    assert laptop_none["apps"] == [] and laptop_none["client"]["traffic_hint"] is None


async def test_the_device_route_takes_days(app_state, repo):
    await api_state(app_state, repo, privacy="full")
    await seed_days(repo, now=time.time())
    async with api() as ac:
        month = await ac.get(f"/api/devices/router/traffic/clients/{GAME}", params={"days": 30})
        bad = await ac.get(f"/api/devices/router/traffic/clients/{GAME}", params={"days": 0})
    assert month.status_code == 200 and len(month.json()["daily"]) == 30 and month.json()["range_days"] == 30
    assert bad.status_code == 422


# --- names given to devices in Pengumon (schema v20) ------------------------------------------------------------------

async def test_v20_adds_the_client_names_table(tmp_path):
    path = str(tmp_path / "v.db")
    assert await migrations.migrate(path) == migrations.LATEST_VERSION >= 20
    con = sqlite3.connect(path)
    try:
        assert [r[1] for r in con.execute("PRAGMA table_info(client_names)")] == ["mac", "name", "updated_at"]
    finally:
        con.close()


async def test_a_name_given_in_pengumon_replaces_the_routers_wherever_names_are_shown_and_can_be_taken_away(app_state, repo):
    await api_state(app_state, repo, privacy="full")
    await seed_traffic(repo, time.time())
    async with api() as ac:
        key = (await ac.get("/api/devices/router/traffic", params={"hours": 6})).json()["clients"][0]["key"]
        put = await ac.put(f"/api/devices/router/clients/{key}/name", json={"name": "  Den   PS5 {when:1} "})
        traffic_rows = (await ac.get("/api/devices/router/traffic", params={"hours": 6})).json()["clients"]
        detail = (await ac.get(f"/api/devices/router/traffic/clients/{key}", params={"hours": 6})).json()["client"]
        clients = {c["key"]: c for c in (await ac.get("/api/devices/router/clients")).json()["clients"]}
        app_state.config = AppConfig(privacy=PrivacyConfig(client_details="masked"))
        masked = (await ac.get("/api/devices/router/traffic", params={"hours": 6})).text
        refused = await ac.put(f"/api/devices/router/clients/{key}/name", json={"name": "x"})
        app_state.config = AppConfig(privacy=PrivacyConfig(client_details="full"))
        cleared = await ac.put(f"/api/devices/router/clients/{key}/name", json={"name": None})
        after = (await ac.get("/api/devices/router/traffic", params={"hours": 6})).json()["clients"][0]
        too_long = await ac.put(f"/api/devices/router/clients/{key}/name", json={"name": "x" * 41})
        unknown = await ac.put("/api/devices/router/clients/nobody/name", json={"name": "x"})
    # spaces collapsed, curly brackets made round (names go into sentences, which must not carry a time token)
    assert put.status_code == 200 and put.json() == {"key": key, "label": "Den PS5 (when:1)", "router_label": "Den console"}
    assert traffic_rows[0]["label"] == "Den PS5 (when:1)" and traffic_rows[0]["router_label"] == "Den console"
    assert detail["label"] == "Den PS5 (when:1)"
    assert clients[key]["label"] == "Den PS5 (when:1)" and clients[key]["router_label"] == "Den console"
    assert "Den PS5" not in masked and refused.status_code == 403  # never where names are hidden
    assert cleared.status_code == 200 and cleared.json()["router_label"] is None
    assert after["label"] == "Den console" and after["router_label"] is None
    assert too_long.status_code == 422 and unknown.status_code == 404
    assert await repo.get_client_names() == {}


async def test_names_are_kept_in_the_database_read_once_and_never_used_for_the_dossier_or_ai_pack(repo):
    from pengumon.clients.names import ClientNames
    names = ClientNames()
    await names.set(repo, GAME, "Den PS5")
    assert await repo.get_client_names() == {GAME: "Den PS5"}
    later = ClientNames()  # a restart: read from the database
    assert await later.load(repo) == {GAME: "Den PS5"} and later.current() == {GAME: "Den PS5"}
    generic = traffic.Namer("generic", inventory(), names={GAME: "Den PS5"})
    masked = traffic.Namer("masked", inventory(), names={GAME: "Den PS5"})
    assert generic.describe(GAME) == "a game console" and generic.row(GAME)["label"] == "game console"
    assert masked.row(GAME)["label"] == "Example Games 0003" and "router_label" not in masked.row(GAME)
    full = traffic.Namer("full", inventory(), names={GAME: "Den PS5"})
    assert full.describe(GAME) == f"Den PS5 ({GAME})"
