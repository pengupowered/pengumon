"""The Linux ICMP backend: packet parsing (byte-exact RFC 792 layouts), matching answers to probes over an injected
socket, and, on Linux, real raw and ping sockets."""

import asyncio
import errno
import os
import random
import socket
import struct
import sys

import pytest

from pengumon.engine import icmp_linux
from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_linux import (
    IP_RECVERR, MSG_ERRQUEUE, SOL_IP, SO_TIMESTAMPNS_NEW, SO_TIMESTAMPNS_OLD, SYSCTL_FIX, Answer, LinuxIcmpBackend,
    _checksum, kernel_timestamp_ns, parse_error_queue, parse_icmp, parse_raw_packet,
)

ME = "192.0.2.10"
TARGET = "198.51.100.7"
ROUTER = "192.0.2.1"
OTHER = "198.51.100.99"
PORT = 4242  # the fake ping socket's port, which the kernel would use as the echo id

on_linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux ICMP sockets")


# ---- packets, built field by field (RFC 791 / RFC 792) ----------------------------------------------------------

def ip_header(src, dst, proto=1, payload_len=0, options=b""):
    ihl = 5 + len(options) // 4
    return struct.pack("!BBHHHBBH4s4s", 0x40 | ihl, 0, ihl * 4 + payload_len, 0x1234, 0, 64, proto, 0,
                       socket.inet_aton(src), socket.inet_aton(dst)) + options


def icmp_msg(icmp_type, code, rest4, body=b""):
    msg = struct.pack("!BBH", icmp_type, code, 0) + rest4 + body
    return msg[:2] + struct.pack("!H", _checksum(msg)) + msg[4:]


def echo(icmp_type, ident, seq, data=b"\0" * 56):
    return icmp_msg(icmp_type, 0, struct.pack("!HH", ident, seq), data)


def icmp_error(icmp_type, ident, seq, dst, src=ME, options=b"", quoted_proto=1, quoted_type=8):
    """A Time Exceeded (11) or Unreachable (3) quoting our request: its IP header and the first 8 bytes of data."""
    first8 = struct.pack("!BBHHH", quoted_type, 0, 0xBEEF, ident, seq)
    quoted = ip_header(src, dst, proto=quoted_proto, payload_len=64, options=options) + first8
    return icmp_msg(icmp_type, 0, b"\0\0\0\0", quoted)


def raw(src, dst, icmp):
    return ip_header(src, dst, payload_len=len(icmp)) + icmp


def recverr_cmsg(ee_type, offender=ROUTER, origin=2, ee_errno=errno.EHOSTUNREACH, family=socket.AF_INET):
    ee = struct.pack("=IBBBBII", ee_errno, origin, ee_type, 0, 0, 0, 0)
    addr = struct.pack("=HH4s8x", family, 0, socket.inet_aton(offender))
    return (SOL_IP, IP_RECVERR, ee + addr)


def ts_cmsg(ns, ctype=SO_TIMESTAMPNS_OLD, fmt="=qq"):
    return (socket.SOL_SOCKET, ctype, struct.pack(fmt, ns // 1_000_000_000, ns % 1_000_000_000))


# ---- parsing ------------------------------------------------------------------------------------------------------

def test_checksum():
    # echo request, id 1, seq 1, no data: 0x0800 + 0x0001 + 0x0001 = 0x0802 -> ~ = 0xF7FD
    assert _checksum(b"\x08\x00\x00\x00\x00\x01\x00\x01") == 0xF7FD
    assert _checksum(b"\x08\x00\x00\x00\x00\x01\x00") == _checksum(b"\x08\x00\x00\x00\x00\x01\x00\x00")


def test_parse_echo_reply():
    assert parse_icmp(echo(0, 0x1234, 0x0102)) == Answer("reply", 0x1234, 0x0102, None)
    assert parse_raw_packet(raw(TARGET, ME, echo(0, 7, 9))) == Answer("reply", 7, 9, None)


@pytest.mark.parametrize("icmp_type, kind", [(11, "ttl_exceeded"), (3, "unreachable")])
def test_parse_icmp_error_reads_the_quoted_request(icmp_type, kind):
    assert parse_icmp(icmp_error(icmp_type, 0xAB01, 0x0203, TARGET)) == Answer(kind, 0xAB01, 0x0203, TARGET)
    packet = raw(ROUTER, ME, icmp_error(icmp_type, 5, 6, TARGET))
    assert parse_raw_packet(packet) == Answer(kind, 5, 6, TARGET)


def test_quoted_header_with_ip_options_is_skipped_by_its_own_length():
    """The quoted IP header's IHL says where the echo header starts; a fixed 20 would read the options as id/seq."""
    packet = icmp_error(11, 0x0A0B, 0x0C0D, TARGET, options=b"\x01\x01\x01\x00")  # NOP NOP NOP EOL: IHL 6
    assert parse_icmp(packet) == Answer("ttl_exceeded", 0x0A0B, 0x0C0D, TARGET)


def test_outer_ip_options_are_skipped_too():
    icmp = echo(0, 3, 4)
    packet = ip_header(TARGET, ME, payload_len=len(icmp), options=b"\x01\x01\x01\x00") + icmp
    assert parse_raw_packet(packet) == Answer("reply", 3, 4, None)


@pytest.mark.parametrize("packet", [
    echo(8, 1, 2),  # our own request (a raw socket sees those to 127.0.0.1)
    icmp_msg(5, 0, b"\0" * 4, ip_header(ME, TARGET) + b"\0" * 8),  # redirect
    icmp_error(11, 1, 2, TARGET, quoted_proto=17),  # quotes a UDP packet (someone's traceroute)
    icmp_error(3, 1, 2, TARGET, quoted_type=0),  # quotes a reply, not a request
], ids=["echo request", "redirect", "quoted udp", "quoted reply"])
def test_other_icmp_is_not_an_answer(packet):
    assert parse_icmp(packet) is None


def test_truncated_packets_are_rejected_at_every_length():
    for full in (icmp_error(11, 1, 2, TARGET), icmp_error(3, 1, 2, TARGET, options=b"\x01" * 8)):
        for n in range(len(full)):
            assert parse_icmp(full[:n]) is None, n
        packet = raw(ROUTER, ME, full)
        for n in range(len(packet)):
            assert parse_raw_packet(packet[:n]) is None, n
    for n in range(8):
        assert parse_icmp(echo(0, 1, 2)[:n]) is None


def test_lying_length_fields_never_read_past_the_packet():
    # outer IHL 15 (60 bytes) on a 28-byte packet; quoted IHL 15 with only 20 bytes quoted
    assert parse_raw_packet(b"\x4f" + raw(TARGET, ME, echo(0, 1, 2, data=b""))[1:]) is None
    bad_quote = bytearray(icmp_error(11, 1, 2, TARGET))
    bad_quote[8] = 0x4F
    assert parse_icmp(bytes(bad_quote)) is None
    bad_quote[8] = 0x44  # IHL 4: shorter than a header
    assert parse_icmp(bytes(bad_quote)) is None


def test_garbage_never_raises():
    rng = random.Random(1792)
    for _ in range(3000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 90)))
        for candidate in (blob, b"\x45" + blob, b"\x0b\x00\x00\x00\x00\x00\x00\x00\x45" + blob):
            parse_icmp(candidate)
            parse_raw_packet(candidate)
            parse_error_queue(candidate, [(SOL_IP, IP_RECVERR, blob)])
            kernel_timestamp_ns([(socket.SOL_SOCKET, SO_TIMESTAMPNS_OLD, blob)])


