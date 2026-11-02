"""Tests for the Windows ICMP backend (IcmpSendEcho2 through ctypes).

Most tests run everywhere with a fake iphlpapi: a Python function that writes a crafted ICMP_ECHO_REPLY into the
buffer the backend passes. The tests at the end need real Windows and are skipped elsewhere.
"""

import asyncio
import os
import ctypes
import ipaddress
import sys
import threading
import time

import pytest

from pengumon.engine import icmp_windows as w
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_windows import (
    ICMP_ECHO_REPLY,
    ICMP_ECHO_REPLY32,
    IP_OPTION_INFORMATION,
    WindowsIcmpBackend,
    parse_reply,
)

TARGET = "192.0.2.10"
ROUTER = "198.51.100.1"


def _ip_dword(addr: str) -> int:
    return int.from_bytes(ipaddress.IPv4Address(addr).packed, sys.byteorder)


class FakeDll:
    """Stands in for iphlpapi: `script(call)` returns (count, status, responder, os_rtt_ms) or ("error", win32)."""

    def __init__(self, script=None):
        self.script = script or (lambda call: (1, w.IP_SUCCESS, call["target"], 0))
        self.calls = []
        self.created = []
        self.closed = []
        self.errno = 0
        self._next = 100
        self._lock = threading.Lock()
        self.create_fails = False
        self.in_flight = 0
        self.max_in_flight = 0

    def IcmpCreateFile(self):
        if self.create_fails:
            self.errno = 5
            return 0xFFFFFFFFFFFFFFFF
        with self._lock:
            self._next += 1
            self.created.append(self._next)
            return self._next

    def IcmpCloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def IcmpSendEcho2(self, handle, event, apc, ctx, dest, request, size, options, buf, bufsize, timeout):
        assert event is None and apc is None and ctx is None  # synchronous call
        call = {
            "handle": handle, "dest": dest, "size": size, "request": bytes(request.raw[:size]),
            "ttl": options.contents.Ttl, "bufsize": bufsize, "timeout": timeout,
            "target": str(ipaddress.IPv4Address(dest.to_bytes(4, sys.byteorder))),
        }  # fmt: skip
        with self._lock:
            self.calls.append(call)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            out = self.script(call)
        finally:
            with self._lock:
                self.in_flight -= 1
        if out[0] == "error":
            self.errno = out[1]
            return 0
        count, status, responder, os_ms = out
        reply = ICMP_ECHO_REPLY()
        reply.Address = _ip_dword(responder)
        reply.Status = status
        reply.RoundTripTime = os_ms
        reply.DataSize = 0
        assert ctypes.sizeof(reply) <= bufsize
        ctypes.memmove(buf, ctypes.addressof(reply), ctypes.sizeof(reply))
        return count

    def last_error(self):
        return self.errno


def make(script=None, **kw):
    dll = FakeDll(script)
    return WindowsIcmpBackend(_dll=dll, _get_last_error=dll.last_error, **kw), dll


# ---- layout ----------------------------------------------------------------------------------------------------


def test_struct_offsets_native():
    # Offsets documented in ipexport.h; pointer fields follow the interpreter's width.
    ptr = ctypes.sizeof(ctypes.c_void_p)
    assert ICMP_ECHO_REPLY.Address.offset == 0
    assert ICMP_ECHO_REPLY.Status.offset == 4
    assert ICMP_ECHO_REPLY.RoundTripTime.offset == 8
    assert ICMP_ECHO_REPLY.DataSize.offset == 12
    assert ICMP_ECHO_REPLY.Data.offset == 16
    assert ICMP_ECHO_REPLY.Options.offset == 16 + ptr
    assert ctypes.sizeof(IP_OPTION_INFORMATION) == (16 if ptr == 8 else 8)
    assert ctypes.sizeof(ICMP_ECHO_REPLY) == (40 if ptr == 8 else 28)


def test_struct_layout_32bit_variant():
    assert ICMP_ECHO_REPLY32.Data.offset == 16
    assert ICMP_ECHO_REPLY32.Options.offset == 20
    assert ctypes.sizeof(ICMP_ECHO_REPLY32) == 28
    # the fields the parser reads are at the same place in both layouts
    for name in ("Address", "Status", "RoundTripTime", "DataSize"):
        assert getattr(ICMP_ECHO_REPLY, name).offset == getattr(ICMP_ECHO_REPLY32, name).offset


