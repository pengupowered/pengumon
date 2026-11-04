"""The traces discovery starts from, and how they become one path.

Three in-process traces (engine/mtr.py), to 1.1.1.1, 8.8.8.8 and 9.9.9.9, one after the other: MtrEngine runs one
trace at a time app-wide anyway, and each takes about one timeout, so the three take a few seconds. The path kept is
the one to 1.1.1.1 (the first server whose trace answered anything, if that one did not), up to but without the
server itself. The other two traces say where the paths part: the divergence point is the last hop on the kept path
that every other trace passes too. Comparing addresses rather than positions keeps a load-balanced provider hop
(another router at the same TTL on one trace) from moving the divergence point early.

The servers themselves are proposed as one group, "Internet", whatever their traces did: they are what the dashboard
probes today.
"""

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from pengumon.engine.mtr import TracerouteHop
from pengumon.observability.telemetry import tracer


@dataclass(frozen=True)
class PublicServer:
    address: str
    name: str
    id: str  # the id the dashboard's default targets use (engine/netinfo.default_targets), so a rediscover matches


PUBLIC_SERVERS: Tuple[PublicServer, ...] = (
    PublicServer("1.1.1.1", "Cloudflare", "cloudflare_dns"),
    PublicServer("8.8.8.8", "Google", "google_dns"),
    PublicServer("9.9.9.9", "Quad9", "quad9_dns"),
)
# Over IPv6, when this host has it: the same three resolvers, one more group ("Internet (IPv6)"). The IPv4 path
# stays the main chain; the IPv6 trace (to the first) only says whether IPv6 gets through.
PUBLIC_SERVERS_V6: Tuple[PublicServer, ...] = (
    PublicServer("2606:4700:4700::1111", "Cloudflare (IPv6)", "cloudflare_dns_v6"),
    PublicServer("2001:4860:4860::8888", "Google (IPv6)", "google_dns_v6"),
    PublicServer("2620:fe::fe", "Quad9 (IPv6)", "quad9_dns_v6"),
)
INTERNET_GROUP = "internet"
INTERNET_V6_GROUP = "internet_v6"
TRACE_MAX_HOPS = 20
TRACE_TIMEOUT_S = 1.5

Trace = Callable[[str], Awaitable[List[TracerouteHop]]]


@dataclass
class PathHop:
    """One TTL of the kept path. `address` is None for a hop that did not answer; `paths` are the servers whose
    trace passed this address (the kept path's own server included)."""

    ttl: int
    address: Optional[str]
    rtt_ms: Optional[float]
    paths: Tuple[str, ...] = ()


@dataclass
class MergedPath:
    hops: List[PathHop]
    divergence: Optional[int]  # index in `hops` of the last hop every answering trace shares (-1: none); None: one trace
    traced_to: Optional[str]  # the server whose trace the path is (None: no trace answered)
    reached: Dict[str, bool] = field(default_factory=dict)  # per server: did it answer its trace
    answered: List[str] = field(default_factory=list)  # servers whose trace returned any hop


async def run_traces(trace: Trace, servers: Sequence[str],
                     progress: Optional[Callable[[str], None]] = None) -> Dict[str, List[TracerouteHop]]:
    """Each server's trace, in order, one at a time."""
    out: Dict[str, List[TracerouteHop]] = {}
    for n, server in enumerate(servers, start=1):
        if progress:
            progress(f"Tracing the path to {server} ({n} of {len(servers)})")
        with tracer.start_as_current_span("discovery.trace", attributes={"discovery.trace.n": n}) as span:
            out[server] = list(await trace(server))
            span.set_attribute("discovery.trace.hops", len(out[server]))
    return out


def _without_server(hops: List[TracerouteHop], server: str) -> Tuple[List[TracerouteHop], bool]:
    """The hops before the server, and whether the server answered (it is then the last hop)."""
    if hops and hops[-1].ip == server:
        return hops[:-1], True
    return hops, False


def merge_paths(traces: Dict[str, List[TracerouteHop]], order: Sequence[str]) -> MergedPath:
    """One path from the traces (keyed by server, `order` saying which is preferred)."""
    stripped: Dict[str, List[TracerouteHop]] = {}
    reached: Dict[str, bool] = {}
    for server in order:
        stripped[server], reached[server] = _without_server(traces.get(server) or [], server)
    answered = [s for s in order if any(h.ip for h in traces.get(s) or [])]
    if not answered:
        return MergedPath(hops=[], divergence=None, traced_to=None, reached=reached, answered=[])
    kept = answered[0]
    seen = {s: {h.ip for h in stripped[s] if h.ip} | ({s} if reached[s] else set()) for s in answered}
    hops = [PathHop(ttl=h.hop_number, address=h.ip, rtt_ms=h.rtt_ms,
                    paths=tuple(s for s in answered if h.ip and h.ip in seen[s]))
            for h in stripped[kept]]
    others = [s for s in answered if s != kept]
    divergence = -1 if others else None  # -1: the traces share no hop at all
    if others:
        for i, hop in enumerate(hops):
            if hop.address and all(hop.address in seen[s] for s in others):
                divergence = i
    return MergedPath(hops=hops, divergence=divergence, traced_to=kept, reached=reached, answered=answered)
