"""Traceroute through the ICMP backend: TTL 1..max_hops, where it stops, how hops are summed up and labeled."""

import asyncio
import os
import sys
import time

import pytest

from icmp_fakes import FakeBackend, FakePath, results_by_ttl
from pengumon.engine import mtr
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.mtr import PROBES_PER_HOP, MtrEngine, summarize_hops
from pengumon.models.probes import TargetConfig

R1, R2, R3 = "192.168.1.1", "198.51.100.1", "198.51.100.2"


def hops_of(trace):
    return [(h.hop_number, h.ip, h.rtt_ms, h.label) for h in trace]


async def test_trace_reaches_the_target_and_stops_there():
    backend = FakeBackend(path=FakePath([R1, R2, R3]))
    trace = await MtrEngine("1.1.1.1", max_hops=15, timeout=0.2, backend=backend).run_trace()
    assert hops_of(trace) == [
        (1, R1, 1.0, "Hop 1 (private)"),
        (2, R2, 2.0, "Hop 2"),
        (3, R3, 3.0, "Hop 3"),
        (4, "1.1.1.1", 4.0, "Hop 4"),
    ]
    # every TTL was asked PROBES_PER_HOP times, all to the target's address
    assert results_by_ttl(backend) == {ttl: PROBES_PER_HOP for ttl in range(1, 16)}
    assert {address for address, _ttl, _timeout in backend.sent} == {"1.1.1.1"}


async def test_probes_go_out_together_so_a_long_path_takes_about_one_timeout():
    path = FakePath([None] * 15, target_answers=False)  # nothing answers: the slowest case
    backend = FakeBackend(path=path)
    started = time.monotonic()
    await MtrEngine("192.0.2.80", max_hops=15, timeout=0.3, backend=backend).run_trace()
    assert time.monotonic() - started < 0.3 + 1.0
    assert backend.max_in_flight == 15 * PROBES_PER_HOP


async def test_silent_hops_are_kept_and_the_end_is_trimmed():
    backend = FakeBackend(path=FakePath([R1, None, R3, None, None], target_answers=False))
    trace = await MtrEngine("192.0.2.80", max_hops=10, timeout=0.05, backend=backend).run_trace()
    # up to the last router that answered, then one timed-out hop: the path goes on unseen
    assert [(h.hop_number, h.ip, h.label) for h in trace] == [
        (1, R1, "Hop 1 (private)"),
        (2, None, "Request Timed Out"),
        (3, R3, "Hop 3"),
        (4, None, "Request Timed Out"),
    ]


async def test_unreachable_ends_the_trace_at_the_router_that_said_so():
    backend = FakeBackend(path=FakePath([R1, R2, R3], unreachable_at=2))
    trace = await MtrEngine("192.0.2.80", max_hops=10, timeout=0.05, backend=backend).run_trace()
    assert [(h.hop_number, h.ip) for h in trace] == [(1, R1), (2, R2)]


async def test_known_targets_name_their_hop():
    backend = FakeBackend(path=FakePath([R1, R2, R3]))
    known = [TargetConfig(id="gw", name="My router", address=R1, category="local")]
    trace = await MtrEngine("8.8.8.8", timeout=0.05, backend=backend, known_targets=known).run_trace()
    assert [h.label for h in trace] == ["My router", "Hop 2", "Hop 3",
                                        "Hop 4"]


def test_hop_labels_name_the_position_and_address_class_and_never_guess_the_equipment():
    label = MtrEngine("1.1.1.1", known_targets=[TargetConfig(id="m", name="Modem", address="192.168.100.1",
                                                             category="gateway")])._label_hop
    assert [label(n, ip) for n, ip in [(1, "192.168.1.1"), (2, "10.0.0.1"), (3, "100.64.0.1"), (4, "169.254.0.1"),
                                       (5, "203.0.113.1"), (6, "1.1.1.1"), (2, "192.168.100.1"), (3, None)]] == [
        "Hop 1 (private)", "Hop 2 (private)", "Hop 3 (CGNAT)", "Hop 4 (link-local)", "Hop 5", "Hop 6", "Modem",
        "Request Timed Out"]


def test_a_hop_answered_by_two_routers_shows_the_one_that_answered_most():
    results = [
        [ProbeResult("ttl_exceeded", R2, 4.0), ProbeResult("ttl_exceeded", R3, 9.0), ProbeResult("ttl_exceeded", R2, 6.0)],
        [ProbeResult("reply", "192.0.2.80", 7.0), ProbeResult("timeout"), ProbeResult("reply", "192.0.2.80", 8.0)],
    ]
    assert summarize_hops(results, "192.0.2.80") == [(1, R2, 5.0), (2, "192.0.2.80", 7.5)]


def test_a_reply_from_somewhere_else_is_not_the_end():
    results = [[ProbeResult("reply", R1, 1.0)], [ProbeResult("reply", "192.0.2.80", 2.0)]]
    assert summarize_hops(results, "192.0.2.80") == [(1, R1, 1.0), (2, "192.0.2.80", 2.0)]


def test_nothing_answered_is_one_timed_out_hop():
    assert summarize_hops([[ProbeResult("timeout")]] * 5, "192.0.2.80") == [(1, None, None)]


async def test_a_failed_send_counts_as_a_silent_probe():
    def answer(address, ttl):
        return OSError("No buffer space available") if ttl == 2 else FakePath([R1, R2]).answer(address, ttl)

    trace = await MtrEngine("192.0.2.80", max_hops=5, timeout=0.05, backend=FakeBackend(answer)).run_trace()
    assert [(h.hop_number, h.ip) for h in trace] == [(1, R1), (2, None), (3, "192.0.2.80")]