def test_buffer_size_follows_the_docs():
    # one ICMP_ECHO_REPLY + request data + 8 bytes for an ICMP error (+ IO_STATUS_BLOCK room)
    need = ctypes.sizeof(ICMP_ECHO_REPLY) + len(w.PAYLOAD) + 8
    assert w.REPLY_BUFFER_SIZE >= need + 16


def test_parse_reply_byte_exact():
    raw = bytearray(w.REPLY_BUFFER_SIZE)
    raw[0:4] = bytes([198, 51, 100, 1])
    raw[4:8] = (11013).to_bytes(4, sys.byteorder)
    raw[8:12] = (7).to_bytes(4, sys.byteorder)
    assert parse_reply(bytes(raw)) == ("198.51.100.1", 11013, 7)


@pytest.mark.parametrize("n", [0, 1, 15])
def test_parse_reply_truncated(n):
    with pytest.raises(ValueError):
        parse_reply(b"\x01" * n)


# ---- status mapping --------------------------------------------------------------------------------------------


async def test_reply():
    b, dll = make(lambda c: (1, w.IP_SUCCESS, c["target"], 3))
    r = await b.probe(TARGET, timeout=1.0)
    assert r.kind == "reply" and r.responder == TARGET
    assert 0 < r.rtt_ms <= 3.0
    call = dll.calls[0]
    assert call["dest"] == _ip_dword(TARGET)
    assert call["ttl"] == 64
    assert call["timeout"] == 1000
    assert call["size"] == len(w.PAYLOAD) == len(call["request"])
    assert call["bufsize"] == w.REPLY_BUFFER_SIZE
    b.close()


@pytest.mark.parametrize("status", [w.IP_TTL_EXPIRED_TRANSIT, w.IP_TTL_EXPIRED_REASSEM])
async def test_ttl_exceeded_reports_the_router(status):
    b, dll = make(lambda c: (1, status, ROUTER, 2))
    r = await b.probe(TARGET, timeout=1.0, ttl=3)
    assert r.kind == "ttl_exceeded" and r.responder == ROUTER and r.rtt_ms > 0
    assert dll.calls[0]["ttl"] == 3  # IP_OPTION_INFORMATION.Ttl reached the API
    b.close()


@pytest.mark.parametrize("status", [11002, 11003, 11004, 11005])
async def test_unreachable_family(status):
    b, _ = make(lambda c: (1, status, ROUTER, 1))
    r = await b.probe(TARGET, timeout=1.0)
    assert r.kind == "unreachable" and r.responder == ROUTER
    b.close()


async def test_timeout_status_in_reply():
    b, _ = make(lambda c: (1, w.IP_REQ_TIMED_OUT, "0.0.0.0", 0))
    assert await b.probe(TARGET, timeout=1.0) == ProbeResult("timeout")
    b.close()


async def test_timeout_from_last_error():
    b, _ = make(lambda c: ("error", w.IP_REQ_TIMED_OUT))
    assert await b.probe(TARGET, timeout=1.0) == ProbeResult("timeout")
    b.close()


async def test_unreachable_from_last_error():
    b, _ = make(lambda c: ("error", 11003))
    r = await b.probe(TARGET, timeout=1.0)
    assert r.kind == "unreachable" and r.responder is None
    b.close()


async def test_unknown_status_is_timeout_and_logged_once(caplog):
    b, _ = make(lambda c: (1, 11050, ROUTER, 0))
    with caplog.at_level("WARNING", logger=w.log.name):
        for _ in range(3):
            assert (await b.probe(TARGET, timeout=1.0)).kind == "timeout"
    assert sum("11050" in r.getMessage() for r in caplog.records) == 1
    b.close()


@pytest.mark.parametrize("err", [1231, 1232, 87, 8])
async def test_call_failure_raises_oserror(err):
    b, _ = make(lambda c: ("error", err))
    with pytest.raises(OSError):
        await b.probe(TARGET, timeout=1.0)
    b.close()


# ---- rtt -------------------------------------------------------------------------------------------------------


def test_rtt_picks_smaller_nonzero():
    assert w._rtt(0, 0.4) == 0.4  # sub-ms LAN hop: the stopwatch
    assert w._rtt(5, 5.3) == 5.0  # stopwatch includes thread wake-up: trust the OS floor
    assert w._rtt(5, 4.9) == 4.9
    assert w._rtt(0, 0.0) == 0.001  # never zero


