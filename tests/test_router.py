"""Unit tests for the AsusWRT router driver, run through a Device built by build_router."""

import base64
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest
from pengumon.capabilities import Capability, DriverError, SystemReading, WanCounters
from pengumon.devices.wan import WanRates
from pengumon.devices.registry import unconfigured_messages
from pengumon.drivers.asuswrt import CPU_MIN_INTERVAL_SECONDS, AsusWrtDriver, _cpu_pct, _parse_clients, _parse_cpu, _parse_mem, _parse_netdev
from pengumon.devices.snapshots import RouterMetricsSnapshot

from device_helpers import build_router


def test_asus_collector_init_defaults():
    device = build_router()
    assert device.driver.host == "192.168.1.1"
    assert device.driver._username == "admin"
    assert device.driver._password is None
    assert device.driver.missing_configuration() is not None


async def test_asus_collector_unconfigured_snapshot():
    device = build_router()
    snap = await device.poll(force=True)
    assert isinstance(snap, RouterMetricsSnapshot)
    assert snap.status == "unconfigured"
    assert snap.error_message == unconfigured_messages("router").missing_credentials
    assert snap.wan_rx_bytes is None
    assert snap.wan_tx_bytes is None


def test_asus_collector_read_only_guarantee():
    """Verify that AsusWrtDriver only interacts with read-only endpoints and has no mutating methods."""
    driver = build_router(username="test", password="password").driver
    assert isinstance(driver, AsusWrtDriver)

    # Check that the public members of the driver are strictly read-only
    member_names = [m for m in dir(driver) if not m.startswith("_")]

    # Must only have safe telemetry collection methods
    for name in member_names:
        assert name in ("poll", "aclose", "set_credentials", "missing_configuration", "host", "name", "offers",
                        "http", "from_config", "options_model", "default_poll_seconds"), f"Unexpected member: {name}"

    # Verify no references to apply.cgi or nvram set in code
    import inspect
    src = inspect.getsource(AsusWrtDriver)
    assert "apply.cgi" not in src
    assert "nvram_set" not in src
    assert "syscmd" not in src


def test_rate_calculation():
    wan = WanRates()
    assert wan.update(1000.0, WanCounters(100_000_000, 50_000_000)) == (None, None)  # baseline

    # 10 seconds later, 12,500,000 bytes Rx (10.0 Mbps), 6,250,000 bytes Tx (5.0 Mbps)
    rx_mbps, tx_mbps = wan.update(1010.0, WanCounters(112_500_000, 56_250_000))
    assert rx_mbps is not None
    assert tx_mbps is not None
    assert round(rx_mbps, 1) == 10.0
    assert round(tx_mbps, 1) == 5.0


# --- appGet requests: one per poll ---------------------------------------------------------------

NETDEV = {"netdev": {"INTERNET_rx": "0x5f5e100", "INTERNET_tx": "0x2faf080"}}  # 100 MB, 50 MB
CPU = {"cpu_usage": {"cpu1_total": "1000", "cpu1_usage": "250", "cpu2_total": "1000", "cpu2_usage": "750"}}


def cpu_answer(n):
    """The n-th cpu_usage() answer: cumulative counters that grow by 1000 jiffies per read, a quarter busy on core
    1 and three quarters on core 2, so every reading after the first gives 50 %."""
    return {"cpu_usage": {"cpu1_total": str(1000 * n), "cpu1_usage": str(250 * n),
                          "cpu2_total": str(1000 * n), "cpu2_usage": str(750 * n)}}
MEM = {"memory_usage": {"mem_total": "1000", "mem_free": "400", "mem_used": "600"}}
CLIENTS = {"get_clientlist": {
    "AA:AA:AA:AA:AA:01": {"isOnline": "1"}, "AA:AA:AA:AA:AA:02": {"isOnline": "1"},
    "AA:AA:AA:AA:AA:03": {"isOnline": "0"}, "maclist": ["AA:AA:AA:AA:AA:01"],
}}
ANSWERS = {"netdev()": NETDEV, "cpu_usage()": CPU, "memory_usage()": MEM, "get_clientlist()": CLIENTS}


