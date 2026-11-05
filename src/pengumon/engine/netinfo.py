"""What this host knows of its network: its LAN addresses, its default gateway, and the targets probed when the
config names none."""

import asyncio
import ipaddress
import socket
import logging
import errno
import struct
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.netinfo")


HOSTNAME_LOOKUP_SECONDS = 1.0


def _hostname_ips(timeout: float = HOSTNAME_LOOKUP_SECONDS) -> List[str]:
    """The IPv4 addresses this host's name resolves to, given up after `timeout` seconds. On macOS a host name with
    no DNS entry is looked up over mDNS, which can block for a minute; the lookup cannot be cancelled, so it runs in
    a daemon thread that is left to finish on its own."""
    found: List[List[str]] = []

    def run() -> None:
        try:
            found.append([info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)])
        except OSError as e:
            logger.debug(f"Hostname addrinfo lookup failed: {e}")

    thread = threading.Thread(target=run, name="pengumon-hostname-lookup", daemon=True)
    thread.start()
    thread.join(timeout)
    if not found:
        if thread.is_alive():
            logger.debug(f"Hostname addrinfo lookup gave no answer within {timeout:g} s")
        return []
    return found[0]


def get_lan_ips(hostname_ips: Callable[[], List[str]] = _hostname_ips) -> List[str]:
    """This host's non-loopback IPv4 LAN addresses: the one it sends from to the internet (a UDP connect() picks the
    route and source address without sending anything), else, where there is no route, the addresses its host name
    resolves to (`hostname_ips`), private ranges first. ["127.0.0.1"] when neither finds one."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if not primary_ip.startswith("127."):
            ips.add(primary_ip)
    except OSError as e:
        logger.debug(f"Primary socket IP discovery failed: {e}")

    if not ips:
        ips.update(ip for ip in hostname_ips() if not ip.startswith("127."))

    # Prioritize private subnet ranges (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    sorted_ips = sorted(
        list(ips),
        key=lambda ip: (0 if ip.startswith("192.168.") else (1 if ip.startswith("10.") else 2))
    )
    return sorted_ips or ["127.0.0.1"]


# --- the default gateway, and what is probed when the config names no targets ------------------------------------

# RTF_UP | RTF_GATEWAY in /proc/net/route's Flags column (linux/route.h)
_RTF_UP, _RTF_GATEWAY = 0x1, 0x2
PROC_NET_ROUTE = Path("/proc/net/route")


def parse_default_gateway(route_table: str) -> Optional[str]:
    """The IPv4 default gateway from the text of /proc/net/route: the usable default route (destination and mask
    0.0.0.0, flags up + gateway) with the lowest metric. Addresses there are little-endian hex. None without one."""
    best: Optional[Tuple[int, str]] = None
    for line in route_table.splitlines()[1:]:  # first line: column headers
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination, gateway, flags = int(fields[1], 16), int(fields[2], 16), int(fields[3], 16)
            metric, mask = int(fields[6]), int(fields[7], 16)
        except ValueError:
            continue
        if destination or mask or not gateway or flags & (_RTF_UP | _RTF_GATEWAY) != (_RTF_UP | _RTF_GATEWAY):
            continue
        if best is None or metric < best[0]:
            best = (metric, socket.inet_ntoa(struct.pack("<L", gateway)))
    return best[1] if best else None


# /proc/net/ipv6_route: destination, its prefix length, source, its prefix length, next hop (32 hex digits each but
# the prefix lengths, 2), then metric, refcount, use and flags (8 hex digits each) and the interface (net/ipv6/route.c,
# rt6_info_seq_show). RTF_REJECT marks the "unreachable" routes the kernel keeps on lo.
_RTF_REJECT = 0x0200
PROC_NET_IPV6_ROUTE = Path("/proc/net/ipv6_route")


def parse_ipv6_default_router(route_table: str) -> Optional[str]:
    """The IPv6 default router from the text of /proc/net/ipv6_route: the usable default route (::/0, flags up +
    gateway, not a reject route) with the lowest metric. A link-local router (the usual case: routers advertise
    themselves by their fe80:: address) gets its interface as the zone, `fe80::1%eth0`, which is what a probe to it
    needs. None without one."""
    best: Optional[Tuple[int, str]] = None
    for line in route_table.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        dest, dest_len, nexthop = fields[0], fields[1], fields[4]
        try:
            if int(dest, 16) or int(dest_len, 16) or len(nexthop) != 32:
                continue
            gateway = bytes.fromhex(nexthop)
            metric, flags = int(fields[5], 16), int(fields[8], 16)
        except ValueError:
            continue
        if not any(gateway) or flags & (_RTF_UP | _RTF_GATEWAY) != (_RTF_UP | _RTF_GATEWAY) or flags & _RTF_REJECT:
            continue
        router = ipaddress.IPv6Address(gateway)
        text = f"{router}%{fields[9]}" if router.is_link_local else str(router)
        if best is None or metric < best[0]:
            best = (metric, text)
    return best[1] if best else None


def get_ipv6_default_router(route_file: Optional[Path] = None) -> Optional[str]:
    """This host's IPv6 default router (Linux, from /proc/net/ipv6_route), or None where there is none or no such
    file (macOS, Windows, a kernel without IPv6)."""
    try:
        return parse_ipv6_default_router((route_file or PROC_NET_IPV6_ROUTE).read_text(encoding="ascii", errors="replace"))
    except OSError:
        return None


# --- is there IPv6 here? ---------------------------------------------------------------------------------------------

IPV6_CHECK_ADDRESS = "2606:4700:4700::1111"  # Cloudflare's resolver: any public IPv6 address would do
IPV6_RECHECK_SECONDS = 300.0
# A send that fails with one of these means this host cannot send IPv6 there at all (no route, no source address,
# no IPv6 in the OS), not that the target is down. Windows: the WSA numbers, and the Win32 error IcmpSendEcho2 gives.
NO_IPV6_ERRNOS = frozenset(
    {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, 10051, 10049, 10047, 1231}
)
IPV6_UNAVAILABLE = "IPv6 unavailable on this host"


def ipv6_route_check(address: str = IPV6_CHECK_ADDRESS) -> Optional[str]:
    """None when this host has a route to the IPv6 `address`, else why not. A UDP connect() picks the route and the
    source address without sending anything."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect((address, 53))
    except OSError as e:
        return e.strerror or str(e)
    return None