async def test_rtt_is_sub_millisecond_when_os_says_zero():
    b, _ = make(lambda c: (1, w.IP_SUCCESS, c["target"], 0))
    r = await b.probe(TARGET, timeout=1.0)
    assert 0 < r.rtt_ms < 1000
    b.close()


# ---- validation ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("addr", ["example.com", "192.0.2", "192.0.2.1/24", "", "300.1.1.1", None, 3232235777,
                                  "2001:db8::1::2", "2001:db8::/64"])
async def test_rejects_non_ip_literals(addr):
    b, dll = make()
    with pytest.raises(ValueError):
        await b.probe(addr, timeout=1.0)
    assert dll.calls == []
    b.close()


@pytest.mark.parametrize("ttl", [0, -1, 256, 1.5, True, "64"])
async def test_rejects_bad_ttl(ttl):
    b, dll = make()
    with pytest.raises(ValueError):
        await b.probe(TARGET, timeout=1.0, ttl=ttl)
    assert dll.calls == []
    b.close()


@pytest.mark.parametrize("ttl", [1, 255])
async def test_ttl_bounds_accepted(ttl):
    b, dll = make()
    await b.probe(TARGET, timeout=1.0, ttl=ttl)
    assert dll.calls[0]["ttl"] == ttl
    b.close()


@pytest.mark.parametrize("secs,ms", [(0.0004, 1), (0.25, 250), (1.5, 1500), (10**9, 60_000)])
async def test_timeout_conversion(secs, ms):
    b, dll = make()
    await b.probe(TARGET, timeout=secs)
    assert dll.calls[0]["timeout"] == ms
    b.close()


# ---- concurrency, handles, close -------------------------------------------------------------------------------


async def test_concurrent_probes_each_get_their_own_answer():
    def script(c):
        time.sleep(0.01)
        return (1, w.IP_SUCCESS, c["target"], 1)

    b, dll = make(script, workers=8)
    targets = [f"192.0.2.{i}" for i in range(1, 41)]
    results = await asyncio.gather(*[b.probe(t, timeout=1.0) for t in targets])
    assert [r.responder for r in results] == targets  # no cross-talk
    assert 1 < dll.max_in_flight <= 8  # parallel, but bounded by the pool
    # one handle per worker thread, never shared between threads
    assert len(dll.created) <= 8 + 1
    assert {c["handle"] for c in dll.calls} <= set(dll.created)
    b.close()


async def test_pool_is_bounded():
    b, dll = make(lambda c: (1, 0, c["target"], 1), workers=3)
    await asyncio.gather(*[b.probe(TARGET, timeout=1.0) for _ in range(30)])
    assert len(set(c["handle"] for c in dll.calls)) <= 3
    b.close()


async def test_close_closes_every_handle_and_refuses_new_probes():
    b, dll = make(lambda c: (1, 0, c["target"], 1), workers=4)
    await asyncio.gather(*[b.probe(TARGET, timeout=1.0) for _ in range(12)])
    worker_handles = set(c["handle"] for c in dll.calls)
    b.close()
    assert worker_handles <= set(dll.closed)
    b.close()  # idempotent
    assert len(dll.closed) == len(set(dll.closed))
    with pytest.raises(OSError):
        await b.probe(TARGET, timeout=1.0)


async def test_a_trace_never_delays_the_probers_probes():
    """A 45-request trace (TTL 1..15) fills its own pool; probes at TTL 64 go to the other one and answer on time."""
    def script(c):
        time.sleep(0.3 if c["ttl"] < 64 else 0.0)
        return (1, w.IP_SUCCESS if c["ttl"] == 64 else w.IP_TTL_EXPIRED_TRANSIT, ROUTER if c["ttl"] < 64 else c["target"], 1)

    b, dll = make(script, workers=2, trace_workers=3)
    trace = [asyncio.ensure_future(b.probe(TARGET, timeout=1.0, ttl=1 + i % 15)) for i in range(45)]
    await asyncio.sleep(0.05)  # the trace holds every trace thread, with 42 more queued
    started = time.monotonic()
    results = await asyncio.gather(*[b.probe(f"192.0.2.{i}", timeout=1.0) for i in range(1, 9)])
    assert time.monotonic() - started < 0.25  # not one 0.3 s wave of the trace
    assert all(r.kind == "reply" for r in results)
    for t in trace:
        t.cancel()
    await asyncio.gather(*trace, return_exceptions=True)
    b.close()


