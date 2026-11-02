"""Unit tests for the kernel-timestamped ICMP pinger."""

import asyncio

import pytest

from pengumon.engine import icmp
from pengumon.engine.icmp import KernelTimestampPinger, _checksum
from pengumon.engine.prober import NetworkProber
from pengumon.models.probes import TargetConfig


KINDS = [None, "raw", "dgram"]


def _pinger_or_skip(kind=None):
    try:
        return KernelTimestampPinger(kind)
    except OSError as e:  # not Linux, no CAP_NET_RAW, or ping_group_range excludes us
        pytest.skip(f"ICMP socket ({kind or 'any'}) unavailable: {e}")


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        KernelTimestampPinger("icmp")


def test_checksum():
    # echo request, id 1, seq 1, no data: 0x0800 + 0x0001 + 0x0001 = 0x0802 -> ~ = 0xF7FD
    assert _checksum(b"\x08\x00\x00\x00\x00\x01\x00\x01") == 0xF7FD
    assert _checksum(b"\x08\x00\x00\x00\x00\x01\x00") == _checksum(b"\x08\x00\x00\x00\x00\x01\x00\x00")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_ping_loopback_uses_kernel_timestamp(kind):
    pinger = _pinger_or_skip(kind)
    assert pinger.kind == (kind or pinger.kind) and pinger.kind in ("raw", "dgram")
    try:
        rtts = await asyncio.gather(*[pinger.ping("127.0.0.1", 1.0) for _ in range(5)])
    finally:
        pinger.close()
    assert all(r is not None and 0 <= r < 50 for r in rtts)
    assert pinger.kernel_timestamps == 5 and pinger.fallback_timestamps == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("step_ms", [-500, 2000])
async def test_wall_clock_step_between_send_and_reply_is_not_measured(monkeypatch, step_ms):
    """An NTP step (timesyncd's first sync after boot) between the send and the kernel's receive stamp used to
    turn into a negative RTT (clock set back) or a false spike (set forward); the monotonic RTT is used instead."""
    pinger = _pinger_or_skip()
    real_time_ns = icmp.time.time_ns
    # The send stamp is read on the old clock; the kernel stamps the reply on the stepped one.
    monkeypatch.setattr(icmp.time, "time_ns", lambda: real_time_ns() - step_ms * 1_000_000)
    try:
        rtt = await pinger.ping("127.0.0.1", 1.0)
    finally:
        monkeypatch.setattr(icmp.time, "time_ns", real_time_ns)
        pinger.close()
    assert rtt is not None and 0 <= rtt < 50
    assert pinger.clock_step_fallbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_ping_timeout_returns_none(kind):
    pinger = _pinger_or_skip(kind)
    try:
        assert await pinger.ping("192.0.2.1", 0.3) is None  # TEST-NET-1, never answers
    finally:
        pinger.close()


@pytest.mark.asyncio
async def test_probe_round_staggers_starts_and_keeps_order(monkeypatch):
    prober = NetworkProber(stagger_seconds=0.02)
    loop = asyncio.get_running_loop()
    started = {}

    async def fake_icmp(target):
        started[target.id] = loop.time()
        from pengumon.models.probes import ProbeSample
        return ProbeSample(target_id=target.id, success=True, latency_ms=1.0)

    monkeypatch.setattr(prober, "probe_icmp", fake_icmp)
    targets = [TargetConfig(id=f"t{i}", name=f"T{i}", address=f"192.0.2.{i + 1}", category="public") for i in range(4)]
    samples = await prober.execute_probe_round(targets=targets, dns_probes=[])

    assert [s.target_id for s in samples] == ["t0", "t1", "t2", "t3"]
    gaps = [started[f"t{i + 1}"] - started[f"t{i}"] for i in range(3)]
    assert all(0.015 <= g <= 0.05 for g in gaps)


@pytest.mark.asyncio
async def test_prober_icmp_sample():
    _pinger_or_skip().close()
    prober = NetworkProber(ping_timeout=1.0)
    target = TargetConfig(id="lo", name="Loopback", address="127.0.0.1", category="local")
    sample = await prober.probe_icmp(target)
    assert sample.success and sample.latency_ms is not None and sample.latency_ms < 50
    assert prober._kernel_pinger is not None