@pytest.mark.parametrize("ee_type, kind", [(11, "ttl_exceeded"), (3, "unreachable")])
def test_parse_error_queue(ee_type, kind):
    data = echo(8, PORT, 77)[:8]
    assert parse_error_queue(data, [ts_cmsg(5), recverr_cmsg(ee_type)]) == (kind, PORT, 77, ROUTER)


def test_error_queue_without_an_icmp_origin_or_offender():
    data = echo(8, PORT, 77)[:8]
    assert parse_error_queue(data, [recverr_cmsg(11, origin=1)]) is None  # SO_EE_ORIGIN_LOCAL (e.g. EMSGSIZE)
    assert parse_error_queue(data, [recverr_cmsg(12)]) is None  # parameter problem
    assert parse_error_queue(data, []) is None
    assert parse_error_queue(data, [(SOL_IP, IP_RECVERR, b"\0" * 15)]) is None
    assert parse_error_queue(data, [recverr_cmsg(11, family=socket.AF_UNSPEC)]) == ("ttl_exceeded", PORT, 77, None)
    ee_only = recverr_cmsg(3)[2][:16]
    assert parse_error_queue(data, [(SOL_IP, IP_RECVERR, ee_only)]) == ("unreachable", PORT, 77, None)
    assert parse_error_queue(echo(0, PORT, 77)[:8], [recverr_cmsg(11)]) is None  # not our request
    assert parse_error_queue(data[:7], [recverr_cmsg(11)]) is None


def test_kernel_timestamp_is_unpacked_by_its_length():
    ns = 1_790_000_000_123_456_789
    assert kernel_timestamp_ns([ts_cmsg(ns)]) == ns
    assert kernel_timestamp_ns([ts_cmsg(ns, SO_TIMESTAMPNS_NEW)]) == ns
    # 32-bit Linux, SO_TIMESTAMPNS_OLD: struct timespec of two 32-bit longs
    small = 1_700_000_000_987_654_321
    assert kernel_timestamp_ns([ts_cmsg(small, fmt="=ii")]) == small
    assert kernel_timestamp_ns([(socket.SOL_SOCKET, SO_TIMESTAMPNS_OLD, b"\0" * 4)]) is None
    assert kernel_timestamp_ns([(SOL_IP, SO_TIMESTAMPNS_OLD, struct.pack("=qq", 1, 2))]) is None
    assert kernel_timestamp_ns([recverr_cmsg(11), ts_cmsg(ns)]) == ns


# ---- the backend over a fake socket ------------------------------------------------------------------------------

class FakeSocket:
    """What the backend uses of a socket. The test queues packets and calls _on_readable itself; fileno() is a real
    (never readable) socket's, for anything that asks."""

    def __init__(self, fail_options=()):
        self._a, self._b = socket.socketpair()
        self.sent, self.options, self.queue, self.errqueue = [], [], [], []
        self.fail_options = set(fail_options)
        self.closed = False
        self.pending_error = 0  # SO_ERROR: set by the kernel when it queues an ICMP error (IP_RECVERR)

    def setsockopt(self, level, option, value):
        if (level, option) in self.fail_options:
            raise OSError(errno.ENOPROTOOPT, "Protocol not available")
        self.options.append((level, option, value))

    def setblocking(self, flag):
        pass

    def bind(self, address):
        pass

    def getsockname(self):
        return ("0.0.0.0", PORT)

    def fileno(self):
        return self._a.fileno()

    def sendto(self, packet, address):
        if self.pending_error:  # as the kernel does: a pending socket error fails the next send, once
            err, self.pending_error = self.pending_error, 0
            raise OSError(err, os.strerror(err))
        self.sent.append((packet, address))

    def getsockopt(self, level, option, *args):
        if (level, option) == (socket.SOL_SOCKET, socket.SO_ERROR):
            err, self.pending_error = self.pending_error, 0
            return err
        raise OSError(errno.ENOPROTOOPT, "Protocol not available")

    def recvmsg(self, bufsize, ancbufsize, flags=0):
        queue = self.errqueue if flags & MSG_ERRQUEUE else self.queue
        if not queue:
            raise BlockingIOError(errno.EAGAIN, "try again")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True
        self._a.close()
        self._b.close()


def sent_request(fake, index=-1):
    """(id, seq, send time ns) of a request the backend sent."""
    packet, _address = fake.sent[index]
    _type, _code, _csum, ident, seq, send_ns = struct.unpack("!BBHHHQ", packet[:16])
    return ident, seq, send_ns


def sent_payload(fake, index=-1):
    """The data of a request the backend sent: what an honest echo reply carries back."""
    return fake.sent[index][0][8:]


@pytest.fixture(autouse=False)
def slow_monotonic(monkeypatch):
    """Every monotonic read is 10 s after the last, so the scripted kernel stamps (a few ms after the send) are never
    taken for a clock step (see _rtt_ms) however fast the test runs."""
    import itertools
    ticks = itertools.count(1)
    monkeypatch.setattr(icmp_linux.time, "monotonic_ns", lambda: next(ticks) * 10_000_000_000)