def test_the_reply_buffer_has_room_for_ip_options():
    assert w.REPLY_BUFFER_SIZE >= 1536
    assert w.REPLY_BUFFER_SIZE >= ctypes.sizeof(ICMP_ECHO_REPLY) + len(w.PAYLOAD) + 8 + 40  # + max IP options


async def test_a_reply_from_someone_else_is_not_a_success():
    b, _ = make(lambda c: (1, w.IP_SUCCESS, ROUTER, 1))
    assert await b.probe(TARGET, timeout=1.0) == ProbeResult("timeout")
    b.close()


async def test_close_does_not_block_and_callers_get_oserror():
    def script(c):
        time.sleep(0.4)
        return (1, w.IP_SUCCESS, c["target"], 1)

    b, dll = make(script, workers=2)
    tasks = [asyncio.ensure_future(b.probe(TARGET, timeout=1.0)) for _ in range(6)]  # 2 running, 4 queued
    await asyncio.sleep(0.1)
    started = time.monotonic()
    b.close()
    assert time.monotonic() - started < 0.1  # the 0.3 s still left of the running calls is not waited for
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(type(o) is OSError for o in outcomes), outcomes  # not CancelledError, not a late "reply"
    for _ in range(50):  # the busy threads closed their own handles when their calls returned
        if sorted(dll.closed) == sorted(dll.created):
            break
        await asyncio.sleep(0.02)
    assert sorted(dll.closed) == sorted(dll.created)  # every handle ever opened is closed, once


def test_unavailable_when_create_fails():
    dll = FakeDll()
    dll.create_fails = True
    with pytest.raises(IcmpUnavailable) as e:
        WindowsIcmpBackend(_dll=dll, _get_last_error=dll.last_error)
    assert "5" in e.value.reason


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_constructor_refuses_off_windows():
    with pytest.raises(IcmpUnavailable):
        WindowsIcmpBackend()


def test_identity():
    b, _ = make()
    assert b.name == "windows-icmpapi" and b.resolution_ms == 1.0
    b.close()


def test_workers_bounds():
    for n in (0, 257):
        with pytest.raises(ValueError):
            make(workers=n)


# ---- real Windows only (run on GitHub's windows-latest) --------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows ICMP API")


@windows_only
async def test_real_loopback_reply():
    b = WindowsIcmpBackend()
    try:
        r = await b.probe("127.0.0.1", timeout=2.0, ttl=64)
    finally:
        b.close()
    assert r.kind == "reply" and r.responder == "127.0.0.1" and r.rtt_ms > 0


@windows_only
async def test_real_many_concurrent_loopback():
    b = WindowsIcmpBackend()
    try:
        rs = await asyncio.gather(*[b.probe("127.0.0.1", timeout=2.0) for _ in range(50)])
    finally:
        b.close()
    assert all(r.kind == "reply" for r in rs)


@windows_only
async def test_real_unroutable_does_not_crash():
    b = WindowsIcmpBackend()
    try:
        try:
            r = await b.probe("192.0.2.1", timeout=0.5)
        except OSError:
            return  # a runner without a route says so; that is a clean answer too
    finally:
        b.close()
    assert r.kind in ("timeout", "unreachable")


# ---- IPv6: Icmp6CreateFile / Icmp6SendEcho2, the ICMPV6_ECHO_REPLY layout ----------------------------------------

TARGET6 = "2001:db8:1::7"
ROUTER6 = "2001:db8::1"


def reply6_bytes(responder: str, status: int, rtt: int, scope_id: int = 0) -> bytes:
    """An ICMPV6_ECHO_REPLY byte by byte (ipexport.h): IPV6_ADDRESS_EX packed to 1 byte (sin6_port u16, sin6_flowinfo
    u32, sin6_addr 16 bytes, sin6_scope_id u32 = 26 bytes), 2 bytes of padding, Status u32, RoundTripTime u32."""
    order = sys.byteorder
    host = responder.partition("%")[0]
    return (b"\0\0" + b"\0\0\0\0" + ipaddress.IPv6Address(host).packed + scope_id.to_bytes(4, order) + b"\0\0"
            + status.to_bytes(4, order) + rtt.to_bytes(4, order))