class FakeRouter:
    """Mock AsusWRT httpd. `combined=False` models firmware that answers only single-hook requests."""

    def __init__(self, combined=True, valid_token="tok"):
        self.combined = combined
        self.valid_token = valid_token
        self.hook_requests = []  # the hook string of every appGet.cgi request
        self.logins = 0
        self.other = []  # paths of anything else (update.cgi, ...)
        self.cpu_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("login.cgi"):
            self.logins += 1
            self.valid_token = "fresh"
            return httpx.Response(200, json={"asus_token": "fresh"})
        if not path.endswith("appGet.cgi"):
            self.other.append(path)
            return httpx.Response(404)
        hook = parse_qs(request.content.decode())["hook"][0]
        self.hook_requests.append(hook)
        if request.headers.get("asus_token") != self.valid_token:
            return httpx.Response(200, json={"error_status": "2"})
        hooks = hook.split(";")
        if len(hooks) > 1 and not self.combined:
            return httpx.Response(200, json={})
        body = {}
        for h in hooks:
            if h == "cpu_usage()":
                self.cpu_reads += 1
                body.update(cpu_answer(self.cpu_reads))
            else:
                body.update(ANSWERS[h])
        return httpx.Response(200, json=body)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _collector(router, clock=None, **config):
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    device = build_router(username="u", password="p", **config, client=client, clock=clock or Clock())
    device.driver._token = "tok"
    return device


async def test_one_combined_request_per_poll_and_clients_only_every_60_seconds():
    router, clock = FakeRouter(), Clock()
    collector = _collector(router, clock)

    first = await collector.poll(force=True)
    assert router.hook_requests == ["netdev();cpu_usage();memory_usage();get_clientlist()"]
    assert (first.wan_rx_bytes, first.wan_tx_bytes) == (100_000_000, 50_000_000)
    assert first.cpu_usage_pct is None and first.mem_usage_pct == 60.0  # CPU % needs a second reading
    assert first.client_count == 2

    for _ in range(5):  # 10 s, 20 s, ... 50 s later: no client list, last count reused
        clock.now += 10
        snap = await collector.poll(force=True)
        assert snap.client_count == 2 and snap.cpu_usage_pct == 50.0
    assert router.hook_requests[1:] == ["netdev();cpu_usage();memory_usage()"] * 5

    clock.now += 10  # 60 s after the last read
    await collector.poll(force=True)
    assert router.hook_requests[-1] == "netdev();cpu_usage();memory_usage();get_clientlist()"
    assert len(router.hook_requests) == 7 and router.other == []


async def test_clientlist_interval_is_configurable():
    router, clock = FakeRouter(), Clock()
    collector = _collector(router, clock, clientlist_interval_seconds=15.0)
    await collector.poll(force=True)
    clock.now += 10
    await collector.poll(force=True)
    clock.now += 10
    await collector.poll(force=True)
    assert [h.endswith("get_clientlist()") for h in router.hook_requests] == [True, False, True]


async def test_falls_back_to_separate_requests_and_remembers(caplog):
    router, clock = FakeRouter(combined=False), Clock()
    collector = _collector(router, clock)
    with caplog.at_level(logging.WARNING, logger="pengumon.drivers.asuswrt"):
        first = await collector.poll(force=True)
        clock.now += 10
        second = await collector.poll(force=True)

    assert collector.driver._combined_ok is False
    assert router.hook_requests == [
        "netdev();cpu_usage();memory_usage();get_clientlist()",  # combined attempt, answered without counters
        "netdev()", "cpu_usage()", "memory_usage()", "get_clientlist()",
        "netdev()", "cpu_usage()", "memory_usage()",  # later polls skip the combined form (and the client list)
    ]
    assert (first.cpu_usage_pct, first.mem_usage_pct, first.client_count) == (None, 60.0, 2)
    assert second.cpu_usage_pct == 50.0
    assert second.wan_rx_bytes == 100_000_000 and second.client_count == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_combined_requests_are_retried_every_hour_and_come_back_when_they_work():
    router, clock = FakeRouter(combined=False), Clock()
    collector = _collector(router, clock)
    await collector.poll(force=True)
    assert collector.driver._combined_ok is False

    router.hook_requests.clear()
    clock.now += 3599  # not yet an hour after giving up: still one request per hook
    await collector.poll(force=True)
    assert all(";" not in h for h in router.hook_requests) and collector.driver._combined_ok is False

    router.hook_requests.clear()
    clock.now += 2  # an hour: the router still cannot, so one combined attempt, then per hook again
    await collector.poll(force=True)
    assert router.hook_requests[0].startswith("netdev();cpu_usage();") and all(";" not in h for h in router.hook_requests[1:])
    assert collector.driver._combined_ok is False

    router.combined = True  # "firmware update"
    router.hook_requests.clear()
    clock.now += 3601
    snap = await collector.poll(force=True)
    assert collector.driver._combined_ok is True
    assert router.hook_requests == ["netdev();cpu_usage();memory_usage();get_clientlist()"]
    assert snap.wan_rx_bytes == 100_000_000 and snap.cpu_usage_pct == 50.0
    router.hook_requests.clear()
    clock.now += 10
    await collector.poll(force=True)
    assert router.hook_requests == ["netdev();cpu_usage();memory_usage()"]


