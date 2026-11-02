"""ICMP echo on macOS: an unprivileged ICMP datagram socket, SO_TIMESTAMP receive times.

`socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)` needs no root on macOS (XNU `icmp_dgram_attach`; Apple's own ping uses
it). It differs from Linux's ping socket in three ways that shape this module:

- Received packets start with the IPv4 header; it is skipped by its IHL field (IP_STRIPHDR is not relied on).
- The kernel does not rewrite the echo id, so the backend picks a random id and matches id + sequence.
- Every ICMP socket on the machine sees all inbound ICMP (other programs' pings, errors for other sockets), so
  anything that is not an answer to one of our pending probes is ignored.

TTL: one socket serves every probe. `IP_TTL` is a per-socket setting, so it is set immediately before each send, in
the same synchronous block (a lock covers the pair, in case a second thread ever sends). Probes with ttl 64 run every
second for every target, traces are rare, and one socket per TTL would mean up to 255 descriptors and readers for
no gain: the TTL is only read by the kernel at send time.

Time Exceeded (type 11) and Destination Unreachable (type 3) quote the original IP header and the first 8 bytes of
our echo request; id and sequence are read from there, and the responder is the error packet's source address.

IPv6 (`family=6`): `socket(AF_INET6, SOCK_DGRAM, IPPROTO_ICMPV6)`, what Apple's ping6 opens when not root (XNU
`icmp6_dgram_attach`: the same raw-socket list, any user, echo requests only). From the XNU source
(bsd/netinet6/icmp6.c): every ICMPv6 message, Time Exceeded (3) and Destination Unreachable (1) included, is handed
to every ICMPv6 socket whose ICMP6_FILTER passes it (`icmp6_rip6_input`, filter "pass all" by default, so no filter
is set), with the IPv6 header stripped: data starts at the ICMPv6 header, the source comes from recvmsg. The kernel
does not touch the echo id and fills in the checksum; the hop limit is IPV6_UNICAST_HOPS (allowed for non-root by
`icmp6_dgram_ctloutput`). Errors quote our request's IPv6 header (40 bytes) and echo header. The parsing is
icmp_packet.parse_icmp6, shared with Linux.
"""

import asyncio
import os
import socket
import struct
import sys
import threading
import time
from typing import Dict, Optional, Tuple

from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_packet import (
    address_key, address_text, check_ttl, echo6_request, echo_request, ip_literal, new_nonce, parse_icmp6,
    probe_payload, sockaddr,
)
from pengumon.engine.icmp_packet import checksum as _checksum  # noqa: F401 (the tests build packets with it)

SO_TIMESTAMP = 0x400  # Darwin's value; Python doesn't export it. The cmsg type (SCM_TIMESTAMP) is the same number.
ICMP_ECHO_REPLY = 0
ICMP_UNREACHABLE = 3
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11
IPPROTO_ICMP_NUM = 1
_TIMEVAL_MIN = 12  # Darwin struct timeval: int64 tv_sec, int32 tv_usec, 4 bytes padding (16 in all)
_CMSG_SPACE = socket.CMSG_SPACE(16) if hasattr(socket, "CMSG_SPACE") else 0
_RECV_SIZE = 2048
_MAX_READS_PER_WAKEUP = 512  # bounds one wakeup if the socket is flooded with foreign ICMP