def _scripted(kind, monkeypatch):
    """A backend on a FakeSocket whose reader the test calls itself: no add_reader, so it runs on any event loop
    (Windows' proactor loop has none)."""
    fake = FakeSocket()
    backend = LinuxIcmpBackend(_sock=fake, _kind=kind)
    monkeypatch.setattr(backend, "_ensure_reader", lambda: setattr(backend, "_loop", asyncio.get_running_loop()))
    return backend, fake


@pytest.fixture
def dgram(slow_monotonic, monkeypatch):
    backend, fake = _scripted("dgram", monkeypatch)
    yield backend, fake
    backend.close()


@pytest.fixture
def raw_backend(slow_monotonic, monkeypatch):
    backend, fake = _scripted("raw", monkeypatch)
    yield backend, fake
    backend.close()


async def started(backend, address, **kwargs):
    task = asyncio.ensure_future(backend.probe(address, **kwargs))
    await asyncio.sleep(0)  # runs up to the await on the answer: the request is sent
    return task


def test_dgram_socket_setup(dgram):
    backend, fake = dgram
    assert backend.name == "linux-dgram" and backend.resolution_ms == 0.001
    assert (SOL_IP, IP_RECVERR, 1) in fake.options
    assert (socket.SOL_SOCKET, SO_TIMESTAMPNS_OLD, 1) in fake.options or icmp_linux._IS_32BIT


def test_raw_socket_has_no_error_queue(raw_backend):
    backend, fake = raw_backend
    assert backend.name == "linux-raw"
    assert (SOL_IP, IP_RECVERR, 1) not in fake.options


@pytest.mark.parametrize("fails, expected", [
    ((), SO_TIMESTAMPNS_NEW), ({(socket.SOL_SOCKET, SO_TIMESTAMPNS_NEW)}, SO_TIMESTAMPNS_OLD),
])
def test_32_bit_prefers_the_64_bit_timestamp(monkeypatch, fails, expected):
    monkeypatch.setattr(icmp_linux, "_IS_32BIT", True)
    fake = FakeSocket(fail_options=fails)
    backend = LinuxIcmpBackend(_sock=fake, _kind="dgram")
    try:
        assert backend._timestamp_option == expected
        assert (socket.SOL_SOCKET, expected, 1) in fake.options
    finally:
        backend.close()


async def test_echo_reply_with_kernel_timestamp(dgram):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=1.0)
    ident, seq, send_ns = sent_request(fake)
    assert ident == PORT and fake.sent[-1][1] == (TARGET, 0)
    assert (SOL_IP, icmp_linux.IP_TTL, 64) in fake.options
    fake.queue.append((echo(0, ident, seq, sent_payload(fake)), [ts_cmsg(send_ns + 1_500_000)], 0, (TARGET, 0)))
    backend._on_readable()
    assert await task == ProbeResult("reply", TARGET, 1.5)
    assert backend.kernel_timestamps == 1 and backend._pending == {}


async def test_time_exceeded_from_the_error_queue(dgram):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=1.0, ttl=3)
    ident, seq, send_ns = sent_request(fake)
    assert fake.options[-1] == (SOL_IP, icmp_linux.IP_TTL, 3)
    # data: the quoted request's ICMP header; address: the original destination; offender: the router
    fake.errqueue.append((echo(8, ident, seq)[:8], [ts_cmsg(send_ns + 2_000_000), recverr_cmsg(11)], 0, (TARGET, 0)))
    backend._on_readable()
    assert await task == ProbeResult("ttl_exceeded", ROUTER, 2.0)


async def test_unreachable_from_the_error_queue(dgram):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=1.0)
    ident, seq, _ = sent_request(fake)
    fake.errqueue.append((echo(8, ident, seq)[:8], [recverr_cmsg(3, offender=ROUTER)], 0, (TARGET, 0)))
    backend._on_readable()
    result = await task
    assert (result.kind, result.responder) == ("unreachable", ROUTER) and result.rtt_ms >= 0
    assert backend.fallback_timestamps == 1  # no timestamp cmsg: stamped in Python


async def test_concurrent_probes_are_matched_by_sequence(dgram):
    backend, fake = dgram
    first = await started(backend, TARGET, timeout=1.0)
    second = await started(backend, OTHER, timeout=1.0, ttl=2)
    (_, seq1, ns1), (_, seq2, ns2) = sent_request(fake, 0), sent_request(fake, 1)
    assert seq1 != seq2
    # answers in reverse order
    fake.errqueue.append((echo(8, PORT, seq2)[:8], [ts_cmsg(ns2 + 4_000_000), recverr_cmsg(11)], 0, (OTHER, 0)))
    fake.queue.append((echo(0, PORT, seq1, sent_payload(fake, 0)), [ts_cmsg(ns1 + 1_000_000)], 0, (TARGET, 0)))
    backend._on_readable()
    assert await first == ProbeResult("reply", TARGET, 1.0)
    assert await second == ProbeResult("ttl_exceeded", ROUTER, 4.0)


@pytest.mark.parametrize("mismatch", ["id", "seq", "source", "errqueue destination", "errqueue seq"])
async def test_answers_to_someone_else_are_ignored(dgram, mismatch):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=0.2)
    ident, seq, send_ns = sent_request(fake)
    ts = [ts_cmsg(send_ns + 1_000_000)]
    if mismatch == "id":
        fake.queue.append((echo(0, ident ^ 1, seq, sent_payload(fake)), ts, 0, (TARGET, 0)))
    elif mismatch == "seq":
        fake.queue.append((echo(0, ident, (seq + 1) & 0xFFFF, sent_payload(fake)), ts, 0, (TARGET, 0)))
    elif mismatch == "source":
        fake.queue.append((echo(0, ident, seq, sent_payload(fake)), ts, 0, (OTHER, 0)))
    elif mismatch == "errqueue destination":
        fake.errqueue.append((echo(8, ident, seq)[:8], ts + [recverr_cmsg(11)], 0, (OTHER, 0)))
    else:
        fake.errqueue.append((echo(8, ident, (seq + 1) & 0xFFFF)[:8], ts + [recverr_cmsg(11)], 0, (TARGET, 0)))
    backend._on_readable()
    assert await task == ProbeResult("timeout")
    assert backend._pending == {}


