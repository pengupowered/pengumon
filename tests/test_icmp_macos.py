"""Tests for the macOS ICMP backend: byte-exact packets with a fake socket, plus real-socket tests on macOS."""

import asyncio
import os
import socket
import struct
import sys

import pytest

from pengumon.engine import icmp_macos
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_macos import MacIcmpBackend, SO_TIMESTAMP, _checksum, parse_packet

DEST = "192.0.2.10"
ROUTER = "198.51.100.1"
OTHER = "203.0.113.7"
LOCAL = "192.0.2.99"


def ip_header(src, dst, proto=1, payload_len=0, ihl_words=5):
    hdr = struct.pack("!BBHHHBBH4s4s", 0x40 | ihl_words, 0, 20 + payload_len, 0, 0, 64, proto, 0,
                      socket.inet_aton(src), socket.inet_aton(dst))
    return hdr + b"\x00" * ((ihl_words - 5) * 4)  # IP options, zeroed


def echo(icmp_type, pkt_id, seq, data=b"payload!"):
    hdr = struct.pack("!BBHHH", icmp_type, 0, 0, pkt_id, seq)
    return struct.pack("!BBHHH", icmp_type, 0, _checksum(hdr + data), pkt_id, seq) + data


def reply_packet(src, pkt_id, seq, ihl_words=5, data=b"payload!"):
    body = echo(0, pkt_id, seq, data)
    return ip_header(src, LOCAL, payload_len=len(body), ihl_words=ihl_words) + body


def error_packet(icmp_type, src, dst, pkt_id, seq, code=0, quoted_ihl_words=5):
    quoted = ip_header(LOCAL, dst, payload_len=64, ihl_words=quoted_ihl_words) + echo(8, pkt_id, seq)[:8]
    body = struct.pack("!BBHI", icmp_type, code, 0, 0) + quoted
    return ip_header(src, LOCAL, payload_len=len(body)) + body


def timeval(sec, usec):
    return [(socket.SOL_SOCKET, SO_TIMESTAMP, struct.pack("=qi", sec, usec) + b"\0\0\0\0")]


class FakeSock:
    def __init__(self):
        self.sent = []
        self.opts = []
        self.queue = []
        self.closed = False

    def setblocking(self, flag):
        pass

    def setsockopt(self, level, opt, value):
        self.opts.append((level, opt, value))

    def sendto(self, data, addr):
        # the TTL in force at this moment, as the kernel would read it
        ttl = [v for (lv, o, v) in self.opts if (lv, o) == (socket.IPPROTO_IP, socket.IP_TTL)][-1]
        self.sent.append((data, addr, ttl))

    def recvmsg(self, size, ancsize):
        if not self.queue:
            raise BlockingIOError
        return (*self.queue.pop(0)[:2], 0, ("0.0.0.0", 0))

    def fileno(self):
        return -1

    def close(self):
        self.closed = True


@pytest.fixture
def backend(monkeypatch):
    b = MacIcmpBackend(_sock=FakeSock())
    monkeypatch.setattr(b, "_ensure_reader", lambda: setattr(b, "_loop", asyncio.get_running_loop()))
    b._id = 0x1234
    yield b
    b.close()


def sent_seq(b, index=-1):
    data = b._sock.sent[index][0]
    return struct.unpack("!BBHHH", data[:8])


def sent_data(b, index=-1):
    """The data of a request the backend sent: what an honest echo reply carries back."""
    return b._sock.sent[index][0][8:]


async def started(b, address=DEST, **kw):
    task = asyncio.ensure_future(b.probe(address, timeout=kw.pop("timeout", 1.0), **kw))
    await asyncio.sleep(0)
    return task


# ---- construction ----

def test_unavailable_off_macos():
    if sys.platform == "darwin":
        pytest.skip("this is macOS")
    with pytest.raises(IcmpUnavailable) as e:
        MacIcmpBackend()
    assert e.value.reason


def test_name_and_resolution():
    b = MacIcmpBackend(_sock=FakeSock())
    assert b.name == "macos-dgram"
    assert b.resolution_ms == 0.001
    b.close()


def test_resolution_without_timestamps():
    class NoStamp(FakeSock):
        def setsockopt(self, level, opt, value):
            if opt == SO_TIMESTAMP:
                raise OSError("nope")
            super().setsockopt(level, opt, value)

    b = MacIcmpBackend(_sock=NoStamp())
    assert b.resolution_ms > 0.001
    b.close()


# ---- parsing ----