async def test_one_502_page_is_a_failed_poll_not_a_mode_change():
    router, clock = FakeRouter(), Clock()
    failing = {"on": True}

    def handler(request):
        if failing["on"] and request.url.path.endswith("appGet.cgi"):
            router.hook_requests.append(parse_qs(request.content.decode())["hook"][0])
            return httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>")
        return router(request)

    collector = _collector(handler, clock)
    snap = await collector.poll(force=True)
    assert snap.status == "error" and snap.wan_rx_bytes is None
    assert "appGet answered HTTP 502" in snap.error_message
    assert collector.driver._combined_ok is True
    assert router.hook_requests == ["netdev();cpu_usage();memory_usage();get_clientlist()"]  # no per-hook retries

    failing["on"] = False
    router.hook_requests.clear()
    clock.now += 10
    snap = await collector.poll(force=True)
    assert collector.driver._combined_ok is True
    assert router.hook_requests == ["netdev();cpu_usage();memory_usage()"]  # the combined form is still used
    assert snap.wan_rx_bytes == 100_000_000 and snap.cpu_usage_pct is None  # the first CPU reading


async def test_a_200_page_that_is_not_json_is_a_failed_poll_too():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    collector = _collector(handler)
    snap = await collector.poll(force=True)
    assert snap.status == "error" and "not JSON" in snap.error_message
    assert snap.wan_rx_bytes is None and collector.driver._combined_ok is True


async def test_transport_error_does_not_disable_combined_requests():
    calls = []

    def flaky(request):
        calls.append(parse_qs(request.content.decode())["hook"][0] if request.method == "POST" else request.url.path)
        raise httpx.ConnectTimeout("timeout")

    collector = _collector(flaky)
    snap = await collector.poll(force=True)
    assert snap.status == "error" and snap.error_message.startswith("Failed to communicate with router:")
    assert snap.wan_rx_bytes is None
    assert collector.driver._combined_ok is True
    assert calls[0] == "netdev();cpu_usage();memory_usage();get_clientlist()"
    assert all(";" not in c for c in calls[1:])  # only the update.cgi fallback, no per-hook retries


async def test_relogin_when_a_200_response_carries_error_status():
    router = FakeRouter(valid_token="expired-elsewhere")
    collector = _collector(router)
    snap = await collector.poll(force=True)
    assert router.logins == 1
    assert len(router.hook_requests) == 2  # refused, then accepted with the new token
    assert snap.wan_rx_bytes == 100_000_000 and snap.client_count == 2


async def test_relogin_on_401():
    state = {"calls": 0}

    def handler(request):
        if request.url.path.endswith("login.cgi"):
            return httpx.Response(200, json={"asus_token": "fresh"})
        state["calls"] += 1
        if request.headers.get("asus_token") != "fresh":
            return httpx.Response(401)
        return httpx.Response(200, json={**NETDEV, **CPU, **MEM, **CLIENTS})

    snap = await _collector(handler).poll(force=True)
    assert state["calls"] == 2 and snap.wan_tx_bytes == 50_000_000


async def test_gives_up_after_one_relogin():
    def handler(request):
        if request.url.path.endswith("login.cgi"):
            return httpx.Response(200, json={"asus_token": "fresh"})
        return httpx.Response(200, json={"error_status": "2"})

    collector = _collector(handler)
    snap = await collector.poll(force=True)  # must terminate
    assert snap.status == "error" and "refused the session token" in snap.error_message
    assert snap.wan_rx_bytes is None and collector.driver._combined_ok is True


# --- parsers ---------------------------------------------------------------------------------------

def test_parse_netdev_counters_hex_and_decimal():
    assert _parse_netdev(json.dumps(NETDEV)) == (100_000_000, 50_000_000)
    assert _parse_netdev('{"netdev":{"INTERNET_rx":"123","INTERNET_tx":456}}') == (123, 456)
    assert _parse_netdev("INTERNET_rx:0x10, INTERNET_tx:0x20") == (16, 32)


