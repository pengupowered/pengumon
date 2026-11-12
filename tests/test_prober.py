"""Unit tests for NetworkProber and DNS probers."""

import pytest
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.models.probes import TargetConfig
from pengumon.engine.prober import NetworkProber


@pytest.mark.asyncio
async def test_prober_execution():
    prober = NetworkProber(ping_timeout=1.0, history_window=10)
    
    # Register targets
    dns_target = DnsProbeConfig(
        id="test_dns",
        name="Test Google DNS",
        domain="google.com",
        record_type="A",
        enabled=True
    )
    
    sample = await prober.probe_dns(dns_target)
    assert sample.target_id == "test_dns"
    # DNS lookup to google.com should succeed if connected
    if sample.success:
        assert sample.latency_ms is not None
        assert sample.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_prober_stats_update():
    prober = NetworkProber(ping_timeout=1.0, history_window=5)
    target = TargetConfig(id="t1", name="Target 1", address="1.1.1.1", category="public")
    prober.register_target(target)

    # Simulate 3 successful samples
    prober.execute_probe_round
    from pengumon.models.probes import ProbeSample
    prober._update_target_stats(ProbeSample(target_id="t1", success=True, latency_ms=10.0))
    prober._update_target_stats(ProbeSample(target_id="t1", success=True, latency_ms=20.0))
    prober._update_target_stats(ProbeSample(target_id="t1", success=True, latency_ms=30.0))

    stat = prober.stats["t1"]
    assert stat.sent == 3
    assert stat.received == 3
    assert stat.min_ms == 10.0
    assert stat.max_ms == 30.0
    assert stat.avg_ms == 20.0
    assert stat.jitter_ms == 10.0
    assert stat.loss_pct == 0.0


# --- per-hop status thresholds ---------------------------------------------------------------------------

def _probe(prober, target_id, latencies):
    from pengumon.models.probes import ProbeSample
    for value in latencies:
        prober._update_target_stats(ProbeSample(target_id=target_id, success=value is not None, latency_ms=value))
    return prober.stats[target_id].status


def _prober_with(category, **kwargs):
    from pengumon.engine.prober_settings import HopStatusConfig
    prober = NetworkProber(status_thresholds=HopStatusConfig(**kwargs) if kwargs else None)
    prober.register_target(TargetConfig(id="t", name="T", category=category, address="192.168.100.1"))
    return prober


def test_hop_status_defaults_equal_the_former_literals():
    from pengumon.engine.prober_settings import HopStatusConfig
    t = HopStatusConfig()
    assert (t.down_loss_fraction, t.degraded_loss_fraction) == (0.4, 0.1)
    assert (t.local_latency_ms, t.local_jitter_ms) == (20.0, 10.0)
    assert (t.gateway_latency_ms, t.gateway_jitter_ms) == (30.0, 15.0)
    assert (t.public_latency_ms, t.public_jitter_ms) == (60.0, 25.0)
    assert (t.dns_latency_ms, t.dns_jitter_ms) == (100.0, 40.0)
    assert t.latency_limits("local") == (20.0, 10.0)
    assert t.latency_limits("isp") is None and t.latency_limits("custom") is None
    assert NetworkProber().status_thresholds == t


def test_loss_fractions_are_strict_and_stay_fractions():
    # 4 of 10 lost is exactly 0.4: degraded, not down (as a percentage 0.4 * 100 would be 40.00000000000001)
    prober = _prober_with("public")
    assert _probe(prober, "t", [None] * 4 + [10.0] * 6) == "degraded"
    prober = _prober_with("public")
    assert _probe(prober, "t", [None] * 5 + [10.0] * 5) == "down"
    prober = _prober_with("public")
    assert _probe(prober, "t", [None] + [10.0] * 9) == "up"                      # exactly 0.1: not degraded
    prober = _prober_with("public")
    assert _probe(prober, "t", [None] * 2 + [10.0] * 8) == "degraded"


def test_a_custom_threshold_changes_the_color():
    assert _probe(_prober_with("local"), "t", [6.0] * 3) == "up"
    assert _probe(_prober_with("local", local_latency_ms=5.0), "t", [6.0] * 3) == "degraded"
    assert _probe(_prober_with("gateway", gateway_jitter_ms=5.0), "t", [10.0, 20.0, 10.0, 20.0]) == "degraded"
    assert _probe(_prober_with("dns", degraded_loss_fraction=0.5, down_loss_fraction=0.9), "t", [None] * 4 + [10.0] * 6) == "up"


def test_isp_and_custom_targets_are_never_latency_degraded():
    assert _probe(_prober_with("isp"), "t", [500.0, 900.0, 100.0]) == "up"
    assert _probe(_prober_with("custom"), "t", [500.0, 900.0, 100.0]) == "up"
    assert _probe(_prober_with("public"), "t", [500.0, 900.0, 100.0]) == "degraded"


