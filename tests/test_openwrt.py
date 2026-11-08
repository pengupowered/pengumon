"""The OpenWrt driver against a fake ubus JSON-RPC endpoint whose answers have the shapes OpenWrt 24.10 gives
(recorded from a real rpcd + uhttpd; every value here is made up)."""

import json
import logging

import httpx
import pytest

from pengumon.capabilities import BAND_2G, BAND_5G, BAND_6G, Capability, DriverError
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.drivers import DRIVERS
from pengumon.drivers.common import proc_stat_cpu
from pengumon.drivers.openwrt import (
    ARP_TABLE, NO_SESSION, PROC_STAT, READ_METHODS, OpenWrtDriver, arp_online, band_of, memory_pct, wan_device,
    wifi_interfaces,
)
from pengumon.secrets import CredentialSources, password_env_var

from device_helpers import Clock

MAC_A, MAC_B, MAC_C = "02:00:00:00:00:0a", "02:00:00:00:00:0b", "02:00:00:00:00:0c"

DUMP = {"interface": [
    {"interface": "lan", "up": True, "l3_device": "br-lan", "device": "br-lan"},
    {"interface": "wan", "up": True, "l3_device": "pppoe-wan", "device": "eth1", "proto": "pppoe"},
    {"interface": "guest", "up": False, "device": "br-guest"},
]}
DEVICES = {
    "br-lan": {"name": "br-lan", "stats": {"rx_bytes": 1, "tx_bytes": 2}},
    "pppoe-wan": {"name": "pppoe-wan", "up": True, "stats": {"rx_bytes": 100_000_000, "tx_bytes": 50_000_000,
                                                            "rx_packets": 9, "tx_packets": 9}},
}
INFO = {"localtime": 1700000000, "uptime": 5000, "load": [1, 2, 3],
        "memory": {"total": 1000, "free": 100, "shared": 10, "buffered": 50, "available": 400, "cached": 200},
        "swap": {"total": 0, "free": 0}}
BOARD = {"kernel": "6.6.0", "hostname": "router", "system": "ARMv8 Processor rev 4", "model": "Example Router X1",
         "board_name": "example,x1", "release": {"distribution": "OpenWrt", "version": "24.10.0"}}
LEASES = {"dhcp_leases": [
    {"expires": 3000, "hostname": "laptop", "ipaddr": "192.0.2.10", "macaddr": MAC_A},
    {"expires": 3000, "hostname": "printer", "ipaddr": "192.0.2.11", "macaddr": MAC_B},
    {"expires": 3000, "hostname": "*", "ipaddr": "192.0.2.12", "macaddr": MAC_C},
], "dhcp6_leases": []}
WIRELESS = {
    "radio0": {"up": True, "config": {"band": "2g", "channel": "1"}, "interfaces": [
        {"section": "default_radio0", "ifname": "phy0-ap0", "config": {"mode": "ap"}, "iwinfo": {"frequency": 2412}}]},
    "radio1": {"up": True, "config": {"band": "5g"}, "interfaces": [
        {"section": "default_radio1", "ifname": "phy1-ap0", "config": {"mode": "ap"}, "iwinfo": {}}]},
}
ASSOC = {"phy0-ap0": {"results": []}, "phy1-ap0": {"results": [
    {"mac": MAC_A.upper(), "signal": -52, "noise": -95, "inactive": 10, "connected_time": 3600,
     "rx": {"rate": 866700, "mhz": 80}, "tx": {"rate": 650000, "mhz": 80}},
]}}
ARP = ("IP address       HW type     Flags       HW address            Mask     Device\n"
       f"192.0.2.11       0x1         0x2         {MAC_B}     *        br-lan\n"
       f"192.0.2.12       0x1         0x0         {MAC_C}     *        br-lan\n")


def proc_stat(n):
    """The n-th /proc/stat: 1000 jiffies more per read, a quarter of them busy."""
    return f"cpu  {150 * n} 0 {100 * n} {700 * n} {50 * n} 0 0 0 0 0\ncpu0 1 0 1 1 0 0 0 0 0 0\nintr 1 2 3\n"


