"""Choosing the ICMP backend per OS, the app's shared backend, and what uses it: the prober's samples, /api/info and
the gateway guess where there is no /proc/net/route."""

import sys

import pytest
from httpx import ASGITransport, AsyncClient

from icmp_fakes import FakeBackend, FakePath
from pengumon.engine import icmp_select, netinfo
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_select import describe_resolution, open_backend, resolve_address
from pengumon.engine.netinfo import gateway_from_ttl_probe, get_default_gateway_ip
from pengumon.engine.prober import NetworkProber
from pengumon.models.probes import TargetConfig


@pytest.fixture(autouse=True)
def _no_shared_backend():
    icmp_select.close_shared_backend()
    yield
    icmp_select.close_shared_backend()


# ---- selection -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform, module, cls, paired", [
    ("linux", "pengumon.engine.icmp_linux", "LinuxIcmpBackend", True),
    ("darwin", "pengumon.engine.icmp_macos", "MacIcmpBackend", True),
    ("win32", "pengumon.engine.icmp_windows", "WindowsIcmpBackend", False),
])
def test_each_os_gets_its_own_backend(monkeypatch, platform, module, cls, paired):
    """Linux and macOS open one socket per family, paired by icmp_dual (IPv6 on first use, of the same socket kind);
    Windows' API does both."""
    import importlib

    from pengumon.engine.icmp_dual import DualStackBackend

    mod = importlib.import_module(module)  # every backend module imports on every OS
    made = []

    def fake(*args, **kwargs):
        made.append((args, kwargs))
        backend = FakeBackend()
        backend.kind = "dgram"
        return backend

    monkeypatch.setattr(mod, cls, fake)
    backend = open_backend(platform)
    if not paired:
        assert isinstance(backend, FakeBackend) and made == [((), {})]
        return
    assert isinstance(backend, DualStackBackend) and backend.name == "fake" and backend.v6 is None
    assert made == [((), {})]  # IPv6 is not opened until an IPv6 probe needs it
    backend.ipv6_backend()
    assert made[1] == ((("dgram",), {"family": 6}) if platform == "linux" else ((), {"family": 6}))


def test_an_unknown_os_has_no_backend():
    with pytest.raises(IcmpUnavailable) as caught:
        open_backend("freebsd14")
    assert "freebsd14" in caught.value.reason


def test_the_shared_backend_is_opened_once(monkeypatch):
    opened = []
    monkeypatch.setattr(icmp_select, "open_backend", lambda: opened.append(FakeBackend()) or opened[-1])
    assert icmp_select.shared_backend() is icmp_select.shared_backend()
    assert len(opened) == 1
    icmp_select.close_shared_backend()
    assert opened[0].closed


def test_a_failed_open_is_retried_only_after_a_while(monkeypatch):
    attempts = []

    def failing():
        attempts.append(1)
        raise IcmpUnavailable("cannot open an ICMP socket", "the fix")

    now = [1000.0]
    monkeypatch.setattr(icmp_select, "open_backend", failing)
    monkeypatch.setattr(icmp_select.time, "monotonic", lambda: now[0])
    for _ in range(3):
        with pytest.raises(IcmpUnavailable):
            icmp_select.shared_backend()
    assert len(attempts) == 1 and icmp_select.shared_failure().fix == "the fix"
    now[0] += icmp_select.RETRY_SECONDS + 1
    monkeypatch.setattr(icmp_select, "open_backend", lambda: FakeBackend())
    assert icmp_select.shared_backend().name == "fake"
    assert icmp_select.shared_failure() is None


def test_resolution_wording():
    assert describe_resolution(1.0) == "1 ms resolution"
    assert describe_resolution(0.001) == "1 µs resolution"


async def test_resolve_address():
    assert await resolve_address("192.0.2.5", 1.0) == ("192.0.2.5", 4)
    assert await resolve_address("localhost", 2.0) == ("127.0.0.1", 4)
    # a literal is its own family, whatever `family` says; IPv6 is written canonically
    assert await resolve_address("2001:DB8:0::1", 1.0) == ("2001:db8::1", 6)
    assert await resolve_address("192.0.2.5", 1.0, family=6) == ("192.0.2.5", 4)
    assert await resolve_address("::ffff:192.0.2.5", 1.0) == ("192.0.2.5", 4)
    for bad in ("-f", "a b", "x;y", ""):
        with pytest.raises(ValueError):
            await resolve_address(bad, 1.0)
    with pytest.raises(ValueError):
        await resolve_address("localhost", 1.0, family=5)
    with pytest.raises(OSError):
        await resolve_address("no-such-host.invalid", 2.0)


