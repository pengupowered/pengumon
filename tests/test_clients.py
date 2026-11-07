"""Client and mesh visibility: the AsusWRT parser on an AiMesh-shaped answer (made-up names, RFC 5737 addresses, MACs
AA:AA:AA:..), the capabilities and features, storage (schema v10, counts only), the history and its bucketing, the
/clients API with its privacy modes, and the guards on the new routes."""

import json
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import secrets as secrets_module
from pengumon import security
from pengumon.api import clients as clients_api
from pengumon.capabilities import BAND_2G, BAND_5G, BAND_OTHER, Capability, Client, ClientsReading, MeshReading
from pengumon.capabilities.features import feature_status
from pengumon.clients.settings import PrivacyConfig
from pengumon.config import AppConfig
from pengumon.devices.settings import DeviceConfig
from pengumon.db import migrations
from pengumon.devices.registry import DeviceRegistry, build_device
from pengumon.devices.summary import mesh_nodes
from pengumon.drivers.asuswrt import (
    _parse_client_list, _parse_clients, _parse_mesh_names, _parse_mesh_nodes, build_mesh, node_id,
)
from pengumon.main import app, security_config

from device_helpers import Clock, build_modem, build_router

TOKEN = "tok-clients"


def entry(**fields):
    base = {"isOnline": "1", "isWL": "0", "name": "", "nickName": "", "vendor": "", "ip": "", "mac": "", "ipMethod": "DHCP",
            "rssi": "0", "curTx": "", "curRx": "", "totalTx": "", "totalRx": "", "wlConnectTime": "",
            "amesh_papMac": "", "isGN": "0"}
    return {**base, **fields}


MAC = "AA:AA:AA:AA:AA:%02X"
CLIENT_ENTRIES = {
    # wired desk PC on node 1
    MAC % 1: entry(isOnline="1", name="desk-pc", vendor="Acme", ip="192.0.2.11", mac=MAC % 1, amesh_papMac="AA:AA:AA:00:00:01"),
    # 5 GHz phone on node 2, 27 hours connected
    MAC % 2: entry(isWL="2", nickName="Ann's phone", name="phone", vendor="Example", ip="192.0.2.12", mac=MAC % 2,
                   rssi="-58", curTx="866", curRx="780", wlConnectTime="27:05:09", amesh_papMac="AA:AA:AA:00:00:02"),
    # 2.4 GHz plug on node 1
    MAC % 3: entry(isWL="1", name="plug", ip="192.0.2.13", mac=MAC % 3, rssi="-71", curTx="144", curRx="130",
                   wlConnectTime="00:00:42", amesh_papMac="AA:AA:AA:00:00:01"),
    # a second high band on node 3
    MAC % 4: entry(isWL="3", name="tv", vendor="Acme", ip="192.0.2.14", mac=MAC % 4, rssi="-64", curTx="1201",
                   curRx="1080", wlConnectTime="03:00:00", amesh_papMac="AA:AA:AA:00:00:03"),
    # offline Wi-Fi client with stale values
    MAC % 5: entry(isOnline="0", isWL="2", name="old-laptop", ip="192.0.2.15", mac=MAC % 5, rssi="-50", curTx="400",
                   curRx="400", wlConnectTime="00:10:00", amesh_papMac="AA:AA:AA:00:00:02"),
    # offline wired client
    MAC % 6: entry(isOnline="0", name="printer", ip="192.0.2.16", mac=MAC % 6),
    # wired on node 2
    MAC % 7: entry(name="nas", ip="192.0.2.17", mac=MAC % 7, amesh_papMac="AA:AA:AA:00:00:02"),
}


def combined_body(entries=None) -> str:
    """What an AsusWRT router answers to a combined request: not valid JSON as a whole."""
    entries = CLIENT_ENTRIES if entries is None else entries
    clientlist = json.dumps({**entries, "maclist": list(entries), "ClientAPILevel": "2"})
    return ('{\n"netdev": "INTERNET_rx":"0x5f5e100","INTERNET_tx":"0x2faf080"\n,'
            '"cpu_usage":"cpu1_total":"1000","cpu1_usage":"250"\n,'
            '"memory_usage":"mem_total":"1000","mem_free":"400","mem_used":"600"\n,'
            f'"get_clientlist":{clientlist}\n}}')


MESH_NODES = {"get_cfg_clientlist": [
    {"alias": "", "model_name": "RT-EXAMPLE", "mac": "AA:AA:AA:00:00:01", "wired_mac": ["AA:AA:AA:00:01:01"], "online": "1"},
    {"alias": "Study", "model_name": "RT-EXAMPLE", "mac": "AA:AA:AA:00:00:F2", "ap5g": "AA:AA:AA:00:00:02", "wired_mac": [],
     "pap5g": "AA:AA:AA:00:00:09", "online": "1"},
]}


