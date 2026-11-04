"""Hop-by-hop traceroute discovery engine (MTR style), in-process through the ICMP backend.

Echo requests go out with TTL 1, 2, ... max_hops, PROBES_PER_HOP of each, all at once (a few ms apart): each router
on the way answers the probe whose TTL runs out there with a Time Exceeded, and the target answers with an echo
reply. So a trace takes about one timeout however long the path is, like `traceroute -N 16`. No traceroute or
tracert program and no text parsing: the same code runs on every OS the backend runs on. IPv6 is the same with the
hop limit in place of the TTL: an IPv6 address is traced over ICMPv6, and a host name over its IPv6 address when
`family` is 6.
"""

import asyncio
import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from pengumon.engine.discovery.addresses import CGNAT, LINK_LOCAL, LOOPBACK, PRIVATE, address_class
from pengumon.engine.icmp_base import IcmpBackend, ProbeResult
from pengumon.engine.icmp_packet import address_key
from pengumon.engine.icmp_select import resolve_address, shared_backend
from pengumon.observability.telemetry import traceroute_duration, traceroute_hops

logger = logging.getLogger("pengumon.mtr")

# The address classes a hop label names (a public address needs no word).
_CLASS_LABELS = {PRIVATE: "private", CGNAT: "CGNAT", LINK_LOCAL: "link-local", LOOPBACK: "loopback"}

PROBES_PER_HOP = 3
SEND_SPACING_S = 0.002  # between two sends, so a burst of 45 requests doesn't arrive at the first router at once
# IPv6 is sent more slowly: a provider network was seen dropping ICMPv6 probes from one source in a burst (at 2 or 20
# ms apart only the first probe to each router was answered and the target never replied; at 100 ms apart every hop
# and the target answered). A 15-hop IPv6 trace then takes about 4.5 s plus one timeout.
SEND_SPACING_V6_S = 0.1


# At most one trace runs at a time in the app: the incident path trace and /api/mtr/trace share these. A trace sends
# max_hops * PROBES_PER_HOP requests at once, which on Windows occupy worker threads (icmp_windows) and everywhere add
# a burst to the network. A second caller for the same target and settings waits for the running trace and gets its
# result; one for another target waits its turn. (Callers are few: the API is rate limited to 6 a minute.)
_gate: Optional[Tuple[Any, asyncio.Lock]] = None  # the lock belongs to one event loop
_running: Dict[tuple, "asyncio.Task[List[TracerouteHop]]"] = {}


def _trace_lock() -> asyncio.Lock:
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Lock())
    return _gate[1]


class TracerouteHop(BaseModel):
    hop_number: int
    ip: Optional[str] = None
    hostname: Optional[str] = None
    rtt_ms: Optional[float] = None
    label: Optional[str] = None


def summarize_hops(results: List[List[ProbeResult]], target: str) -> List[tuple]:
    """(hop number, responder, average rtt ms) per TTL from each TTL's probe results, up to where the trace ends:
    the first hop whose answer is the target's echo reply, or an Unreachable (nothing beyond it answers). When
    neither came back, the hops up to the last one that answered, plus one timed-out hop to show the path goes
    on unseen. A hop answered by several routers (load balancing) is shown as the one that answered most."""
    hops = []
    end = None
    target_key = _key(target)
    for ttl, probes in enumerate(results, start=1):
        answered = [p for p in probes if p.kind != "timeout" and p.responder]
        if not answered:
            hops.append((ttl, None, None))
            continue
        responder = Counter(p.responder for p in answered).most_common(1)[0][0]
        rtts = [p.rtt_ms for p in answered if p.responder == responder and p.rtt_ms is not None]
        hops.append((ttl, responder, round(sum(rtts) / len(rtts), 2) if rtts else None))
        if any(p.kind == "reply" and _key(p.responder) == target_key for p in answered) or \
                any(p.kind == "unreachable" for p in answered):
            end = ttl
            break
    if end is None:
        last = max((h[0] for h in hops if h[1] is not None), default=0)
        hops = hops[:last + 1]
    return hops


def _key(address: Optional[str]):
    """An address compared by its value (`2001:DB8::1` is `2001:db8::1`), or the text itself if it is not one."""
    try:
        return address_key(address)
    except (OSError, ValueError, TypeError, AttributeError):
        return address


