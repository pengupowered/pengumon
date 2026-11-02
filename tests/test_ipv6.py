"""IPv6 beyond the backends' own packets: addresses (icmp_packet), the dual-stack pairing (icmp_dual), "IPv6
unavailable on this host" (netinfo.Ipv6Availability, the prober, the path rule, the classifier's metrics), host-name
targets with `family`, traceroute over IPv6, and the IPv6 default router."""

import errno
import socket

import pytest
from httpx import ASGITransport, AsyncClient

from icmp_fakes import FakeBackend, FakePath
from pengumon.engine import icmp_dual, netinfo
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_dual import DualStackBackend
from pengumon.engine.icmp_packet import address_key, address_text, echo6_request, ip_literal, sockaddr
from pengumon.engine.mtr import MtrEngine
from pengumon.engine.netinfo import Ipv6Availability, needs_route_check, parse_ipv6_default_router
from pengumon.engine.path_rules import ignoring_ping
from pengumon.engine.prober import NetworkProber
from pengumon.models.probes import TargetConfig, TracerouteRequest

V6 = "2001:db8::7"
ROUTER6 = "2001:db8:ffff::1"


# ---- addresses --------------------------------------------------------------------------------------------------

def test_ip_literals_of_both_families():
    assert ip_literal("192.0.2.1") == ("192.0.2.1", 4)
    assert ip_literal("2001:DB8:0:0::7") == (V6, 6)  # canonical text (RFC 5952)
    assert ip_literal("fe80::1%eth0") == ("fe80::1%eth0", 6)  # the zone stays: a link-local address needs it
    assert ip_literal("::ffff:192.0.2.1") == ("192.0.2.1", 4)
    for bad in ("example.com", "", "1.2.3", "2001:db8::1::2", "-f", None, 3232235777):
        with pytest.raises(ValueError):
            ip_literal(bad)


def test_sockaddr_per_family():
    assert sockaddr("192.0.2.1", 4) == ("192.0.2.1", 0)
    assert sockaddr(V6, 6) == (V6, 0, 0, 0)
    assert sockaddr("fe80::1%7", 6) == ("fe80::1", 0, 0, 7)
    lo = socket.if_nameindex()[0] if hasattr(socket, "if_nameindex") else None
    if lo:
        assert sockaddr(f"fe80::1%{lo[1]}", 6) == ("fe80::1", 0, 0, lo[0])
    with pytest.raises(OSError):
        sockaddr("fe80::1%no-such-interface0", 6)


def test_addresses_compare_by_value():
    assert address_key("2001:DB8::7") == address_key(V6) == socket.inet_pton(socket.AF_INET6, V6)
    assert address_key("fe80::1%eth0") == address_key("fe80::1")
    assert address_key("192.0.2.1") == bytes([192, 0, 2, 1])
    assert address_text(bytes([192, 0, 2, 1])) == "192.0.2.1"
    assert address_text(socket.inet_pton(socket.AF_INET6, V6), 5) == V6  # only link-local addresses get a zone
    assert address_text(socket.inet_pton(socket.AF_INET6, "fe80::1"), 0) == "fe80::1"
    assert address_text(socket.inet_pton(socket.AF_INET6, "fe80::1"), 999999).startswith("fe80::1%")


def test_echo6_request_layout():
    """RFC 4443 4.1: type 128, code 0, checksum (0: the kernel fills it), identifier, sequence, data."""
    assert echo6_request(0x1234, 0x0506, b"xy") == bytes([128, 0, 0, 0, 0x12, 0x34, 0x05, 0x06]) + b"xy"


# ---- the dual-stack pairing --------------------------------------------------------------------------------------

def _dual(v6_error=None, has_ipv6=True, now=None):
    v4 = FakeBackend()
    opened = []

    def open_v6():
        if v6_error is not None:
            raise v6_error
        opened.append(FakeBackend())
        opened[-1].name = "fake6"
        return opened[-1]

    clock = (lambda: now[0]) if now is not None else None
    kwargs = {"_has_ipv6": lambda: has_ipv6}
    if clock:
        kwargs["_clock"] = clock
    return DualStackBackend(v4, open_v6, **kwargs), v4, opened