def _dns_cfg():
    return DnsProbeConfig(id="d", name="D", domain="example.com", record_type="A", nameserver="192.0.2.53", enabled=True)


async def test_dns_probe_does_not_read_the_system_resolver_configuration(monkeypatch):
    import dns.asyncresolver
    import dns.resolver

    def configured(self, *a, **k):
        raise AssertionError("the probe read the system's resolver configuration")

    async def resolve(self, domain, record_type):
        assert self.nameservers == ["192.0.2.53"]
        return ["192.0.2.1"]

    monkeypatch.setattr(dns.resolver.BaseResolver, "read_resolv_conf", configured)
    monkeypatch.setattr(dns.resolver.BaseResolver, "read_registry", configured, raising=False)
    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)
    sample = await NetworkProber(ping_timeout=1.0, history_window=10).probe_dns(_dns_cfg())
    assert sample.success and sample.packet_loss_pct == 0.0 and sample.latency_ms >= 0.0


async def test_dns_probe_answers_empty_and_failed_resolutions_as_lost(monkeypatch):
    import dns.asyncresolver

    answers = []

    async def resolve(self, domain, record_type):
        if answers == "boom":
            raise RuntimeError("boom")
        return answers

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)
    prober = NetworkProber(ping_timeout=1.0, history_window=10)
    empty = await prober.probe_dns(_dns_cfg())
    assert (empty.success, empty.latency_ms, empty.packet_loss_pct, empty.error) == (False, None, 100.0, "No DNS records returned")
    answers = "boom"
    failed = await prober.probe_dns(_dns_cfg())
    assert (failed.success, failed.packet_loss_pct, failed.error) == (False, 100.0, "DNS Error: boom")


# --- TCP connect probes (engine/probe_tcp.py through NetworkProber) --------------------------------------------------

import asyncio  # noqa: E402

from icmp_fakes import FakeBackend  # noqa: E402
from pengumon.engine.icmp_base import IcmpUnavailable  # noqa: E402
from pengumon.engine.netinfo import IPV6_UNAVAILABLE, Ipv6Availability  # noqa: E402


@pytest.fixture
async def listener():
    accepted = []

    async def handle(reader, writer):
        accepted.append(1)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.accepted = accepted
    yield server, server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def tcp_target(port, **kw):
    base = dict(id="web", name="Web", category="public", address="127.0.0.1", probe="tcp", port=port)
    base.update(kw)
    return TargetConfig(**base)


async def test_a_round_probes_a_ping_hop_and_a_tcp_hop_together(listener):
    _, port = listener
    prober = NetworkProber(ping_timeout=1.0, backend=FakeBackend(), socket_policy=lambda a: None)
    ping = TargetConfig(id="gw", name="Gateway", category="gateway", address="192.0.2.1")
    samples = await prober.execute_probe_round([ping, tcp_target(port)], [])
    by_id = {s.target_id: s for s in samples}
    assert set(by_id) == {"gw", "web"} and by_id["web"].success and 0 < by_id["web"].latency_ms < 50
    assert by_id["gw"].latency_ms == 1.0
    stats = prober.stats["web"]
    assert (stats.probe, stats.probe_label, stats.sent, stats.received) == ("tcp", f"TCP {port}", 1, 1)
    assert prober.stats["gw"].probe == "icmp" and prober.stats["gw"].probe_label is None


async def test_the_default_policy_never_connects_to_loopback(listener):
    server, port = listener
    prober = NetworkProber(ping_timeout=1.0, backend=FakeBackend())
    samples = await prober.execute_probe_round([tcp_target(port)], [])
    assert not samples[0].success and samples[0].latency_ms is None
    assert samples[0].error.startswith("Not probed: 127.0.0.1 is a loopback address")
    await asyncio.sleep(0.05)
    assert server.accepted == []  # nothing connected


async def test_a_name_that_resolves_to_loopback_is_refused_at_the_probe(listener, monkeypatch):
    server, port = listener

    async def resolve(host, timeout, family=4):
        return "127.0.0.1", 4
    monkeypatch.setattr("pengumon.engine.prober.resolve_address", resolve)
    prober = NetworkProber(backend=FakeBackend())
    a = await prober.socket_attempt(tcp_target(port, address="rebind.example.com"))
    assert a["kind"] == "not_allowed" and a["address"] == "127.0.0.1"
    assert a["error"].startswith("Not probed: rebind.example.com resolves to 127.0.0.1, a loopback address")
    assert server.accepted == []


