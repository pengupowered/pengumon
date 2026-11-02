"""One IcmpBackend for IPv4 and IPv6 out of two single-family ones (the socket backends: icmp_linux, icmp_macos).

An ICMP socket speaks one address family, so the Linux and macOS backends open one per family; this pairs them under
the interface the prober and traceroute use, sending each probe to the one its address needs. The IPv6 one is
opened on the first IPv6 probe: a host with IPv4 targets only never opens it, and a host without IPv6 fails only
the IPv6 probes. A failed open is remembered for RETRY_SECONDS, then tried again (IPv6 can be turned on later).
"""

import errno
import socket
import time
from typing import Callable, Optional

from pengumon.engine.icmp_base import IcmpBackend, IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_packet import ip_literal

RETRY_SECONDS = 60.0


def _host_has_ipv6() -> bool:
    """Whether this OS can make IPv6 sockets at all (no: a kernel built or booted without IPv6)."""
    if not socket.has_ipv6:
        return False
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_DGRAM).close()
    except OSError as e:
        return e.errno not in (errno.EAFNOSUPPORT, getattr(errno, "WSAEAFNOSUPPORT", errno.EAFNOSUPPORT))
    return True


class DualStackBackend:
    """IPv4 probes go to `v4`; IPv6 probes to the backend `open_v6()` returns, opened on first use. `name` and
    `resolution_ms` are the IPv4 backend's (both families use the same kind of socket); `ipv6_name` is the IPv6 one's
    once it is open."""

    def __init__(self, v4: IcmpBackend, open_v6: Callable[[], IcmpBackend], *, _has_ipv6=_host_has_ipv6,
                 _clock=time.monotonic):
        self.v4 = v4
        self.v6: Optional[IcmpBackend] = None
        self._open_v6 = open_v6
        self._has_ipv6 = _has_ipv6
        self._clock = _clock
        self._v6_error: Optional[OSError] = None
        self._v6_failed_at = 0.0
        self._closed = False
        self.name = v4.name
        self.resolution_ms = v4.resolution_ms

    @property
    def ipv6_name(self) -> Optional[str]:
        return self.v6.name if self.v6 is not None else None

    def ipv6_backend(self) -> IcmpBackend:
        """The IPv6 backend, opened now if it isn't. OSError(EAFNOSUPPORT) when this host has no IPv6 at all,
        IcmpUnavailable when the socket can't be opened for another reason."""
        if self._closed:
            raise OSError(errno.EBADF, "ICMP backend is closed")
        if self.v6 is not None:
            return self.v6
        if self._v6_error is not None and self._clock() - self._v6_failed_at < RETRY_SECONDS:
            raise self._v6_error
        try:
            self.v6 = self._open_v6()
        except OSError as e:
            if not self._has_ipv6():
                e = OSError(errno.EAFNOSUPPORT, "this host has no IPv6")
            self._v6_error, self._v6_failed_at = e, self._clock()
            raise e
        self._v6_error = None
        return self.v6

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        if self._closed:
            raise OSError(errno.EBADF, "ICMP backend is closed")
        _text, version = ip_literal(address)  # ValueError for a name or anything else
        backend = self.v4 if version == 4 else self.ipv6_backend()
        return await backend.probe(address, timeout=timeout, ttl=ttl)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.v4.close()
        finally:
            if self.v6 is not None:
                self.v6.close()

    def __getattr__(self, attr):
        # what tests and scripts read of the IPv4 backend (kind, kernel_timestamps, ...)
        if attr.startswith("__") or attr in ("v4", "v6"):
            raise AttributeError(attr)
        return getattr(self.v4, attr)


__all__ = ["DualStackBackend", "IcmpUnavailable", "RETRY_SECONDS"]
