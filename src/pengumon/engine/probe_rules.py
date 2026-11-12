"""Where TCP and HTTP probes may connect, and how many there may be (the path's socket hops).

A hop probed by a TCP connection or an HTTP request makes this host open a connection to an address the owner typed.
Anyone who can edit the path could so make the host reach what only the host itself reaches: its own loopback (admin
APIs, this app) and link-local addresses (cloud metadata at 169.254.169.254). Those are refused, as a literal when the
path is edited (`socket_hop_problem`) and as the resolved address at every probe (`refused_address`, called by
NetworkProber.socket_attempt), which also defeats a name that is made to point at 127.0.0.1. Public, private-network and
carrier-grade addresses are allowed: the person can reach those anyway, and this host's own LAN address is a private
address like any other (Service checks watch this computer's own apps). docs/security.md has the whole picture.
"""

from typing import Iterable, Optional

from pengumon.engine.discovery.addresses import CLASS_WORDS, LINK_LOCAL, LOOPBACK, SPECIAL, address_class
from pengumon.models.probes import MAX_SOCKET_HOPS, TargetConfig

__all__ = ["MAX_SOCKET_HOPS", "refused_address", "socket_hop_problem"]


def refused_address(address: str) -> Optional[str]:
    """Why a TCP or HTTP probe never connects to `address` (words that follow "is"), or None when it may.

    Loopback, link-local and special-purpose addresses (0.0.0.0/8, ::, multicast, broadcast, reserved, and IPv4-mapped
    IPv6 such as ::ffff:127.0.0.1) and the names localhost and *.localhost are refused."""
    host = (address or "").strip().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]  # an IPv6 zone
    cls = address_class(host)
    if cls in (LOOPBACK, LINK_LOCAL, SPECIAL):
        return CLASS_WORDS[cls]
    lowered = host.lower()
    if cls is None and (lowered == "localhost" or lowered.endswith(".localhost")):
        return "a loopback name"
    return None


def socket_hop_problem(targets: Iterable[TargetConfig]) -> Optional[str]:
    """The first reason a path's TCP and HTTP hops are refused (too many, or one aimed at the host itself), or None."""
    socket_hops = [t for t in targets if t.probe != "icmp"]
    if len(socket_hops) > MAX_SOCKET_HOPS:
        return (f"At most {MAX_SOCKET_HOPS} hops may be probed by TCP or HTTP (each opens a connection every second)")
    for t in socket_hops:
        why = refused_address(t.address)
        if why:
            return (f"\"{t.name}\": {t.address} is {why}; TCP and HTTP probes never connect there "
                    "(Settings, Service checks watch this computer's own apps)")
    return None
