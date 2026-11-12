"""A TCP connect probe: the time for the handshake (SYN to SYN-ACK), one round trip like a ping.

On Linux the kernel measured that round trip itself (TCP_INFO's tcpi_rtt, microseconds), so how late the event loop
woke up does not matter, as with SO_TIMESTAMPNS for ICMP. Elsewhere, or when the kernel says 0, the reading is the
app's own perf_counter time: an upper bound, never below the true handshake. No data is sent: the socket is closed at
once (a FIN, not an RST: a reset every second looks like a port scan to a firewall). Which addresses may be probed is
decided by the caller (engine.probe_rules); this module connects to what it is given.

Errors follow the ICMP backends' contract: a refusal and a timeout are answers (`SocketAnswer`), any other OSError is
raised to the prober, which handles IPv6 and unreachable networks in one place.
"""

import asyncio
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

KERNEL_TIMING = sys.platform.startswith("linux") and hasattr(socket, "TCP_INFO")
# struct tcp_info (linux/tcp.h): 8 x __u8, then __u32 rto, ato, snd_mss, rcv_mss, unacked, sacked, lost, retrans,
# fackets, last_data_sent, last_ack_sent, last_data_recv, last_ack_recv, pmtu, rcv_ssthresh, rtt (the 16th u32)
_TCPI_RTT_OFFSET = 8 + 15 * 4
_TCP_INFO_SIZE = 104


@dataclass(frozen=True)
class SocketAnswer:
    kind: str  # reply | timeout | refused | tls | http_error | bad_answer
    rtt_ms: Optional[float] = None  # tcp: the handshake; http: connect start to status line
    connect_ms: Optional[float] = None  # http only: the handshake inside rtt_ms
    status: Optional[int] = None  # http status code
    error: Optional[str] = None  # the ProbeSample.error text; None for "reply"


def kernel_rtt_ms(sock) -> Optional[float]:
    """The kernel's handshake round trip of a connected socket in ms; None when this OS has no such figure, the answer
    is too short, or the kernel's value is 0."""
    if not KERNEL_TIMING:
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, _TCP_INFO_SIZE)
    except OSError:
        return None
    return rtt_from_tcp_info(raw)


def rtt_from_tcp_info(raw: bytes) -> Optional[float]:
    """tcpi_rtt (microseconds, the kernel's native byte order) of a struct tcp_info as ms; None when absent or 0."""
    if len(raw) < _TCPI_RTT_OFFSET + 4:
        return None
    micros = struct.unpack_from("=I", raw, _TCPI_RTT_OFFSET)[0]
    return micros / 1000 if micros else None


async def connect_socket(address: str, version: int, port: int, *,
                         kernel_rtt: Callable[[socket.socket], Optional[float]] = kernel_rtt_ms
                         ) -> Tuple[socket.socket, float]:
    """A socket connected to address:port and the handshake's time in ms (the kernel's when it has one, else the
    app's). Closes the socket on any exception, cancellation included, and re-raises; after a return the caller owns
    it."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET6 if version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        sockaddr = (address, port, 0, 0) if version == 6 else (address, port)
        start = time.perf_counter()
        await loop.sock_connect(sock, sockaddr)
        ms = (time.perf_counter() - start) * 1000
        return sock, kernel_rtt(sock) or ms
    except BaseException:
        sock.close()
        raise


def refused(port: int) -> SocketAnswer:
    return SocketAnswer("refused", error=f"Connection refused on port {port}")


async def tcp_connect(address: str, version: int, port: int, timeout: float, *,
                      kernel_rtt: Callable[[socket.socket], Optional[float]] = kernel_rtt_ms) -> SocketAnswer:
    """One handshake to address:port, closed at once. A refusal and a timeout are answers; any other OSError (no route,
    network unreachable, ...) is raised."""
    try:
        async with asyncio.timeout(timeout):
            sock, ms = await connect_socket(address, version, port, kernel_rtt=kernel_rtt)
    except ConnectionRefusedError:
        return refused(port)
    except TimeoutError:
        return SocketAnswer("timeout", error="No answer in time (TCP handshake)")
    sock.close()
    return SocketAnswer("reply", rtt_ms=ms)