def test_parse_echo_reply_skips_ip_header_by_ihl():
    assert parse_packet(reply_packet(DEST, 7, 9)) == ("reply", 7, 9, DEST, None)
    assert parse_packet(reply_packet(DEST, 7, 9, ihl_words=7)) == ("reply", 7, 9, DEST, None)


def test_parse_time_exceeded_and_unreachable():
    assert parse_packet(error_packet(11, ROUTER, DEST, 7, 9)) == ("ttl_exceeded", 7, 9, ROUTER, DEST)
    assert parse_packet(error_packet(3, ROUTER, DEST, 7, 9, code=1)) == ("unreachable", 7, 9, ROUTER, DEST)
    assert parse_packet(error_packet(11, ROUTER, DEST, 7, 9, quoted_ihl_words=6)) == \
        ("ttl_exceeded", 7, 9, ROUTER, DEST)


def test_parse_ignores_other_types_and_protocols():
    assert parse_packet(ip_header(DEST, LOCAL, payload_len=8) + echo(8, 1, 1)) is None  # an echo request
    assert parse_packet(ip_header(DEST, LOCAL, proto=17, payload_len=8) + echo(0, 1, 1)) is None
    assert parse_packet(error_packet(4, ROUTER, DEST, 1, 1)) is None  # source quench
    # an error quoting something that is not an echo request
    pkt = bytearray(error_packet(11, ROUTER, DEST, 1, 1))
    pkt[28 + 20] = 0  # quoted ICMP type -> echo reply
    assert parse_packet(bytes(pkt)) is None


@pytest.mark.parametrize("cut", range(0, 56))
def test_parse_truncated_never_raises(cut):
    for full in (reply_packet(DEST, 7, 9), error_packet(11, ROUTER, DEST, 7, 9)):
        result = parse_packet(full[:cut])
        if cut < len(full) and result is not None:
            # a cut that still leaves every field we read intact is fine, but then it must match
            assert result[1:3] == (7, 9)


def test_parse_garbage():
    assert parse_packet(b"") is None
    assert parse_packet(b"\xff" * 40) is None  # IP version 15
    assert parse_packet(b"\x4f" + b"\x00" * 30) is None  # IHL says 60 bytes, only 31 present
    assert parse_packet(b"\x41" + b"\x00" * 30) is None  # IHL below 20
    # outer IHL is huge but total length lies: only the received bytes count
    pkt = bytearray(reply_packet(DEST, 7, 9))
    pkt[0] = 0x4F
    assert parse_packet(bytes(pkt)) is None


# ---- probing with a fake socket ----

async def test_reply_matches_id_and_sequence(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    assert pid == 0x1234
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), timeval(0, 0)))
    backend._on_readable()
    result = await task
    assert result.kind == "reply" and result.responder == DEST and result.rtt_ms > 0
    assert not backend._pending


async def test_sent_packet_is_a_valid_echo_request_to_the_target(backend):
    task = await started(backend)
    data, addr, ttl = backend._sock.sent[0]
    assert addr == (DEST, 0) and ttl == 64
    assert data[0] == 8 and _checksum(data) == 0
    backend.close()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_foreign_traffic_is_ignored(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    q = backend._sock.queue
    q.append((reply_packet(DEST, pid + 1, seq), []))  # someone else's id
    q.append((reply_packet(DEST, pid, seq + 1), []))  # our id, a sequence we did not send
    q.append((reply_packet(OTHER, pid, seq), []))  # right id and seq, wrong source
    q.append((error_packet(11, ROUTER, OTHER, pid, seq), []))  # error quoting a request to another host
    q.append((error_packet(11, ROUTER, DEST, pid + 1, seq), []))
    q.append((b"\x45garbage", []))
    backend._on_readable()
    await asyncio.sleep(0.01)
    assert not task.done()
    q.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), []))
    backend._on_readable()
    assert (await task).kind == "reply"


async def test_forged_reply_with_the_right_id_sequence_and_source_but_other_data_is_ignored(backend):
    """Someone who guessed or sniffed id, sequence and target cannot make a dead host look alive without the nonce."""
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    good = sent_data(backend)
    forged = [b"not our payload", good[:-1], good + b"\0", bytes(len(good)), good[:8] + bytes(len(good) - 8), b""]
    for data in forged:
        backend._sock.queue.append((reply_packet(DEST, pid, seq, data=data), []))
    backend._on_readable()
    await asyncio.sleep(0.01)
    assert not task.done()
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=good), []))
    backend._on_readable()
    assert (await task).kind == "reply"


async def test_the_nonce_is_random_per_backend_and_in_every_request(backend):
    other = MacIcmpBackend(_sock=FakeSock())
    try:
        assert other._nonce != backend._nonce and len(backend._nonce) == 8
        t = await started(backend)
        assert backend._nonce in sent_data(backend)
        backend.close()
        await asyncio.gather(t, return_exceptions=True)
    finally:
        other.close()


