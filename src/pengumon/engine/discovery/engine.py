"""One discovery run: traces, lookups and device checks, the inference, the draft and its diff.

The traces run first and one at a time (about TRACE_TIMEOUT_S each). The lookups and the device checks then run
together, each bounded by its own short timeouts and the whole phase by CHECKS_DEADLINE_S, so a run takes about ten
seconds and never much more than twenty. Every outside part can be replaced (`Deps`) for tests and for the one-off
run on a real network; by default they are the app's own (the shared ICMP backend, the system resolver).

Nothing here changes anything: the result is a draft and some notes.
"""

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, FrozenSet, List, Optional, Sequence, Tuple

import httpx

from pengumon.engine.discovery import devices, upnp
from pengumon.engine.discovery.addresses import LINK_LOCAL, PRIVATE, PUBLIC, address_class
from pengumon.engine.discovery.draft import address_key, diff_against, missing_home_hops, proposed_hops, trim
from pengumon.engine.discovery.infer import Facts, Inference, infer
from pengumon.engine.discovery.lookups import Lookups, Query
from pengumon.engine.discovery.traces import (
    PUBLIC_SERVERS, PUBLIC_SERVERS_V6, TRACE_MAX_HOPS, TRACE_TIMEOUT_S, MergedPath, PathHop, Trace, merge_paths,
    run_traces,
)

CHECKS_DEADLINE_S = 15.0
PING_TIMEOUT_S = 1.0
MAX_PINGED = 8  # live home hops off the traced path, pinged before they are called gone
MAX_DEVICE_HOSTS = 8  # web pages fetched at most (the private hops before the first public one, and the modem address)


@dataclass(frozen=True)
class DiscoveryOptions:
    asn_lookup: bool = True  # Team Cymru, for public hops (docs/discovery.md says what is sent)
    device_checks: bool = True  # web page of private hops and 192.168.100.1, UPnP IGD
    snmp: bool = False  # SNMP "public" sysDescr of private hops


@dataclass
class Deps:
    trace: Optional[Trace] = None  # host -> its hops (default: MtrEngine on the shared backend)
    query: Optional[Query] = None  # DNS (default: dnspython's async resolver)
    http_client: Optional[Callable[[], httpx.AsyncClient]] = None
    ssdp: Optional[upnp.Search] = None
    snmp_get: Optional[Callable[[str], Awaitable[Optional[str]]]] = None
    gateway: Optional[Callable[[], Optional[str]]] = None  # this host's default gateway (route table)
    ping: Optional[Callable[[str], Awaitable[bool]]] = None  # does this address answer an echo request
    ipv6: Optional[Callable[[], Optional[str]]] = None  # why this host has no IPv6 route out, or None (netinfo)
    # Hosts that get at most one GET of their front page and nothing else (no HTTPS retry, no redirect, no UPnP
    # description or SOAP, no SNMP): for a device that must not be bothered.
    limited_hosts: FrozenSet[str] = frozenset()


@dataclass
class DiscoveryResult:
    hops: List[dict]
    notes: List[str]
    path: MergedPath
    facts: Facts
    inference: Inference


def _default_trace(host: str):
    from pengumon.engine.mtr import MtrEngine
    return MtrEngine(target_host=host, max_hops=TRACE_MAX_HOPS, timeout=TRACE_TIMEOUT_S,
                     family=6 if ":" in host else 4).run_trace()


def _default_ipv6() -> Optional[str]:
    from pengumon.engine.netinfo import ipv6_route_check
    return ipv6_route_check()


def _default_gateway() -> Optional[str]:
    from pengumon.engine.netinfo import get_default_gateway_ip
    return get_default_gateway_ip(probe_gateway=lambda: None)  # the route table only: the trace's first hop is enough


async def _default_ping(address: str) -> bool:
    from pengumon.engine.icmp_select import resolve_address, shared_backend
    try:
        target, _ = await resolve_address(address, PING_TIMEOUT_S)
        result = await shared_backend().probe(target, timeout=PING_TIMEOUT_S)
    except (OSError, ValueError):
        return False
    return result.kind == "reply"