async def test_a_name_is_resolved_to_ipv4_first():
    backend = FakeBackend(path=FakePath([]))
    trace = await MtrEngine("localhost", timeout=0.5, backend=backend).run_trace()
    assert backend.sent and {address for address, _ttl, _timeout in backend.sent} == {"127.0.0.1"}
    assert [(h.hop_number, h.ip) for h in trace] == [(1, "127.0.0.1")]


async def test_no_trace_for_an_unknown_name():
    backend = FakeBackend()
    assert await MtrEngine("no-such-host.invalid", timeout=0.5, backend=backend).run_trace() == []
    assert backend.sent == []


async def test_no_trace_when_icmp_is_unavailable(monkeypatch):
    def unavailable():
        raise IcmpUnavailable("cannot open an ICMP socket", "the fix")

    monkeypatch.setattr(mtr, "shared_backend", unavailable)
    assert await MtrEngine("192.0.2.80", timeout=0.1).run_trace() == []


def test_max_hops_is_bounded():
    for bad in (0, 256):
        with pytest.raises(ValueError):
            MtrEngine("192.0.2.80", max_hops=bad)


# ---- the real backend: a one-hop trace to loopback (every OS the CI runs on) ------------------------------------

@pytest.mark.skipif(sys.platform not in ("linux", "darwin", "win32"), reason="no ICMP backend for this OS")
async def test_real_trace_to_loopback_is_one_hop():
    from pengumon.engine.icmp_select import open_backend

    try:
        backend = open_backend()
    except IcmpUnavailable as e:
        if os.environ.get("PENGUMON_REQUIRE_ICMP"):
            raise
        pytest.skip(f"ICMP unavailable here: {e}")
    try:
        trace = await asyncio.wait_for(MtrEngine("127.0.0.1", max_hops=4, timeout=1.0, backend=backend).run_trace(), 10)
    finally:
        backend.close()
    assert [(h.hop_number, h.ip) for h in trace] == [(1, "127.0.0.1")]
    assert trace[0].rtt_ms is not None and trace[0].rtt_ms >= 0


async def test_only_one_trace_runs_at_a_time():
    """The incident path trace and /api/mtr/trace never overlap: a trace to another target waits for the running one."""
    backend = FakeBackend(path=FakePath([R1, R2], target_answers=True), delay=0.05)
    first = asyncio.ensure_future(MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend).run_trace())
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(MtrEngine("192.0.2.81", max_hops=5, timeout=0.2, backend=backend).run_trace())
    await asyncio.sleep(0.01)
    assert {a for a, _t, _o in backend.sent} == {"192.0.2.80"}  # the second has not sent anything yet
    await asyncio.gather(first, second)
    assert backend.max_in_flight == 5 * PROBES_PER_HOP  # never two traces' worth
    assert {a for a, _t, _o in backend.sent} == {"192.0.2.80", "192.0.2.81"}
    assert mtr._running == {}


async def test_a_second_trace_to_the_same_target_reuses_the_running_one():
    backend = FakeBackend(path=FakePath([R1, R2]), delay=0.05)
    engines = [MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend) for _ in range(3)]
    traces = await asyncio.gather(*(e.run_trace() for e in engines))
    assert len(backend.sent) == 5 * PROBES_PER_HOP  # one trace's probes in all
    assert traces[0] == traces[1] == traces[2] and traces[0]


async def test_a_caller_that_goes_away_does_not_cancel_the_shared_trace():
    backend = FakeBackend(path=FakePath([R1, R2]), delay=0.05)
    a = asyncio.ensure_future(MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend).run_trace())
    b = asyncio.ensure_future(MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend).run_trace())
    await asyncio.sleep(0.01)
    a.cancel()
    assert len(await b) == 3


class _Policer:
    """A provider that drops ICMPv6 probes arriving less than `gap` s after the previous one (seen live: bursts lost
    everything past the first router answer, the target included)."""

    def __init__(self, path, gap):
        self.path, self.gap, self.last = path, gap, None

    def answer(self, address, ttl):
        now = time.monotonic()
        dropped = self.last is not None and now - self.last < self.gap
        self.last = now
        return ProbeResult("timeout") if dropped else self.path.answer(address, ttl)


async def test_an_ipv6_trace_is_paced_so_a_policing_provider_still_answers():
    target = "2001:db8:ffff::1"
    policer = _Policer(FakePath(["2001:db8::a", "2001:db8::b", "2001:db8::c"]), gap=0.05)
    hops = await MtrEngine(target, max_hops=6, timeout=0.3, backend=FakeBackend(answer=policer.answer), family=6).run_trace()
    assert hops and hops[-1].ip == target
    assert [h.ip for h in hops[:3]] == ["2001:db8::a", "2001:db8::b", "2001:db8::c"]


async def test_a_probe_that_fails_in_any_way_is_a_silent_probe_not_a_failed_trace_R4_L12():
    def answer(address, ttl):
        return RuntimeError("backend closed") if ttl == 2 else FakePath([R1, R2]).answer(address, ttl)

    trace = await MtrEngine("192.0.2.80", max_hops=5, timeout=0.05, backend=FakeBackend(answer)).run_trace()
    assert [(h.hop_number, h.ip) for h in trace] == [(1, R1), (2, None), (3, "192.0.2.80")]


async def test_callers_with_other_known_targets_get_their_own_labels_R4_L12():
    backend = FakeBackend(path=FakePath([R1, R2]), delay=0.05)
    known = [TargetConfig(id="gw", name="My router", address=R1, category="local")]
    plain, named = await asyncio.gather(
        MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend).run_trace(),
        MtrEngine("192.0.2.80", max_hops=5, timeout=0.2, backend=backend, known_targets=known).run_trace())
    assert plain[0].label == "Hop 1 (private)" and named[0].label == "My router"