def by_mac(reading: ClientsReading):
    return {c.mac: c for c in reading.clients}


# --- the parser -----------------------------------------------------------------------------------------------------

def test_the_non_json_combined_answer_gives_the_client_list():
    reading = _parse_client_list(combined_body())
    assert reading is not None and len(reading.clients) == 7  # maclist and ClientAPILevel are not clients
    assert sum(c.online for c in reading.clients) == _parse_clients(combined_body()) == 5
    assert _parse_client_list("<html>") is None and _parse_client_list(json.dumps({"netdev": {}})) is None


def test_wired_and_wifi_clients_and_their_bands():
    c = by_mac(_parse_client_list(combined_body()))
    assert (c[MAC % 1].connection, c[MAC % 1].band) == ("wired", None)
    assert (c[MAC % 2].connection, c[MAC % 2].band) == ("wifi", BAND_5G)
    assert c[MAC % 3].band == BAND_2G
    assert c[MAC % 4].band == BAND_OTHER and "6" not in c[MAC % 4].band  # a second high band, never claimed as 6 GHz


def test_wifi_values_are_parsed_and_wired_ones_are_none():
    c = by_mac(_parse_client_list(combined_body()))
    phone = c[MAC % 2]
    assert (phone.rssi_dbm, phone.link_tx_mbps, phone.link_rx_mbps, phone.connected_s) == (-58, 866.0, 780.0, 27 * 3600 + 5 * 60 + 9)
    desk = c[MAC % 1]  # rssi "0" and empty rates and time of a wired client
    assert (desk.rssi_dbm, desk.link_tx_mbps, desk.link_rx_mbps, desk.connected_s) == (None, None, None, None)
    assert phone.label_parts == ("Ann's phone", "phone") and phone.vendor == "Example" and phone.ip == "192.0.2.12"
    assert c[MAC % 3].label_parts == ("plug",)


def test_an_offline_client_keeps_none_of_the_stale_values():
    old = by_mac(_parse_client_list(combined_body()))[MAC % 5]
    assert old.online is False and old.connection == "wifi" and old.band == BAND_5G
    assert (old.rssi_dbm, old.link_tx_mbps, old.link_rx_mbps, old.connected_s, old.node) == (None, None, None, None, None)


def test_no_traffic_is_read_from_a_list_whose_byte_counters_are_empty():
    """totalTx/totalRx are empty on every entry of the firmware checked, and curTx/curRx are link rates: the Client
    has no traffic field at all, so nothing can present a rate as traffic."""
    assert not {f for f in Client.__dataclass_fields__ if "byte" in f or "traffic" in f or "total" in f}
    assert all(e["totalTx"] == "" and e["totalRx"] == "" for e in CLIENT_ENTRIES.values())


def test_nodes_are_grouped_by_the_mesh_node_a_client_is_on():
    mesh = build_mesh(_parse_client_list(combined_body()), {}, {})
    assert [(n.id, n.label, n.clients, n.wired) for n in mesh.nodes] == [
        ("000001", "Node 1", 2, 1), ("000002", "Node 2", 2, 1), ("000003", "Node 3", 1, 0)]  # offline clients not counted


def test_node_labels_come_from_the_options_first_then_the_router_then_numbers():
    names = _parse_mesh_names(json.dumps(MESH_NODES))
    assert names == {"000001": "RT-EXAMPLE", "0000f2": "Study", "000002": "Study"}  # alias, else model;
    # every MAC of a node (its own and its radios') names it; the pap5g of its parent and its wired_mac do not
    reading = _parse_client_list(combined_body())
    mesh = build_mesh(reading, names, {"000003": "Garage", "000001": "Hall"})
    assert [(n.id, n.label) for n in mesh.nodes] == [("000001", "Hall"), ("000002", "Study"), ("000003", "Garage")]
    assert [n.label for n in build_mesh(reading, {}, {"000002": "Loft"}).nodes] == ["Node 1", "Loft", "Node 2"]


