"""What every socket-based ICMP backend (icmp_linux, icmp_macos) builds the same way: the echo request (RFC 792 for
ICMPv4, RFC 4443 for ICMPv6), its checksum, and the checks of a probe's address and TTL. Standard library only;
imports on every OS."""

import ipaddress
import os
import socket
import struct
from typing import NamedTuple, Optional, Tuple

ICMP_ECHO_REQUEST = 8
ICMP6_DEST_UNREACH = 1
ICMP6_PACKET_TOO_BIG = 2
ICMP6_TIME_EXCEEDED = 3
ICMP6_ECHO_REQUEST = 128
ICMP6_ECHO_REPLY = 129
IPPROTO_ICMPV6 = 58
# Packet Too Big counts as unreachable: our 104-byte probes fit every IPv6 link (1280 bytes at least), so a router
# that says they don't cannot pass them
ERROR6_KINDS = {ICMP6_TIME_EXCEEDED: "ttl_exceeded", ICMP6_DEST_UNREACH: "unreachable",
                ICMP6_PACKET_TOO_BIG: "unreachable"}
_IPV6_HEADER = 40
_FILLER = bytes(range(40))
NONCE_SIZE = 8



def checksum(data: bytes) -> int:
    """The Internet checksum (RFC 1071) of `data`."""
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def echo_request(ident: int, seq: int, data: bytes) -> bytes:
    """An ICMP echo request (type 8, code 0) with its checksum filled in."""
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, ident, seq)
    return struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, checksum(header + data), ident, seq) + data


def echo6_request(ident: int, seq: int, data: bytes) -> bytes:
    """An ICMPv6 echo request (type 128, code 0) with a zero checksum. The ICMPv6 checksum covers a pseudo-header with
    the source address (RFC 4443 2.3), which only the kernel knows when it sends, so the kernel fills it in: Linux for
    every ICMPv6 raw socket (raw(7): IPV6_CHECKSUM is fixed at offset 2 for IPPROTO_ICMPV6) and ping socket, macOS for
    ICMPv6 sockets (xnu rip6_output), whatever the field held."""
    return struct.pack("!BBHHH", ICMP6_ECHO_REQUEST, 0, 0, ident, seq) + data


def ip_literal(address: str) -> Tuple[str, int]:
    """(the address in its canonical text, 4 or 6) if `address` is an IPv4 or IPv6 literal; ValueError otherwise (the
    backends never resolve names). An IPv6 link-local address keeps its zone (`fe80::1%eth0`); an IPv4-mapped IPv6
    address (`::ffff:192.0.2.1`) is its IPv4 address."""
    if not isinstance(address, str):
        raise ValueError(f"not an IP address: {address!r}")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(f"not an IP address: {address!r}") from None
    if ip.version == 6 and ip.ipv4_mapped is not None and ip.scope_id is None:
        return str(ip.ipv4_mapped), 4
    return str(ip), ip.version


def sockaddr(address: str, version: int) -> tuple:
    """The sendto() address of a canonical literal from ip_literal: (host, 0) for IPv4, (host, 0, 0, scope id) for
    IPv6, the zone's interface name turned into its index (OSError when there is no such interface)."""
    if version == 4:
        return (address, 0)
    host, _, zone = address.partition("%")
    if not zone:
        return (host, 0, 0, 0)
    return (host, 0, 0, int(zone) if zone.isdigit() else socket.if_nametoindex(zone))


def address_key(address: str) -> bytes:
    """The packed address of a literal (its zone dropped), to compare an answer's addresses with a probe's whatever
    their spelling: the kernel reports `2001:db8::1`, a quoted header holds 16 bytes."""
    host = address.partition("%")[0]
    return socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)


def address_text(packed: bytes, scope_id: int = 0) -> str:
    """The canonical text of a 4- or 16-byte address; a link-local IPv6 address with a scope gets its zone
    (`fe80::1%eth0`, the interface's index when it has no name)."""
    if len(packed) == 4:
        return socket.inet_ntoa(packed)
    text = str(ipaddress.IPv6Address(packed))
    if scope_id and ipaddress.IPv6Address(packed).is_link_local:
        try:
            zone = socket.if_indextoname(scope_id)
        except (OSError, AttributeError):  # gone, or Windows without the call
            zone = str(scope_id)
        text = f"{text}%{zone}"
    return text


class Answer(NamedTuple):
    """What an ICMP message says about one of our requests: its kind, the echo id and sequence, and the destination
    the request was sent to (from the quoted IP header; None for an echo reply, whose source is the target)."""

    kind: str
    ident: int
    seq: int
    quoted_dst: Optional[str]


def parse_icmp6(icmp: bytes) -> Optional[Answer]:
    """An ICMPv6 message (starting at its type byte; IPv6 sockets never return the IP header): an echo reply, or a
    Time Exceeded / Destination Unreachable / Packet Too Big that quotes an echo request (RFC 4443: type, code,
    checksum, 4 bytes (unused, or the MTU), then the invoking packet: its 40-byte IPv6 header and as much of the rest as
    fits; our requests have no extension headers, so the echo header follows at once). None for anything else, and for
    anything too short for what it claims."""
    if len(icmp) < 8:
        return None
    icmp_type = icmp[0]
    if icmp_type == ICMP6_ECHO_REPLY:
        if icmp[1] != 0:
            return None
        ident, seq = struct.unpack_from("!HH", icmp, 4)
        return Answer("reply", ident, seq, None)
    kind = ERROR6_KINDS.get(icmp_type)
    if kind is None or len(icmp) < 8 + _IPV6_HEADER + 8:
        return None
    quoted = icmp[8:]
    if quoted[0] >> 4 != 6 or quoted[6] != IPPROTO_ICMPV6 or quoted[_IPV6_HEADER] != ICMP6_ECHO_REQUEST:
        return None
    ident, seq = struct.unpack_from("!HH", quoted, _IPV6_HEADER + 4)
    return Answer(kind, ident, seq, address_text(quoted[24:40]))


def check_ttl(ttl: int) -> int:
    """`ttl` if it is an int in 1..255 (ValueError otherwise)."""
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 255:
        raise ValueError(f"ttl must be 1..255, not {ttl!r}")
    return ttl


def new_nonce() -> bytes:
    """A random per-backend secret that goes out in every request's data and must come back in the echo reply."""
    return os.urandom(NONCE_SIZE)


def probe_payload(send_ns: int, nonce: bytes) -> bytes:
    """The data of one echo request: the send time (8 bytes), the backend's nonce (8 bytes) and a fixed filler, 56
    bytes as ping sends. An echo reply is accepted only if it carries exactly these bytes: whoever cannot see our
    requests (or guess the nonce) cannot forge a reply, even when it knows the id and sequence."""
    return struct.pack("!Q", send_ns) + nonce + _FILLER