class FakeUbus:
    """uhttpd's /ubus: JSON-RPC 2.0, single requests and batches, sessions, and an ACL (`denied`, as (object,
    method) or "file <path>")."""

    def __init__(self, password="pw", denied=()):
        self.password = password
        self.denied = set(denied)
        self.sessions = set()
        self.logins = 0
        self.posts = []  # the (object, method) calls of every request after the login, one list per request
        self.stat_reads = 0
        self.missing = set()  # (object, method) the router does not have (no procd, no LuCI)

    def answer(self, request):
        sid, obj, method, args = request["params"]
        rid = request["id"]
        if (obj, method) == ("session", "login"):
            self.logins += 1
            if args.get("password") != self.password:
                return {"jsonrpc": "2.0", "id": rid, "result": [6]}
            session = f"{self.logins:032x}"
            self.sessions.add(session)
            return {"jsonrpc": "2.0", "id": rid, "result": [0, {"ubus_rpc_session": session, "timeout": 300}]}
        if sid not in self.sessions or (obj, method) in self.denied:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "Access denied"}}
        if (obj, method) in self.missing:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Object not found"}}
        if obj == "file":
            if f"file {args['path']}" in self.denied:
                return {"jsonrpc": "2.0", "id": rid, "result": [6]}
            if args["path"] == PROC_STAT:
                self.stat_reads += 1
                return {"jsonrpc": "2.0", "id": rid, "result": [0, {"data": proc_stat(self.stat_reads)}]}
            return {"jsonrpc": "2.0", "id": rid, "result": [0, {"data": ARP}]}
        data = {("network.interface", "dump"): DUMP, ("luci-rpc", "getNetworkDevices"): DEVICES,
                ("system", "info"): INFO, ("system", "board"): BOARD, ("luci-rpc", "getDHCPLeases"): LEASES,
                ("luci-rpc", "getWirelessDevices"): WIRELESS}.get((obj, method))
        if (obj, method) == ("iwinfo", "assoclist"):
            data = ASSOC.get(args["device"])
            if data is None:
                return {"jsonrpc": "2.0", "id": rid, "result": [4]}
        return {"jsonrpc": "2.0", "id": rid, "result": [0, data]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ubus" and request.method == "POST"
        body = json.loads(request.content)
        if isinstance(body, list):
            self.posts.append([tuple(r["params"][1:3]) for r in body])
            return httpx.Response(200, json=[self.answer(r) for r in body])
        if body["params"][1:3] != ["session", "login"]:
            self.posts.append([tuple(body["params"][1:3])])
        return httpx.Response(200, json=self.answer(body))


def build(ubus, clock=None, password="pw", **options):
    cfg = DeviceConfig(id="owrt", driver="openwrt", url="http://192.0.2.1", options=options)
    client = httpx.AsyncClient(transport=httpx.MockTransport(ubus))
    secrets = CredentialSources(env={password_env_var("owrt"): password}, secrets_dir=None, store={})
    return build_device(cfg, None, secrets, client=client, clock=clock or Clock(), wall_clock=lambda: 1_700_000_000.0)


def test_registered_with_root_as_its_default_user():
    assert DRIVERS["openwrt"] is OpenWrtDriver
    cfg = DeviceConfig(id="r", driver="openwrt", url="https://192.0.2.1")
    assert cfg.username == "root" and cfg.poll_interval_seconds == 10.0
    assert DeviceConfig(id="r", driver="openwrt", url="http://192.0.2.1", username="monitor").username == "monitor"
    assert DeviceConfig(id="r", driver="asuswrt", url="http://192.0.2.1").username == "admin"  # unchanged


@pytest.mark.parametrize("options, message", [
    ({"wan_interface": "wan; rm"}, "wan_interface"),
    ({"clientlist_interval_seconds": 0}, "greater than 0"),
    ({"other": 1}, "Extra inputs are not permitted"),
])
def test_options_are_checked(options, message):
    with pytest.raises(ValueError, match=message):
        DeviceConfig(id="r", driver="openwrt", url="http://192.0.2.1", options=options)


async def test_one_poll_reads_every_capability_in_two_requests():
    ubus, clock = FakeUbus(), Clock()
    device = build(ubus, clock)
    result = await device.driver.poll()
    assert ubus.logins == 1
    assert len(ubus.posts) == 2  # the batch, then the Wi-Fi stations of both interfaces
    assert ("iwinfo", "assoclist") in ubus.posts[1] and len(ubus.posts[1]) == 2

    wan = result.readings[Capability.WAN_COUNTERS]
    assert (wan.rx_bytes, wan.tx_bytes, wan.counter_bits) == (100_000_000, 50_000_000, None)  # of pppoe-wan
    assert wan.read_at == 1_700_000_000.0
    system = result.readings[Capability.ROUTER_SYSTEM]
    assert system.mem_pct == 60.0 and system.cpu_pct is None  # CPU % needs two readings
    assert system.client_count == 2  # the Wi-Fi station and the wired client with a complete ARP entry
    assert system.model == "Example Router X1"
    assert result.missing == {}

    clients = {c.mac: c for c in result.readings[Capability.CLIENT_INVENTORY].clients}
    laptop = clients[MAC_A.upper()]
    assert laptop.connection == "wifi" and laptop.band == BAND_5G and laptop.online  # band from the radio's config
    assert laptop.label_parts == ("laptop",) and laptop.ip == "192.0.2.10"
    assert (laptop.rssi_dbm, laptop.link_rx_mbps, laptop.link_tx_mbps, laptop.connected_s) == (-52, 866.7, 650.0, 3600)
    printer, unnamed = clients[MAC_B.upper()], clients[MAC_C.upper()]
    assert printer.connection == "wired" and printer.online and printer.rssi_dbm is None
    assert unnamed.label_parts == () and not unnamed.online  # incomplete ARP entry


async def test_cpu_from_proc_stat_and_clients_only_every_interval():
    ubus, clock = FakeUbus(), Clock()
    device = build(ubus, clock)
    await device.driver.poll()
    clock.now += 10
    result = await device.driver.poll()
    assert result.readings[Capability.ROUTER_SYSTEM].cpu_pct == 25.0
    assert result.readings[Capability.ROUTER_SYSTEM].client_count == 2  # kept from the last client read
    assert Capability.CLIENT_INVENTORY not in result.readings and result.missing == {}
    assert len(ubus.posts) == 3  # one request: no client calls, no board
    assert ubus.posts[2] == [("network.interface", "dump"), ("luci-rpc", "getNetworkDevices"), ("system", "info"),
                             ("file", "read")]
    clock.now += 50
    await device.driver.poll()
    assert ("luci-rpc", "getDHCPLeases") in ubus.posts[3] and ("system", "board") not in ubus.posts[3]


async def test_an_expired_session_costs_one_login_and_the_poll_still_succeeds():
    ubus, clock = FakeUbus(), Clock()
    device = build(ubus, clock)
    await device.driver.poll()
    ubus.sessions.clear()  # rpcd restarted, or the session timed out
    clock.now += 10
    result = await device.driver.poll()
    assert ubus.logins == 2 and Capability.WAN_COUNTERS in result.readings and result.missing == {}


async def test_a_wrong_password_is_an_auth_error():
    device = build(FakeUbus(), password="wrong")
    with pytest.raises(DriverError) as err:
        await device.driver.poll()
    assert err.value.kind == "auth" and "'root'" in err.value.message
    snapshot = await device.poll(force=True)
    assert snapshot.status == "error" and snapshot.error_kind == "auth"


async def test_new_credentials_drop_the_session():
    ubus = FakeUbus()
    device = build(ubus)
    await device.driver.poll()
    device.set_credentials("monitor", "pw")
    await device.driver.poll()
    assert ubus.logins == 2


async def test_calls_the_acl_refuses_leave_out_only_their_readings(caplog):
    ubus = FakeUbus(denied={f"file {PROC_STAT}", ("luci-rpc", "getWirelessDevices"), f"file {ARP_TABLE}"})
    clock = Clock()
    device = build(ubus, clock)
    with caplog.at_level(logging.WARNING, logger="pengumon.drivers.openwrt"):
        await device.driver.poll()
        clock.now += 10
        result = await device.driver.poll()
        clock.now += 60  # the client calls again, one of them refused with the error an unknown session gets
        await device.driver.poll()
    assert ubus.logins == 1  # a refusal right after a login is the ACL, not the session: no login on every poll
    system = result.readings[Capability.ROUTER_SYSTEM]
    assert system.cpu_pct is None and system.mem_pct == 60.0
    assert system.client_count == 3  # without the ARP table every leased client counts as online
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([w for w in warnings if PROC_STAT in w]) == 1  # once, not once per poll


async def test_a_session_refused_everything_is_an_auth_error_naming_the_acl():
    ubus = FakeUbus(denied=READ_METHODS)
    with pytest.raises(DriverError) as err:
        await build(ubus).driver.poll()
    assert err.value.kind == "auth" and "ACL" in err.value.message
    assert ubus.logins == 1


async def test_a_router_without_procd_or_luci_reports_what_it_can():
    ubus = FakeUbus()
    ubus.missing = {("system", "info"), ("system", "board"), ("luci-rpc", "getNetworkDevices"),
                    ("luci-rpc", "getDHCPLeases"), ("luci-rpc", "getWirelessDevices")}
    result = await build(ubus).driver.poll()
    assert "getNetworkDevices" in result.missing[Capability.WAN_COUNTERS]
    assert "getDHCPLeases" in result.missing[Capability.CLIENT_INVENTORY]
    system = result.readings[Capability.ROUTER_SYSTEM]  # /proc/stat was readable: the CPU baseline is set
    assert system.mem_pct is None and system.client_count is None and system.model is None


@pytest.mark.parametrize("interface, why", [
    ("wan6", "no interface 'wan6'"), ("guest", "'guest' is down"),
])
async def test_a_missing_or_down_wan_interface_is_explained(interface, why):
    result = await build(FakeUbus(), wan_interface=interface).driver.poll()
    assert why in result.missing[Capability.WAN_COUNTERS]
    assert Capability.ROUTER_SYSTEM in result.readings


async def test_no_ubus_endpoint_and_non_json_answers_are_protocol_errors():
    for response in (httpx.Response(404, text="Not Found"), httpx.Response(200, text="<html>")):
        device = build(lambda request, r=response: r)
        with pytest.raises(DriverError) as err:
            await device.driver.poll()
        assert err.value.kind == "protocol"
    device = build(lambda request: httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": [0, {}]}))
    with pytest.raises(DriverError, match="no session id"):
        await device.driver.poll()


async def test_a_transport_error_becomes_an_unreachable_snapshot():
    def down(request):
        raise httpx.ConnectError("connection refused")
    snapshot = await build(down).poll(force=True)
    assert snapshot.status == "error" and snapshot.error_kind == "unreachable"


def test_only_methods_that_report_state_are_called():
    assert all(method in {"dump", "getNetworkDevices", "info", "board", "read", "getDHCPLeases",
                          "getWirelessDevices", "assoclist"} for _, method in READ_METHODS)
    driver = OpenWrtDriver("http://192.0.2.1", "root", "pw")
    with pytest.raises(DriverError) as err:
        driver._request(NO_SESSION, ("system", "reboot", {}))
    assert err.value.kind == "internal"


def test_parsers():
    assert wan_device(DUMP, "wan") == ("pppoe-wan", None)
    assert wan_device({"interface": "x"}, "wan")[0] is None
    assert memory_pct(INFO) == 60.0
    assert memory_pct({"memory": {"total": 1000, "free": 100, "buffered": 100, "cached": 200}}) == 60.0  # no available
    assert memory_pct({"memory": {"total": 0}}) is None and memory_pct({"memory": "x"}) is None
    assert proc_stat_cpu(proc_stat(2)) == {"cpu": (2000, 500)}
    assert proc_stat_cpu("intr 1 2\n") == {} and proc_stat_cpu("") == {}
    assert arp_online(ARP) == {MAC_B.upper(): True, MAC_C.upper(): False}
    assert wifi_interfaces(WIRELESS) == {"phy0-ap0": BAND_2G, "phy1-ap0": BAND_5G}
    assert [band_of(f) for f in (2437, 5180, 5955, None)] == [BAND_2G, BAND_5G, BAND_6G, None]


# --- the speed test's light counter read ------------------------------------------------------------------------------

async def test_the_speed_test_reads_the_wan_counters_alone_in_one_request_on_the_polls_session():
    ubus = FakeUbus()
    device = build(ubus)
    await device.driver.poll()
    logins, posts = ubus.logins, len(ubus.posts)
    counters = await device.driver.read_wan_counters()
    assert (counters.rx_bytes, counters.tx_bytes, counters.read_at) == (100_000_000, 50_000_000, 1_700_000_000.0)
    assert ubus.logins == logins and len(ubus.posts) == posts + 1      # the poll's session, one request
    assert ubus.posts[-1] == [("network.interface", "dump"), ("luci-rpc", "getNetworkDevices")]


async def test_a_counter_read_the_acl_refuses_is_none():
    device = build(FakeUbus(denied={("luci-rpc", "getNetworkDevices")}))
    assert await device.driver.read_wan_counters() is None
