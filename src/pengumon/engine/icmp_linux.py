"""The Linux ICMP backend: echo requests over one raw or ping socket per address family, RTT from kernel receive
timestamps.

Like iputils ping, it asks the kernel for each answer's arrival time (SO_TIMESTAMPNS), so event-loop wakeup and
scheduling delays don't count (icmplib stamped replies in Python, which on a VM-hosted container added 0.3-0.6 ms to
every reading). One socket is shared by every probe of its family; answers are matched by echo id and sequence.

The socket is raw when the process may open one (CAP_NET_RAW in the network namespace's owner: the image's
interpreter has it as a file capability; a systemd unit can grant it). Otherwise it is an unprivileged ICMP datagram
socket ("ping socket"), which any process gets when net.ipv4.ping_group_range covers one of its groups (the same
sysctl rules the IPv6 ping socket). Fedora and Arch allow every group; Debian, Ubuntu, Mint and Raspberry Pi OS allow
none until a sysctl drop-in says so (`IcmpUnavailable.fix` has the line).

What comes back, per socket kind (IPv4):
- raw: every ICMP packet the host receives, IP header first. An echo reply carries our id and sequence; a Time
  Exceeded (type 11) or Destination Unreachable (type 3) quotes the IP header of our request plus its first 8 bytes,
  which hold the id and sequence (RFC 792).
- ping socket: the kernel sets the echo id to the socket's port and delivers only our echo replies, without the IP
  header. ICMP errors about our requests go to the socket's error queue when IP_RECVERR is on: recvmsg(MSG_ERRQUEUE)
  returns the quoted ICMP header of the request (id and sequence) as data and a `sock_extended_err` (ee_origin ICMP,
  ee_type 11 or 3) followed by the offender's sockaddr_in (SO_EE_OFFENDER: the router that answered) as ancillary
  data. See ip(7), "IP_RECVERR", and the kernel's net/ipv4/ping.c (ping_err) and ip_sockglue.c (ip_recv_error).

IPv6 (`family=6`, RFC 4443) is the same with ICMPv6 numbers: echo request 128 and reply 129, Time Exceeded 3,
Destination Unreachable 1, Packet Too Big 2 (counted as unreachable: our 104-byte probes fit every IPv6 link, so a
router that says so cannot pass them). An IPv6 socket never returns the IP header; a raw one gets every ICMPv6 message
(neighbor discovery too), so an ICMP6_FILTER lets through only those four types, and an error quotes the 40-byte
IPv6 header of our request plus the echo header. The kernel computes the ICMPv6 checksum. The hop limit is
IPV6_UNICAST_HOPS; the ping socket's errors come from IPV6_RECVERR (SOL_IPV6), ee_origin SO_EE_ORIGIN_ICMP6 and a
sockaddr_in6 offender (ipv6(7); the kernel's net/ipv6/datagram.c, ipv6_icmp_error and ipv6_recv_error).
"""

import asyncio
import errno
import logging
import os
import socket
import struct
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_packet import (  # noqa: F401 (Answer and parse_icmp6 are this module's API too)
    ERROR6_KINDS as _ERROR6_KINDS, ICMP6_DEST_UNREACH, ICMP6_ECHO_REPLY, ICMP6_ECHO_REQUEST, ICMP6_PACKET_TOO_BIG,
    ICMP6_TIME_EXCEEDED, Answer, address_key, address_text, check_ttl, echo6_request, echo_request, ip_literal,
    new_nonce, parse_icmp6, probe_payload, sockaddr,
)
from pengumon.engine.icmp_packet import checksum as _checksum  # noqa: F401 (tests and older imports use this name)

# Python doesn't export these. Values from the kernel's uapi headers (asm-generic/socket.h, linux/in.h,
# linux/errqueue.h); the same on x86, arm, arm64 and riscv.
SO_TIMESTAMPNS_OLD = 35  # also called SO_TIMESTAMPNS; struct timespec: 16 bytes on 64-bit, 8 bytes on 32-bit
SO_TIMESTAMPNS_NEW = 64  # struct __kernel_timespec (16 bytes everywhere), Linux 5.1+; preferred on 32-bit
_TIMESTAMP_TYPES = (SO_TIMESTAMPNS_OLD, SO_TIMESTAMPNS_NEW)  # the SCM_ type equals the option number
SOL_IP = 0
IP_TTL = 2
IP_RECVERR = 11
MSG_ERRQUEUE = 0x2000
SO_EE_ORIGIN_ICMP = 2
# IPv6 (linux/in6.h, linux/icmpv6.h): Python exports some of these on Linux only, so they are defined here
SOL_IPV6 = 41  # also IPPROTO_IPV6
IPV6_UNICAST_HOPS = 16
IPV6_RECVERR = 25
SO_EE_ORIGIN_ICMP6 = 3
IPPROTO_ICMPV6 = 58  # also SOL_ICMPV6
ICMPV6_FILTER = 1