class MtrEngine:
    """Discovers the hop-by-hop route to a host. `backend` defaults to the app's shared ICMP backend."""

    def __init__(
        self,
        target_host: str = "1.1.1.1",
        max_hops: int = 15,
        timeout: float = 1.5,
        known_targets: Optional[List] = None,
        backend: Optional[IcmpBackend] = None,
        family: int = 4,
    ):
        if target_host.startswith("-"):
            # Never an option-like value (the models refuse them too; this guards anything constructed by hand)
            raise ValueError("invalid target host")
        if not 1 <= max_hops <= 255:
            raise ValueError("max_hops must be 1..255")
        self.target_host = target_host
        self.max_hops = max_hops
        self.timeout = timeout
        self.known_targets = known_targets or []
        self._backend = backend
        self.family = family  # for a host name: trace to its IPv4 (4) or IPv6 (6) address

    async def run_trace(self) -> List[TracerouteHop]:
        """The hops to the target; [] when the trace can't run (no ICMP here, the name doesn't resolve)."""
        try:
            address, _version = await resolve_address(self.target_host, self.timeout, self.family)
            backend = self._backend if self._backend is not None else shared_backend()
        except (ValueError, OSError) as ex:  # IcmpUnavailable is an OSError
            logger.warning(f"Traceroute to {self.target_host} not run: {getattr(ex, 'reason', ex)}")
            return []
        key = (asyncio.get_running_loop(), id(backend), address, self.max_hops, self.timeout)
        task = _running.get(key)
        if task is None:
            task = asyncio.ensure_future(self._one_at_a_time(backend, address))
            _running[key] = task
            task.add_done_callback(lambda t, k=key: _running.pop(k, None) if _running.get(k) is t else None)
        # shielded: a caller that goes away (a closed request) doesn't cancel the trace the others wait for
        return await asyncio.shield(task)

    async def _one_at_a_time(self, backend: IcmpBackend, address: str) -> List[TracerouteHop]:
        async with _trace_lock():
            started = time.perf_counter()  # (not the wait for another trace's turn)
            results = await self._probe_all(backend, address)
            took = time.perf_counter() - started
        hops = [
            TracerouteHop(hop_number=hop_no, ip=ip, rtt_ms=rtt, label=self._label_hop(hop_no, ip))
            for hop_no, ip, rtt in summarize_hops(results, address)
        ]
        attributes = {"family": "6" if ":" in address else "4"}
        traceroute_duration.record(took, attributes)
        traceroute_hops.record(len(hops), attributes)
        return hops

    async def _probe_all(self, backend: IcmpBackend, address: str) -> List[List[ProbeResult]]:
        async def one(delay: float, ttl: int) -> ProbeResult:
            await asyncio.sleep(delay)
            try:
                return await backend.probe(address, timeout=self.timeout, ttl=ttl)
            except OSError as ex:
                logger.debug(f"Traceroute probe ttl={ttl} to {address} failed: {ex}")
                return ProbeResult("timeout")

        order = [(round_no, ttl) for round_no in range(PROBES_PER_HOP) for ttl in range(1, self.max_hops + 1)]
        spacing = SEND_SPACING_V6_S if ":" in address else SEND_SPACING_S
        done = await asyncio.gather(*(one(i * spacing, ttl) for i, (_r, ttl) in enumerate(order)))
        results: List[List[ProbeResult]] = [[] for _ in range(self.max_hops)]
        for (_r, ttl), result in zip(order, done):
            results[ttl - 1].append(result)
        return results

    def _label_hop(self, hop_no: int, ip: Optional[str]) -> str:
        """A plain label: a configured target's name when the hop is one, else its position and, for an address that
        is not public, its class ("Hop 3", "Hop 1 (private)"). No guess at what equipment it is: the path can be any
        shape (an all-in-one ISP router, a router and a modem, carrier-grade NAT or not)."""
        if not ip:
            return "Request Timed Out"
        for t in self.known_targets:
            t_addr = getattr(t, "address", None)
            t_name = getattr(t, "name", None)
            if t_addr and t_name and _key(t_addr) == _key(ip):
                return t_name
        words = _CLASS_LABELS.get(address_class(ip))
        return f"Hop {hop_no} ({words})" if words else f"Hop {hop_no}"