def test_the_main_routers_wired_neighbours_do_not_take_the_satellites_names():
    # As an AiMesh main router answers: its wired_mac lists the satellites' own MACs (wired backhaul), and the clients
    # of a satellite name that satellite's own MAC in amesh_papMac. It comes first, and must not label them all.
    nodes = {"get_cfg_clientlist": [
        {"alias": "Basement", "mac": "AA:AA:AA:00:00:01", "ap2g": "AA:AA:AA:00:00:01", "ap5g": "AA:AA:AA:00:00:05",
         "wired_mac": ["AA:AA:AA:00:00:02", "AA:AA:AA:00:00:03"]},
        {"alias": "Office", "mac": "AA:AA:AA:00:00:02", "ap5g": "AA:AA:AA:00:02:05", "wired_mac": ["AA:AA:AA:00:00:01"]},
        {"alias": "Attic", "mac": "AA:AA:AA:00:00:03", "ap5g": "AA:AA:AA:00:03:05", "wired_mac": []},
    ]}
    names = _parse_mesh_names(json.dumps(nodes))
    assert names == {"000001": "Basement", "000005": "Basement", "000002": "Office", "000205": "Office",
                     "000003": "Attic", "000305": "Attic"}
    mesh = build_mesh(_parse_client_list(combined_body()), names, {})
    assert [(n.id, n.label) for n in mesh.nodes] == [("000001", "Basement"), ("000002", "Office"), ("000003", "Attic")]


def test_a_nodes_own_mac_outranks_another_nodes_radio_field():
    nodes = {"get_cfg_clientlist": [{"alias": "Main", "mac": "AA:AA:AA:00:00:01", "ap5g": "AA:AA:AA:00:00:02"},
                                    {"alias": "Sat", "mac": "AA:AA:AA:00:00:02"}]}
    assert _parse_mesh_names(json.dumps(nodes)) == {"000001": "Main", "000002": "Sat"}


def test_a_lone_router_has_no_mesh_even_when_the_router_names_itself():
    lone = _parse_client_list(combined_body({MAC % 1: entry(mac=MAC % 1)}))
    assert build_mesh(lone, {"0000aa": "Router"}, {}) is None


def test_node_ids_take_a_mac_or_its_last_six_hex_digits():
    assert node_id("AA:AA:AA:00:0B:C1") == "000bc1" and node_id("000BC1") == "000bc1" and node_id("nope") is None


def test_mesh_node_labels_are_validated_in_the_device_config():
    cfg = DeviceConfig(id="r", driver="asuswrt", url="http://192.168.1.1",
                       options={"mesh_node_labels": {"AA:AA:AA:00:0B:C1": "Hall", "00000F": "Loft"}})
    driver = build_device(cfg).driver
    assert driver._mesh_node_labels == {"000bc1": "Hall", "00000f": "Loft"}
    with pytest.raises(ValueError, match="not a MAC"):
        DeviceConfig(id="r", driver="asuswrt", url="http://192.168.1.1", options={"mesh_node_labels": {"zz": "x"}})


# --- capabilities and features --------------------------------------------------------------------------------------

def test_only_devices_that_can_offer_the_capabilities_do():
    assert {Capability.CLIENT_INVENTORY, Capability.MESH} <= build_router().driver.offers
    assert not {Capability.CLIENT_INVENTORY, Capability.MESH} & build_modem().driver.offers


def test_the_clients_and_mesh_features_follow_the_devices():
    status = {s.feature: s for s in feature_status(DeviceRegistry([build_modem(password="pw"), build_router(password="pw")]))}
    assert status["clients"].available and status["mesh"].available and status["clients"].provided_by == ("router",)
    status = {s.feature: s for s in feature_status(DeviceRegistry([build_modem(password="pw")]))}
    assert not status["clients"].available and status["clients"].reason == "no enabled device provides client_inventory"
    status = {s.feature: s for s in feature_status(DeviceRegistry([build_router()]))}
    assert status["clients"].reason_kind == "not_configured"


# --- the driver: no new request, readings every client-list read ----------------------------------------------------

class Router:
    def __init__(self, body=None, nodes=MESH_NODES):
        self.body, self.nodes, self.hooks = body or combined_body(), nodes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login.cgi"):
            return httpx.Response(200, json={"asus_token": "tok"})
        hook = parse_qs(request.content.decode())["hook"][0]
        self.hooks.append(hook)
        if hook == "get_cfg_clientlist()":
            return httpx.Response(200, json=self.nodes) if self.nodes is not None else httpx.Response(200, text="{}")
        return httpx.Response(200, text=self.body)


def make_router(router, clock, repository=None, **config):
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    device = build_router(repository, username="u", password="p", client=client, clock=clock, wall_clock=clock, **config)
    device.driver._token = "tok"
    return device