async def test_each_probe_goes_to_its_family():
    dual, v4, opened = _dual()
    assert dual.name == "fake" and dual.ipv6_name is None
    assert (await dual.probe("192.0.2.1", timeout=0.1)).responder == "192.0.2.1"
    assert opened == []  # no IPv6 probe yet: no IPv6 socket
    assert (await dual.probe(V6, timeout=0.1, ttl=3)).responder == V6
    assert (await dual.probe("2001:db8::8", timeout=0.1)).kind == "reply"
    assert len(opened) == 1 and dual.ipv6_name == "fake6"  # opened once
    assert v4.sent == [("192.0.2.1", 64, 0.1)]
    assert opened[0].sent == [(V6, 3, 0.1), ("2001:db8::8", 64, 0.1)]
    with pytest.raises(ValueError):
        await dual.probe("example.com", timeout=0.1)
    dual.close()
    assert v4.closed and opened[0].closed
    with pytest.raises(OSError):
        await dual.probe("192.0.2.1", timeout=0.1)


async def test_a_host_without_ipv6_fails_only_ipv6_probes_with_eafnosupport():
    dual, v4, _ = _dual(v6_error=IcmpUnavailable("cannot open an ICMPv6 socket: Address family not supported"),
                        has_ipv6=False)
    assert (await dual.probe("192.0.2.1", timeout=0.1)).kind == "reply"
    with pytest.raises(OSError) as caught:
        await dual.probe(V6, timeout=0.1)
    assert caught.value.errno == errno.EAFNOSUPPORT and netinfo.is_no_ipv6_error(caught.value)


async def test_an_ipv6_open_failure_is_remembered_then_retried():
    now = [100.0]
    failure = IcmpUnavailable("cannot open an ICMPv6 socket: raw socket: Operation not permitted")
    dual, _v4, opened = _dual(v6_error=failure, now=now)
    attempts = []
    real_open = dual._open_v6

    def counting():
        attempts.append(1)
        return real_open()

    dual._open_v6 = counting
    for _ in range(3):
        with pytest.raises(IcmpUnavailable):
            await dual.probe(V6, timeout=0.1)
    assert len(attempts) == 1
    now[0] += icmp_dual.RETRY_SECONDS + 1
    dual._open_v6 = lambda: FakeBackend()
    assert (await dual.probe(V6, timeout=0.1)).kind == "reply"


def test_attributes_of_the_ipv4_backend_show_through():
    v4 = FakeBackend()
    v4.kernel_timestamps = 7
    dual = DualStackBackend(v4, lambda: FakeBackend())
    assert dual.kernel_timestamps == 7
    with pytest.raises(AttributeError):
        dual.no_such_attribute


# ---- is there IPv6 here? -------------------------------------------------------------------------------------------

def test_availability_is_checked_then_rechecked_every_few_minutes():
    now = [0.0]
    answers = ["Network is unreachable", None]
    calls = []

    def check():
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    ipv6 = Ipv6Availability(check=check, clock=lambda: now[0], recheck_seconds=300)
    assert not ipv6.available() and ipv6.reason == "Network is unreachable"
    now[0] = 299
    assert not ipv6.available() and len(calls) == 1  # cached
    now[0] = 300
    assert ipv6.available() and ipv6.reason is None and len(calls) == 2  # the network changed


def test_a_send_without_a_route_marks_ipv6_unavailable_until_the_next_check():
    now = [0.0]
    ipv6 = Ipv6Availability(check=lambda: None, clock=lambda: now[0], recheck_seconds=300)
    assert ipv6.available()
    ipv6.no_route(OSError(errno.ENETUNREACH, "Network is unreachable"))
    now[0] = 10
    assert not ipv6.available() and "Network is unreachable" in ipv6.reason
    now[0] = 311
    assert ipv6.available()


def test_the_real_route_check_never_raises():
    assert netinfo.ipv6_route_check() is None or isinstance(netinfo.ipv6_route_check(), str)
    assert isinstance(netinfo.ipv6_route_check("::1"), (str, type(None)))