class FakeDll6(FakeDll):
    """FakeDll with the IPv6 calls; `script(call)` gets call["target"] as text and call["version"] == 6."""

    def __init__(self, script=None, create6_fails=False):
        super().__init__(script)
        self.create6_fails = create6_fails
        self.created6 = []

    def Icmp6CreateFile(self):
        if self.create6_fails:
            self.errno = 50
            return 0xFFFFFFFFFFFFFFFF
        with self._lock:
            self._next += 1
            self.created6.append(self._next)
            return self._next

    def IcmpSendEcho2(self, *args):
        assert args[0] not in self.created6  # never an IPv6 handle for an IPv4 call
        return super().IcmpSendEcho2(*args)

    def Icmp6SendEcho2(self, handle, event, apc, ctx, source, dest, request, size, options, buf, bufsize, timeout):
        assert event is None and apc is None and ctx is None
        assert handle in self.created6
        src, dst = source._obj, dest._obj  # ctypes.byref objects
        assert src.sin6_family == dst.sin6_family == w.AF_INET6_WINDOWS
        assert bytes(src.sin6_addr) == bytes(16)  # in6addr_any
        call = {
            "version": 6, "handle": handle, "size": size, "request": bytes(request.raw[:size]),
            "ttl": options.contents.Ttl, "bufsize": bufsize, "timeout": timeout,
            "target": str(ipaddress.IPv6Address(bytes(dst.sin6_addr))), "scope_id": dst.sin6_scope_id,
        }  # fmt: skip
        with self._lock:
            self.calls.append(call)
        out = self.script(call)
        if out[0] == "error":
            self.errno = out[1]
            return 0
        count, status, responder, os_ms = out[:4]
        raw = reply6_bytes(responder, status, os_ms, out[4] if len(out) > 4 else 0)
        assert len(raw) == ctypes.sizeof(w.ICMPV6_ECHO_REPLY) <= bufsize
        ctypes.memmove(buf, raw, len(raw))
        return count


def make6(script=None, **kw):
    dll = FakeDll6(script, **kw)
    return WindowsIcmpBackend(_dll=dll, _get_last_error=dll.last_error), dll


def test_icmpv6_echo_reply_layout():
    assert ctypes.sizeof(w.IPV6_ADDRESS_EX) == 26
    assert w.IPV6_ADDRESS_EX.sin6_addr.offset == 6 and w.IPV6_ADDRESS_EX.sin6_scope_id.offset == 22
    assert w.ICMPV6_ECHO_REPLY.Status.offset == 28 and w.ICMPV6_ECHO_REPLY.RoundTripTime.offset == 32
    assert ctypes.sizeof(w.ICMPV6_ECHO_REPLY) == 36
    assert ctypes.sizeof(w.SOCKADDR_IN6) == 28 and w.SOCKADDR_IN6.sin6_scope_id.offset == 24
    assert w.REPLY_BUFFER_SIZE >= 36 + len(w.PAYLOAD) + 8


def test_parse_reply6_byte_exact():
    raw = reply6_bytes(ROUTER6, w.IP_TIME_EXCEEDED, 9) + bytes(64)
    assert w.parse_reply6(raw) == (ROUTER6, 11041, 9)
    linklocal = reply6_bytes("fe80::1", 0, 0, scope_id=999999)
    assert w.parse_reply6(linklocal)[0].startswith("fe80::1%")  # the zone of a link-local responder
    for n in (0, 1, 35):
        with pytest.raises(ValueError):
            w.parse_reply6(raw[:n])


async def test_ipv6_reply_goes_through_icmp6sendecho2():
    b, dll = make6(lambda c: (1, w.IP_SUCCESS, c["target"], 3))
    r = await b.probe("2001:DB8:1::7", timeout=1.0, ttl=7)
    assert r.kind == "reply" and r.responder == TARGET6 and 0 < r.rtt_ms <= 3.0
    call = dll.calls[0]
    assert call["version"] == 6 and call["target"] == TARGET6 and call["ttl"] == 7 and call["timeout"] == 1000
    assert call["size"] == len(w.PAYLOAD) and call["bufsize"] == w.REPLY_BUFFER_SIZE
    v4 = await b.probe("192.0.2.10", timeout=1.0)  # the IPv4 call on the same thread uses its own handle
    assert v4.kind == "reply"
    b.close()
    assert set(dll.created6) <= set(dll.closed)