async def test_readings_come_with_the_client_list_without_another_request_and_the_monitor_keeps_the_last():
    router, clock = Router(), Clock()
    device = make_router(router, clock)
    await device.poll(force=True)
    # one combined request for everything; the node names are the one rare extra request
    assert router.hooks == ["netdev();cpu_usage();memory_usage();get_clientlist()", "get_cfg_clientlist()"]
    monitor = device.monitor
    assert len(monitor.latest_clients.clients) == 7 and monitor.clients_at == clock.now
    assert [n.label for n in monitor.latest_mesh.nodes] == ["RT-EXAMPLE", "Study", "Node 1"]
    assert device.latest_snapshot.client_count == 5

    first_read = monitor.latest_clients
    for _ in range(5):  # polls between client-list reads: no reading, the last one stays
        clock.now += 10
        await device.poll(force=True)
    assert monitor.latest_clients is first_read and monitor.clients_at == 1000.0
    assert router.hooks[2:] == ["netdev();cpu_usage();memory_usage()"] * 5
    clock.now += 10  # a minute after the first read: read again, with the node list (its online state: every minute)
    await device.poll(force=True)
    assert router.hooks[-2:] == ["netdev();cpu_usage();memory_usage();get_clientlist()", "get_cfg_clientlist()"]
    assert router.hooks.count("get_cfg_clientlist()") == 2 and monitor.clients_at == clock.now


async def test_without_node_names_from_the_router_nodes_are_numbered_and_option_labels_win():
    device = make_router(Router(nodes=None), Clock(), options={"mesh_node_labels": {"000003": "Garage"}})
    await device.poll(force=True)
    assert [(n.id, n.label) for n in device.monitor.latest_mesh.nodes] == [
        ("000001", "Node 1"), ("000002", "Node 2"), ("000003", "Garage")]


async def test_node_names_can_be_switched_off():
    router = Router()
    device = make_router(router, Clock(), options={"mesh_node_names": False})
    await device.poll(force=True)
    assert "get_cfg_clientlist()" not in router.hooks and device.monitor.latest_mesh is not None


async def test_a_single_router_has_no_mesh_reading_and_no_complaint(caplog):
    device = make_router(Router(body=combined_body({MAC % 1: entry(mac=MAC % 1, ip="192.0.2.11")})), Clock())
    await device.poll(force=True)
    assert device.monitor.latest_clients is not None and device.monitor.latest_mesh is None
    assert "mesh" not in caplog.text.lower()


# --- storage: counts only -------------------------------------------------------------------------------------------

async def test_a_client_list_poll_stores_counts_and_per_node_counts_never_a_client(repo, tmp_path):
    clock = Clock()
    device = make_router(Router(), clock, repository=repo)
    await device.poll(force=True)
    clock.now += 10
    await device.poll(force=True)  # between client-list reads: a row without counts
    await repo.flush_samples()
    samples = await repo.get_device_samples("router", 0, 1e12)
    assert [(s.clients_online, s.clients_wired, s.clients_wifi_2g, s.clients_wifi_5g, s.clients_wifi_other)
            for s in samples] == [(5, 2, 1, 1, 1), (None,) * 5]
    assert await repo.get_mesh_counts("router", 0, 1e12, 1) == [(1000, "000001", 2.0), (1000, "000002", 2.0), (1000, "000003", 1.0)]
    # the whole database file holds no client: no name, address or MAC
    await repo.close()
    raw = open(tmp_path / "repo.db", "rb").read() + open(tmp_path / "c.db-wal", "rb").read() if (tmp_path / "c.db-wal").exists() \
        else open(tmp_path / "repo.db", "rb").read()
    for needle in (b"desk-pc", b"192.0.2.1", b"AA:AA:AA:AA", b"Ann"):
        assert needle not in raw


async def test_retention_prunes_mesh_counts_like_device_samples(repo):
    import time
    now = time.time()
    from pengumon.db.samples import MeshCount
    await repo.insert_mesh_counts([MeshCount("router", now - 9 * 86400, "000001", 3), MeshCount("router", now - 60, "000001", 4)])
    removed = await repo.run_retention(retention_days=7) if hasattr(repo, "run_retention") else await repo._prune(7, 400)
    assert removed["mesh_samples"] == 1
    assert [r[2] for r in await repo.get_mesh_counts("router", 0, now + 1, 60)] == [4.0]


def _v9_database(path, monkeypatch):
    """A database as the v9 build left it, with one router row."""
    async def build():
        monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:9])
        monkeypatch.setattr(migrations, "LATEST_VERSION", 9)
        assert await migrations.migrate(path) == 9
    return build