@pytest.mark.parametrize("code, no_ipv6", [
    (errno.ENETUNREACH, True), (errno.EADDRNOTAVAIL, True), (errno.EAFNOSUPPORT, True), (10051, True), (1231, True),
    (errno.EHOSTUNREACH, False), (errno.EPERM, False), (None, False),
])
def test_which_send_errors_mean_no_ipv6(code, no_ipv6):
    assert netinfo.is_no_ipv6_error(OSError(code, "x") if code is not None else OSError("x")) is no_ipv6


def test_loopback_and_link_local_need_no_internet_route():
    assert not needs_route_check("::1") and not needs_route_check("fe80::1%eth0")
    assert needs_route_check(V6) and needs_route_check("2606:4700:4700::1111")


# ---- the prober: no route, no samples ------------------------------------------------------------------------------

def _v6_target(address=V6, tid="v6", category="public", family=4):
    return TargetConfig(id=tid, name="V6", address=address, category=category, family=family)


def _prober(backend, route=None):
    """A prober whose IPv6 check says `route` (None: there is a route; else why not)."""
    checks = []

    def check():
        checks.append(1)
        return route

    prober = NetworkProber(ping_timeout=0.05, backend=backend, ipv6=Ipv6Availability(check=check))
    return prober, checks


async def test_without_ipv6_an_ipv6_target_records_nothing_and_says_why():
    backend = FakeBackend()
    prober, checks = _prober(backend, route="Network is unreachable")
    target = _v6_target()
    prober.register_target(target)
    assert await prober.probe_icmp(target) is None  # no sample: not a loss, not an outage
    assert prober.stats["v6"].ipv6_unavailable and backend.sent == []
    assert prober.stats["v6"].sent == 0 and prober.stats["v6"].status == "up"
    await prober.probe_icmp(target)
    assert len(checks) == 1  # checked once per recheck period, not every probe


async def test_with_ipv6_an_ipv6_target_is_probed_and_a_silent_one_is_a_loss():
    answers = {V6: ProbeResult("reply", V6, 4.2), "2001:db8::dead": ProbeResult("timeout")}
    backend = FakeBackend(lambda address, ttl: answers[address])
    prober, _ = _prober(backend)
    alive, dead = _v6_target(), _v6_target("2001:db8::dead", tid="dead")
    for t in (alive, dead):
        prober.register_target(t)
    ok = await prober.probe_icmp(alive)
    assert ok.success and ok.latency_ms == 4.2 and not prober.stats["v6"].ipv6_unavailable
    lost = await prober.probe_icmp(dead)
    assert not lost.success and lost.error == "Request timed out"  # IPv6 works here: a normal outage
    assert not prober.stats["dead"].ipv6_unavailable


async def test_a_send_that_finds_no_ipv6_route_turns_the_target_neutral():
    backend = FakeBackend(lambda address, ttl: OSError(errno.ENETUNREACH, "Network is unreachable"))
    prober, _ = _prober(backend)
    target = _v6_target()
    prober.register_target(target)
    assert await prober.probe_icmp(target) is None
    assert prober.stats["v6"].ipv6_unavailable and not prober.ipv6.available()
    # an IPv4 target with the same error is still a loss: IPv4 is what the dashboard is about
    v4 = TargetConfig(id="v4", name="V4", address="192.0.2.9", category="public")
    sample = await prober.probe_icmp(v4)
    assert not sample.success and sample.error == "ICMP send failed: [Errno %d] Network is unreachable" % errno.ENETUNREACH


async def test_other_ipv6_send_errors_are_losses():
    backend = FakeBackend(lambda address, ttl: OSError(errno.EHOSTUNREACH, "No route to host"))
    prober, _ = _prober(backend)
    target = _v6_target()
    prober.register_target(target)
    sample = await prober.probe_icmp(target)
    assert not sample.success and "No route to host" in sample.error and not prober.stats["v6"].ipv6_unavailable


async def test_ipv6_coming_back_clears_the_flag():
    now = [0.0]
    route = ["Network is unreachable"]
    backend = FakeBackend()
    prober = NetworkProber(ping_timeout=0.05, backend=backend,
                           ipv6=Ipv6Availability(check=lambda: route[0], clock=lambda: now[0]))
    target = _v6_target()
    prober.register_target(target)
    assert await prober.probe_icmp(target) is None and prober.stats["v6"].ipv6_unavailable
    route[0], now[0] = None, netinfo.IPV6_RECHECK_SECONDS
    sample = await prober.probe_icmp(target)
    assert sample.success and not prober.stats["v6"].ipv6_unavailable


