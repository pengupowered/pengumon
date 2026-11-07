"""Address classes and host-name domains, the plainest evidence about who operates a hop.

Classes: `private` (RFC 1918, IPv6 unique local fc00::/7), `cgnat` (100.64.0.0/10, the shared space providers use
for carrier-grade NAT), `link_local` (169.254/16, fe80::/10), `loopback`, `special` (anything else that is not a
routable unicast address: 0/8, 192.0.0.0/24, 198.18/15, multicast, reserved, NAT64) and `public`.

The documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) count as public: they never
appear on a real path, and examples and tests use them as stand-ins for public addresses. Nothing is ever sent to an
outside service about an address unless it is `public` (lookups.py checks again).

The registrable domain of a host name ("be-1.core.example.net" -> "example.net") is a small heuristic, not the
Public Suffix List (no new dependency): the last two labels, or the last three when the second-to-last is a common
second-level label under a two-letter country code ("co.uk", "com.au", "ne.jp", ...). Its limits: suffixes that are
not of that form (private registries such as "github.io" or "cloudfront.net", three-level suffixes like
"pvt.k12.ma.us") come out one label too short or too long. For grouping the routers of one operator that is enough:
those rarely name routers, and a wrong guess costs a "different domain" reason, never a wrong address.
"""

import ipaddress
import re
from typing import Optional

PRIVATE, CGNAT, LINK_LOCAL, LOOPBACK, SPECIAL, PUBLIC = "private", "cgnat", "link_local", "loopback", "special", "public"

_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DOCUMENTATION = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))
_ULA = ipaddress.ip_network("fc00::/7")

# The plain words the evidence uses for each class.
CLASS_WORDS = {
    PRIVATE: "a private address",
    CGNAT: "a carrier-grade NAT address (100.64.0.0/10)",
    LINK_LOCAL: "a link-local address",
    LOOPBACK: "a loopback address",
    SPECIAL: "a special-purpose address",
    PUBLIC: "a public address",
}


def parse(address: Optional[str]):
    """The address as an ipaddress object, or None when it is not an address."""
    try:
        return ipaddress.ip_address((address or "").strip())
    except ValueError:
        return None


def address_class(address: Optional[str]) -> Optional[str]:
    """One of the classes above, or None when `address` is not an IP address."""
    ip = parse(address)
    if ip is None:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return SPECIAL
    if any(ip in net for net in _DOCUMENTATION if net.version == ip.version):
        return PUBLIC
    if ip.is_loopback:
        return LOOPBACK
    if ip.is_link_local:
        return LINK_LOCAL
    if ip.version == 4:
        if any(ip in net for net in _RFC1918):
            return PRIVATE
        if ip in _CGNAT:
            return CGNAT
    elif ip in _ULA:
        return PRIVATE
    if ip.is_global and not ip.is_multicast:
        return PUBLIC
    return SPECIAL


def is_public(address: Optional[str]) -> bool:
    return address_class(address) == PUBLIC


def same_subnet(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two addresses share a /24 (IPv4) or /64 (IPv6): a plain "same network" test for two home hops."""
    ip_a, ip_b = parse(a), parse(b)
    if ip_a is None or ip_b is None or ip_a.version != ip_b.version:
        return False
    prefix = 24 if ip_a.version == 4 else 64
    return ipaddress.ip_network(f"{ip_a}/{prefix}", strict=False) == ipaddress.ip_network(f"{ip_b}/{prefix}",
                                                                                           strict=False)


# Second-level labels that are a public suffix under most two-letter country codes ("co.uk", "com.au", "ne.jp").
_SECOND_LEVEL = frozenset({
    "ac", "ad", "biz", "co", "com", "ed", "edu", "gen", "go", "gob", "gov", "gr", "info", "lg", "ltd", "me", "mil",
    "ne", "net", "nhs", "nom", "or", "org", "plc", "sch",
})
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def registrable_domain(name: Optional[str]) -> Optional[str]:
    """The registrable domain of a host name (see the module docstring for the heuristic and its limits); None for
    anything that is not a dotted DNS name (a bare label, an address, a name with odd characters)."""
    if not name:
        return None
    labels = name.strip().rstrip(".").lower().split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        return None
    if labels[-1] == "arpa":
        return None  # an unanswered PTR comes back as the in-addr.arpa name itself in some resolvers
    take = 2
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL and len(labels) >= 3:
        take = 3
    return ".".join(labels[-take:])