def test_parse_netdev_interface_table_and_misses():
    body = json.dumps({"netdev": {"br0": {"rx": 1, "tx": 2}, "vlan2": {"rx_bytes": 700, "tx_bytes": 800}}})
    assert _parse_netdev(body) == (700, 800)
    assert _parse_netdev(json.dumps({"netdev": {"br0": {"rx": 1, "tx": 2}}})) is None
    assert _parse_netdev("{}") is None
    assert _parse_netdev("netdev <html>not json</html>") is None


def test_parse_cpu_and_mem():
    assert _parse_cpu(json.dumps(CPU)) == {"1": (1000, 250), "2": (1000, 750)}
    assert _parse_cpu("{}") == {}
    assert _parse_mem(json.dumps(MEM)) == 60.0
    assert _parse_mem(json.dumps({"memory_usage": {"mem_total": "0", "mem_used": "0"}})) is None
    assert _parse_mem("{}") is None


def test_parse_cpu_and_mem_from_a_combined_body():
    body = json.dumps({**NETDEV, **CPU, **MEM, **CLIENTS})
    assert (_parse_cpu(body), _parse_mem(body), _parse_netdev(body)) == (
        {"1": (1000, 250), "2": (1000, 750)}, 60.0, (100_000_000, 50_000_000))


def test_cpu_percentage_comes_from_the_difference_between_readings():
    """The counters count from the router's boot: one reading alone gives the average since then (a value that
    barely moves in a day), so the percentage is the busy share of the jiffies between two readings."""
    first = {"1": (1000, 250), "2": (1000, 750)}
    assert _cpu_pct({}, first) is None
    assert _cpu_pct(first, {"1": (2000, 350), "2": (2000, 1650)}) == 50.0  # 10 % and 90 %
    assert _cpu_pct(first, first) is None  # no time passed
    assert _cpu_pct(first, {"1": (10, 1), "2": (10, 9)}) is None  # counters reset by a reboot
    assert _cpu_pct(first, {"1": (2000, 500), "3": (900, 100)}) == 25.0  # only cores seen in both


# The shape of an RT-AX router's answer to a combined request, with made-up values: sections such as "cpu_usage" are
# not valid JSON ("key":"k1":"v1",... without braces), so the body as a whole cannot be parsed, while the
# get_clientlist object inside it can.
MESH_COMBINED = (
    '{\n"netdev": "INTERNET_rx":"0x5f5e100","INTERNET_tx":"0x2faf080"\n,'
    '"cpu_usage":"cpu1_total":"1000","cpu1_usage":"250","cpu2_total":"1000","cpu2_usage":"750"\n,'
    '"memory_usage":"mem_total":"1000","mem_free":"400","mem_used":"600"\n,'
    '"get_clientlist":{"AA:AA:AA:AA:AA:01":{"isOnline":"1","name":"x {y}"},'
    '"AA:AA:AA:AA:AA:02":{"isOnline":"0"},"AA:AA:AA:AA:AA:03":{"isOnline":"1"},'
    '"maclist":["AA:AA:AA:AA:AA:01","AA:AA:AA:AA:AA:03"],"ClientAPILevel":"2"}\n}'
)


def test_firmware_with_a_non_json_combined_answer_still_gives_every_value():
    assert _parse_netdev(MESH_COMBINED) == (100_000_000, 50_000_000)
    assert _parse_cpu(MESH_COMBINED) == {"1": (1000, 250), "2": (1000, 750)}
    assert _parse_mem(MESH_COMBINED) == 60.0
    assert _parse_clients(MESH_COMBINED) == 2  # online entries; maclist and ClientAPILevel are not clients


def test_parse_clients():
    assert _parse_clients(json.dumps(CLIENTS)) == 2
    assert _parse_clients(json.dumps({"get_clientlist": {"x": {"online": "1"}}})) == 1
    # nobody flagged online but the list is not empty: count the entries (existing behaviour)
    assert _parse_clients(json.dumps({"get_clientlist": {"x": {"isOnline": "0"}, "y": {}}})) == 2
    assert _parse_clients(json.dumps({"get_clientlist": {"x": {"isOnline": "0"}, "maclist": [], "v": "2"}})) == 1
    assert _parse_clients(json.dumps({"get_clientlist": {}})) == 0
    # absent or unparseable: unknown, not zero
    assert _parse_clients(json.dumps(NETDEV)) is None
    assert _parse_clients("<html>") is None