async def test_migration_v10_adds_the_count_columns_and_the_mesh_table_to_a_v9_database(tmp_path, monkeypatch):
    path = str(tmp_path / "v9.db")
    full = migrations.MIGRATIONS
    await _v9_database(path, monkeypatch)()
    con = sqlite3.connect(path)
    con.execute("INSERT INTO device_samples (device_id, ts, status, cpu_pct, client_count) VALUES ('router', 1000.0, 'healthy', 12.5, 9)")
    con.commit()
    assert "clients_online" not in {r[1] for r in con.execute("PRAGMA table_info(device_samples)")}
    con.close()

    monkeypatch.setattr(migrations, "MIGRATIONS", full[:10])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 10)
    assert await migrations.migrate(path) == 10
    con = sqlite3.connect(path)
    columns = {r[1] for r in con.execute("PRAGMA table_info(device_samples)")}
    assert {"clients_online", "clients_wired", "clients_wifi_2g", "clients_wifi_5g", "clients_wifi_other"} <= columns
    assert con.execute("SELECT status, cpu_pct, client_count, clients_online FROM device_samples").fetchall() == [
        ("healthy", 12.5, 9, None)]  # the old row is kept and has no counts
    assert [r[1] for r in con.execute("PRAGMA table_info(mesh_samples)")] == ["device_id", "ts", "node_id", "clients"]
    con.close()
    assert await migrations.migrate(path) == 10  # runs once


# --- the history and its bucketing ----------------------------------------------------------------------------------

