"""What this host knows of its network: its LAN addresses, its default gateway, and the targets probed when the
config names none."""

import socket
import logging
import struct
from pathlib import Path
from typing import List, Optional, Tuple

from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.models.probes import TargetConfig

logger = logging.getLogger("pengumon.netinfo")


def get_lan_ips() -> List[str]:
    """Discovers non-loopback IPv4 addresses assigned to physical LAN interfaces."""
    ips = set()
    try:
        # Standard UDP socket trick to discover primary outbound interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # 1.1.1.1 is used as dummy target without sending actual packets
        s.connect(("1.1.1.1", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if not primary_ip.startswith("127."):
            ips.add(primary_ip)
    except Exception as e:
        logger.debug(f"Primary socket IP discovery failed: {e}")

    try:
        # Fallback host name resolution
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception as e:
        logger.debug(f"Hostname addrinfo lookup failed: {e}")

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


def get_default_gateway_ip(route_file: Optional[Path] = None) -> Optional[str]:
    """This host's IPv4 default gateway (Linux; `route_file` defaults to /proc/net/route), or None when there is none
    or the route table can't be read. Never a guess: a made-up gateway target would report a hop that does not
    exist."""
    try:
        return parse_default_gateway((route_file or PROC_NET_ROUTE).read_text(encoding="ascii", errors="replace"))
    except OSError:
        return None


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