class Ipv6Availability:
    """Whether this host can send IPv6 to the internet, checked at most every `recheck_seconds` (networks change:
    a laptop joins another network, a router starts or stops advertising a prefix). `no_route` records a send that
    failed for lack of IPv6; the verdict then holds until the next check. `check` (ipv6_route_check) and `clock`
    are for tests."""

    def __init__(self, check: Callable[[], Optional[str]] = ipv6_route_check, clock: Callable[[], float] = time.monotonic,
                 recheck_seconds: float = IPV6_RECHECK_SECONDS):
        self._check = check
        self._clock = clock
        self.recheck_seconds = recheck_seconds
        self._checked_at: Optional[float] = None
        self.reason: Optional[str] = None  # why not, while unavailable

    def available(self) -> bool:
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self.recheck_seconds:
            was = self.reason
            self.reason = self._check()
            self._checked_at = now
            if self.reason != was:
                if self.reason:
                    logger.info(f"{IPV6_UNAVAILABLE} ({self.reason}): IPv6 targets are not probed; checked again "
                                f"every {self.recheck_seconds:g} s")
                elif was:
                    logger.info("IPv6 is available again: IPv6 targets are probed")
        return self.reason is None

    def no_route(self, error: OSError) -> None:
        if self.reason is None:
            logger.info(f"{IPV6_UNAVAILABLE} (sending failed: {error.strerror or error})")
        self.reason = f"sending failed: {error.strerror or error}"
        self._checked_at = self._clock()


def is_no_ipv6_error(error: OSError) -> bool:
    return getattr(error, "errno", None) in NO_IPV6_ERRNOS