@pytest.mark.parametrize("kind", ["dgram", "raw"])
async def test_forged_echo_reply_with_the_right_id_sequence_and_source_is_ignored(kind, slow_monotonic, monkeypatch):
    """A spoofed reply: one "from" a dead host, right id and sequence, other data, made it look alive."""
    backend, fake = _scripted(kind, monkeypatch)
    try:
        task = await started(backend, TARGET, timeout=0.2)
        ident, seq, send_ns = sent_request(fake)
        good = sent_payload(fake)
        for data in (b"not our payload", good[:-1], good + b"\0", bytes(len(good)), good[:8] + bytes(len(good) - 8), b""):
            reply = echo(0, ident, seq, data)
            packet = reply if kind == "dgram" else raw(TARGET, ME, reply)
            fake.queue.append((packet, [ts_cmsg(send_ns + 1_000_000)], 0, (TARGET, 0)))
        backend._on_readable()
        assert await task == ProbeResult("timeout")
    finally:
        backend.close()


@pytest.mark.parametrize("kind", ["dgram", "raw"])
async def test_the_honest_reply_is_accepted_after_forgeries(kind, slow_monotonic, monkeypatch):
    backend, fake = _scripted(kind, monkeypatch)
    try:
        task = await started(backend, TARGET, timeout=1.0)
        ident, seq, send_ns = sent_request(fake)
        for data in (b"forged", sent_payload(fake)):
            reply = echo(0, ident, seq, data)
            packet = reply if kind == "dgram" else raw(TARGET, ME, reply)
            fake.queue.append((packet, [ts_cmsg(send_ns + 1_000_000)], 0, (TARGET, 0)))
        backend._on_readable()
        assert await task == ProbeResult("reply", TARGET, 1.0)
    finally:
        backend.close()


async def test_echo_reply_code_must_be_zero(dgram):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=0.2)
    ident, seq, send_ns = sent_request(fake)
    bad = bytearray(echo(0, ident, seq, sent_payload(fake)))
    bad[1] = 1
    assert parse_icmp(bytes(bad)) is None
    fake.queue.append((bytes(bad), [ts_cmsg(send_ns + 1_000_000)], 0, (TARGET, 0)))
    backend._on_readable()
    assert await task == ProbeResult("timeout")


def test_raw_id_is_random_not_the_pid(monkeypatch):
    ids = set()
    for _ in range(8):
        b = LinuxIcmpBackend(_sock=FakeSocket(), _kind="raw")
        ids.add(b._id)
        assert len(b._nonce) == 8
        b.close()
    assert len(ids) > 1


async def test_probe_after_close_raises_oserror(dgram):
    backend, fake = dgram
    backend.close()
    with pytest.raises(OSError):
        await backend.probe(TARGET, timeout=0.1)
    backend.close()  # twice is fine


async def test_a_pending_socket_error_does_not_stop_the_reader(dgram):
    """With IP_RECVERR a normal read reports a queued ICMP error once (sk_err); the replies behind it still count."""
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=1.0)
    ident, seq, send_ns = sent_request(fake)
    fake.queue += [OSError(errno.EHOSTUNREACH, "No route to host"),
                   (echo(0, ident, seq, sent_payload(fake)), [ts_cmsg(send_ns + 700_000)], 0, (TARGET, 0))]
    backend._on_readable()
    assert await task == ProbeResult("reply", TARGET, 0.7)


async def test_a_socket_that_keeps_failing_to_read_pauses_the_reader_instead_of_spinning_R4_L8(dgram, monkeypatch):
    backend, fake = dgram
    removed = []
    await started(backend, TARGET, timeout=0.2)

    class Loop:
        def remove_reader(self, fd):
            removed.append(fd)

        def is_closed(self):
            return False

    backend._loop = Loop()
    reads = []

    def broken(bufsize, ancbufsize, flags=0):
        reads.append(flags)
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(fake, "recvmsg", broken)
    backend._on_readable()
    assert removed == [fake.fileno()] and backend._loop is None
    assert len(reads) < 20  # not the 256 of a spinning drain, twice


async def test_raw_socket_time_exceeded_and_reply(raw_backend):
    backend, fake = raw_backend
    hop = await started(backend, TARGET, timeout=1.0, ttl=1)
    end = await started(backend, TARGET, timeout=1.0, ttl=64)
    (ident, seq1, ns1), (_, seq2, ns2) = sent_request(fake, 0), sent_request(fake, 1)
    fake.queue += [
        (raw(ME, TARGET, echo(8, ident, seq2)), [], 0, (ME, 0)),  # our own request, looped back
        (raw(OTHER, ME, echo(0, ident ^ 0xFF, seq1, sent_payload(fake, 0))), [ts_cmsg(ns1)], 0, (OTHER, 0)),  # another pinger's reply
        (raw(ROUTER, ME, icmp_error(11, ident, seq1, OTHER)), [ts_cmsg(ns1)], 0, (ROUTER, 0)),  # about another target
        (raw(ROUTER, ME, icmp_error(11, ident, seq1, TARGET)), [ts_cmsg(ns1 + 300_000)], 0, (ROUTER, 0)),
        (raw(TARGET, ME, echo(0, ident, seq2, sent_payload(fake, 1))), [ts_cmsg(ns2 + 900_000)], 0, (TARGET, 0)),
    ]
    backend._on_readable()
    assert await hop == ProbeResult("ttl_exceeded", ROUTER, 0.3)
    assert await end == ProbeResult("reply", TARGET, 0.9)


async def test_reads_per_wakeup_are_bounded(dgram, monkeypatch):
    backend, fake = dgram
    monkeypatch.setattr(icmp_linux, "_MAX_PER_WAKEUP", 5)
    fake.queue += [(b"junk", [], 0, (OTHER, 0))] * 12
    backend._on_readable()
    assert len(fake.queue) == 7
    backend._on_readable()
    backend._on_readable()
    assert fake.queue == []


@pytest.mark.parametrize("address", ["example.com", "::1", "1.2.3", "-f", ""])
async def test_only_ipv4_literals_are_probed(dgram, address):
    backend, fake = dgram
    with pytest.raises(ValueError):
        await backend.probe(address, timeout=0.1)
    assert fake.sent == [] and backend._pending == {}