# --- driver behaviour the Device tests above do not reach ------------------------------------------------------------

MESH_WITHOUT_COUNTERS = MESH_COMBINED.replace('"netdev": "INTERNET_rx":"0x5f5e100","INTERNET_tx":"0x2faf080"\n,', "")


def _driver(handler, clock=None, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    driver = AsusWrtDriver("http://192.168.1.1", "u", "p", client=client, clock=clock or Clock(), **kwargs)
    driver._token = "tok"
    return driver


def _mesh_without_counters(request):
    assert request.url.path.endswith("appGet.cgi")
    return httpx.Response(200, text=MESH_WITHOUT_COUNTERS)


async def test_a_combined_answer_without_counters_keeps_cpu_memory_and_clients():
    assert MESH_WITHOUT_COUNTERS != MESH_COMBINED and "INTERNET_rx" not in MESH_WITHOUT_COUNTERS
    with pytest.raises(ValueError):
        json.loads(MESH_WITHOUT_COUNTERS)  # like the real firmware's answer: not JSON as a whole

    def handler(request):
        if request.url.path.endswith("update.cgi"):
            return httpx.Response(404)
        return _mesh_without_counters(request)

    result = await _driver(handler).poll()  # an error would raise
    assert Capability.WAN_COUNTERS not in result.readings
    assert result.missing[Capability.WAN_COUNTERS]  # with a reason
    system = result.readings[Capability.ROUTER_SYSTEM]
    assert system.mem_pct == 60.0 and system.client_count == 2 and system.cpu_pct is None  # CPU % needs two reads
    assert Capability.ROUTER_SYSTEM not in result.missing


async def test_a_combined_answer_without_counters_is_a_healthy_snapshot_without_wan_bytes():
    def handler(request):
        if request.url.path.endswith("update.cgi"):
            return httpx.Response(404)
        return _mesh_without_counters(request)

    device = build_router(username="u", password="p",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=Clock())
    device.driver._token = "tok"
    snap = await device.poll(force=True)
    assert snap.status == "healthy" and snap.error_message is None
    assert snap.wan_rx_bytes is None and snap.wan_tx_bytes is None and snap.wan_rx_rate_mbps is None
    assert snap.mem_usage_pct == 60.0 and snap.client_count == 2


async def test_counters_read_earlier_in_a_per_hook_poll_survive_a_refused_relogin():
    calls = []

    def handler(request):
        path = request.url.path
        if path.endswith("login.cgi"):
            calls.append("login")
            return httpx.Response(200, json={"error_status": "3"})  # the router refuses the login
        hook = parse_qs(request.content.decode())["hook"][0]
        calls.append(hook)
        if ";" in hook:
            return httpx.Response(200, json={})  # firmware that cannot answer several hooks at once
        if hook == "netdev()":
            return httpx.Response(200, json=NETDEV)
        return httpx.Response(401)  # the session dies after the first hook

    driver = _driver(handler)
    result = await driver.poll()  # not an error
    assert result.readings[Capability.WAN_COUNTERS].rx_bytes == 100_000_000
    assert Capability.ROUTER_SYSTEM in result.missing
    assert "login" in calls and calls.index("netdev()") < calls.index("cpu_usage()")

    device = build_router(username="u", password="p",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=Clock())
    device.driver._token = "tok"
    snap = await device.poll(force=True)
    assert snap.status == "healthy" and snap.wan_rx_bytes == 100_000_000 and snap.wan_tx_bytes == 50_000_000


async def test_a_transport_error_while_logging_in_is_an_error_snapshot():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    device = build_router(username="u", password="p",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=Clock())
    assert device.driver._token is None
    snap = await device.poll(force=True)
    assert snap.status == "error" and snap.error_message.startswith("Failed to communicate with router:")
    assert snap.error_kind == "unreachable"


async def test_new_credentials_reset_the_token_and_the_next_poll_logs_in_with_them():
    logins = []

    def handler(request):
        if request.url.path.endswith("login.cgi"):
            logins.append(parse_qs(request.content.decode())["login_authorization"][0])
            return httpx.Response(200, json={"asus_token": f"tok{len(logins)}"},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json={**NETDEV, **CPU, **MEM, **CLIENTS})

    device = build_router(username="admin", password="old",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=Clock())
    await device.poll(force=True)
    await device.poll(force=True)
    assert len(logins) == 1 and device.driver._token == "tok1"  # the token is reused

    device.set_credentials("root", "new:p@ss")
    assert device.driver._token is None
    snap = await device.poll(force=True)
    assert snap.status == "healthy" and len(logins) == 2
    assert [base64.b64decode(x).decode() for x in logins] == ["admin:old", "root:new:p@ss"]


async def test_the_update_cgi_fallback_supplies_the_counters():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("update.cgi"):
            return httpx.Response(200, text='{"INTERNET": {"rx": "123456", "tx": "7890"}, "eth0": {"rx": "1"}}')
        hook = parse_qs(request.content.decode())["hook"][0]
        body = {}
        for h in hook.split(";"):
            body.update({k: v for k, v in ANSWERS[h].items() if k != "netdev"})  # appGet has no counters
        return httpx.Response(200, json=body)

    collector = _collector(handler)
    snap = await collector.poll(force=True)
    assert snap.status == "healthy"
    assert (snap.wan_rx_bytes, snap.wan_tx_bytes) == (123456, 7890)
    assert snap.mem_usage_pct == 60.0 and snap.client_count == 2
    assert ("GET", "/update.cgi", {"output": "netdev"}) in requests


async def test_cpu_readings_closer_than_the_minimum_interval_repeat_the_percentage_and_keep_the_baseline():
    cpu = {}

    def handler(request):
        hook = parse_qs(request.content.decode())["hook"][0]
        body = {"netdev": NETDEV["netdev"],
                "cpu_usage": {"cpu1_total": str(cpu["total"]), "cpu1_usage": str(cpu["busy"])}}
        return httpx.Response(200, json=body)

    clock = Clock()
    driver = _driver(handler, clock)

    async def read(at, total, busy):
        clock.now = at
        cpu.update(total=total, busy=busy)
        return (await driver.poll()).readings[Capability.ROUTER_SYSTEM].cpu_pct

    assert CPU_MIN_INTERVAL_SECONDS > 1.0
    assert await read(1000.0, 1000, 100) is None  # baseline
    assert await read(1010.0, 2000, 600) == 50.0
    # Too soon after the last reading: the percentage is repeated (0 % between these two would be noise) ...
    assert await read(1010.0 + CPU_MIN_INTERVAL_SECONDS - 0.5, 2100, 600) == 50.0
    # ... and the baseline is still the 1010 s reading: 1000 jiffies, all busy, since then.
    assert await read(1020.0, 3000, 1600) == 100.0
    assert await read(1030.0, 4000, 1600) == 0.0  # and readings far enough apart give a new percentage each time


async def test_a_poll_with_only_a_cached_client_count_reports_the_system_data_missing():
    state = {"first": True}

    def handler(request):
        hook = parse_qs(request.content.decode())["hook"][0]
        if state["first"]:
            return httpx.Response(200, json={**NETDEV, **CPU, **MEM, **CLIENTS})
        return httpx.Response(200, json=NETDEV)  # counters only: no CPU, memory or client list this time

    clock = Clock()
    driver = _driver(handler, clock)
    first = await driver.poll()
    assert first.readings[Capability.ROUTER_SYSTEM].client_count == 2

    state["first"] = False
    clock.now += 10  # the client list is not due again (60 s)
    second = await driver.poll()
    assert Capability.WAN_COUNTERS in second.readings
    assert Capability.ROUTER_SYSTEM not in second.readings
    assert Capability.ROUTER_SYSTEM in second.missing


async def test_counters_are_stamped_with_the_wall_clock_at_which_they_were_read():
    wall = Clock()
    wall.now = 5000.0
    driver = _driver(FakeRouter(), wall_clock=wall)
    driver._token = "tok"
    first = await driver.poll()
    assert first.readings[Capability.WAN_COUNTERS].read_at == 5000.0
    wall.now = 6000.0
    second = await driver.poll()
    assert second.readings[Capability.WAN_COUNTERS].read_at == 6000.0


# --- the router model reaches the snapshots -----------------------------------------------------------------------------

async def test_the_configured_model_names_the_routers_snapshots_and_no_model_means_none():
    with_model = await _collector(FakeRouter(), model="RT-AX88U").poll(force=True)
    without = await _collector(FakeRouter()).poll(force=True)
    assert with_model.status == "healthy" and with_model.model_name == "RT-AX88U"
    assert without.status == "healthy" and without.model_name is None

    failing = build_router(model="RT-AX88U", password="p",
                           client=httpx.AsyncClient(transport=httpx.MockTransport(
                               lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))))
    assert (await failing.poll(force=True)).model_name == "RT-AX88U"