def live_path_from_targets(targets: Sequence) -> List[dict]:
    """The live path in the shape of GET /api/path's hops, from the probe targets (TargetConfig) in order."""
    servers = {s.address for s in PUBLIC_SERVERS}
    out = []
    for position, t in enumerate(targets):
        hop = t.model_dump() if hasattr(t, "model_dump") else dict(t)
        if hop.get("group") is None:  # the public servers are one node on Path health whether grouped or not
            hop["group"] = "internet" if hop.get("category") == "public" and hop.get("address") in servers else None
        hop.setdefault("ignores_ping", None)
        hop.setdefault("device_id", None)
        hop["position"] = position
        out.append(hop)
    return out


async def _checks(path: MergedPath, options: DiscoveryOptions, deps: Deps):
    """rDNS, ASN, web pages, UPnP and SNMP, all at once. Returns (names, asns, pages, modem page, igds, snmp)."""
    lookups = Lookups(deps.query)
    addresses = [h.address for h in path.hops if h.address]
    public = [a for a in addresses if address_class(a) == PUBLIC]
    first_public = next((i for i, h in enumerate(path.hops) if h.address and address_class(h.address) == PUBLIC),
                        len(path.hops))
    private = []
    for h in path.hops[:first_public]:
        if h.address and address_class(h.address) in (PRIVATE, LINK_LOCAL) and h.address not in private:
            private.append(h.address)
    private = private[:MAX_DEVICE_HOSTS - 1]
    limited = {address_key(a) for a in deps.limited_hosts}

    async def pages(client):
        hosts = list(private)
        if devices.CABLE_MODEM_ADDRESS not in hosts:
            hosts.append(devices.CABLE_MODEM_ADDRESS)

        async def one(address):
            if address_key(address) in limited:
                return address, await devices.fetch_page(address, client, budget=1, fallback_https=False)
            return address, await devices.fetch_page(address, client)

        return dict(await asyncio.gather(*(one(a) for a in hosts)))

    async def igds(client):
        # only a gateway that is a hop of the traced path is followed (anything else on the LAN can claim to be one)
        return await upnp.discover_igds(client, deps.ssdp, skip=lambda a: address_key(a) in limited,
                                        allowed={address_key(a) for a in addresses})

    async def asns(igds_task):
        # this host's own public address (the router's UPnP-reported external address, or a hop that equals it) is
        # never sent to the ASN lookup
        own = {address_key(i.external_ip) for i in (await igds_task) if i.external_ip}
        return await lookups.asns([a for a in public if address_key(a) not in own])

    async def snmp():
        get = deps.snmp_get or devices.snmp_sysdescr
        targets = [a for a in private if address_key(a) not in limited]
        answers = await asyncio.gather(*(get(a) for a in targets))
        return {a: d for a, d in zip(targets, answers) if d}

    async def nothing():
        return {}

    client = (deps.http_client or devices.new_client)()
    try:
        igds_task = asyncio.ensure_future(igds(client) if options.device_checks else asyncio.sleep(0, result=[]))
        try:
            names, found_asns, found_pages, found_igds, found_snmp = await asyncio.gather(
                lookups.reverse_names(addresses),
                asns(igds_task) if options.asn_lookup else nothing(),
                pages(client) if options.device_checks else nothing(),
                igds_task,
                snmp() if options.snmp else nothing(),
            )
        finally:
            igds_task.cancel()
    finally:
        await client.aclose()
    modem = found_pages.pop(devices.CABLE_MODEM_ADDRESS, None) if devices.CABLE_MODEM_ADDRESS not in private else \
        found_pages.get(devices.CABLE_MODEM_ADDRESS)
    found_pages = {a: p for a, p in found_pages.items() if p is not None}
    return names, found_asns, found_pages, modem, found_igds, found_snmp


async def _ipv6(deps: Deps, say: Callable[[str], None], notes: List[str]) -> Optional[Tuple[Optional[int], bool]]:
    """None when this host has no IPv6 route to the internet (and a note why), else what the IPv6 trace to the first
    IPv6 server found: (its hop count, or None when nothing answered; whether the server answered)."""
    reason = await asyncio.to_thread(deps.ipv6 or _default_ipv6)
    if reason is not None:
        notes.append(f"This host has no IPv6 route to the internet ({reason}), so no IPv6 targets are proposed.")
        return None
    server = PUBLIC_SERVERS_V6[0].address
    say(f"Tracing the IPv6 path to {server}")
    hops = list(await (deps.trace or _default_trace)(server))
    reached = bool(hops) and address_key(hops[-1].ip) == address_key(server)
    if not any(h.ip for h in hops):
        notes.append("This host has IPv6, but its IPv6 trace got no answer; the IPv6 servers are proposed anyway.")
        return None, False
    return len(hops), reached