def needs_route_check(address: str) -> bool:
    """Whether an IPv6 probe to `address` depends on this host's route to the internet: not for ::1 or a link-local
    address (they work on any host with IPv6 at all; a send failure still says when there is none)."""
    try:
        ip = ipaddress.IPv6Address(address)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_link_local)


GATEWAY_PROBE_ADDRESS = "1.1.1.1"
GATEWAY_PROBE_TIMEOUT = 1.0
_RFC1918 = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_rfc1918(address: Optional[str]) -> bool:
    try:
        ip = ipaddress.IPv4Address(address or "")
    except ValueError:
        return False
    return any(ip in net for net in _RFC1918)


def gateway_from_ttl_probe(timeout: float = GATEWAY_PROBE_TIMEOUT, open_backend: Optional[Callable] = None) -> Optional[str]:
    """The first router on the way to the internet: the responder of a TTL=1 echo request to 1.1.1.1, when it is a
    private (RFC 1918) address, else None. Runs in its own thread with its own event loop and backend, so it works
    from sync code and from inside a running loop alike, and gives up after about `timeout` seconds."""
    from pengumon.engine import icmp_select

    found: List[Optional[str]] = [None]

    def run() -> None:
        try:
            backend = (open_backend or icmp_select.open_backend)()
        except OSError as e:  # IcmpUnavailable included
            logger.debug(f"Gateway probe not sent: {e}")
            return
        try:
            result = asyncio.run(backend.probe(GATEWAY_PROBE_ADDRESS, timeout=timeout, ttl=1))
        except OSError as e:
            logger.debug(f"Gateway probe failed: {e}")
            return
        finally:
            backend.close()
        if result.kind == "ttl_exceeded" and is_rfc1918(result.responder):
            found[0] = result.responder

    thread = threading.Thread(target=run, name="pengumon-gateway-probe", daemon=True)
    thread.start()
    thread.join(timeout + 1.0)
    return found[0]


def get_default_gateway_ip(route_file: Optional[Path] = None,
                           probe_gateway: Optional[Callable[[], Optional[str]]] = None) -> Optional[str]:
    """This host's IPv4 default gateway, or None. On Linux from the route table (`route_file` defaults to
    /proc/net/route): never a guess there, a made-up gateway target would report a hop that does not exist. Where
    the route table can't be read (macOS, Windows, a sandbox without /proc), the first router that answers a TTL=1
    probe (`probe_gateway`, default gateway_from_ttl_probe), if it is a private address."""
    try:
        return parse_default_gateway((route_file or PROC_NET_ROUTE).read_text(encoding="ascii", errors="replace"))
    except OSError:
        pass
    return (probe_gateway or gateway_from_ttl_probe)()


def default_targets(gateway: Optional[str]) -> List[TargetConfig]:
    """Targets used when the config has none: the default gateway (when known) and two public anycast resolvers."""
    targets = []
    if gateway:
        targets.append(TargetConfig(id="local_router", name=f"Local router ({gateway})",
                                    category="local", address=gateway, enabled=True))
    targets += [
        TargetConfig(id="cloudflare_dns", name="Public: Cloudflare (1.1.1.1)", category="public",
                     address="1.1.1.1", enabled=True),
        TargetConfig(id="google_dns", name="Public: Google (8.8.8.8)", category="public",
                     address="8.8.8.8", enabled=True),
    ]
    return targets


def default_dns_probes() -> List[DnsProbeConfig]:
    """DNS probes used when the config has none: the system resolver, and Cloudflare's asked directly."""
    return [
        DnsProbeConfig(id="dns_system_google", name="DNS Lookup: google.com (System DNS)", domain="google.com",
                       record_type="A", enabled=True),
        DnsProbeConfig(id="dns_direct_cloudflare", name="DNS Lookup: cloudflare.com (Direct via 1.1.1.1)",
                       domain="cloudflare.com", nameserver="1.1.1.1", record_type="A", enabled=True),
    ]