async def seed_counts(repo, start, minutes, nodes=("000001", "000002")):
    from pengumon.db.samples import DeviceSample, MeshCount
    for m in range(minutes):
        ts = start + m * 60
        online = 10 + m % 5
        await repo.insert_device_sample(DeviceSample("router", ts, "healthy", clients_online=online, clients_wired=4,
                                                     clients_wifi_2g=2, clients_wifi_5g=online - 6, clients_wifi_other=0))
        await repo.insert_mesh_counts([MeshCount("router", ts, n, online // 2) for n in nodes])
    await repo.insert_device_sample(DeviceSample("router", start + 30, "healthy"))  # a poll without counts


async def test_bucketing_gives_at_most_300_points_and_means(repo):
    start = 1_700_000_100.0  # a multiple of 300 and of 60
    await seed_counts(repo, start, 24 * 60)
    assert clients_api.bucket_seconds(start, start + 3600) == 13 and clients_api.bucket_seconds(0, 30) == 1
    assert clients_api.bucket_seconds(0, 86400) == 300 and clients_api.bucket_seconds(0, 7 * 86400) == 2040
    bucket = clients_api.bucket_seconds(start, start + 86400)
    rows = await repo.get_client_counts("router", start, start + 86400, bucket)
    assert 0 < len(rows) <= 300 and rows[0][0] % bucket == 0
    assert all(r[2] == 4.0 for r in rows)  # the rows without counts are not averaged in
    five = await repo.get_client_counts("router", start, start + 86400, 300)
    assert five[0][1] == pytest.approx(sum(10 + m % 5 for m in range(5)) / 5)  # mean of five readings
    nodes = await repo.get_mesh_counts("router", start, start + 86400, 300)
    assert {n[1] for n in nodes} == {"000001", "000002"} and len(nodes) == 2 * len(five)


# --- the API ---------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _guards(monkeypatch, app_state):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", "/nonexistent/secrets.json")
    security.reset_rate_limits()
    yield
    security.reset_rate_limits()


def api():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def polled_state(app_state, repo=None, privacy="token"):
    clock = Clock()
    router = make_router(Router(), clock, repository=repo)
    modem = build_modem(password="pw", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    app_state.devices = DeviceRegistry([modem, router])
    app_state.config = AppConfig(privacy=PrivacyConfig(client_details=privacy))
    app_state.repository = repo
    await router.poll(force=True)
    return router


FULL_ONLY = ("desk-pc", "Ann's phone", "192.0.2.", "AA:AA:AA:AA")


async def test_the_clients_answer_has_the_list_the_nodes_and_a_summary(app_state):
    await polled_state(app_state, privacy="full")
    async with api() as ac:
        res = await ac.get("/api/devices/router/clients")
    body = res.json()
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert body["ts"] == 1000.0 and len(body["clients"]) == 7
    assert body["summary"] == {"online": 5, "wired": 2, "by_band": {BAND_2G: 1, BAND_5G: 1, BAND_OTHER: 1},
                               "by_node": {"000001": 2, "000002": 2, "000003": 1}}
    assert [(n["id"], n["label"], n["clients"]) for n in body["nodes"]] == [
        ("000001", "RT-EXAMPLE", 2), ("000002", "Study", 2), ("000003", "Node 1", 1)]
    phone = next(c for c in body["clients"] if c["label"] == "Ann's phone")
    assert phone["ip"] == "192.0.2.12" and phone["mac"] == MAC % 2 and phone["rssi_dbm"] == -58
    assert (phone["link_tx_mbps"], phone["link_rx_mbps"], phone["connected_s"]) == (866.0, 780.0, 97509)
    assert not {k for k in phone if "traffic" in k or "bytes" in k}
    assert next(c for c in body["clients"] if c["online"] is False and c["connection"] == "wired")["label"] == "printer"
    assert body["privacy"] == {"configured": "full", "applied": "full", "hint": None}


@pytest.mark.parametrize("setting, token, applied", [
    ("token", False, "masked"), ("token", True, "full"), ("full", False, "full"), ("full", True, "full"),
    ("masked", False, "masked"), ("masked", True, "masked")])
async def test_privacy_modes(app_state, monkeypatch, setting, token, applied):
    await polled_state(app_state, privacy=setting)
    monkeypatch.setattr(security_config, "access_token", TOKEN if token else "")
    headers = {"Authorization": f"Bearer {TOKEN}"} if token else {}
    async with api() as ac:
        res = await ac.get("/api/devices/router/clients", headers=headers)
    body, text = res.json(), res.text
    assert body["privacy"]["applied"] == applied and body["privacy"]["configured"] == setting
    if applied == "full":
        assert body["privacy"]["hint"] is None
        assert all(needle in text for needle in FULL_ONLY)
        assert {c["label"] for c in body["clients"]} >= {"desk-pc", "Ann's phone"} or "desk-pc" in text
    else:
        hint = body["privacy"]["hint"]
        assert "Settings > Privacy" in hint and "config.yaml" not in hint  # Settings is where it is changed
        assert ("Settings > Sign-in" in hint) == (setting == "token")  # a password does not help under "masked"
        assert not any(needle in text for needle in FULL_ONLY)
        assert all("ip" not in c and "mac" not in c for c in body["clients"])
        labels = {c["label"] for c in body["clients"]}
        assert "Example AA02" in labels and "Example AAA2" not in labels  # vendor and the last 4 hex digits
        assert "Device AA03" in labels  # no vendor: "Device"
        assert body["summary"]["online"] == 5  # counts are never hidden


@pytest.mark.parametrize("setting, applied", [("token", "masked"), ("masked", "masked"), ("full", "full")])
async def test_a_viewer_without_the_token_gets_masked_clients(app_state, monkeypatch, setting, applied):
    """PENGUMON_VIEW_WITHOUT_TOKEN: a token is set, but this request does not carry it. "token" means "only to whoever
    has the token", so the viewer is shown the masked list (and told to sign in); "full" is the owner's own choice."""
    await polled_state(app_state, privacy=setting)
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "view_without_token", True)
    async with api() as ac:
        res = await ac.get("/api/devices/router/clients")
        signed_in = await ac.get("/api/devices/router/clients", headers={"Authorization": f"Bearer {TOKEN}"})
    body, text = res.json(), res.text
    assert res.status_code == 200 and body["privacy"]["applied"] == applied
    if applied == "masked":
        assert not any(needle in text for needle in FULL_ONLY)
        assert all("ip" not in c and "mac" not in c for c in body["clients"])
        assert "Sign in" in body["privacy"]["hint"] if setting == "token" else "Settings > Privacy" in body["privacy"]["hint"]
    assert signed_in.json()["privacy"]["applied"] == ("masked" if setting == "masked" else "full")


async def test_masked_rows_carry_no_mac_in_their_keys(app_state):
    await polled_state(app_state, privacy="masked")
    async with api() as ac:
        keys = [c["key"] for c in (await ac.get("/api/devices/router/clients")).json()["clients"]]
    assert len(set(keys)) == 7 and not any("AA" in k.upper() and ":" in k for k in keys)


async def test_unknown_device_404_and_a_device_without_clients_400(app_state):
    await polled_state(app_state)
    async with api() as ac:
        for suffix in ("clients", "clients/history"):
            assert (await ac.get(f"/api/devices/nope/{suffix}")).status_code == 404
            assert (await ac.get(f"/api/devices/modem/{suffix}")).status_code == 400


async def test_503_before_startup(app_state):
    async with api() as ac:
        assert (await ac.get("/api/devices/router/clients")).status_code == 503


async def test_the_list_is_empty_until_the_client_list_has_been_read(app_state):
    router = make_router(Router(), Clock())
    app_state.devices = DeviceRegistry([router])
    router.monitor.latest_snapshot = object.__new__(type("S", (), {}))  # polled, but no client list yet
    async with api() as ac:
        body = (await ac.get("/api/devices/router/clients")).json()
    assert body["ts"] is None and body["clients"] == [] and body["summary"]["online"] == 0


async def test_the_clients_route_never_polls_a_device_that_has_not_been_read_yet(app_state):
    router_double = Router()
    router = make_router(router_double, Clock())
    app_state.devices = DeviceRegistry([router])
    async with api() as ac:
        body = (await ac.get("/api/devices/router/clients")).json()
    assert body["ts"] is None and body["clients"] == [] and router_double.hooks == []


async def test_no_client_row_reaches_the_logs_metrics_or_the_stored_snapshot(app_state, caplog):
    caplog.set_level("DEBUG")
    router = await polled_state(app_state, privacy="full")
    async with api() as ac:
        await ac.get("/api/devices/router/clients")
        metrics = (await ac.get("/metrics")).text
        status = (await ac.get("/api/devices/router")).text
        devices = (await ac.get("/api/devices")).text
    for needle in ("desk-pc", "Ann", "192.0.2.1", "AA:AA:AA:AA", "Example"):
        assert needle not in caplog.text and needle not in metrics and needle not in status and needle not in devices
    assert "clients" not in router.latest_snapshot.model_dump_json().lower().replace("client_count", "")


async def test_history_endpoint_serves_bucketed_counts_and_nodes(app_state, repo):
    await polled_state(app_state, repo=repo)
    import time
    start = float(int(time.time())) - 86400 + 3600
    await seed_counts(repo, start, 600)
    async with api() as ac:
        res = await ac.get(f"/api/devices/router/clients/history?start_time={start}&end_time={start + 36000}")
        day = await ac.get("/api/devices/router/clients/history")
        bad = await ac.get("/api/devices/router/clients/history?start_time=10&end_time=5")
    body = res.json()
    assert res.status_code == 200 and body["bucket_seconds"] == clients_api.bucket_seconds(start, start + 36000) == 180
    assert 0 < len(body["points"]) <= 300
    p = body["points"][0]
    assert set(p) == {"ts", "online", "wired", "wifi", "by_band", "by_node"} and p["wired"] == 4.0
    assert set(p["by_band"]) == {BAND_2G, BAND_5G, BAND_OTHER} and set(p["by_node"]) == {"000001", "000002"}
    assert [n["id"] for n in body["nodes"]] == ["000001", "000002"] and body["nodes"][1]["label"] == "Study"
    assert day.status_code == 200 and day.json()["bucket_seconds"] == 300
    assert bad.status_code == 400


# --- the guards on the new routes ------------------------------------------------------------------------------------

PATHS = ("/api/devices/router/clients", "/api/devices/router/clients/history")


@pytest.mark.parametrize("path", PATHS)
async def test_the_token_applies_to_the_client_routes(app_state, monkeypatch, path, repo):
    await polled_state(app_state, repo=repo)
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with api() as ac:
        assert (await ac.get(path)).status_code == 401
        assert (await ac.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await ac.get(path, headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 200


@pytest.mark.parametrize("path", PATHS)
async def test_the_host_check_applies_to_the_client_routes(app_state, path):
    await polled_state(app_state)
    async with api() as ac:
        assert (await ac.get(path, headers={"Host": "evil.example"})).status_code == 421


async def test_the_client_routes_are_rate_limited_and_share_one_bucket(app_state, repo):
    await polled_state(app_state, repo=repo)
    async with api() as ac:
        codes = [(await ac.get(PATHS[i % 2])).status_code for i in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


async def test_the_client_routes_only_read(app_state):
    await polled_state(app_state)
    async with api() as ac:
        for path in PATHS:
            assert (await ac.post(path, json={})).status_code in (403, 405)


def test_a_signal_is_graded_by_the_server():
    from pengumon.grades import signal_grade
    assert signal_grade(None) is None
    assert signal_grade(-58) == {"word": "Excellent", "bars": 4, "grade": "good"}
    assert signal_grade(-60)["word"] == "Good" and signal_grade(-66)["word"] == "Good"
    assert signal_grade(-70) == {"word": "Fair", "bars": 2, "grade": "warn"}
    assert signal_grade(-75) == {"word": "Weak", "bars": 1, "grade": "critical"}


# --- mesh node status, backhaul and client types ---------------------------------------------------------------------

def _node(alias, mac, **fields):
    return {"alias": alias, "model_name": "RT-EXAMPLE", "mac": mac, "online": "1", "wired_mac": [], "pap2g": "",
            "pap5g": "", "pap5g1": "", "pap6g": "", "rssi2g": "", "rssi5g": "", "rssi5g1": "", "rssi6g": "", **fields}


# As an AiMesh main router answers: the main router lists the satellite it reaches by cable in wired_mac, the satellite
# on Wi-Fi names its parent's radio and the signal to it, and a satellite that dropped off says online "0".
STATUS_NODES = {"get_cfg_clientlist": [
    _node("Hall", "AA:AA:AA:00:00:01", wired_mac=["AA:AA:AA:00:00:02"]),
    _node("Study", "AA:AA:AA:00:00:02", ap5g="AA:AA:AA:00:02:05"),
    _node("Loft", "AA:AA:AA:00:00:03", ap5g="AA:AA:AA:00:03:05", pap5g="AA:AA:AA:00:01:05", rssi5g="-54"),
    _node("Garage", "AA:AA:AA:00:00:04", online="0"),
]}


def test_node_status_and_backhaul_come_from_the_nodes_list():
    infos, by_id = _parse_mesh_nodes(json.dumps(STATUS_NODES))
    assert [(i.label, i.online, i.backhaul, i.band, i.rssi_dbm) for i in infos] == [
        ("Hall", True, None, None, None),            # the main router: no backhaul
        ("Study", True, "wired", None, None),        # listed in the main router's wired_mac, no parent radio
        ("Loft", True, "wifi", "5 GHz", -54),
        ("Garage", False, None, None, None)]         # offline: nothing is known of its link
    assert by_id["000205"].label == "Study"  # a radio's MAC names its node as well


def test_an_offline_node_is_listed_without_clients_and_online_nodes_carry_their_backhaul():
    infos, by_id = _parse_mesh_nodes(json.dumps(STATUS_NODES))
    mesh = build_mesh(_parse_client_list(combined_body()), {}, {}, infos, by_id)
    by_label = {n.label: n for n in mesh.nodes}
    assert set(by_label) == {"Hall", "Study", "Loft", "Garage"}  # Garage carries no client but is listed
    assert (by_label["Garage"].online, by_label["Garage"].clients) == (False, 0)
    assert (by_label["Study"].online, by_label["Study"].backhaul, by_label["Study"].clients) == (True, "wired", 2)
    assert (by_label["Loft"].backhaul, by_label["Loft"].backhaul_band, by_label["Loft"].backhaul_rssi_dbm) == ("wifi", "5 GHz", -54)
    assert build_mesh(_parse_client_list(combined_body()), {}, {}).nodes[0].online is None  # without the list: unknown


def test_a_lone_router_with_a_node_list_still_has_no_mesh():
    infos, by_id = _parse_mesh_nodes(json.dumps({"get_cfg_clientlist": [_node("Main", "AA:AA:AA:00:00:01")]}))
    lone = _parse_client_list(combined_body({MAC % 1: entry(mac=MAC % 1)}))
    assert build_mesh(lone, {}, {}, infos, by_id) is None


async def test_the_router_card_warns_when_a_node_is_offline_and_the_api_says_why():
    router, clock = Router(nodes=STATUS_NODES), Clock()
    device = make_router(router, clock)
    await device.poll(force=True)
    assert device.latest_snapshot.grades["mesh"] == "warn" and device.latest_snapshot.status == "warning"
    nodes = {n["label"]: n for n in mesh_nodes(device.monitor.latest_mesh)}
    assert (nodes["Garage"]["online"], nodes["Loft"]["backhaul"], nodes["Loft"]["backhaul_rssi_dbm"]) == (False, "wifi", -54)
    clock.now += 10
    await device.poll(force=True)  # between client-list reads the last mesh reading stands
    assert device.latest_snapshot.grades["mesh"] == "warn"
    healthy = {"get_cfg_clientlist": [dict(n, online="1") for n in STATUS_NODES["get_cfg_clientlist"]]}
    ok_router = make_router(Router(nodes=healthy), Clock())
    await ok_router.poll(force=True)
    assert ok_router.latest_snapshot.grades["mesh"] == "good" and ok_router.latest_snapshot.status == "healthy"


async def test_without_a_node_list_there_is_no_mesh_grade():
    device = make_router(Router(nodes=None), Clock())
    await device.poll(force=True)
    assert "mesh" not in device.latest_snapshot.grades


def test_client_types_become_plain_words_and_unknown_ones_stay_empty():
    kinds = {"9": "phone", "10": "phone", "6": "computer", "34": "computer", "7": "game", "24": "network",
             "0": None, "18": None, "99": None, "": None}
    entries = {MAC % (i + 1): entry(mac=MAC % (i + 1), type=t) for i, t in enumerate(kinds)}
    got = [c.kind for c in _parse_client_list(combined_body(entries)).clients]
    assert got == list(kinds.values())
    printer = _parse_client_list(combined_body({MAC % 1: entry(mac=MAC % 1, type="0", isPrinter="1")})).clients[0]
    assert printer.kind == "printer"


async def test_the_clients_api_sends_the_kind_even_when_names_are_masked():
    entries = {MAC % 1: entry(mac=MAC % 1, type="9", name="x")}
    router, clock = Router(body=combined_body(entries)), Clock()
    device = make_router(router, clock)
    await device.poll(force=True)
    assert clients_api._row(device.monitor.latest_clients.clients[0], False)["kind"] == "phone"
