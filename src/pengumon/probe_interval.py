"""The probe round's interval: the fixed choices, and what depends on it (pure functions, no imports from the app).

The interval is a setting (prober.interval_seconds). The rules that count probes (outage onset, recovery, the hop's
window) stay in probes; this module turns them into the time they mean at an interval, the probes a minute must hold
to be judged, and the cost of probing at it.
"""

import ipaddress
import math
from typing import Optional

# The choices, in seconds. 1 s is the default; the rest trade detail for traffic and storage.
INTERVAL_CHOICES = (1, 2, 5, 10, 30, 60)
INTERVAL_LABELS = {"1": "Every second", "2": "Every 2 seconds", "5": "Every 5 seconds", "10": "Every 10 seconds",
                   "30": "Every 30 seconds", "60": "Every minute"}
DEFAULT_INTERVAL = 1

# A minute counts as dead (every probe lost) when it holds at least this many probes at 1 s and 2 s; at a longer
# interval it needs only the probes about half a minute gives.
DEAD_MINUTE_MAX_PROBES = 10
DEAD_MINUTE_SECONDS = 30.0
# An uptime block says nothing with fewer probes than this (or than half of what its interval should give).
UPTIME_MAX_MIN_PROBES = 10


def dead_minute_probes(interval: float) -> int:
    """The probes a minute with every probe lost needs to count as dead: min(10, ceil(30 / interval)). 10 at 1 s and
    2 s, 6 at 5 s, 3 at 10 s, 1 at 30 s and 60 s."""
    interval = _positive(interval)
    return max(1, min(DEAD_MINUTE_MAX_PROBES, math.ceil(DEAD_MINUTE_SECONDS / interval)))


def uptime_min_probes(block_seconds: float, interval: float) -> float:
    """The probes an uptime block needs to be judged at all: min(10, half of what the block's interval gives), at least
    1."""
    return max(1.0, min(float(UPTIME_MAX_MIN_PROBES), block_seconds / _positive(interval) / 2))


def _positive(interval: float) -> float:
    try:
        value = float(interval)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0 else 1.0


def dns_every(base: float, dns: Optional[float]) -> int:
    """How many rounds apart the DNS lookups run: 1 when the lookups follow the base round (dns None), else dns / base,
    rounded and at least 1."""
    if not dns or base <= 0:
        return 1
    return max(1, round(dns / base))


def dns_interval_problem(base: float, dns: Optional[float]) -> Optional[str]:
    """Why `dns` cannot be the lookups' interval at the base `base`, or None when it can (None means every round)."""
    if dns is None:
        return None
    if dns < base:
        return f"must not be shorter than the probe interval ({base:g} s)"
    ratio = dns / base
    if abs(ratio - round(ratio)) > 1e-9:
        return f"must be a whole multiple of the probe interval ({base:g} s)"
    return None


# --- what an interval costs ------------------------------------------------------------------------------------------
#
# Internet traffic: an ICMP echo of 56 data bytes is 84 B (IPv4) or 104 B (IPv6) each way, plus 38 B of Ethernet
# framing each way. A TCP check is a handshake and a close (about 7 small packets), an HTTPS check adds the TLS
# handshake; a DNS lookup is about 150 B there and back. All of it "about".
ETHERNET_FRAMING = 38
ECHO_BYTES = {4: 84, 6: 104}
TCP_CHECK_BYTES = 700
HTTP_CHECK_BYTES = 1500
HTTPS_CHECK_BYTES = 5000
DNS_LOOKUP_BYTES = 150
# Storage: the samples table's pages over its rows on the live database (326 MB over 8.2 million rows: its primary
# key is the table, so there is no separate index), rounded up.
SAMPLE_ROW_BYTES = 40.0
SECONDS_A_DAY = 86400
DAYS_A_MONTH = 30


def hop_round_bytes(probe: str, family: int, https: bool = True) -> int:
    """Traffic of one probe of a hop, both ways, framing included."""
    if probe == "tcp":
        return TCP_CHECK_BYTES
    if probe == "http":
        return HTTPS_CHECK_BYTES if https else HTTP_CHECK_BYTES
    return 2 * (ECHO_BYTES.get(family, 84) + ETHERNET_FRAMING)


def monthly_traffic_bytes(interval: float, hop_bytes_per_round: float, dns_lookups: int,
                          dns_interval: Optional[float] = None) -> float:
    """Internet traffic of a month of probing: the hops past the gateway every `interval`, and `dns_lookups` lookups
    every `dns_interval` (None: every round)."""
    interval = _positive(interval)
    per_second = hop_bytes_per_round / interval + dns_lookups * DNS_LOOKUP_BYTES / _positive(dns_interval or interval)
    return per_second * SECONDS_A_DAY * DAYS_A_MONTH


def daily_storage_bytes(interval: float, hops: int, dns_lookups: int, dns_interval: Optional[float] = None) -> float:
    """Stored bytes of a day of raw samples: a row per hop every `interval` and per lookup every `dns_interval`."""
    interval = _positive(interval)
    rows_per_second = hops / interval + dns_lookups / _positive(dns_interval or interval)
    return rows_per_second * SECONDS_A_DAY * SAMPLE_ROW_BYTES


def unseen_seconds(interval: float) -> float:
    """Drops shorter than about this may fall between two probes."""
    return float(_positive(interval))


def effect_seconds(interval: float, probes: int) -> float:
    """The time `probes` rounds take at `interval` (the rules count probes; this is what they mean in time)."""
    return probes * float(_positive(interval))


def _is_past_gateway(target) -> bool:
    """Whether probing the hop sends traffic out to the internet: the ISP's and the public hops, and a custom one that
    is not a private or loopback address (a host name is taken to be outside)."""
    if target.category in ("local", "gateway"):
        return False
    if target.category == "custom":
        try:
            address = ipaddress.ip_address(target.address)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local)
    return True


def probing_cost(targets) -> dict:
    """The per-round figures the Settings page multiplies by the interval (api.settings_models.ProbingCost): the
    enabled hops, the traffic of one round of those past the gateway, and the constants."""
    enabled = [t for t in targets if getattr(t, "enabled", True)]
    traffic = sum(hop_round_bytes(t.probe, t.family, getattr(t, "http_scheme", "https") == "https")
                  for t in enabled if _is_past_gateway(t))
    return {"hops": len(enabled), "hop_bytes_per_round": traffic, "dns_lookup_bytes": DNS_LOOKUP_BYTES,
            "sample_bytes": SAMPLE_ROW_BYTES}