async def run_discovery(options: DiscoveryOptions, live_path: Callable[[], Any],
                        deps: Optional[Deps] = None,
                        progress: Optional[Callable[[str], None]] = None) -> DiscoveryResult:
    """Discovers the path and returns the draft (contract-shaped hops, diffed against `live_path()`) and notes.
    `live_path()` gives the live hops, or (awaitable or not) a pair (live hops, hops removed from the path)."""
    deps = deps or Deps()
    say = progress or (lambda text: None)
    notes: List[str] = []
    order = [s.address for s in PUBLIC_SERVERS]
    traces = await run_traces(deps.trace or _default_trace, order, say)
    path = merge_paths(traces, order)
    gateway = await asyncio.to_thread(deps.gateway or _default_gateway)
    if not path.hops:
        notes.append("No trace got an answer from any router (ICMP may be unavailable here, or blocked); the draft "
                     "has the public servers" + (" and this host's default gateway." if gateway else "."))
        if gateway:
            path.hops.append(PathHop(ttl=1, address=gateway, rtt_ms=None, paths=()))
    elif path.traced_to != order[0]:
        notes.append(f"The trace to {order[0]} got no answer; the path is the one to {path.traced_to}.")
    if path.divergence is None and path.hops:
        notes.append("Only one trace answered, so where the paths part is not known.")
    unreached = [s for s in order if not path.reached.get(s)]
    if unreached and path.hops:
        notes.append(f"Not answering its trace: {', '.join(unreached)}.")
    ipv6 = await _ipv6(deps, say, notes)

    say("Looking up names" + (", who operates each hop" if options.asn_lookup else "") +
        (" and checking devices" if options.device_checks or options.snmp else ""))
    try:
        names, asns, pages, modem, igds, snmp = await asyncio.wait_for(
            _checks(path, options, deps), CHECKS_DEADLINE_S)
    except asyncio.TimeoutError:
        notes.append("The lookups and device checks took too long and were left out.")
        names, asns, pages, modem, igds, snmp = {}, {}, {}, None, [], {}
    if not options.asn_lookup:
        notes.append("The ASN lookup was off: who operates each public hop is judged from names and the paths only.")
    if not options.device_checks:
        notes.append("Device checks were off.")
    igds_on_path = [i for i in igds if i.address in {h.address for h in path.hops}]
    if len(igds) > len(igds_on_path):
        notes.append(f"{len(igds) - len(igds_on_path)} UPnP gateway(s) answered that are not on the path.")

    say("Working out who operates each hop")
    facts = Facts(path=path, names=names, asns=asns, pages=pages, snmp=snmp, igds=igds_on_path, gateway=gateway)
    inference = infer(facts, modem_page=modem)
    notes.extend(inference.notes)
    proposed = proposed_hops(inference.hops, path.reached, ipv6)
    got = live_path()
    if inspect.isawaitable(got):
        got = await got
    live, removed = (list(got[0]), list(got[1])) if isinstance(got, tuple) else (list(got), [])
    missing = missing_home_hops(proposed, live)[:MAX_PINGED]
    answers = await asyncio.gather(*((deps.ping or _default_ping)(a) for a in missing))
    answering = [a for a, ok in zip(missing, answers) if ok]
    if answering:
        notes.append(f"Not on the traced path but answering ping, so kept as they are (a modem in bridge mode is "
                     f"not seen by a trace): {len(answering)} hop(s) of your network.")
    draft = diff_against(proposed, live, answering, removed)
    trimmed = trim(draft)
    if len(trimmed) < len(draft):
        notes.append(f"The path is longer than {len(trimmed)} hops; the internet hops nearest the servers were left "
                     "out.")
    say("Done")
    return DiscoveryResult(hops=trimmed, notes=notes, path=path, facts=facts, inference=inference)
