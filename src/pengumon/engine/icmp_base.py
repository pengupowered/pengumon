"""The ICMP backend interface: one per OS, chosen at startup by pengumon.engine.icmp_select.

Every backend sends ICMP echo requests (ICMPv4 to an IPv4 address, ICMPv6 to an IPv6 one) without admin rights and
reports what came back: an echo reply from the target, a Time Exceeded from a router on the way (when `ttl`, the hop
limit for IPv6, is smaller than the path), a Destination Unreachable, or nothing within `timeout`. The prober asks for echo replies (ttl 64); traceroute and gateway discovery ask with ttl
1, 2, 3, ... and read the responder's address.

Backends must allow many probes in flight at once (one per target per round, from one asyncio loop) and match each
answer to its own request (id and sequence; for Time Exceeded and Unreachable, from the original header quoted in the
ICMP error).
"""

from dataclasses import dataclass
from typing import Literal, Optional, Protocol

ProbeKind = Literal["reply", "ttl_exceeded", "unreachable", "timeout"]


@dataclass(frozen=True)
class ProbeResult:
    kind: ProbeKind
    responder: Optional[str] = None  # the address that answered (the target for "reply", a router otherwise)
    rtt_ms: Optional[float] = None  # None for "timeout"


class IcmpUnavailable(OSError):
    """No backend can work here. `reason` says why in plain words; `fix` is the one step that would make it work
    (e.g. the sysctl line for Debian-family Linux), or None."""

    def __init__(self, reason: str, fix: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


class IcmpBackend(Protocol):
    name: str  # e.g. "linux-dgram", "linux-raw", "macos-dgram", "windows-icmpapi"
    resolution_ms: float  # how fine rtt_ms is: about 0.001 with kernel timestamps, 1.0 for IcmpSendEcho2

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        """One echo request to an IPv4 or IPv6 address (a literal, never a name; a link-local IPv6 address carries
        its zone, `fe80::1%eth0`). `ttl` is the IPv6 hop limit for an IPv6 address. Raises ValueError for anything
        else, OSError only when sending fails (e.g. no route, or no IPv6 on this host: errno ENETUNREACH,
        EADDRNOTAVAIL or EAFNOSUPPORT); a lost probe is ProbeResult("timeout")."""
        ...

    def close(self) -> None:
        ...
