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


def test_a_custom_threshold_changes_the_colour():
    assert _probe(_prober_with("local"), "t", [6.0] * 3) == "up"
    assert _probe(_prober_with("local", local_latency_ms=5.0), "t", [6.0] * 3) == "degraded"
    assert _probe(_prober_with("gateway", gateway_jitter_ms=5.0), "t", [10.0, 20.0, 10.0, 20.0]) == "degraded"
    assert _probe(_prober_with("dns", degraded_loss_fraction=0.5, down_loss_fraction=0.9), "t", [None] * 4 + [10.0] * 6) == "up"


def test_isp_and_custom_targets_are_never_latency_degraded():
    assert _probe(_prober_with("isp"), "t", [500.0, 900.0, 100.0]) == "up"
    assert _probe(_prober_with("custom"), "t", [500.0, 900.0, 100.0]) == "up"
    assert _probe(_prober_with("public"), "t", [500.0, 900.0, 100.0]) == "degraded"