@pytest.mark.parametrize("status, kind", [
    (w.IP_TTL_EXPIRED_TRANSIT, "ttl_exceeded"),  # IP_HOP_LIMIT_EXCEEDED
    (w.IP_TIME_EXCEEDED, "ttl_exceeded"),
    (w.IP_TTL_EXPIRED_REASSEM, "ttl_exceeded"),
    (w.IP_DEST_UNREACHABLE, "unreachable"),
    (w.IP_DEST_NET_UNREACHABLE, "unreachable"),  # IP_DEST_NO_ROUTE
    (w.IP_DEST_HOST_UNREACHABLE, "unreachable"),  # IP_DEST_ADDR_UNREACHABLE
    (w.IP_DEST_PROT_UNREACHABLE, "unreachable"),  # IP_DEST_PROHIBITED
    (w.IP_DEST_SCOPE_MISMATCH, "unreachable"),
    (w.IP_PACKET_TOO_BIG, "unreachable"),
    (w.IP_REQ_TIMED_OUT, "timeout"),
    (11050, "timeout"),  # IP_GENERAL_FAILURE: lost, and logged
])
async def test_ipv6_status_mapping(status, kind):
    b, _ = make6(lambda c: (1, status, ROUTER6, 2))
    r = await b.probe(TARGET6, timeout=1.0, ttl=3)
    assert r.kind == kind
    if kind != "timeout":
        assert r.responder == ROUTER6
    b.close()


async def test_ipv6_reply_from_someone_else_is_not_a_success():
    b, _ = make6(lambda c: (1, w.IP_SUCCESS, ROUTER6, 1))
    assert await b.probe(TARGET6, timeout=1.0) == ProbeResult("timeout")
    b.close()


async def test_ipv6_link_local_zone_is_the_scope_id():
    b, dll = make6(lambda c: (1, w.IP_SUCCESS, c["target"], 1, c["scope_id"]))
    r = await b.probe("fe80::1%12", timeout=1.0)
    assert dll.calls[0]["scope_id"] == 12 and r.kind == "reply" and r.responder.startswith("fe80::1%")
    b.close()


async def test_ipv6_errors_from_last_error():
    b, _ = make6(lambda c: ("error", w.IP_REQ_TIMED_OUT))
    assert await b.probe(TARGET6, timeout=1.0) == ProbeResult("timeout")
    b.close()
    b, _ = make6(lambda c: ("error", w.IP_DEST_UNREACHABLE))
    assert (await b.probe(TARGET6, timeout=1.0)).kind == "unreachable"
    b.close()
    b, _ = make6(lambda c: ("error", w.ERROR_NETWORK_UNREACHABLE))
    with pytest.raises(OSError) as caught:
        await b.probe(TARGET6, timeout=1.0)
    assert caught.value.errno == 1231  # what the prober reads as "IPv6 unavailable on this host"
    b.close()


async def test_without_ipv6_only_ipv6_probes_fail_with_eafnosupport():
    import errno

    for dll in (FakeDll6(create6_fails=True), FakeDll()):  # Icmp6CreateFile fails, or is missing altogether
        b = WindowsIcmpBackend(_dll=dll, _get_last_error=dll.last_error)
        assert (await b.probe("192.0.2.10", timeout=1.0)).kind == "reply"
        with pytest.raises(OSError) as caught:
            await b.probe(TARGET6, timeout=1.0)
        assert caught.value.errno == errno.EAFNOSUPPORT
        b.close()


@windows_only
async def test_real_ipv6_loopback_reply():
    b = WindowsIcmpBackend()
    try:
        try:
            r = await b.probe("::1", timeout=2.0, ttl=64)
        except OSError as e:
            if os.environ.get("PENGUMON_REQUIRE_ICMP"):  # CI's native job: a skip would hide a broken backend
                pytest.fail(f"IPv6 loopback unavailable: {e}")
            pytest.skip(f"no IPv6 on this runner: {e}")
    finally:
        b.close()
    assert r.kind == "reply" and r.responder == "::1" and r.rtt_ms > 0


@windows_only
async def test_real_ipv6_unroutable_does_not_crash():
    b = WindowsIcmpBackend()
    try:
        try:
            r = await b.probe("2001:db8::1", timeout=0.5)
        except OSError:
            return  # no IPv6 route: a clean answer too
    finally:
        b.close()
    assert r.kind in ("timeout", "unreachable")