ICMP_ECHO_REPLY = 0
ICMP_DEST_UNREACH = 3
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11
_ERROR_KINDS = {ICMP_TIME_EXCEEDED: "ttl_exceeded", ICMP_DEST_UNREACH: "unreachable"}

_RECV_SIZE = 2048
_ANC_SIZE = 512  # a timestamp (16 bytes) and a sock_extended_err + sockaddr_in6 (44 bytes), with their headers
_MAX_PER_WAKEUP = 256  # packets read per wakeup; the rest waits for the next one (the socket stays readable)
_MAX_RECV_ERRORS = 8  # reads in a row that fail: one is a pending ICMP error (cleared by the read), many are a dead socket
_MAX_PENDING = 0xFF00  # probes in flight: the sequence is 16 bits, so this is the hard bound
_IS_32BIT = struct.calcsize("P") == 4
logger = logging.getLogger("pengumon.icmp")
PING_GROUP_RANGE = Path("/proc/sys/net/ipv4/ping_group_range")
SYSCTL_FIX = ("echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/60-unprivileged-ping.conf"
              " && sudo sysctl --system")


def parse_icmp(icmp: bytes) -> Optional[Answer]:
    """An ICMP message (starting at its type byte): an echo reply, or a Time Exceeded / Destination Unreachable that
    quotes an echo request (RFC 792: type, code, checksum, 4 unused bytes, then the original IP header and the first
    8 bytes of its data). None for anything else, and for anything too short for what its own headers claim."""
    if len(icmp) < 8:
        return None
    icmp_type = icmp[0]
    if icmp_type == ICMP_ECHO_REPLY:
        if icmp[1] != 0:
            return None
        ident, seq = struct.unpack_from("!HH", icmp, 4)
        return Answer("reply", ident, seq, None)
    kind = _ERROR_KINDS.get(icmp_type)
    if kind is None or len(icmp) < 8 + 20:
        return None
    quoted = icmp[8:]
    if quoted[0] >> 4 != 4 or quoted[9] != socket.IPPROTO_ICMP:
        return None
    ihl = (quoted[0] & 0x0F) * 4
    if ihl < 20 or len(quoted) < ihl + 8 or quoted[ihl] != ICMP_ECHO_REQUEST:
        return None
    ident, seq = struct.unpack_from("!HH", quoted, ihl + 4)
    return Answer(kind, ident, seq, socket.inet_ntoa(quoted[16:20]))


