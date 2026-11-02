"""Recorded-style networks for the discovery tests: traces, DNS answers, web pages and UPnP devices, all made up
(RFC 1918 and documentation addresses, example.net names, private-use AS numbers)."""

from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from pengumon.engine.discovery.engine import Deps, DiscoveryOptions, run_discovery
from pengumon.engine.discovery.lookups import origin_name, ptr_name
from pengumon.engine.mtr import TracerouteHop

Hop = Tuple[Optional[str], Optional[float]]


def trace(hops: Sequence[Hop]) -> List[TracerouteHop]:
    return [TracerouteHop(hop_number=n, ip=a, rtt_ms=r if a else None) for n, (a, r) in enumerate(hops, start=1)]


def three_paths(shared: Sequence[Hop], to_1111: Sequence[Hop] = (), to_8888: Sequence[Hop] = (("198.51.100.88", 14.0),),
                to_9999: Sequence[Hop] = (("198.51.100.99", 15.0),), reach: bool = True) -> Dict[str, List[TracerouteHop]]:
    """Traces that share `shared`, then go their own way to each server (which answers when `reach`)."""
    end = 30.0

    def one(own, server):
        hops = list(shared) + list(own)
        if reach:
            hops.append((server, end))
        return trace(hops)

    return {"1.1.1.1": one(to_1111, "1.1.1.1"), "8.8.8.8": one(to_8888, "8.8.8.8"), "9.9.9.9": one(to_9999, "9.9.9.9")}


class FakeDns:
    """PTR names and Team Cymru answers; records every query name."""

    def __init__(self, names: Optional[Dict[str, str]] = None, asns: Optional[Dict[str, Tuple[int, str]]] = None,
                 raw: Optional[Dict[str, List[str]]] = None):
        self.answers: Dict[Tuple[str, str], List[str]] = {}
        for address, name in (names or {}).items():
            self.answers[(ptr_name(address), "PTR")] = [name + "."]
        for address, (number, as_name) in (asns or {}).items():
            self.answers[(origin_name(address), "TXT")] = [f"{number} | 203.0.113.0/24 | ZZ | arin | 2001-01-01"]
            self.answers[(f"AS{number}.asn.cymru.com", "TXT")] = [f"{number} | ZZ | arin | 2001-01-01 | {as_name}"]
        for qname, texts in (raw or {}).items():
            self.answers[(qname, "TXT")] = texts
        self.asked: List[Tuple[str, str]] = []

    async def __call__(self, qname: str, rdtype: str) -> List[str]:
        self.asked.append((qname, rdtype))
        return list(self.answers.get((qname, rdtype), []))


class FakeLan:
    """Web servers on the LAN by host: `pages` maps a host to an HTML body (200) or to a handler
    (request -> Response). Any other host refuses the connection. Records every request."""

    def __init__(self, pages: Optional[Dict[str, object]] = None):
        self.pages = dict(pages or {})
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(request.url.host)
        if page is None:
            raise httpx.ConnectError("connection refused", request=request)
        if callable(page):
            return page(request)
        if request.url.scheme != "http":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=False)

    def hosts(self) -> List[str]:
        return [r.url.host for r in self.requests]


async def discover(traces, *, names=None, asns=None, pages=None, ssdp=(), gateway=None, snmp=None, live=(),
                   options: DiscoveryOptions = DiscoveryOptions(), limited=frozenset(), dns=None, lan=None,
                   answers_ping=(), ipv6="no IPv6 route"):
    dns = dns or FakeDns(names, asns)
    lan = lan or FakeLan(pages)

    traced = []

    async def fake_trace(host):
        traced.append(host)
        return list(traces.get(host, []))

    async def fake_ssdp():
        return list(ssdp)

    async def fake_snmp(address):
        return (snmp or {}).get(address)

    pinged = []

    async def fake_ping(address):
        pinged.append(address)
        return address in answers_ping

    deps = Deps(trace=fake_trace, query=dns, http_client=lan.client, ssdp=fake_ssdp, snmp_get=fake_snmp,
                gateway=lambda: gateway, limited_hosts=frozenset(limited), ping=fake_ping, ipv6=lambda: ipv6)
    result = await run_discovery(options, lambda: list(live), deps)
    result.dns, result.lan, result.pinged, result.traced = dns, lan, pinged, traced
    return result


def by_address(result) -> Dict[str, dict]:
    return {h["address"]: h for h in result.hops}


def kinds(hop: dict) -> set:
    return {e["kind"] for e in hop["evidence"]}


XFINITY_LOGIN = "<html><head><title>Login</title></head><body><form action='check.jst' method='post'></form></body></html>"
ASUS_LOGIN = ("<html><head><title>Router Login</title></head><body><form action='login.cgi'>"
              "<input type='hidden' name='login_authorization' value=''></form></body></html>")
# An AsusWRT front page: only the generic marker (a script sends the browser to the firmware's login page)
ASUS_FRONT = "<HTML><HEAD><script>window.top.location.href='/Main_Login.asp';</script>\n</HEAD></HTML>"
LUCI_INDEX = ('<html><head><meta http-equiv="refresh" content="0; URL=cgi-bin/luci/" /></head>'
              '<body><a href="cgi-bin/luci/">LuCI - Lua Configuration Interface</a></body></html>')