async def test_a_name_resolves_in_the_family_asked_for(monkeypatch):
    import asyncio
    import socket

    asked = []

    async def fake_getaddrinfo(host, port, family=0, type=0):
        asked.append(family)
        if family == socket.AF_INET6:
            return [(family, type, 17, "", ("2001:db8::7", 0, 0, 0))]
        return [(family, type, 17, "", ("192.0.2.7", 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    assert await resolve_address("dual.example.com", 1.0) == ("192.0.2.7", 4)
    assert await resolve_address("dual.example.com", 1.0, family=6) == ("2001:db8::7", 6)
    assert asked == [socket.AF_INET, socket.AF_INET6]


# ---- the prober's samples --------------------------------------------------------------------------------------------

def _target(address="192.0.2.5"):
    return TargetConfig(id="t", name="T", address=address, category="public")


@pytest.mark.parametrize("result, success, error", [
    (ProbeResult("reply", "192.0.2.5", 1.234), True, None),
    (ProbeResult("timeout"), False, "Request timed out"),
    (ProbeResult("unreachable", "192.0.2.1", 3.0), False, "Destination unreachable (reported by 192.0.2.1)"),
    (ProbeResult("ttl_exceeded", "192.0.2.1", 3.0), False, "TTL exceeded in transit at 192.0.2.1 (routing loop?)"),
    (OSError("Network is unreachable"), False, "ICMP send failed: Network is unreachable"),
])
async def test_only_an_echo_reply_is_a_success(result, success, error):
    backend = FakeBackend(lambda address, ttl: result)
    sample = await NetworkProber(ping_timeout=0.05, backend=backend).probe_icmp(_target())
    assert (sample.success, sample.error) == (success, error)
    assert sample.latency_ms == (1.23 if success else None)
    assert sample.packet_loss_pct == (0.0 if success else 100.0)
    assert backend.sent == [("192.0.2.5", 64, 0.05)]


async def test_unknown_names_are_losses_that_say_why():
    backend = FakeBackend()
    prober = NetworkProber(ping_timeout=1.0, backend=backend)
    name = await prober.probe_icmp(_target("no-such-host.invalid"))
    assert not name.success and name.error.startswith("Name lookup failed")
    assert backend.sent == []


async def test_a_name_target_is_probed_at_its_ipv4_address():
    backend = FakeBackend()
    sample = await NetworkProber(ping_timeout=1.0, backend=backend).probe_icmp(_target("localhost"))
    assert sample.success and backend.sent[0][0] == "127.0.0.1"


async def test_no_icmp_is_a_loss_with_the_reason(monkeypatch):
    def unavailable():
        raise IcmpUnavailable("cannot open an ICMP socket: ping socket: Permission denied", "sudo ...")

    monkeypatch.setattr(icmp_select, "open_backend", unavailable)
    prober = NetworkProber(ping_timeout=0.1)
    sample = await prober.probe_icmp(_target())
    assert not sample.success and sample.error == "ICMP unavailable: cannot open an ICMP socket: ping socket: Permission denied"
    assert prober.probe_method() == {"backend": None, "resolution_ms": None,
                                     "error": "cannot open an ICMP socket: ping socket: Permission denied",
                                     "fix": "sudo ..."}


def test_probe_method_names_the_backend():
    prober = NetworkProber(backend=FakeBackend())
    assert prober.probe_method() == {"backend": "fake", "resolution_ms": 0.001, "error": None, "fix": None}


async def test_api_info_shows_the_probe_method(app_state):
    from pengumon.main import app

    app_state.prober = NetworkProber(backend=FakeBackend())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        info = (await ac.get("/api/info")).json()
    assert info["probe"] == {"backend": "fake", "resolution_ms": 0.001, "error": None, "fix": None}


# ---- the gateway where there is no route table ----------------------------------------------------------------------

def test_the_route_table_comes_first(tmp_path):
    (tmp_path / "route").write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                                    "eth0\t00000000\t0114A8C0\t0003\t0\t0\t100\t00000000\n")
    probed = []
    assert get_default_gateway_ip(tmp_path / "route", lambda: probed.append(1)) == "192.168.20.1"
    assert probed == []


def test_without_a_route_table_the_ttl_probe_is_asked(tmp_path):
    assert get_default_gateway_ip(tmp_path / "missing", lambda: "192.168.1.1") == "192.168.1.1"
    assert get_default_gateway_ip(tmp_path / "missing", lambda: None) is None


@pytest.mark.parametrize("result, gateway", [
    (ProbeResult("ttl_exceeded", "192.168.1.1", 0.5), "192.168.1.1"),
    (ProbeResult("ttl_exceeded", "10.0.0.1", 0.5), "10.0.0.1"),
    (ProbeResult("ttl_exceeded", "172.31.255.254", 0.5), "172.31.255.254"),
    (ProbeResult("ttl_exceeded", "198.51.100.1", 0.5), None),  # a public first hop is not "the gateway"
    (ProbeResult("ttl_exceeded", "100.64.0.1", 0.5), None),  # carrier-grade NAT: the ISP's, not ours
    (ProbeResult("ttl_exceeded", None, 0.5), None),
    (ProbeResult("unreachable", "192.168.1.1", 0.5), None),
    (ProbeResult("reply", "1.1.1.1", 0.5), None),
    (ProbeResult("timeout"), None),
    (OSError("Network is unreachable"), None),
])
def test_gateway_from_the_first_router(result, gateway):
    backend = FakeBackend(lambda address, ttl: result)
    assert gateway_from_ttl_probe(timeout=0.05, open_backend=lambda: backend) == gateway
    assert backend.sent == [("1.1.1.1", 1, 0.05)] and backend.closed


def test_gateway_probe_without_icmp_is_none():
    def unavailable():
        raise IcmpUnavailable("no ICMP here")

    assert gateway_from_ttl_probe(timeout=0.05, open_backend=unavailable) is None


def test_gateway_probe_gives_up_after_its_timeout():
    import time

    backend = FakeBackend(path=FakePath([None]))  # the first router never answers: the probe takes its timeout
    started = time.monotonic()
    assert gateway_from_ttl_probe(timeout=0.2, open_backend=lambda: backend) is None
    assert time.monotonic() - started < 1.5


async def test_gateway_probe_works_inside_a_running_loop():
    backend = FakeBackend(lambda address, ttl: ProbeResult("ttl_exceeded", "192.168.7.1", 0.4))
    assert gateway_from_ttl_probe(timeout=0.2, open_backend=lambda: backend) == "192.168.7.1"


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="Linux reads /proc/net/route")
def test_load_config_without_targets_does_not_hang_off_linux(tmp_path):
    import time

    from pengumon.config import load_config

    started = time.monotonic()
    config = load_config(str(tmp_path / "none.yaml"))
    assert time.monotonic() - started < netinfo.GATEWAY_PROBE_TIMEOUT + 2.0
    assert [t.id for t in config.targets][-2:] == ["cloudflare_dns", "google_dns"]


async def test_probe_round_staggers_starts_and_keeps_order(monkeypatch):
    import asyncio

    from pengumon.models.probes import ProbeSample

    prober = NetworkProber(stagger_seconds=0.02, backend=FakeBackend())
    loop = asyncio.get_running_loop()
    started = {}

    async def fake_icmp(target):
        started[target.id] = loop.time()
        return ProbeSample(target_id=target.id, success=True, latency_ms=1.0)

    monkeypatch.setattr(prober, "probe_icmp", fake_icmp)
    targets = [TargetConfig(id=f"t{i}", name=f"T{i}", address=f"192.0.2.{i + 1}", category="public") for i in range(4)]
    samples = await prober.execute_probe_round(targets=targets, dns_probes=[])

    assert [s.target_id for s in samples] == ["t0", "t1", "t2", "t3"]
    gaps = [started[f"t{i + 1}"] - started[f"t{i}"] for i in range(3)]
    if sys.platform == "win32":  # loop.time() moves in ~15.6 ms ticks there (GetTickCount64): check the spread
        assert all(0 <= g <= 0.08 for g in gaps) and 0.03 <= sum(gaps) <= 0.2, gaps
    else:
        # asyncio.sleep may wake a little early or late (macOS): bound each gap loosely and the spread of the starts
        # more tightly; with no stagger the starts are within ~1 ms and the spread bound fails
        assert all(0.005 <= g <= 0.08 for g in gaps) and 0.04 <= sum(gaps) <= 0.1, gaps


@pytest.mark.skipif(sys.platform not in ("linux", "darwin", "win32"), reason="no ICMP backend for this OS")
async def test_prober_sample_from_the_real_backend():
    import os

    try:
        icmp_select.shared_backend()
    except IcmpUnavailable as e:
        if os.environ.get("PENGUMON_REQUIRE_ICMP"):
            raise
        pytest.skip(f"ICMP unavailable here: {e}")
    prober = NetworkProber(ping_timeout=1.0)
    sample = await prober.probe_icmp(_target("127.0.0.1"))
    assert sample.success and sample.latency_ms is not None and 0 <= sample.latency_ms < 50
    assert prober.probe_method()["backend"] in ("linux-raw", "linux-dgram", "macos-dgram", "windows-icmpapi")
