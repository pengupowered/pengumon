"""Preview before Apply: the draft hops of the path editor probed for a short while, so they show real latency and
loss before the owner commits to them (POST /api/path/preview, api/path.py).

A preview probes each hop it is given (the API leaves out the hops already probed live) once a second for
PREVIEW_SECONDS, through the app's ICMP backend, and records nothing: no sample, no statistics of the prober. It is
single flight per client (a new preview from the same client stops its previous one), at most MAX_PREVIEWS run at
once app-wide, and one stops early when the editor asks (DELETE) or closes. A finished preview is kept KEEP_SECONDS
for its last read, then forgotten.
"""

import asyncio
import secrets
import statistics
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

PREVIEW_SECONDS = 30
INTERVAL_SECONDS = 1.0
MAX_PREVIEWS = 2
KEEP_SECONDS = 120.0
STAGGER_SECONDS = 0.025  # between the hops of one round, as the prober staggers its probes

# One probe of an address: (address, family) -> {"kind": reply|timeout|unreachable|ttl_exceeded|ipv6_unavailable|
# error, "rtt_ms": float or None, ...} (api/path.probe_address).
Probe = Callable[[str, int], Awaitable[dict]]

_KINDS = {"reply": "reply", "timeout": "timeout", "ipv6_unavailable": "ipv6_unavailable"}


class PreviewBusy(Exception):
    """MAX_PREVIEWS previews are running already."""


@dataclass
class HopTally:
    sent: int = 0
    received: int = 0
    rtts: List[float] = field(default_factory=list)
    last_ms: Optional[float] = None
    kind: Optional[str] = None

    def add(self, answer: dict) -> None:
        kind = _KINDS.get(answer.get("kind"), "unreachable")  # an unreachable, a TTL exceeded, a failed lookup
        self.kind = kind
        if kind == "ipv6_unavailable":
            return  # nothing was sent: not a loss
        self.sent += 1
        rtt = answer.get("rtt_ms")
        if kind == "reply" and rtt is not None:
            self.received += 1
            self.rtts.append(float(rtt))
            self.last_ms = round(float(rtt), 2)

    def public(self) -> dict:
        return {
            "sent": self.sent, "received": self.received,
            "loss_pct": round((self.sent - self.received) / self.sent * 100, 1) if self.sent else None,
            "median_ms": round(statistics.median(self.rtts), 2) if self.rtts else None,
            "last_ms": self.last_ms, "kind": self.kind,
        }


@dataclass
class Preview:
    id: str
    client: str
    hops: Dict[str, Tuple[str, int]]  # hop id -> (address, family)
    started: float
    state: str = "running"  # running | done
    finished: Optional[float] = None
    tallies: Dict[str, HopTally] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None


class PreviewStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic, seconds: int = PREVIEW_SECONDS,
                 interval: float = INTERVAL_SECONDS, stagger: float = STAGGER_SECONDS):
        self._clock = clock
        self.seconds = seconds
        self.interval = interval
        self.stagger = stagger
        self._previews: Dict[str, Preview] = {}

    def running(self) -> List[Preview]:
        return [p for p in self._previews.values() if p.state == "running"]

    async def start(self, client: str, hops: Dict[str, Tuple[str, int]], probe: Probe) -> str:
        """Starts probing `hops` for `client` and returns the preview's id. The client's previous preview, if one is
        running, is stopped first. Raises PreviewBusy when MAX_PREVIEWS others are running."""
        self._prune()
        for old in [p for p in self.running() if p.client == client]:
            await self._stop(old)
        if len(self.running()) >= MAX_PREVIEWS:
            raise PreviewBusy()
        preview = Preview(id=secrets.token_hex(8), client=client, hops=dict(hops), started=self._clock(),
                          tallies={hop_id: HopTally() for hop_id in hops})
        self._previews[preview.id] = preview
        preview.task = asyncio.create_task(self._run(preview, probe))
        return preview.id

    def get(self, preview_id: str) -> Optional[dict]:
        self._prune()
        preview = self._previews.get(preview_id)
        if preview is None:
            return None
        end = preview.finished if preview.finished is not None else self._clock()
        return {"id": preview.id, "state": preview.state,
                "elapsed_s": round(min(end - preview.started, float(self.seconds)), 1),
                "hops": {hop_id: tally.public() for hop_id, tally in preview.tallies.items()}}

    async def stop(self, preview_id: str) -> bool:
        """Stops a preview (it stays readable, done, for KEEP_SECONDS). False for an unknown id."""
        preview = self._previews.get(preview_id)
        if preview is None:
            return False
        await self._stop(preview)
        return True

    async def close(self) -> None:
        """Stops every preview (at shutdown)."""
        for preview in self.running():
            await self._stop(preview)

    async def _stop(self, preview: Preview) -> None:
        task = preview.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._finish(preview)

    def _finish(self, preview: Preview) -> None:
        if preview.state != "done":
            preview.state, preview.finished = "done", self._clock()
        preview.task = None

    async def _run(self, preview: Preview, probe: Probe) -> None:
        async def one(delay: float, hop_id: str, address: str, family: int) -> None:
            if delay:
                await asyncio.sleep(delay)
            try:
                answer = await probe(address, family)
            except Exception:  # a failing probe is an unanswered one; the preview goes on
                answer = {"kind": "error", "rtt_ms": None}
            preview.tallies[hop_id].add(answer)

        try:
            for n in range(self.seconds):
                due = preview.started + n * self.interval
                wait = due - self._clock()
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.gather(*(one(k * self.stagger, hop_id, address, family)
                                       for k, (hop_id, (address, family)) in enumerate(preview.hops.items())))
        finally:
            self._finish(preview)

    def _prune(self) -> None:
        now = self._clock()
        for preview in [p for p in self._previews.values() if p.state == "done"]:
            if now - (preview.finished or now) > KEEP_SECONDS:
                del self._previews[preview.id]