async def test_rtt_is_rounded_to_three_decimals(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), []))
    backend._on_readable()
    rtt = (await task).rtt_ms
    assert rtt == round(rtt, 3) and rtt > 0


def test_probes_of_a_dead_loop_are_dropped_when_a_new_loop_starts(monkeypatch):
    b = MacIcmpBackend(_sock=FakeSock())
    b._sock.fileno = lambda: 5
    old = asyncio.new_event_loop()
    stale = old.create_future()
    b._pending[7] = (stale, DEST, b"x", 0, 0)
    b._loop = old
    old.close()

    async def go():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_reader", lambda *a: None, raising=False)
        b._ensure_reader()

    asyncio.run(go())
    assert b._pending == {}


async def test_ttl_exceeded_uses_quoted_header_and_router_source(backend):
    task = await started(backend, ttl=3)
    _t, _c, _s, pid, seq = sent_seq(backend)
    assert backend._sock.sent[0][2] == 3
    backend._sock.queue.append((error_packet(11, ROUTER, DEST, pid, seq), timeval(0, 0)))
    backend._on_readable()
    result = await task
    assert result.kind == "ttl_exceeded" and result.responder == ROUTER and result.rtt_ms > 0


async def test_unreachable(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    backend._sock.queue.append((error_packet(3, ROUTER, DEST, pid, seq, code=1), []))
    backend._on_readable()
    result = await task
    assert result.kind == "unreachable" and result.responder == ROUTER


async def test_concurrent_probes_each_get_their_own_answer(backend):
    targets = [f"192.0.2.{i}" for i in range(1, 21)]
    tasks = [await started(backend, t) for t in targets]
    seqs = {addr[0]: struct.unpack("!H", data[6:8])[0] for data, addr, _ in backend._sock.sent}
    datas = {addr[0]: data[8:] for data, addr, _ in backend._sock.sent}
    assert len(set(seqs.values())) == 20
    for t in reversed(targets):  # answer in the opposite order
        backend._sock.queue.append((reply_packet(t, 0x1234, seqs[t], data=datas[t]), []))
    backend._on_readable()
    results = await asyncio.gather(*tasks)
    assert [r.responder for r in results] == targets


async def test_ttl_is_set_per_probe_before_each_send(backend):
    t1 = await started(backend, ttl=2)
    t2 = await started(backend, "192.0.2.11", ttl=64)
    t3 = await started(backend, "192.0.2.12", ttl=5)
    assert [s[2] for s in backend._sock.sent] == [2, 64, 5]
    backend.close()
    for t in (t1, t2, t3):
        t.cancel()
    await asyncio.gather(t1, t2, t3, return_exceptions=True)


async def test_timeout_cleans_up(backend):
    result = await backend.probe(DEST, timeout=0.02)
    assert result == ProbeResult("timeout")
    assert backend._pending == {}
    # a late answer for the expired probe is dropped without error
    backend._sock.queue.append((reply_packet(DEST, 0x1234, sent_seq(backend)[4], data=sent_data(backend)), []))
    backend._on_readable()


async def test_many_timeouts_do_not_accumulate(backend):
    await asyncio.gather(*[backend.probe(f"192.0.2.{i % 200 + 1}", timeout=0.01) for i in range(300)])
    assert backend._pending == {}


async def test_send_failure_raises_and_cleans_up(backend):
    def boom(data, addr):
        raise OSError(65, "No route to host")
    backend._sock.sendto = boom
    with pytest.raises(OSError):
        await backend.probe(DEST, timeout=1.0)
    assert backend._pending == {}


@pytest.mark.parametrize("address", ["example.com", "", "192.0.2.256", "::1", "192.0.2.1; ls"])
async def test_rejects_non_ipv4_literals(backend, address):
    with pytest.raises(ValueError):
        await backend.probe(address, timeout=0.1)
    assert backend._sock.sent == []


@pytest.mark.parametrize("ttl", [0, -1, 256, 1.5, "64", True])
async def test_rejects_bad_ttl(backend, ttl):
    with pytest.raises(ValueError):
        await backend.probe(DEST, timeout=0.1, ttl=ttl)
    assert backend._sock.sent == []


async def test_sequence_skips_in_flight_numbers(backend):
    backend._seq = 0xFFFE
    t1 = await started(backend)
    t2 = await started(backend, "192.0.2.11")
    t3 = await started(backend, "192.0.2.12")  # wraps past 0xFFFF to 0, 1
    assert sorted(backend._pending) == [0, 1, 0xFFFF]
    backend.close()
    await asyncio.gather(t1, t2, t3, return_exceptions=True)


async def test_kernel_timestamp_used_for_rtt(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    send_ns = backend._pending[seq][3]
    await asyncio.sleep(0.02)  # real time must pass: a stamp later than our wakeup is rejected
    rx = send_ns + 2_500_000  # 2.5 ms after the send, in the kernel's microsecond units
    rx -= rx % 1000
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), timeval(rx // 10**9, rx % 10**9 // 1000)))
    backend._on_readable()
    result = await task
    assert result.rtt_ms == pytest.approx((rx - send_ns) / 1e6, abs=0.001)
    assert backend.kernel_timestamps == 1


async def test_impossible_kernel_timestamp_falls_back_to_monotonic(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), timeval(1, 0)))  # 1970: the clock "stepped"
    backend._on_readable()
    result = await task
    assert 0 < result.rtt_ms < 5000
    assert backend.fallback_timestamps == 1


