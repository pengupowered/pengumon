"""DNS lookups for discovery: the reverse-DNS name of each hop, and who operates a public hop (Team Cymru's DNS
interface to the routing tables).

Every query goes to this host's resolver (dnspython, /etc/resolv.conf or the OS settings), with a short timeout and
at most CONCURRENCY at once. The ASN lookup sends the hop's address to Team Cymru (as a DNS name under
origin.asn.cymru.com, through the resolver): it is asked only for public addresses, never a private, CGNAT,
link-local or special one, and only when the owner leaves the "who operates each hop" box ticked
(docs/discovery.md).

Team Cymru's answers (TXT):
  <reversed address>.origin.asn.cymru.com  ->  "7922 | 203.0.113.0/24 | US | arin | 2006-02-22"
  AS7922.asn.cymru.com                      ->  "7922 | US | arin | 2006-02-22 | EXAMPLE-NET - Example Networks, US"
The first field of an origin answer may hold several AS numbers (a prefix announced by more than one); the first is
used. Anything that does not have that shape is ignored (no ASN rather than a wrong one).
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

from pengumon.drivers.common import clean_text
from pengumon.engine.discovery.addresses import is_public, parse

DNS_TIMEOUT_S = 1.5
CONCURRENCY = 8
TXT_MAX = 512  # a longer TXT answer is not one of Cymru's
MAX_ASN = 4294967295

# (query name, record type "PTR" or "TXT") -> the answers as text; [] for none or any failure
Query = Callable[[str, str], Awaitable[List[str]]]


@dataclass(frozen=True)
class AsnInfo:
    number: int
    name: Optional[str] = None  # the registry's name for it, e.g. "EXAMPLE-NET - Example Networks, US"

    @property
    def operator(self) -> Optional[str]:
        return short_operator(self.name)


def dns_query(timeout: float = DNS_TIMEOUT_S) -> Query:
    """A Query through dnspython's async resolver (the system's resolvers, no search domains)."""
    import dns.asyncresolver
    import dns.exception

    resolver = None

    async def query(qname: str, rdtype: str) -> List[str]:
        nonlocal resolver
        try:
            if resolver is None:
                resolver = dns.asyncresolver.Resolver()
            answer = await resolver.resolve(qname, rdtype, lifetime=timeout, search=False)
        except (dns.exception.DNSException, OSError, ValueError):
            return []
        out = []
        for rr in answer:
            if rdtype == "PTR":
                out.append(rr.target.to_text())
            else:
                out.append(b"".join(rr.strings).decode("ascii", "replace"))
        return out

    return query


def ptr_name(address: str) -> Optional[str]:
    ip = parse(address)
    return ip.reverse_pointer if ip is not None else None


def origin_name(address: str) -> Optional[str]:
    """The Team Cymru origin query for a public address; None for any other address (never asked)."""
    ip = parse(address)
    if ip is None or not is_public(address):
        return None
    if ip.version == 4:
        return ip.reverse_pointer.removesuffix(".in-addr.arpa") + ".origin.asn.cymru.com"
    return ip.reverse_pointer.removesuffix(".ip6.arpa") + ".origin6.asn.cymru.com"


def _fields(text: str) -> Optional[List[str]]:
    if not isinstance(text, str) or len(text) > TXT_MAX:
        return None
    return [f.strip() for f in text.strip().strip('"').split("|")]


def _asn(field: str) -> Optional[int]:
    first = field.split()[0] if field.split() else ""
    if not re.fullmatch(r"[0-9]{1,10}", first):
        return None
    number = int(first)
    return number if 0 < number <= MAX_ASN else None


def parse_origin_txt(text: str) -> Optional[int]:
    """The AS number of an origin answer, or None when the answer is not one."""
    fields = _fields(text)
    if not fields or len(fields) < 3:
        return None
    return _asn(fields[0])


def parse_as_name_txt(text: str, number: int) -> Optional[str]:
    """The AS name of an AS answer for `number` (the answer must be about that AS), or None."""
    fields = _fields(text)
    if not fields or len(fields) < 5 or _asn(fields[0]) != number:
        return None
    return clean_text("|".join(fields[4:]), limit=100)


def short_operator(as_name: Optional[str]) -> Optional[str]:
    """A short operator name from an AS name: "EXAMPLE-NET - Example Networks, LLC, US" -> "Example Networks"."""
    if not as_name:
        return None
    text = as_name.split(" - ", 1)[1] if " - " in as_name else as_name
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1 and re.fullmatch(r"[A-Z]{2}", parts[-1]):
        parts = parts[:-1]  # the country code
    while len(parts) > 1 and parts[-1].rstrip(".").lower() in {"inc", "llc", "ltd", "gmbh", "sa", "ag", "bv", "plc",
                                                                "co", "corp", "limited", "s.a", "b.v"}:
        parts = parts[:-1]
    return clean_text(", ".join(parts), limit=40)


class Lookups:
    """The lookups of one discovery run. `query` defaults to dns_query()."""

    def __init__(self, query: Optional[Query] = None, concurrency: int = CONCURRENCY):
        self._query = query or dns_query()
        self._gate = asyncio.Semaphore(concurrency)
        self.asked: List[str] = []  # every query name sent, for tests and the notes

    async def _ask(self, qname: str, rdtype: str) -> List[str]:
        async with self._gate:
            self.asked.append(qname)
            try:
                return list(await self._query(qname, rdtype))
            except Exception:  # a failed lookup is no answer, never a failed discovery
                return []

    async def reverse_names(self, addresses: Iterable[str]) -> Dict[str, str]:
        """address -> its reverse-DNS name (without the trailing dot), for those that have one."""
        unique = sorted({a for a in addresses if a and ptr_name(a)})

        async def one(address: str):
            for answer in await self._ask(ptr_name(address), "PTR"):
                name = clean_text(answer.strip().rstrip("."), limit=253)
                if name and " " not in name:
                    return address, name.lower()
            return address, None

        return {a: n for a, n in await asyncio.gather(*(one(a) for a in unique)) if n}

    async def asns(self, addresses: Iterable[str]) -> Dict[str, AsnInfo]:
        """address -> its AS (number and name), for the public addresses Team Cymru knows. Other addresses are
        never sent."""
        unique = sorted({a for a in addresses if a and origin_name(a)})

        async def origin(address: str):
            for answer in await self._ask(origin_name(address), "TXT"):
                number = parse_origin_txt(answer)
                if number is not None:
                    return address, number
            return address, None

        numbers = {a: n for a, n in await asyncio.gather(*(origin(a) for a in unique)) if n is not None}

        async def name(number: int):
            for answer in await self._ask(f"AS{number}.asn.cymru.com", "TXT"):
                parsed = parse_as_name_txt(answer, number)
                if parsed:
                    return number, parsed
            return number, None

        names = dict(await asyncio.gather(*(name(n) for n in sorted(set(numbers.values())))))
        return {a: AsnInfo(n, names.get(n)) for a, n in numbers.items()}
