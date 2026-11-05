"""Which ICMP backend this OS uses (pengumon.engine.icmp_base has the interface), and the one the app shares.

Linux: icmp_linux (raw or ping socket, kernel timestamps). macOS: icmp_macos (ICMP datagram socket). Both open one
socket per address family, paired by icmp_dual (the IPv6 one on the first IPv6 probe). Windows: icmp_windows
(IcmpSendEcho2 and Icmp6SendEcho2 in a thread pool). Each module is imported only on its own OS, so a missing
platform API never matters elsewhere.

The app opens one backend (`shared_backend`) for the prober, traceroute and the incident path trace: one socket,
one id, many probes in flight. `open_backend` gives a separate one (pengumon doctor, the gateway guess at startup,
which run outside the app's event loop).
"""

import asyncio
import logging
import socket
import sys
import time
from typing import Optional, Tuple

from pengumon.engine.icmp_base import IcmpBackend, IcmpUnavailable
from pengumon.engine.icmp_dual import DualStackBackend
from pengumon.engine.icmp_packet import ip_literal
from pengumon.models.hosts import HOSTNAME_PATTERN

logger = logging.getLogger("pengumon.icmp")

RETRY_SECONDS = 60.0  # after a failed open, how long shared_backend() reports the same failure before trying again


def open_backend(platform: Optional[str] = None) -> IcmpBackend:
    """A new backend for this OS (`platform` defaults to sys.platform). Raises IcmpUnavailable when it can't work."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        from pengumon.engine.icmp_linux import LinuxIcmpBackend
        v4 = LinuxIcmpBackend()
        return DualStackBackend(v4, lambda: LinuxIcmpBackend(v4.kind, family=6))  # the same kind: the same rules
    if platform == "darwin":
        from pengumon.engine.icmp_macos import MacIcmpBackend
        return DualStackBackend(MacIcmpBackend(), lambda: MacIcmpBackend(family=6))
    if platform == "win32":
        from pengumon.engine.icmp_windows import WindowsIcmpBackend
        return WindowsIcmpBackend()
    raise IcmpUnavailable(f"no ICMP backend for this operating system ({platform}); Linux, macOS and Windows have one")


_shared: Optional[IcmpBackend] = None
_failure: Optional[IcmpUnavailable] = None
_failed_at = 0.0


def shared_backend() -> IcmpBackend:
    """The app's backend, opened on first use. After a failure the same IcmpUnavailable is raised for RETRY_SECONDS
    (so a probe round doesn't retry for every target), then opening is tried again (e.g. after the sysctl fix)."""
    global _shared, _failure, _failed_at
    if _shared is not None:
        return _shared
    if _failure is not None and time.monotonic() - _failed_at < RETRY_SECONDS:
        raise _failure
    try:
        _shared = open_backend()
    except IcmpUnavailable as e:
        if _failure is None or str(_failure) != str(e):
            logger.warning(f"ICMP probing is unavailable: {e.reason}" + (f" Fix: {e.fix}" if e.fix else ""))
        _failure, _failed_at = e, time.monotonic()
        raise
    _failure = None
    logger.info(f"ICMP probes use the {_shared.name} backend ({describe_resolution(_shared.resolution_ms)}).")
    return _shared


def close_shared_backend() -> None:
    global _shared, _failure
    if _shared is not None:
        _shared.close()
    _shared, _failure = None, None


def describe_resolution(resolution_ms: float) -> str:
    if resolution_ms >= 1:
        return f"{resolution_ms:g} ms resolution"
    return f"{resolution_ms * 1000:g} µs resolution"


async def resolve_address(host: str, timeout: float, family: int = 4) -> Tuple[str, int]:
    """(the address to probe, 4 or 6) for a target's address: an IP literal as is (its own family, whatever `family`
    says; an IPv6 link-local one keeps its zone), a name through the system resolver: its first address of `family`
    (4: IPv4, 6: IPv6). Raises ValueError for anything that is not an address or a host name, and OSError when the
    name doesn't resolve (to that family) in time."""
    if family not in (4, 6):
        raise ValueError(f"family must be 4 or 6, not {family!r}")
    try:
        return ip_literal(host)
    except ValueError:
        pass
    if not HOSTNAME_PATTERN.fullmatch(host):
        raise ValueError("invalid address")  # what the models refuse (an option-like value, spaces), built by hand
    loop = asyncio.get_running_loop()
    af = socket.AF_INET if family == 4 else socket.AF_INET6
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, family=af, type=socket.SOCK_DGRAM), timeout)
    except asyncio.TimeoutError:
        raise OSError(f"{host}: name lookup timed out") from None
    except socket.gaierror as e:
        raise OSError(f"{host}: {e.strerror or e}") from None
    for _family, _type, _proto, _canon, sockaddr in infos:
        return ip_literal(sockaddr[0])
    raise OSError(f"{host}: no IPv{family} address")
