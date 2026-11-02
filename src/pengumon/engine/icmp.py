"""ICMP echo with kernel receive timestamps (Linux, IPv4).

icmplib timestamps replies in Python after the event loop wakes up, which on a VM-hosted container
adds 0.3-0.6 ms to every reading (more than the whole LAN round trip). Like iputils ping, this prober
asks the kernel for each reply's arrival time (SO_TIMESTAMPNS), so wakeup and scheduling delays
don't count. One socket is shared by all targets; replies are matched by id and sequence.

The socket is raw when the process may open one (CAP_NET_RAW in the network namespace's owner:
rootful containers, some VM-based runtimes). Otherwise it is an unprivileged ICMP datagram socket ("ping socket"),
which a rootless container on the host network gets when net.ipv4.ping_group_range covers its
group. On a datagram socket the kernel sets the echo id to the socket's local port and delivers
replies without the IP header; SO_TIMESTAMPNS works the same on both.
"""

import asyncio
import os
import socket
import struct
import sys
import time
from typing import Dict, Optional, Tuple

# Python doesn't export it; 35 on Linux x86/arm (also the SCM_ type of the reply's ancillary data)
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35 if sys.platform.startswith("linux") else None)
ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
PAYLOAD = bytes(range(48))  # 8-byte send time + 48 = 56 bytes of ICMP data, same as ping
_CMSG_SPACE = socket.CMSG_SPACE(struct.calcsize("qq")) if hasattr(socket, "CMSG_SPACE") else 0


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class KernelTimestampPinger:
    """Async ICMPv4 echo over one raw or datagram socket, RTT from kernel receive timestamps.

    `kind` is "raw", "dgram" or None (raw if permitted, else dgram). Raises OSError on creation
    when neither socket type is permitted or SO_TIMESTAMPNS isn't available (not Linux);
    callers fall back to icmplib.
    """

    def __init__(self, kind: Optional[str] = None):
        if SO_TIMESTAMPNS is None or not hasattr(socket, "CMSG_SPACE"):
            raise OSError("SO_TIMESTAMPNS not supported on this platform")
        if kind not in (None, "raw", "dgram"):
            raise ValueError(f"unknown ICMP socket kind {kind!r}")
        errors = []
        self._sock = None
        for candidate in [kind] if kind else ["raw", "dgram"]:
            sock_type = socket.SOCK_RAW if candidate == "raw" else socket.SOCK_DGRAM
            try:
                self._sock = socket.socket(socket.AF_INET, sock_type, socket.IPPROTO_ICMP)
                self.kind = candidate
                break
            except OSError as e:
                errors.append(f"{candidate}: {e}")
        if self._sock is None:
            raise OSError("; ".join(errors))
        self._sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        self._sock.setblocking(False)
        if self.kind == "dgram":
            # The kernel uses the bound port as the echo id; bind now so requests and replies carry it
            self._sock.bind(("0.0.0.0", 0))
            self._id = self._sock.getsockname()[1]
        else:
            self._id = os.getpid() & 0xFFFF
        self._seq = 0
        # (sequence) -> (future, destination ip, send time ns on the wall clock, send time ns on the monotonic clock)
        self._pending: Dict[int, Tuple[asyncio.Future, str, int, int]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Replies timed by the kernel vs. by Python (only if the timestamp cmsg is missing)
        self.kernel_timestamps = 0
        self.fallback_timestamps = 0
        # Replies whose wall-clock RTT was impossible because the clock was stepped mid-flight (see _rtt_ms)
        self.clock_step_fallbacks = 0

    def _ensure_reader(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._loop is not None:
                self._loop.remove_reader(self._sock.fileno())
            loop.add_reader(self._sock.fileno(), self._on_readable)
            self._loop = loop

    async def ping(self, address: str, timeout: float) -> Optional[float]:
        """Sends one echo request; returns RTT in ms, or None on timeout."""
        self._ensure_reader()
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        fut = self._loop.create_future()

        send_ns = time.time_ns()  # same clock (CLOCK_REALTIME) as SO_TIMESTAMPNS
        data = struct.pack("!Q", send_ns) + PAYLOAD
        header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, self._id, seq)
        csum = _checksum(header + data)
        packet = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, csum, self._id, seq) + data

        self._pending[seq] = (fut, address, send_ns, time.monotonic_ns())
        try:
            self._sock.sendto(packet, (address, 0))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(seq, None)

    def _on_readable(self):
        while True:
            try:
                packet, ancdata, _flags, (src, _port) = self._sock.recvmsg(2048, _CMSG_SPACE)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            rx_ns = None
            for level, ctype, cdata in ancdata:
                if level == socket.SOL_SOCKET and ctype == SO_TIMESTAMPNS:
                    sec, nsec = struct.unpack("qq", cdata[: struct.calcsize("qq")])
                    rx_ns = sec * 1_000_000_000 + nsec
            kernel_ts = rx_ns is not None
            if not kernel_ts:
                rx_ns = time.time_ns()

            # Raw sockets deliver the IP header first; datagram sockets start at the ICMP header
            ihl = (packet[0] & 0x0F) * 4 if packet and self.kind == "raw" else 0
            if len(packet) < ihl + 8:
                continue
            icmp_type, _code, _csum, pkt_id, seq = struct.unpack("!BBHHH", packet[ihl:ihl + 8])
            if icmp_type != ICMP_ECHO_REPLY or pkt_id != self._id:
                continue
            entry = self._pending.get(seq)
            if not entry:
                continue
            fut, dest, send_ns, send_mono_ns = entry
            if src != dest or fut.done():
                continue
            if kernel_ts:
                self.kernel_timestamps += 1
            else:
                self.fallback_timestamps += 1
            fut.set_result(self._rtt_ms(rx_ns - send_ns, time.monotonic_ns() - send_mono_ns))

    def _rtt_ms(self, wall_ns: int, mono_ns: int) -> float:
        """The kernel stamps replies on the wall clock (CLOCK_REALTIME), so an NTP step between send and reply
        (timesyncd's first sync after boot, for one) shifts the difference by the step: negative, or a false
        spike. The monotonic difference, read here after the event loop woke up, can only be longer than the
        true round trip; a wall-clock RTT outside [0, monotonic] is a step, and the monotonic one is used."""
        if 0 <= wall_ns <= mono_ns + 1_000_000:  # 1 ms slack for clock slewing
            return round(wall_ns / 1_000_000, 3)
        self.clock_step_fallbacks += 1
        return round(mono_ns / 1_000_000, 3)

    def close(self):
        if self._loop is not None:
            self._loop.remove_reader(self._sock.fileno())
            self._loop = None
        self._sock.close()