@pytest.mark.parametrize("ttl", [0, 256, -1, True, 1.5])
async def test_ttl_must_be_1_to_255(dgram, ttl):
    backend, fake = dgram
    with pytest.raises(ValueError):
        await backend.probe(TARGET, timeout=0.1, ttl=ttl)
    assert fake.sent == []


async def test_a_failed_send_raises_and_is_not_left_pending(dgram, monkeypatch):
    backend, fake = dgram

    def refuse(packet, address):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(fake, "sendto", refuse)
    with pytest.raises(OSError):
        await backend.probe(TARGET, timeout=0.5)
    assert backend._pending == {}


async def test_close_fails_what_is_in_flight_with_an_oserror_not_a_cancel_R4_L9(dgram):
    backend, fake = dgram
    task = await started(backend, TARGET, timeout=5.0)
    backend.close()
    with pytest.raises(OSError) as raised:
        await task
    assert raised.value.errno == errno.EBADF and not task.cancelled()
    assert fake.closed


def _refusing_socket(raw_errno, dgram_errno):
    def fake_socket(family, sock_type, proto):
        code = raw_errno if sock_type == socket.SOCK_RAW else dgram_errno
        raise OSError(code, "Operation not permitted" if code == errno.EPERM else "Permission denied")
    return fake_socket


def test_no_socket_at_all_names_the_sysctl_fix(monkeypatch):
    monkeypatch.setattr(icmp_linux.socket, "socket", _refusing_socket(errno.EPERM, errno.EACCES))
    with pytest.raises(IcmpUnavailable) as caught:
        LinuxIcmpBackend()
    assert caught.value.fix == SYSCTL_FIX and "net.ipv4.ping_group_range = 0 2147483647" in SYSCTL_FIX
    assert "raw socket" in caught.value.reason and "ping socket" in caught.value.reason


def test_other_failures_have_no_sysctl_fix(monkeypatch):
    monkeypatch.setattr(icmp_linux.socket, "socket", _refusing_socket(errno.EPERM, errno.EAFNOSUPPORT))
    with pytest.raises(IcmpUnavailable) as caught:
        LinuxIcmpBackend()
    assert caught.value.fix is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        LinuxIcmpBackend("icmp")


# ---- real sockets (Linux) -----------------------------------------------------------------------------------------

KINDS = [None, "raw", "dgram"]


def _backend_or_skip(kind=None):
    try:
        return LinuxIcmpBackend(kind)
    except IcmpUnavailable as e:  # no CAP_NET_RAW, or ping_group_range excludes us
        pytest.skip(f"ICMP socket ({kind or 'any'}) unavailable: {e}")


@on_linux
@pytest.mark.parametrize("kind", KINDS)
async def test_loopback_reply_with_kernel_timestamps(kind):
    backend = _backend_or_skip(kind)
    assert backend.kind in ("raw", "dgram") and backend.kind == (kind or backend.kind)
    try:
        results = await asyncio.gather(*[backend.probe("127.0.0.1", timeout=1.0) for _ in range(5)])
    finally:
        backend.close()
    for r in results:
        assert r.kind == "reply" and r.responder == "127.0.0.1" and 0 < r.rtt_ms < 50
    assert backend.kernel_timestamps == 5 and backend.fallback_timestamps == 0


@on_linux
@pytest.mark.parametrize("step_ms", [-500, 2000])
async def test_wall_clock_step_between_send_and_reply_is_not_measured(monkeypatch, step_ms):
    """An NTP step (timesyncd's first sync after boot) between the send and the kernel's receive stamp used to
    turn into a negative RTT (clock set back) or a false spike (set forward); the monotonic RTT is used instead."""
    backend = _backend_or_skip()
    real_time_ns = icmp_linux.time.time_ns
    # The send stamp is read on the old clock; the kernel stamps the reply on the stepped one.
    monkeypatch.setattr(icmp_linux.time, "time_ns", lambda: real_time_ns() - step_ms * 1_000_000)
    try:
        result = await backend.probe("127.0.0.1", timeout=1.0)
    finally:
        monkeypatch.setattr(icmp_linux.time, "time_ns", real_time_ns)
        backend.close()
    assert result.kind == "reply" and 0 <= result.rtt_ms < 50
    assert backend.clock_step_fallbacks == 1


@on_linux
@pytest.mark.parametrize("kind", KINDS)
async def test_unroutable_address_times_out_or_is_unreachable(kind):
    backend = _backend_or_skip(kind)
    try:
        result = await backend.probe("192.0.2.1", timeout=0.3)  # TEST-NET-1, never answers
    except OSError:
        result = ProbeResult("unreachable")  # no route at all: the send itself fails
    finally:
        backend.close()
    assert result.kind in ("timeout", "unreachable")
    assert backend._pending == {}


# ---- IPv6 (RFC 4443): packets built field by field, the backend over a fake socket, real sockets -----------------

from pengumon.engine.icmp_linux import (  # noqa: E402
    ICMPV6_FILTER, IPPROTO_ICMPV6, IPV6_RECVERR, IPV6_UNICAST_HOPS, SOL_IPV6, icmp6_filter, parse_error_queue6,
    parse_icmp6,
)

ME6 = "2001:db8::10"
TARGET6 = "2001:db8:1::7"
ROUTER6 = "2001:db8::1"
OTHER6 = "2001:db8:1::99"


def ip6_header(src, dst, next_header=58, payload_len=64, hop_limit=64):
    """RFC 8200: version 6, traffic class, flow label (4 bytes), payload length, next header, hop limit, addresses."""
    return struct.pack("!IHBB16s16s", 6 << 28, payload_len, next_header, hop_limit,
                       socket.inet_pton(socket.AF_INET6, src), socket.inet_pton(socket.AF_INET6, dst))


def echo6(icmp_type, ident, seq, data=b"\0" * 56):
    return struct.pack("!BBHHH", icmp_type, 0, 0, ident, seq) + data  # checksum: the kernel's (and checked by it)


def icmp6_error(icmp_type, ident, seq, dst, src=ME6, next_header=58, quoted_type=128, code=0, extra=b"\0" * 56):
    """A Time Exceeded (3), Unreachable (1) or Packet Too Big (2) quoting our request: its IPv6 header and the echo
    request (RFC 4443 quotes as much as fits; the parser needs the first 8 bytes)."""
    quoted = ip6_header(src, dst, next_header) + struct.pack("!BBHHH", quoted_type, 0, 0xBEEF, ident, seq) + extra
    return struct.pack("!BBHI", icmp_type, code, 0, 1280 if icmp_type == 2 else 0) + quoted