async def test_a_refusal_and_a_dead_host_are_losses_with_their_words():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    prober = NetworkProber(ping_timeout=5.0, backend=FakeBackend(), socket_policy=lambda a: None)  # Windows reports a refusal after ~2 s
    sample = await prober.probe_target(tcp_target(port))
    assert not sample.success and sample.error == f"Connection refused on port {port}"


def test_set_targets_restarts_a_hop_whose_port_changed_and_keeps_one_that_was_renamed():
    prober = NetworkProber()
    prober.set_targets([tcp_target(443)])
    prober.stats["web"].sent = 30
    prober.set_targets([tcp_target(443, name="Renamed")])
    assert prober.stats["web"].sent == 30 and prober.stats["web"].name == "Renamed"
    prober.set_targets([tcp_target(8443)])
    assert prober.stats["web"].sent == 0 and prober.stats["web"].probe_label == "TCP 8443"
    prober.set_targets([TargetConfig(id="web", name="Web", category="public", address="127.0.0.1")])
    assert prober.stats["web"].probe == "icmp" and prober.stats["web"].probe_label is None


def test_without_pings_a_path_of_tcp_hops_is_still_probed_and_only_ping_hops_are_blind():
    def broken():
        raise IcmpUnavailable("cannot open an ICMP socket", "the fix")
    prober = NetworkProber()
    prober.backend = broken
    ping = TargetConfig(id="gw", name="Gateway", category="gateway", address="192.0.2.1")
    prober.set_targets([tcp_target(443)])
    assert prober.probing_problem() is None
    assert prober.stats["web"].probing_unavailable is False
    prober.set_targets([ping, tcp_target(443)])
    assert prober.probing_problem() == ("cannot open an ICMP socket", "the fix")
    assert prober.stats["gw"].probing_unavailable is True and prober.stats["web"].probing_unavailable is False
    prober.set_targets([ping.model_copy(update={"enabled": False}), tcp_target(443)])
    assert prober.probing_problem() is None  # the only enabled hop uses a socket


async def test_an_ipv6_tcp_hop_without_a_route_is_not_probed_and_says_so():
    prober = NetworkProber(backend=FakeBackend(), ipv6=Ipv6Availability(check=lambda: "no route"),
                           socket_policy=lambda a: None)
    target = tcp_target(443, address="example.com", family=6)
    prober.register_target(target)
    assert await prober.probe_target(target) is None
    assert prober.stats["web"].ipv6_unavailable is True
    a = await prober.socket_attempt(target)
    assert (a["kind"], a["error"]) == ("ipv6_unavailable", IPV6_UNAVAILABLE)


async def test_a_disabled_tcp_hop_is_not_probed(listener):
    server, port = listener
    prober = NetworkProber(backend=FakeBackend(), socket_policy=lambda a: None)
    assert await prober.probe_target(tcp_target(port, enabled=False)) is None
    assert server.accepted == []


@pytest.mark.parametrize("kernel_timing, who", [(True, "the kernel: the same round trip"), (False, "Pengumon: can read a few ms high")])
def test_the_probe_note_says_what_the_figure_is(monkeypatch, kernel_timing, who):
    """Both wordings on every system: whether the kernel's handshake time is available is injected."""
    monkeypatch.setattr("pengumon.engine.prober.KERNEL_TIMING", kernel_timing)
    prober = NetworkProber()
    prober.register_target(tcp_target(443))
    assert prober.stats["web"].probe_note.startswith(f"TCP handshake to port 443, timed by {who}")


def test_the_probe_note_on_this_system_matches_what_it_can_do():
    prober = NetworkProber()
    prober.register_target(tcp_target(443))
    from pengumon.engine.probe_tcp import KERNEL_TIMING
    assert ("timed by the kernel" in prober.stats["web"].probe_note) is KERNEL_TIMING


async def test_an_http_hop_is_probed_through_the_prober_and_a_5xx_is_a_loss():
    answers = [b"HTTP/1.1 200 OK\r\n\r\n", b"HTTP/1.1 503 Busy\r\n\r\n"]
    requests = []

    async def handle(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(answers[len(requests) - 1])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        prober = NetworkProber(ping_timeout=1.0, backend=FakeBackend(), socket_policy=lambda a: None)
        site = TargetConfig(id="site", name="Site", category="custom", address="127.0.0.1", probe="http", port=port,
                            http_scheme="http", http_path="/health")
        ok, bad = await prober.probe_target(site), await prober.probe_target(site)
    finally:
        server.close()
        await server.wait_closed()
    assert ok.success and ok.latency_ms > 0
    assert not bad.success and bad.error == "HTTP 503 from the server"
    assert requests[0].startswith(b"HEAD /health HTTP/1.1\r\nHost: 127.0.0.1:")
    a = prober._probe_note(site)
    assert a.startswith("Time to the server's answer to HTTP HEAD /health ") and "never judged as a stage" in a