def raw_icmp(packet: bytes) -> Optional[bytes]:
    """The ICMP message of a packet from a raw ICMP socket (IPv4 header first), or None if the packet is too short."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if ihl < 20 or len(packet) < ihl + 8:
        return None
    return packet[ihl:]


def parse_raw_packet(packet: bytes) -> Optional[Answer]:
    """A packet from a raw ICMP socket: the IPv4 header, then the ICMP message."""
    icmp = raw_icmp(packet)
    return None if icmp is None else parse_icmp(icmp)


def parse_error_queue(data: bytes, ancdata: List[Tuple[int, int, bytes]]) -> Optional[Tuple[str, int, int, Optional[str]]]:
    """An error-queue message of a ping socket: (kind, echo id, sequence, offender address or None). `data` is the
    quoted ICMP header of our request; the IP_RECVERR control message holds
    struct sock_extended_err {u32 ee_errno; u8 ee_origin, ee_type, ee_code, ee_pad; u32 ee_info, ee_data;}
    followed by the offender's struct sockaddr_in (family, port, address). None unless it is an ICMP Time Exceeded
    or Unreachable about an echo request."""
    if len(data) < 8 or data[0] != ICMP_ECHO_REQUEST:
        return None
    for level, ctype, cdata in ancdata:
        if level != SOL_IP or ctype != IP_RECVERR or len(cdata) < 16:
            continue
        _errno, origin, ee_type, _code = struct.unpack_from("=IBBB", cdata, 0)
        kind = _ERROR_KINDS.get(ee_type)
        if origin != SO_EE_ORIGIN_ICMP or kind is None:
            return None
        offender = None
        if len(cdata) >= 16 + 8 and struct.unpack_from("=H", cdata, 16)[0] == socket.AF_INET:
            offender = socket.inet_ntoa(cdata[20:24])
        ident, seq = struct.unpack_from("!HH", data, 4)
        return kind, ident, seq, offender
    return None


def _sockaddr_in6_text(data: bytes, offset: int) -> Optional[str]:
    """The address of a struct sockaddr_in6 {u16 family, u16 port, u32 flowinfo, u8 addr[16], u32 scope_id} at
    `offset`, or None when it is not one (or is cut short)."""
    if len(data) < offset + 28 or struct.unpack_from("=H", data, offset)[0] != socket.AF_INET6:
        return None
    scope_id = struct.unpack_from("=I", data, offset + 24)[0]
    return address_text(data[offset + 8:offset + 24], scope_id)


def parse_error_queue6(data: bytes, ancdata: List[Tuple[int, int, bytes]]) -> Optional[Tuple[str, int, int, Optional[str]]]:
    """An error-queue message of an IPv6 ping socket: (kind, echo id, sequence, offender address or None). `data` is
    the quoted ICMPv6 header of our request; the IPV6_RECVERR control message (level SOL_IPV6) holds a
    sock_extended_err with ee_origin SO_EE_ORIGIN_ICMP6 and the ICMPv6 type, followed by the offender's
    sockaddr_in6 (with its scope id: a link-local router comes back as `fe80::1%eth0`)."""
    if len(data) < 8 or data[0] != ICMP6_ECHO_REQUEST:
        return None
    for level, ctype, cdata in ancdata:
        if level != SOL_IPV6 or ctype != IPV6_RECVERR or len(cdata) < 16:
            continue
        _errno, origin, ee_type, _code = struct.unpack_from("=IBBB", cdata, 0)
        kind = _ERROR6_KINDS.get(ee_type)
        if origin != SO_EE_ORIGIN_ICMP6 or kind is None:
            return None
        ident, seq = struct.unpack_from("!HH", data, 4)
        return kind, ident, seq, _sockaddr_in6_text(cdata, 16)
    return None


def icmp6_filter(passed) -> bytes:
    """The ICMP6_FILTER value that lets only the `passed` ICMPv6 types through. Linux inverts the RFC 3542 macros: a
    set bit blocks its type (include/uapi/linux/icmpv6.h, net/ipv6/raw.c icmpv6_filter), eight native u32 words."""
    words = [0xFFFFFFFF] * 8
    for t in passed:
        words[t >> 5] &= ~(1 << (t & 31)) & 0xFFFFFFFF
    return struct.pack("=8I", *words)


def kernel_timestamp_ns(ancdata: List[Tuple[int, int, bytes]]) -> Optional[int]:
    """The receive time (ns, CLOCK_REALTIME) from an SO_TIMESTAMPNS control message, unpacked by its length: 16 bytes
    is a 64-bit timespec (64-bit Linux, or SO_TIMESTAMPNS_NEW), 8 bytes the 32-bit one of SO_TIMESTAMPNS_OLD on a
    32-bit system."""
    for level, ctype, cdata in ancdata:
        if level != socket.SOL_SOCKET or ctype not in _TIMESTAMP_TYPES:
            continue
        if len(cdata) >= 16:
            sec, nsec = struct.unpack_from("=qq", cdata, 0)
        elif len(cdata) >= 8:
            sec, nsec = struct.unpack_from("=ii", cdata, 0)
        else:
            continue
        return sec * 1_000_000_000 + nsec
    return None


class _Pending(NamedTuple):
    future: asyncio.Future
    dest: bytes  # the packed destination address (address_key): answers are compared with it, whatever their spelling
    payload: bytes  # the request's data; an echo reply must carry exactly this
    send_ns: int  # wall clock (the kernel's receive stamps use CLOCK_REALTIME)
    send_mono_ns: int


class _Family(NamedTuple):
    """What differs between ICMPv4 and ICMPv6 on Linux."""

    version: int
    af: int
    proto: int
    hops: Tuple[int, int]  # the (level, option) of the TTL / hop limit
    recverr: Tuple[int, int]  # the (level, option) that queues ICMP errors on a ping socket
    any_address: str


_V4 = _Family(4, socket.AF_INET, socket.IPPROTO_ICMP, (SOL_IP, IP_TTL), (SOL_IP, IP_RECVERR), "0.0.0.0")
_V6 = _Family(6, socket.AF_INET6, IPPROTO_ICMPV6, (SOL_IPV6, IPV6_UNICAST_HOPS), (SOL_IPV6, IPV6_RECVERR), "::")


def _ping_group_reason() -> str:
    try:
        low, high = PING_GROUP_RANGE.read_text().split()[:2]
    except (OSError, ValueError):
        return ""
    groups = set(os.getgroups()) | {os.getgid()}
    if any(int(low) <= g <= int(high) for g in groups):
        return ""
    return f" (net.ipv4.ping_group_range is {low} {high}, which includes none of this process's groups)"


def _source_text(address) -> Optional[str]:
    """The canonical text of a recvmsg() source address: (host, port) for IPv4, (host, port, flowinfo, scope id) for
    IPv6, whose link-local hosts get their zone."""
    if not address:
        return None
    if len(address) >= 4 and address[3]:
        try:
            return address_text(socket.inet_pton(socket.AF_INET6, address[0]), address[3])
        except (OSError, ValueError):
            return address[0]
    return address[0]


class LinuxIcmpBackend:
    """ICMP echo over one raw or ping socket of one address family (`family` 4 or 6), with kernel receive timestamps.
    pengumon.engine.icmp_dual pairs an IPv4 and an IPv6 one into the backend the app uses.

    `kind` is "raw", "dgram" or None (raw if permitted, else dgram). Raises IcmpUnavailable when neither socket can
    be opened (an IPv6 one on a kernel without IPv6 too: the reason says so). `_sock` (with `_kind`) injects a
    socket-like object for tests."""

    resolution_ms = 0.001

    def __init__(self, kind: Optional[str] = None, *, family: int = 4, _sock=None, _kind: str = "dgram"):
        if kind not in (None, "raw", "dgram"):
            raise ValueError(f"unknown ICMP socket kind {kind!r}")
        if family not in (4, 6):
            raise ValueError(f"family must be 4 or 6, not {family!r}")
        self._fam = _V4 if family == 4 else _V6
        self.family = family
        self._sock = None
        if _sock is not None:
            self._sock, self.kind = _sock, _kind
        else:
            self._open(kind)
        self.name = f"linux-{self.kind}"
        self._timestamp_option = self._enable_timestamps()
        if self.kind == "dgram":
            # ICMP errors about our requests (Time Exceeded, Unreachable) go to the error queue
            self._sock.setsockopt(*self._fam.recverr, 1)
        elif family == 6:
            # A raw ICMPv6 socket gets every ICMPv6 message the host receives (neighbor and router discovery,
            # multicast listener reports): only replies and the errors that can be about our requests get through.
            # Only fewer wakeups: the parser ignores the rest anyway.
            try:
                self._sock.setsockopt(IPPROTO_ICMPV6, ICMPV6_FILTER, icmp6_filter(
                    (ICMP6_ECHO_REPLY, ICMP6_DEST_UNREACH, ICMP6_PACKET_TOO_BIG, ICMP6_TIME_EXCEEDED)))
            except OSError:
                pass
        self._sock.setblocking(False)
        if self.kind == "dgram":
            # The kernel uses the bound port as the echo id; bind now so requests and replies carry it
            self._sock.bind((self._fam.any_address, 0))
            self._id = self._sock.getsockname()[1]
        else:
            self._id = int.from_bytes(os.urandom(2), "big")  # not the pid: pid 1 in a container, easy to guess
        self._nonce = new_nonce()
        self._closed = False
        self._ttl: Optional[int] = None  # the socket's current IP_TTL / IPV6_UNICAST_HOPS
        self._seq = 0
        self._pending: Dict[int, _Pending] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Answers timed by the kernel vs. by Python (only if the timestamp cmsg is missing)
        self.kernel_timestamps = 0
        self.fallback_timestamps = 0
        # Answers whose wall-clock RTT was impossible because the clock was stepped mid-flight (see _rtt_ms)
        self.clock_step_fallbacks = 0

    def _open(self, kind: Optional[str]) -> None:
        errors = []
        dgram_denied = False
        for candidate in [kind] if kind else ["raw", "dgram"]:
            sock_type = socket.SOCK_RAW if candidate == "raw" else socket.SOCK_DGRAM
            try:
                self._sock = socket.socket(self._fam.af, sock_type, self._fam.proto)
                self.kind = candidate
                return
            except OSError as e:
                label = "raw socket" if candidate == "raw" else "ping socket"
                if candidate == "dgram" and e.errno == errno.EACCES:
                    dgram_denied = True
                    errors.append(f"{label}: {e.strerror}{_ping_group_reason()}")
                else:
                    errors.append(f"{label}: {e.strerror or e}")
        what = "an ICMP socket" if self.family == 4 else "an ICMPv6 socket"
        raise IcmpUnavailable(f"cannot open {what}: " + "; ".join(errors), SYSCTL_FIX if dgram_denied else None)

    def _enable_timestamps(self) -> Optional[int]:
        """Turns on kernel receive timestamps; the option that worked, or None (then Python stamps answers). On a
        32-bit system SO_TIMESTAMPNS_NEW comes first: the old option's 32-bit seconds end in 2038."""
        options = (SO_TIMESTAMPNS_NEW, SO_TIMESTAMPNS_OLD) if _IS_32BIT else (SO_TIMESTAMPNS_OLD,)
        for option in options:
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, option, 1)
                return option
            except OSError:
                continue
        return None

    def _ensure_reader(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.remove_reader(self._sock.fileno())
            loop.add_reader(self._sock.fileno(), self._on_readable)
            self._loop = loop
            # Probes of a loop that ended without finishing them (tests, pengumon doctor) can never be answered
            self._pending = {seq: p for seq, p in self._pending.items() if p.future.get_loop() is loop}

    def _next_seq(self) -> int:
        if len(self._pending) >= _MAX_PENDING:
            raise OSError(errno.ENOBUFS, "too many ICMP probes in flight")
        while True:
            self._seq = (self._seq + 1) & 0xFFFF
            if self._seq not in self._pending:
                return self._seq

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        if self._closed:
            raise OSError(errno.EBADF, "ICMP backend is closed")
        dest, version = ip_literal(address)
        if version != self.family:
            raise ValueError(f"not an IPv{self.family} address: {address!r}")
        check_ttl(ttl)
        to = sockaddr(dest, version)  # OSError for a zone that names no interface
        self._ensure_reader()
        seq = self._next_seq()
        fut = self._loop.create_future()
        send_ns = time.time_ns()  # same clock (CLOCK_REALTIME) as SO_TIMESTAMPNS
        payload = probe_payload(send_ns, self._nonce)
        packet = (echo_request if version == 4 else echo6_request)(self._id, seq, payload)
        self._pending[seq] = _Pending(fut, address_key(dest), payload, send_ns, time.monotonic_ns())
        try:
            if ttl != self._ttl:
                self._sock.setsockopt(*self._fam.hops, ttl)
                self._ttl = ttl
            if self.kind == "dgram":
                # With IP(V6)_RECVERR the kernel also leaves each ICMP error it queued for us as the socket's pending
                # error, and the next send fails with it (EHOSTUNREACH, seen on ICMPv6 traces: a burst of TTL probes
                # failed after the first Time Exceeded). Reading SO_ERROR clears it; the error queue our reader
                # drains is not touched.
                self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            self._sock.sendto(packet, to)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return ProbeResult("timeout")
        finally:
            self._pending.pop(seq, None)

    def _on_readable(self):
        """Reads what is queued: on a ping socket the error queue first (ICMP errors), then answers."""
        budget = _MAX_PER_WAKEUP
        if self.kind == "dgram":
            budget = self._drain(MSG_ERRQUEUE, budget)
        self._drain(0, budget)

    def _drain(self, flags: int, budget: int) -> int:
        failures = 0
        while budget > 0:
            try:
                packet, ancdata, _flags, address = self._sock.recvmsg(_RECV_SIZE, _ANC_SIZE, flags)
            except (BlockingIOError, InterruptedError):
                return budget
            except OSError as exc:
                # With IP_RECVERR a pending ICMP error is also reported once by a normal read (the socket's sk_err);
                # the read clears it, so keep reading. An error that does not clear (a socket in a hard error state)
                # would make the event loop call this again at once, for ever: stop reading instead.
                failures += 1
                if failures >= _MAX_RECV_ERRORS:
                    self._reader_failed(exc)
                    return 0
                budget -= 1
                continue
            failures = 0
            budget -= 1
            self._handle(packet, ancdata, address, errqueue=bool(flags & MSG_ERRQUEUE))
        return budget

    def _reader_failed(self, exc: OSError) -> None:
        """The socket cannot be read: stop the event loop's reader (once), say so; the next probe adds it again."""
        if self._loop is not None:
            logger.warning(f"Reading the ICMP socket fails ({exc}); pausing the reader until the next probe")
            if not self._loop.is_closed():
                try:
                    self._loop.remove_reader(self._sock.fileno())
                except (NotImplementedError, OSError, ValueError):
                    pass
            self._loop = None

    def _parse(self, packet: bytes, ancdata, src: Optional[str], errqueue: bool):
        """(kind, id, seq, responder, the destination the answer is about, the echoed data or None) or None."""
        v6 = self.family == 6
        if errqueue:
            parsed = (parse_error_queue6 if v6 else parse_error_queue)(packet, ancdata)
            if parsed is None:
                return None
            kind, ident, seq, responder = parsed
            return kind, ident, seq, responder, src, None  # the error queue's address is the original destination
        if self.kind == "raw" and not v6:
            answer = parse_raw_packet(packet)
            echoed = None if answer is None else raw_icmp(packet)[8:]
        else:
            # IPv6 sockets and the IPv4 ping socket start at the ICMP header
            answer = (parse_icmp6 if v6 else parse_icmp)(packet)
            echoed = packet[8:]
        if answer is None or (self.kind == "dgram" and answer.kind != "reply"):
            return None  # a ping socket delivers only echo replies on its normal queue
        dest_seen = src if answer.kind == "reply" else answer.quoted_dst
        return answer.kind, answer.ident, answer.seq, src, dest_seen, echoed

    def _handle(self, packet: bytes, ancdata, address, *, errqueue: bool) -> None:
        rx_ns = kernel_timestamp_ns(ancdata)
        parsed = self._parse(packet, ancdata, _source_text(address), errqueue)
        if parsed is None:
            return
        kind, ident, seq, responder, dest_seen, echoed = parsed
        if ident != self._id:
            return
        entry = self._pending.get(seq)
        if entry is None or entry.future.done() or dest_seen is None:
            return
        try:
            if address_key(dest_seen) != entry.dest:
                return
        except (OSError, ValueError):
            return
        # An echo reply must carry our request's data back unchanged (the nonce): id and sequence alone can be guessed
        # or seen by anyone who can sniff or spoof. ICMP errors quote only 8 bytes of the request, so they cannot.
        if kind == "reply" and echoed != entry.payload:
            return
        if rx_ns is not None:
            self.kernel_timestamps += 1
        else:
            self.fallback_timestamps += 1
            rx_ns = time.time_ns()
        rtt = self._rtt_ms(rx_ns - entry.send_ns, time.monotonic_ns() - entry.send_mono_ns)
        entry.future.set_result(ProbeResult(kind, responder, rtt))

    def _rtt_ms(self, wall_ns: int, mono_ns: int) -> float:
        """The kernel stamps answers on the wall clock (CLOCK_REALTIME), so an NTP step between send and answer
        (timesyncd's first sync after boot, for one) shifts the difference by the step: negative, or a false
        spike. The monotonic difference, read here after the event loop woke up, can only be longer than the
        true round trip; a wall-clock RTT outside [0, monotonic] is a step, and the monotonic one is used."""
        if 0 <= wall_ns <= mono_ns + 1_000_000:  # 1 ms slack for clock slewing
            return round(wall_ns / 1_000_000, 3)
        self.clock_step_fallbacks += 1
        return round(mono_ns / 1_000_000, 3)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None:
            if not self._loop.is_closed():
                try:
                    self._loop.remove_reader(self._sock.fileno())
                except (NotImplementedError, OSError, ValueError):  # a loop without readers (tests off Linux)
                    pass
            self._loop = None
        for entry in self._pending.values():
            if not entry.future.done() and not entry.future.get_loop().is_closed():
                # an error, not a cancel: a cancel would surface as CancelledError in whoever awaits the probe, which
                # looks like that task being cancelled (as icmp_windows.py explains)
                entry.future.set_exception(OSError(errno.EBADF, "ICMP backend is closed"))
        self._pending.clear()
        self._sock.close()