def recverr6_cmsg(ee_type, offender=ROUTER6, origin=3, scope_id=0, family=socket.AF_INET6, ee_errno=errno.EHOSTUNREACH):
    ee = struct.pack("=IBBBBII", ee_errno, origin, ee_type, 0, 0, 0, 0)
    addr = struct.pack("=HHI16sI", family, 0, 0, socket.inet_pton(socket.AF_INET6, offender), scope_id)
    return (SOL_IPV6, IPV6_RECVERR, ee + addr)


def test_parse_icmp6_echo_reply():
    assert parse_icmp6(echo6(129, 0x1234, 0x0102)) == Answer("reply", 0x1234, 0x0102, None)
    bad = bytearray(echo6(129, 1, 2))
    bad[1] = 1
    assert parse_icmp6(bytes(bad)) is None


@pytest.mark.parametrize("icmp_type, kind", [(3, "ttl_exceeded"), (1, "unreachable"), (2, "unreachable")])
def test_parse_icmp6_error_reads_the_quoted_request(icmp_type, kind):
    assert parse_icmp6(icmp6_error(icmp_type, 0xAB01, 0x0203, TARGET6)) == Answer(kind, 0xAB01, 0x0203, TARGET6)
    # only the first 8 bytes of our request quoted: still enough
    assert parse_icmp6(icmp6_error(icmp_type, 5, 6, TARGET6, extra=b"")) == Answer(kind, 5, 6, TARGET6)


@pytest.mark.parametrize("packet", [
    echo6(128, 1, 2),  # an echo request (ours, looped back, or someone pinging us)
    struct.pack("!BBHI", 135, 0, 0, 0) + b"\0" * 16,  # neighbor solicitation
    struct.pack("!BBHI", 134, 0, 0, 0) + b"\0" * 48,  # router advertisement
    icmp6_error(3, 1, 2, TARGET6, next_header=17),  # quotes a UDP packet (someone's traceroute)
    icmp6_error(1, 1, 2, TARGET6, next_header=0),  # quotes a packet with a hop-by-hop header: never ours
    icmp6_error(3, 1, 2, TARGET6, quoted_type=129),  # quotes a reply, not a request
    icmp6_error(4, 1, 2, TARGET6),  # parameter problem
    echo(0, 1, 2),  # an ICMPv4 echo reply on the wrong socket
], ids=["echo request", "neighbor solicitation", "router advertisement", "quoted udp", "quoted ext header",
        "quoted reply", "parameter problem", "icmpv4 reply"])
def test_other_icmp6_is_not_an_answer(packet):
    assert parse_icmp6(packet) is None


def test_quoted_header_must_be_ipv6():
    bad = bytearray(icmp6_error(3, 1, 2, TARGET6))
    bad[8] = 0x45  # an IPv4 header where the IPv6 one belongs
    assert parse_icmp6(bytes(bad)) is None


def test_truncated_icmp6_is_rejected_at_every_length():
    for full in (icmp6_error(3, 1, 2, TARGET6, extra=b""), icmp6_error(1, 1, 2, TARGET6, extra=b"")):
        for n in range(len(full)):
            assert parse_icmp6(full[:n]) is None, n
    for n in range(8):
        assert parse_icmp6(echo6(129, 1, 2)[:n]) is None


def test_icmp6_garbage_never_raises():
    rng = random.Random(4443)
    for _ in range(3000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 120)))
        for candidate in (blob, b"\x03\x00\x00\x00\x00\x00\x00\x00\x60" + blob, b"\x81" + blob):
            parse_icmp6(candidate)
            parse_error_queue6(b"\x80" + candidate, [(SOL_IPV6, IPV6_RECVERR, blob)])
            parse_error_queue6(candidate, [(SOL_IPV6, IPV6_RECVERR, b"\x00" * 5 + b"\x03\x03" + blob)])


@pytest.mark.parametrize("ee_type, kind", [(3, "ttl_exceeded"), (1, "unreachable"), (2, "unreachable")])
def test_parse_error_queue6(ee_type, kind):
    data = echo6(128, PORT, 77)[:8]
    assert parse_error_queue6(data, [ts_cmsg(5), recverr6_cmsg(ee_type)]) == (kind, PORT, 77, ROUTER6)


def test_error_queue6_offender_keeps_its_zone_and_odd_messages_are_dropped():
    data = echo6(128, PORT, 77)[:8]
    kind, _i, _s, offender = parse_error_queue6(data, [recverr6_cmsg(3, "fe80::1", scope_id=1)])
    assert kind == "ttl_exceeded" and offender.startswith("fe80::1%")  # %lo on Linux: interface 1
    assert parse_error_queue6(data, [recverr6_cmsg(3, origin=2)]) is None  # SO_EE_ORIGIN_ICMP: not ICMPv6
    assert parse_error_queue6(data, [recverr6_cmsg(3, origin=1)]) is None  # SO_EE_ORIGIN_LOCAL
    assert parse_error_queue6(data, [recverr6_cmsg(4)]) is None  # parameter problem
    assert parse_error_queue6(data, [recverr_cmsg(11)]) is None  # an IPv4 (SOL_IP) control message
    assert parse_error_queue6(data, [(SOL_IPV6, IPV6_RECVERR, b"\0" * 15)]) is None
    assert parse_error_queue6(data, [recverr6_cmsg(3, family=socket.AF_UNSPEC)]) == ("ttl_exceeded", PORT, 77, None)
    assert parse_error_queue6(data, [(SOL_IPV6, IPV6_RECVERR, recverr6_cmsg(1)[2][:30])]) == ("unreachable", PORT, 77, None)
    assert parse_error_queue6(echo6(129, PORT, 77)[:8], [recverr6_cmsg(3)]) is None  # not our request
    assert parse_error_queue6(data[:7], [recverr6_cmsg(3)]) is None


def test_icmp6_filter_blocks_all_but_the_answers():
    words = struct.unpack("=8I", icmp6_filter((129, 1, 2, 3)))
    passes = {t for t in range(256) if not words[t >> 5] & (1 << (t & 31))}  # Linux: a set bit blocks
    assert passes == {1, 2, 3, 129}


