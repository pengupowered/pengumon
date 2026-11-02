"""A scripted ICMP backend (pengumon.engine.icmp_base's interface) for tests of what uses one: the prober,
traceroute, gateway discovery and pengumon doctor."""

import asyncio
from typing import Callable, Dict, List, Optional, Tuple

from pengumon.engine.icmp_base import ProbeResult


class FakePath:
    """A route: `hops` are the routers at TTL 1, 2, ... (None: that router never answers); the target answers past
    them. `rtt` per hop is 1.0, 2.0, ... ms. `unreachable_at` makes the router at that TTL answer Unreachable instead
    of forwarding."""

    def __init__(self, hops: List[Optional[str]], target_answers: bool = True, unreachable_at: Optional[int] = None):
        self.hops = hops
        self.target_answers = target_answers
        self.unreachable_at = unreachable_at

    def answer(self, address: str, ttl: int) -> ProbeResult:
        if self.unreachable_at is not None and ttl >= self.unreachable_at:
            return ProbeResult("unreachable", self.hops[self.unreachable_at - 1], float(self.unreachable_at))
        if ttl <= len(self.hops):
            router = self.hops[ttl - 1]
            return ProbeResult("ttl_exceeded", router, float(ttl)) if router else ProbeResult("timeout")
        if self.target_answers:
            return ProbeResult("reply", address, float(len(self.hops) + 1))
        return ProbeResult("timeout")


class FakeBackend:
    name = "fake"
    resolution_ms = 0.001

    def __init__(self, answer: Optional[Callable[[str, int], ProbeResult]] = None, path: Optional[FakePath] = None,
                 delay: float = 0.0):
        self._answer = answer or (path.answer if path else (lambda address, ttl: ProbeResult("reply", address, 1.0)))
        self.delay = delay
        self.sent: List[Tuple[str, int, float]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = False

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        self.sent.append((address, ttl, timeout))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            result = self._answer(address, ttl)
            if isinstance(result, BaseException):
                raise result
            # a timeout takes the whole timeout; an answer takes `delay`
            await asyncio.sleep(timeout if result.kind == "timeout" else self.delay)
            return result
        finally:
            self.in_flight -= 1

    def close(self) -> None:
        self.closed = True


def results_by_ttl(backend: FakeBackend) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for _address, ttl, _timeout in backend.sent:
        counts[ttl] = counts.get(ttl, 0) + 1
    return counts