async def test_missing_timestamp_falls_back(backend):
    task = await started(backend)
    _t, _c, _s, pid, seq = sent_seq(backend)
    backend._sock.queue.append((reply_packet(DEST, pid, seq, data=sent_data(backend)), []))
    backend._on_readable()
    assert (await task).rtt_ms > 0
    assert backend.fallback_timestamps == 1


async def test_close_cancels_pending_and_closes_socket(backend):
    task = await started(backend)
    backend.close()
    assert backend._sock.closed and backend._pending == {}
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(OSError):
        await backend.probe(DEST, timeout=0.1)
    backend.close()  # twice is fine


def test_flood_of_foreign_packets_is_bounded(backend):
    backend._sock.queue.extend([(reply_packet(OTHER, 1, 1), [])] * (icmp_macos._MAX_READS_PER_WAKEUP + 50))
    backend._on_readable()
    assert len(backend._sock.queue) == 50


# ---- real macOS sockets (run on GitHub's macOS runners) ----

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs the macOS ICMP datagram socket")


@darwin_only
async def test_real_loopback_reply():
    b = MacIcmpBackend()
    try:
        results = await asyncio.gather(*[b.probe("127.0.0.1", timeout=2.0, ttl=64) for _ in range(5)])
        for r in results:
            assert r.kind == "reply" and r.responder == "127.0.0.1" and r.rtt_ms > 0
    finally:
        b.close()


@darwin_only
async def test_real_unroutable_times_out_or_is_unreachable():
    b = MacIcmpBackend()
    try:
        try:
            r = await b.probe("192.0.2.1", timeout=0.5)
        except OSError:  # no route at all (a runner without a default route) is a legal answer
            return
        assert r.kind in ("timeout", "unreachable", "ttl_exceeded")
        assert b._pending == {}
    finally:
        b.close()


# ---- IPv6: the ICMPv6 datagram socket (no IP header; source from recvmsg) ----

DEST6 = "2001:db8:1::7"
ROUTER6 = "2001:db8::1"
OTHER6 = "2001:db8:1::99"
LOCAL6 = "2001:db8::10"


def echo6(icmp_type, pkt_id, seq, data=b"payload!"):
    return struct.pack("!BBHHH", icmp_type, 0, 0, pkt_id, seq) + data


def error6(icmp_type, dst, pkt_id, seq, next_header=58, quoted_type=128):
    """RFC 4443: type, code, checksum, 4 bytes, then our request's IPv6 header and its echo header."""
    ip6 = struct.pack("!IHBB16s16s", 6 << 28, 64, next_header, 1, socket.inet_pton(socket.AF_INET6, LOCAL6),
                      socket.inet_pton(socket.AF_INET6, dst))
    return struct.pack("!BBHI", icmp_type, 0, 0, 0) + ip6 + struct.pack("!BBHHH", quoted_type, 0, 0, pkt_id, seq)


class FakeSock6(FakeSock):
    def sendto(self, data, addr):
        hops = [v for (lv, o, v) in self.opts if lv == socket.IPPROTO_IPV6][-1]  # IPV6_UNICAST_HOPS
        self.sent.append((data, addr, hops))

    def recvmsg(self, size, ancsize):
        if not self.queue:
            raise BlockingIOError
        data, anc, src = self.queue.pop(0)
        return data, anc, 0, src


@pytest.fixture
def backend6(monkeypatch):
    b = MacIcmpBackend(family=6, _sock=FakeSock6())
    monkeypatch.setattr(b, "_ensure_reader", lambda: setattr(b, "_loop", asyncio.get_running_loop()))
    b._id = 0x1234
    yield b
    b.close()