@pytest.mark.parametrize("address", ["::1", "fe80::1%7"])
async def test_loopback_and_link_local_targets_are_probed_without_an_internet_route(address):
    backend = FakeBackend()
    prober, checks = _prober(backend, route="Network is unreachable")
    target = _v6_target(address)
    prober.register_target(target)
    assert (await prober.probe_icmp(target)).success
    assert checks == [] and backend.sent[0][0] == address


async def test_a_host_name_target_uses_its_family(monkeypatch):
    import asyncio

    async def fake_getaddrinfo(host, port, family=0, type=0):
        if family == socket.AF_INET6:
            return [(family, type, 17, "", (V6, 0, 0, 0))]
        return [(family, type, 17, "", ("192.0.2.7", 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    backend = FakeBackend()
    prober, _ = _prober(backend)
    four, six = _v6_target("dual.example.com", "four"), _v6_target("dual.example.com", "six", family=6)
    assert (await prober.probe_icmp(four)).success and (await prober.probe_icmp(six)).success
    assert [a for a, _t, _o in backend.sent] == ["192.0.2.7", V6]
    # without IPv6 a family-6 name is not even looked up
    prober, _ = _prober(FakeBackend(), route="Network is unreachable")
    prober.register_target(six)
    assert await prober.probe_icmp(six) is None and prober.stats["six"].ipv6_unavailable


def test_family_defaults_to_4_and_is_4_or_6():
    assert TargetConfig(id="a", name="A", address="example.com").family == 4
    assert TargetConfig(id="a", name="A", address="example.com", family=6).family == 6
    for bad in (5, 0, "ipv6"):
        with pytest.raises(ValueError):
            TargetConfig(id="a", name="A", address="example.com", family=bad)
    # old rows (no family) still load, and the field round-trips through the database's JSON
    old = TargetConfig.model_validate_json('{"id": "a", "name": "A", "address": "2001:db8::1"}')
    assert old.family == 4 and TargetConfig.model_validate_json(old.model_dump_json()) == old


async def test_a_round_skips_the_unavailable_target_and_the_rules_leave_it_out():
    backend = FakeBackend(lambda address, ttl: ProbeResult("timeout") if address == "192.0.2.1" else
                          ProbeResult("reply", address, 1.0))
    prober, _ = _prober(backend, route="Network is unreachable")
    targets = [
        TargetConfig(id="router", name="Router", address="192.0.2.1", category="local"),
        TargetConfig(id="pub6", name="Public v6", address=V6, category="public"),
    ]
    for _ in range(10):
        samples = await prober.execute_probe_round(targets=targets, dns_probes=[])
        assert [s.target_id for s in samples] == ["router"]
    stats = prober.stats
    assert stats["router"].status == "down" and stats["pub6"].ipv6_unavailable
    # the unprobed IPv6 target (status still "up") does not vouch for the router: it is down, not "ignores ping"
    assert not stats["router"].ignores_ping and not stats["pub6"].ignores_ping
    assert ignoring_ping(stats.values()) == set()
    metrics = round_metrics(stats, samples)
    assert metrics.public.targets == () and not metrics.public.measured


# ---- traceroute over IPv6 ------------------------------------------------------------------------------------------

async def test_trace_over_ipv6():
    backend = FakeBackend(path=FakePath(["fe80::1%eth0", ROUTER6, None]))
    trace = await MtrEngine("2001:DB8::7", max_hops=8, timeout=0.05, backend=backend).run_trace()
    assert [(h.hop_number, h.ip, h.label) for h in trace] == [
        (1, "fe80::1%eth0", "Hop 1 (link-local)"),
        (2, ROUTER6, "Hop 2"),
        (3, None, "Request Timed Out"),
        (4, V6, "Hop 4"),
    ]
    assert {a for a, _t, _o in backend.sent} == {V6}  # the canonical address, hop limit 1..8


def test_ipv6_hop_labels():
    label = MtrEngine("::1")._label_hop
    assert label(4, "fd00::1") == "Hop 4 (private)"
    assert label(5, "fe80::2%eth0") == "Hop 5 (link-local)"
    assert label(6, "2606:4700:4700::1111") == "Hop 6"
    assert label(6, "2001:4860:4860::8888") == "Hop 6"
    assert label(7, "2001:db8::9") == "Hop 7"


async def test_trace_api_accepts_ipv6_and_a_family(app_state, monkeypatch):
    from pengumon.api import ops
    from pengumon.main import app

    made = []

    class Engine:
        def __init__(self, **kwargs):
            made.append(kwargs)

        async def run_trace(self):
            return []

    monkeypatch.setattr(ops, "MtrEngine", Engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.post("/api/mtr/trace", json={"target_host": "2001:db8::7"})).status_code == 200
        assert (await ac.post("/api/mtr/trace", json={"target_host": "example.com", "family": 6})).status_code == 200
        assert (await ac.post("/api/mtr/trace", json={"target_host": "example.com", "family": 5})).status_code == 422
    assert [(m["target_host"], m["family"]) for m in made] == [("2001:db8::7", 4), ("example.com", 6)]
    assert TracerouteRequest(target_host="fe80::1%eth0").target_host == "fe80::1%eth0"


async def test_targets_api_takes_an_ipv6_address_and_a_family(app_state, tmp_path):
    from pengumon.config import AppConfig
    from pengumon.db.repository import DatabaseRepository
    from pengumon.main import app

    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    app_state.config = AppConfig()
    app_state.prober = NetworkProber(backend=FakeBackend())
    app_state.repository = repo
    body = {"id": "cf6", "name": "Cloudflare v6", "address": "2606:4700:4700::1111", "category": "public"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.post("/api/targets", json=body)).status_code == 200
        named = {"id": "named6", "name": "Named", "address": "one.one.one.one", "category": "public", "family": 6}
        assert (await ac.post("/api/targets", json=named)).json()["family"] == 6
        listed = (await ac.get("/api/targets")).json()
    assert [t.family for t in (await repo.load_path())[1]] == [4, 6]  # stored in the probe path
    assert {t["target_id"]: t["ipv6_unavailable"] for t in listed} == {"cf6": False, "named6": False}


# ---- the IPv6 default router (/proc/net/ipv6_route) ----------------------------------------------------------------

def _route(dest="0" * 32, plen="00", nexthop="0" * 32, metric=1024, flags=0x3, iface="eth0"):
    return f"{dest} {plen} {'0' * 32} 00 {nexthop} {metric:08x} 00000001 00000000 {flags:08x} {iface:>8}"


def test_ipv6_default_router_from_the_route_table():
    ll = "fe80" + "0" * 24 + "0001"
    table = "\n".join([
        _route("20010db8000000000000000000000000", "40", flags=0x1),  # an on-link prefix
        _route(nexthop=ll, metric=1024, iface="wlan0"),
        _route(nexthop="20010db8ffff00000000000000000001", metric=100, iface="eth0"),  # lower metric, global
        _route(metric=0xFFFFFFFF, flags=0x00200200, iface="lo"),  # the kernel's reject route
    ])
    assert parse_ipv6_default_router(table) == "2001:db8:ffff::1"
    assert parse_ipv6_default_router(_route(nexthop=ll, iface="wlan0")) == "fe80::1%wlan0"
    assert parse_ipv6_default_router(_route(nexthop=ll, flags=0x1)) is None  # no gateway flag
    assert parse_ipv6_default_router(_route(nexthop=ll, flags=0x0203)) is None  # reject
    assert parse_ipv6_default_router(_route(flags=0x3)) is None  # no next hop
    assert parse_ipv6_default_router("") is None
    assert parse_ipv6_default_router("garbage line\n" + "x " * 12) is None


def test_ipv6_default_router_without_the_file(tmp_path):
    assert netinfo.get_ipv6_default_router(tmp_path / "missing") is None


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="reads /proc/net/ipv6_route")
def test_the_real_route_table_parses():
    router = netinfo.get_ipv6_default_router()
    assert router is None or ip_literal(router)[1] == 6