def _scripted6(kind, monkeypatch):
    fake = FakeSocket()
    backend = LinuxIcmpBackend(family=6, _sock=fake, _kind=kind)
    monkeypatch.setattr(backend, "_ensure_reader", lambda: setattr(backend, "_loop", asyncio.get_running_loop()))
    return backend, fake


@pytest.fixture
def dgram6(slow_monotonic, monkeypatch):
    backend, fake = _scripted6("dgram", monkeypatch)
    yield backend, fake
    backend.close()


@pytest.fixture
def raw6(slow_monotonic, monkeypatch):
    backend, fake = _scripted6("raw", monkeypatch)
    yield backend, fake
    backend.close()


def a6(host, scope_id=0):
    return (host, 0, 0, scope_id)


def test_ipv6_dgram_socket_setup(dgram6):
    backend, fake = dgram6
    assert backend.name == "linux-dgram" and backend.family == 6
    assert (SOL_IPV6, IPV6_RECVERR, 1) in fake.options and (SOL_IP, IP_RECVERR, 1) not in fake.options
    assert not any(o[1] == ICMPV6_FILTER for o in fake.options)


def test_ipv6_raw_socket_gets_the_filter(raw6):
    backend, fake = raw6
    assert (IPPROTO_ICMPV6, ICMPV6_FILTER, icmp6_filter((129, 1, 2, 3))) in fake.options
    assert (SOL_IPV6, IPV6_RECVERR, 1) not in fake.options


async def test_ipv6_echo_reply_with_kernel_timestamp(dgram6):
    backend, fake = dgram6
    task = await started(backend, "2001:DB8:1:0::7", timeout=1.0)
    packet, to = fake.sent[-1]
    assert to == a6(TARGET6) and packet[0] == 128 and packet[2:4] == b"\0\0"  # the kernel fills the checksum
    ident, seq, send_ns = sent_request(fake)
    assert ident == PORT and (SOL_IPV6, IPV6_UNICAST_HOPS, 64) in fake.options
    fake.queue.append((echo6(129, ident, seq, sent_payload(fake)), [ts_cmsg(send_ns + 1_500_000)], 0, a6(TARGET6)))
    backend._on_readable()
    assert await task == ProbeResult("reply", TARGET6, 1.5)
    assert backend._pending == {}


async def test_ipv6_time_exceeded_from_the_error_queue(dgram6):
    backend, fake = dgram6
    task = await started(backend, TARGET6, timeout=1.0, ttl=3)
    ident, seq, send_ns = sent_request(fake)
    assert fake.options[-1] == (SOL_IPV6, IPV6_UNICAST_HOPS, 3)
    fake.errqueue.append((echo6(128, ident, seq), [ts_cmsg(send_ns + 2_000_000), recverr6_cmsg(3)], 0, a6(TARGET6)))
    backend._on_readable()
    assert await task == ProbeResult("ttl_exceeded", ROUTER6, 2.0)


@pytest.mark.parametrize("ee_type", [1, 2])
async def test_ipv6_unreachable_and_packet_too_big_from_the_error_queue(dgram6, ee_type):
    backend, fake = dgram6
    task = await started(backend, TARGET6, timeout=1.0)
    ident, seq, send_ns = sent_request(fake)
    fake.errqueue.append((echo6(128, ident, seq), [ts_cmsg(send_ns + 1_000_000), recverr6_cmsg(ee_type)], 0, a6(TARGET6)))
    backend._on_readable()
    assert await task == ProbeResult("unreachable", ROUTER6, 1.0)


@pytest.mark.parametrize("mismatch", ["id", "seq", "source", "errqueue destination", "errqueue seq", "v4 cmsg"])
async def test_ipv6_answers_to_someone_else_are_ignored(dgram6, mismatch):
    backend, fake = dgram6
    task = await started(backend, TARGET6, timeout=0.2)
    ident, seq, send_ns = sent_request(fake)
    ts = [ts_cmsg(send_ns + 1_000_000)]
    if mismatch == "id":
        fake.queue.append((echo6(129, ident ^ 1, seq, sent_payload(fake)), ts, 0, a6(TARGET6)))
    elif mismatch == "seq":
        fake.queue.append((echo6(129, ident, (seq + 1) & 0xFFFF, sent_payload(fake)), ts, 0, a6(TARGET6)))
    elif mismatch == "source":
        fake.queue.append((echo6(129, ident, seq, sent_payload(fake)), ts, 0, a6(OTHER6)))
    elif mismatch == "errqueue destination":
        fake.errqueue.append((echo6(128, ident, seq), ts + [recverr6_cmsg(3)], 0, a6(OTHER6)))
    elif mismatch == "errqueue seq":
        fake.errqueue.append((echo6(128, ident, (seq + 1) & 0xFFFF), ts + [recverr6_cmsg(3)], 0, a6(TARGET6)))
    else:
        fake.errqueue.append((echo6(128, ident, seq), ts + [recverr_cmsg(11)], 0, a6(TARGET6)))
    backend._on_readable()
    assert await task == ProbeResult("timeout")
    assert backend._pending == {}


@pytest.mark.parametrize("kind", ["dgram", "raw"])
async def test_ipv6_forged_reply_is_ignored_and_the_honest_one_accepted(kind, slow_monotonic, monkeypatch):
    backend, fake = _scripted6(kind, monkeypatch)
    try:
        task = await started(backend, TARGET6, timeout=1.0)
        ident, seq, send_ns = sent_request(fake)
        good = sent_payload(fake)
        for data in (b"not our payload", good[:-1], good + b"\0", bytes(len(good)), b"", good):
            fake.queue.append((echo6(129, ident, seq, data), [ts_cmsg(send_ns + 1_000_000)], 0, a6(TARGET6)))
        backend._on_readable()
        assert await task == ProbeResult("reply", TARGET6, 1.0)
        assert fake.queue == []  # the forgeries were read and dropped before the honest one
    finally:
        backend.close()