def a6(host, scope=0):
    return (host, 0, 0, scope)


def test_parse_packet6():
    assert icmp_macos.parse_packet6(echo6(129, 7, 9), DEST6) == ("reply", 7, 9, DEST6, None)
    assert icmp_macos.parse_packet6(error6(3, DEST6, 7, 9), ROUTER6) == ("ttl_exceeded", 7, 9, ROUTER6, DEST6)
    assert icmp_macos.parse_packet6(error6(1, DEST6, 7, 9), ROUTER6) == ("unreachable", 7, 9, ROUTER6, DEST6)
    assert icmp_macos.parse_packet6(error6(2, DEST6, 7, 9), ROUTER6)[0] == "unreachable"
    assert icmp_macos.parse_packet6(echo6(128, 7, 9), DEST6) is None
    assert icmp_macos.parse_packet6(error6(3, DEST6, 7, 9, next_header=17), ROUTER6) is None
    assert icmp_macos.parse_packet6(error6(3, DEST6, 7, 9, quoted_type=129), ROUTER6) is None
    assert icmp_macos.parse_packet6(echo6(129, 7, 9), None) is None
    full = error6(3, DEST6, 7, 9)
    for n in range(len(full)):
        assert icmp_macos.parse_packet6(full[:n], ROUTER6) is None


async def test_ipv6_reply_hop_limit_and_destination(backend6):
    task = await started(backend6, "2001:DB8:1::7", ttl=5)
    data, addr, hops = backend6._sock.sent[0]
    assert addr == a6(DEST6) and hops == 5 and data[0] == 128 and data[2:4] == b"\0\0"
    _t, _c, _s, pid, seq = sent_seq(backend6)
    backend6._sock.queue.append((echo6(129, pid, seq, sent_data(backend6)), [], a6(DEST6)))
    backend6._on_readable()
    result = await task
    assert result.kind == "reply" and result.responder == DEST6 and result.rtt_ms > 0


async def test_ipv6_time_exceeded_and_foreign_traffic(backend6):
    task = await started(backend6, DEST6, ttl=2)
    _t, _c, _s, pid, seq = sent_seq(backend6)
    q = backend6._sock.queue
    q.append((echo6(129, pid + 1, seq, sent_data(backend6)), [], a6(DEST6)))  # someone else's id
    q.append((echo6(129, pid, seq, sent_data(backend6)), [], a6(OTHER6)))  # wrong source
    q.append((echo6(129, pid, seq, b"forged"), [], a6(DEST6)))  # not our data
    q.append((error6(3, OTHER6, pid, seq), [], a6(ROUTER6)))  # about another target
    q.append((struct.pack("!BBHI", 134, 0, 0, 0) + b"\0" * 12, [], a6("fe80::1", 4)))  # a router advertisement
    backend6._on_readable()
    await asyncio.sleep(0.01)
    assert not task.done()
    q.append((error6(3, DEST6, pid, seq), [], a6(ROUTER6)))
    backend6._on_readable()
    result = await task
    assert result.kind == "ttl_exceeded" and result.responder == ROUTER6


async def test_ipv4_and_ipv6_backends_refuse_each_others_addresses(backend, backend6):
    with pytest.raises(ValueError):
        await backend.probe(DEST6, timeout=0.1)
    with pytest.raises(ValueError):
        await backend6.probe(DEST, timeout=0.1)
    assert backend._sock.sent == [] and backend6._sock.sent == []


@darwin_only
async def test_real_ipv6_loopback_reply():
    try:
        b = MacIcmpBackend(family=6)
    except IcmpUnavailable as e:
        if os.environ.get("PENGUMON_REQUIRE_ICMP"):  # CI's native job: a skip would hide a broken backend
            pytest.fail(f"IPv6 loopback unavailable: {e}")
        pytest.skip(f"no ICMPv6 datagram socket: {e}")
    try:
        results = await asyncio.gather(*[b.probe("::1", timeout=2.0, ttl=64) for _ in range(5)])
        for r in results:
            assert r.kind == "reply" and r.responder == "::1" and r.rtt_ms > 0
    finally:
        b.close()


@darwin_only
async def test_real_ipv6_unroutable_times_out_or_is_unreachable():
    b = MacIcmpBackend(family=6)
    try:
        try:
            r = await b.probe("2001:db8::1", timeout=0.5)
        except OSError:  # no IPv6 route (GitHub's runners have none to the internet): a clean answer too
            return
        assert r.kind in ("timeout", "unreachable", "ttl_exceeded")
        assert b._pending == {}
    finally:
        b.close()