def _ip_header(packet: bytes) -> Optional[Tuple[int, int, str]]:
    """(header length, protocol, source address) of an IPv4 packet, or None if it is not a plausible one."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if ihl < 20 or len(packet) < ihl:
        return None
    return ihl, packet[9], socket.inet_ntoa(packet[12:16])


def parse_packet(packet: bytes) -> Optional[Tuple[str, int, int, str, Optional[str]]]:
    """Reads one received packet (IPv4 header first).

    Returns (kind, id, seq, source, quoted destination) or None when it is not an echo reply, Time Exceeded or
    Unreachable quoting an echo request (type 8). For a reply the quoted
    destination is None. Never trusts a length field beyond the bytes actually received.
    """
    outer = _ip_header(packet)
    if outer is None or outer[1] != IPPROTO_ICMP_NUM:
        return None
    ihl, _proto, src = outer
    icmp = packet[ihl:]
    if len(icmp) < 8:
        return None
    icmp_type = icmp[0]
    if icmp_type == ICMP_ECHO_REPLY:
        if icmp[1] != 0:
            return None
        _t, _c, _s, pkt_id, seq = struct.unpack("!BBHHH", icmp[:8])
        return "reply", pkt_id, seq, src, None
    if icmp_type not in (ICMP_TIME_EXCEEDED, ICMP_UNREACHABLE):
        return None
    quoted = icmp[8:]  # original IP header + first 8 bytes of the original datagram
    inner = _ip_header(quoted)
    if inner is None:
        return None
    qihl, qproto, _qsrc = inner
    if qproto != IPPROTO_ICMP_NUM or len(quoted) < qihl + 8:
        return None
    q_type, _qcode, _qcsum, q_id, q_seq = struct.unpack("!BBHHH", quoted[qihl:qihl + 8])
    if q_type != ICMP_ECHO_REQUEST:
        return None
    q_dst = socket.inet_ntoa(quoted[16:20])
    kind = "ttl_exceeded" if icmp_type == ICMP_TIME_EXCEEDED else "unreachable"
    return kind, q_id, q_seq, src, q_dst


def _kernel_rx_ns(ancdata) -> Optional[int]:
    """The SO_TIMESTAMP receive time (wall clock, ns) from the ancillary data, or None."""
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == SO_TIMESTAMP and len(cdata) >= _TIMEVAL_MIN:
            sec, usec = struct.unpack("=qi", cdata[:12])
            if 0 <= usec < 1_000_000:
                return sec * 1_000_000_000 + usec * 1000
    return None


def parse_packet6(packet: bytes, src: Optional[str]) -> Optional[Tuple[str, int, int, str, Optional[str]]]:
    """One packet from the ICMPv6 socket (no IP header) and its source: (kind, id, seq, source, quoted destination)
    like parse_packet, or None."""
    answer = parse_icmp6(packet)
    if answer is None or not src:
        return None
    return answer.kind, answer.ident, answer.seq, src, answer.quoted_dst


def _source6(address) -> Optional[str]:
    """The text of a recvmsg() IPv6 source (host, port, flowinfo, scope id); link-local ones get their zone."""
    if not address:
        return None
    try:
        return address_text(socket.inet_pton(socket.AF_INET6, address[0].partition("%")[0]),
                            address[3] if len(address) >= 4 else 0)
    except (OSError, ValueError, IndexError):
        return None


class MacIcmpBackend:
    name = "macos-dgram"

    def __init__(self, *, family: int = 4, _sock=None):
        """`family` is 4 or 6 (one socket speaks one; icmp_dual pairs them). `_sock` is for tests: an object with
        setsockopt/sendto/recvmsg/close standing in for the socket."""
        if family not in (4, 6):
            raise ValueError(f"family must be 4 or 6, not {family!r}")
        self.family = family
        self.kernel_timestamps = 0
        self.fallback_timestamps = 0
        if _sock is None:
            if sys.platform != "darwin":
                raise IcmpUnavailable("the macOS ICMP backend only runs on macOS")
            try:
                if family == 4:
                    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
                else:
                    _sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, socket.IPPROTO_ICMPV6)
            except OSError as e:
                what = "an ICMP" if family == 4 else "an ICMPv6"
                raise IcmpUnavailable(f"cannot open {what} datagram socket: {e}") from e
        if family == 4:
            self._hops = (socket.IPPROTO_IP, socket.IP_TTL)
        else:
            # resolved here, not at import: the module imports on every OS
            self._hops = (socket.IPPROTO_IPV6, getattr(socket, "IPV6_UNICAST_HOPS", 4))  # 4 on Darwin
        self._sock = _sock
        try:
            self._sock.setblocking(False)
        except (OSError, AttributeError):
            pass
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMP, 1)
            self._stamped = True
        except OSError:
            self._stamped = False
        self.resolution_ms = 0.001 if self._stamped else 0.1
        self._id = int.from_bytes(os.urandom(2), "big")
        self._nonce = new_nonce()
        self._seq = 0
        self._send_lock = threading.Lock()
        # seq -> (future, packed destination, request data, wall-clock send ns, monotonic send ns)
        self._pending: Dict[int, Tuple[asyncio.Future, str, bytes, int, int]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    def _ensure_reader(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.remove_reader(self._sock.fileno())
            loop.add_reader(self._sock.fileno(), self._on_readable)
            self._loop = loop
            # Probes of a loop that ended without finishing them can never be answered
            self._pending = {seq: p for seq, p in self._pending.items() if p[0].get_loop() is loop}

    def _next_seq(self) -> int:
        for _ in range(0x10000):
            self._seq = (self._seq + 1) & 0xFFFF
            if self._seq not in self._pending:
                return self._seq
        raise OSError("too many ICMP probes in flight")

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        if self._closed:
            raise OSError("ICMP backend is closed")
        address, version = ip_literal(address)
        if version != self.family:
            raise ValueError(f"not an IPv{self.family} address: {address!r}")
        check_ttl(ttl)
        to = sockaddr(address, version)  # OSError for a zone that names no interface
        self._ensure_reader()
        seq = self._next_seq()
        fut = self._loop.create_future()
        send_ns = time.time_ns()
        payload = probe_payload(send_ns, self._nonce)
        packet = (echo_request if version == 4 else echo6_request)(self._id, seq, payload)
        self._pending[seq] = (fut, address_key(address), payload, send_ns, time.perf_counter_ns())
        try:
            with self._send_lock:  # IP_TTL is per socket: set and send with nothing in between
                self._sock.setsockopt(*self._hops, ttl)
                self._sock.sendto(packet, to)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return ProbeResult("timeout")
        finally:
            self._pending.pop(seq, None)

    def _on_readable(self):
        for _ in range(_MAX_READS_PER_WAKEUP):
            try:
                packet, ancdata, _flags, addr = self._sock.recvmsg(_RECV_SIZE, _CMSG_SPACE)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            wake_ns = time.perf_counter_ns()
            try:
                parsed = parse_packet(packet) if self.family == 4 else parse_packet6(packet, _source6(addr))
            except (struct.error, ValueError, OSError):
                continue
            if parsed is None:
                continue
            kind, pkt_id, seq, src, q_dst = parsed
            if pkt_id != self._id:
                continue
            entry = self._pending.get(seq)
            if entry is None:
                continue
            fut, dest, payload, send_ns, send_mono_ns = entry
            if fut.done():
                continue
            # a reply must come from the address we pinged; an error must quote a request sent to it
            try:
                if address_key(src if kind == "reply" else q_dst) != dest:
                    continue
            except (OSError, ValueError, TypeError):
                continue
            data_at = 8 if self.family == 6 else _ip_header(packet)[0] + 8  # an IPv6 socket has no IP header
            if kind == "reply" and packet[data_at:] != payload:
                continue  # not our data: a forged or foreign reply (the nonce must come back unchanged)
            mono_ms = (wake_ns - send_mono_ns) / 1e6
            rtt_ms = mono_ms
            rx_ns = _kernel_rx_ns(ancdata)
            if rx_ns is not None:
                wall_ms = (rx_ns - send_ns) / 1e6
                # the wall clock can step mid-flight; the kernel stamp can't be later than our wakeup
                if 0 < wall_ms <= mono_ms + 1.0:
                    rtt_ms = wall_ms
                    self.kernel_timestamps += 1
                else:
                    self.fallback_timestamps += 1
            else:
                self.fallback_timestamps += 1
            rtt_ms = round(rtt_ms, 3)
            if rtt_ms <= 0:
                rtt_ms = 0.001
            fut.set_result(ProbeResult(kind, src, rtt_ms))
        # more may be queued: the reader is level-triggered, so the loop calls us again

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None:
            try:
                self._loop.remove_reader(self._sock.fileno())
            except (OSError, ValueError, RuntimeError):
                pass
            self._loop = None
        for fut, *_ in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        try:
            self._sock.close()
        except OSError:
            pass