async def test_ipv6_raw_socket_time_exceeded_and_reply(raw6):
    backend, fake = raw6
    hop = await started(backend, TARGET6, timeout=1.0, ttl=1)
    end = await started(backend, TARGET6, timeout=1.0, ttl=64)
    (ident, seq1, ns1), (_, seq2, ns2) = sent_request(fake, 0), sent_request(fake, 1)
    fake.queue += [
        (echo6(128, ident, seq2), [], 0, a6(ME6)),  # our own request, looped back
        (struct.pack("!BBHI", 135, 0, 0, 0) + b"\0" * 16, [], 0, a6("fe80::1", 2)),  # neighbor discovery
        (echo6(129, ident ^ 0xFF, seq1, sent_payload(fake, 0)), [ts_cmsg(ns1)], 0, a6(OTHER6)),  # another pinger
        (icmp6_error(3, ident, seq1, OTHER6), [ts_cmsg(ns1)], 0, a6(ROUTER6)),  # about another target
        (icmp6_error(3, ident, seq1, TARGET6), [ts_cmsg(ns1 + 300_000)], 0, a6(ROUTER6)),
        (echo6(129, ident, seq2, sent_payload(fake, 1)), [ts_cmsg(ns2 + 900_000)], 0, a6(TARGET6)),
    ]
    backend._on_readable()
    assert await hop == ProbeResult("ttl_exceeded", ROUTER6, 0.3)
    assert await end == ProbeResult("reply", TARGET6, 0.9)


async def test_ipv6_link_local_target_with_its_zone(raw6):
    """fe80::/10 needs the interface: the zone becomes the scope id of the send, and comes back on the responder."""
    backend, fake = raw6
    if_index, if_name = socket.if_nameindex()[0]
    task = await started(backend, f"fe80::1%{if_name}", timeout=1.0)
    ident, seq, send_ns = sent_request(fake)
    assert fake.sent[-1][1] == a6("fe80::1", if_index)
    fake.queue.append((echo6(129, ident, seq, sent_payload(fake)), [ts_cmsg(send_ns + 100_000)], 0, a6("fe80::1", if_index)))
    backend._on_readable()
    assert await task == ProbeResult("reply", f"fe80::1%{if_name}", 0.1)


@pytest.mark.parametrize("address", ["example.com", "192.0.2.1", "1.2.3", "-f", "", "2001:db8::/64"])
async def test_an_ipv6_backend_probes_only_ipv6_literals(dgram6, address):
    backend, fake = dgram6
    with pytest.raises(ValueError):
        await backend.probe(address, timeout=0.1)
    assert fake.sent == [] and backend._pending == {}


async def test_an_ipv6_send_failure_keeps_its_errno(dgram6, monkeypatch):
    backend, fake = dgram6

    def refuse(packet, address):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(fake, "sendto", refuse)
    with pytest.raises(OSError) as caught:
        await backend.probe(TARGET6, timeout=0.5)
    assert caught.value.errno == errno.ENETUNREACH and backend._pending == {}


def test_no_ipv6_socket_names_the_family(monkeypatch):
    def refuse(family, sock_type, proto):
        assert family == socket.AF_INET6 and proto == IPPROTO_ICMPV6
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

    monkeypatch.setattr(icmp_linux.socket, "socket", refuse)
    with pytest.raises(IcmpUnavailable) as caught:
        LinuxIcmpBackend(family=6)
    assert "ICMPv6" in caught.value.reason and caught.value.fix is None
    with pytest.raises(ValueError):
        LinuxIcmpBackend(family=5)


def _ipv6_backend_or_skip(kind=None):
    if not socket.has_ipv6:
        pytest.skip("this Python has no IPv6")
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.bind(("::1", 0))
    except OSError as e:
        pytest.skip(f"no ::1 here (IPv6 disabled in this network namespace?): {e}")
    try:
        return LinuxIcmpBackend(kind, family=6)
    except IcmpUnavailable as e:
        pytest.skip(f"ICMPv6 socket ({kind or 'any'}) unavailable: {e}")


@on_linux
@pytest.mark.parametrize("kind", KINDS)
async def test_ipv6_loopback_reply_with_kernel_timestamps(kind):
    backend = _ipv6_backend_or_skip(kind)
    try:
        results = await asyncio.gather(*[backend.probe("::1", timeout=1.0) for _ in range(5)])
    finally:
        backend.close()
    for r in results:
        assert r.kind == "reply" and r.responder == "::1" and 0 < r.rtt_ms < 50
    assert backend.kernel_timestamps == 5 and backend.fallback_timestamps == 0


@on_linux
@pytest.mark.parametrize("kind", KINDS)
async def test_ipv6_unroutable_address_times_out_or_is_unreachable(kind):
    backend = _ipv6_backend_or_skip(kind)
    try:
        result = await backend.probe("2001:db8::1", timeout=0.3)  # documentation prefix, never answers
    except OSError:
        result = ProbeResult("unreachable")  # no IPv6 route at all: the send itself fails
    finally:
        backend.close()
    assert result.kind in ("timeout", "unreachable")
    assert backend._pending == {}


@on_linux
async def test_the_app_backend_probes_both_families():
    from pengumon.engine.icmp_select import open_backend

    _ipv6_backend_or_skip().close()
    try:
        backend = open_backend()
    except IcmpUnavailable as e:
        pytest.skip(f"no ICMP here: {e}")
    try:
        v4, v6 = await asyncio.gather(backend.probe("127.0.0.1", timeout=1.0), backend.probe("::1", timeout=1.0))
    finally:
        backend.close()
    assert (v4.kind, v4.responder, v6.kind, v6.responder) == ("reply", "127.0.0.1", "reply", "::1")
    assert backend.ipv6_name == backend.name


async def test_a_pending_icmp_error_does_not_fail_the_next_send(monkeypatch):
    # Live ICMPv6 traces broke off: with IPV6_RECVERR the kernel leaves each queued ICMP error as the socket's
    # pending error too, and the next sendto failed with EHOSTUNREACH. The backend clears it (SO_ERROR) first.
    fake = FakeSocket()
    backend = LinuxIcmpBackend(_sock=fake, _kind="dgram")
    # no add_reader on Windows' Proactor loop (the suite runs there too); the test answers nothing anyway
    monkeypatch.setattr(backend, "_ensure_reader", lambda: setattr(backend, "_loop", asyncio.get_running_loop()))
    try:
        fake.pending_error = errno.EHOSTUNREACH  # a router's Time Exceeded for an earlier probe was just queued
        result = await backend.probe("192.0.2.1", timeout=0.05, ttl=2)
        assert result.kind == "timeout"  # sent, then no answer: not an OSError from the stale error
        assert len(fake.sent) == 1
    finally:
        backend.close()
