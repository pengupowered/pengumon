"""What every speed test provider shares: the interface, the byte meter, and the worker loop of the HTTP providers.

A provider measures one direction at a time: `download(cap_bytes, time_limit_s)` and `upload(...)` each return a
`Transfer`, or raise `ProviderError` (a short reason for the log and the record; `moved` is what was transferred before
it failed, which still counts toward the data cap).

The rules every provider keeps:

* **Hard byte cap.** Nothing is requested, read or sent beyond `cap_bytes` per direction: the cap is divided into
  requests up front (`plan_requests`: no request asks for more than its share), and a response that carries more than
  was asked for is cut off at the amount asked for and the stream closed. `Meter.add` never counts past the cap. The only
  slack: bytes the transport had already read from the socket when a response was cut off (at most one read buffer per
  request, about 16 to 64 KiB), which are not counted, and, for M-Lab, whole WebSocket messages (see mlab.py).
* **Time limit.** `time_limit_s` of the injectable `clock`, checked at every chunk; a request that stalls is cut off in
  real time (`CUT_GRACE_S` past the limit).
* **Throughput.** `STREAMS` parallel streams for the HTTP providers, the cap divided into equal requests up front
  (`plan_requests`: one per stream, more rounds only to keep each under `MAX_REQUEST`). A request spends its first 40 ms
  to a second (at Cloudflare) waiting for the answer to start; that time, and the connection setup, are not part of the
  speed (`Meter`: the time data flowed, without a download's slow start and lagging tail). All bytes moved count toward
  the cap and the record.
* **Honest client.** User-Agent `Pengumon/<version>`, explicit httpx timeouts, no redirects (a 3xx is a failure, so a
  server cannot send the test to another host).
"""

import asyncio
import math
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from pengumon import __version__

USER_AGENT = f"Pengumon/{__version__}"
STREAMS = 8
UPLOAD_ROUNDS = 4  # an upload's cap is spread over this many rounds of the streams (request_size)
UPLOAD_SETTLE = 0.2  # an upload's speed leaves out the first fifth of the flow time (Meter._steady_upload)
STEADY_MIN_S = 2.0  # ... when the flow lasted at least this long
RAMP_FRACTION = 0.2  # a download's speed leaves out the first fifth of the bytes (slow start) ...
TAIL_FRACTION = 0.1  # ... and the last tenth (a request that lags behind the others, or that the server slows down)
# One request carries less than 10 MB. Cloudflare refuses `__down?bytes=100000000` outright (403; 99999999 is served),
# and rate-limits requests of 10000000 bytes or more: after a few dozen of them within minutes from one address it
# refuses every one (403, then 429) for more than half an hour, while smaller ones are still served (seen 2026-11).
MAX_REQUEST = 9_999_999
STALL_GRACE_S = 10.0
CUT_GRACE_S = 2.0  # a stream still waiting for its answer this long after the time limit is cut off (real time)
FINISH_GRACE_S = 2.0  # an upload's requests running when it is done may finish this long (real time), then are cut off
WATCH_S = 0.1  # how often run_streams looks whether the direction is done
TIMEOUTS = httpx.Timeout(connect=8.0, read=10.0, write=10.0, pool=5.0)
_BLOCK = os.urandom(64 * 1024)  # upload payload: random, so no link-level compression flatters the result


class ProviderError(Exception):
    """A test phase that did not work, with a reason short enough to show. `moved`: bytes transferred before it failed."""

    def __init__(self, reason: str, moved: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.moved = moved


@dataclass(frozen=True)
class Transfer:
    """One finished direction: bytes moved (all streams, ramp-up included), elapsed seconds (connection setup
    included), the speed without the ramp-up (None when too little moved to measure), the server's label, and the
    seconds data flowed (`span_s`: Meter.active, the time the speed is about)."""

    bytes: int
    elapsed_s: float
    mbps: Optional[float]
    server: Optional[str] = None
    span_s: Optional[float] = None


class Meter:
    """Counts the bytes of one direction against its cap and its time limit, and works out the speed.

    The speed is measured over the time data flowed: each request's span, from its first byte to its last (a
    download's last byte read, or an upload's answer: the server has everything then), and the union of those spans
    (`active`). Left out: the connection setup, and every moment no request moved data (Cloudflare takes 40 ms to
    almost a second to start an answer, sometimes several seconds). A download's speed is that of the bytes between the
    first `ramp` (20 %: TCP slow start) and the last `tail` (10 %, or one request's worth if more: a request that lags
    behind the others or that the server slows to a trickle, seen at Cloudflare, would otherwise read as a slow line),
    from counted moment to counted moment. The bytes counted at a moment arrived before it, so the first chunk of each
    request that begins inside the measured time is left out. An upload's bytes are counted as they are handed to the
    socket, so the first ones go in a burst, and only the answer says they arrived: its speed is the rate the sockets
    took them at once the buffers were full (_steady_upload), or for a short one the plain average over the active
    time, up to the answers.

    With a target time (`target_s`) a direction is done once data has flowed that long, the cap and the time limit
    still holding."""

    def __init__(self, cap_bytes: int, time_limit_s: float, clock: Callable[[], float], ramp: float = RAMP_FRACTION,
                 tail: float = TAIL_FRACTION, target_s: Optional[float] = None):
        self.cap, self.limit, self.clock, self.ramp, self.tail = cap_bytes, time_limit_s, clock, ramp, tail
        self.target = target_s  # data has flowed long enough after this many seconds from the first byte (None: no aim)
        self.started = clock()
        self.total = 0
        self.first_at: Optional[float] = None  # seconds since start of the first counted byte
        self._points: List[Tuple[float, int]] = []  # (seconds since start, cumulative bytes)
        # request -> [first byte, last activity, bytes of the first chunk, bytes]
        self._requests: Dict[int, List[float]] = {}
        self._next_request = 0

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def remaining(self) -> int:
        return self.cap - self.total

    @property
    def over_time(self) -> bool:
        return self.elapsed >= self.limit

    @property
    def reached(self) -> bool:
        """Data has flowed for the target time (since the first byte)."""
        return self.target is not None and self.first_at is not None and self.elapsed - self.first_at >= self.target

    @property
    def done(self) -> bool:
        return self.total >= self.cap or self.over_time or self.reached

    def request(self) -> int:
        """A key for a new request, to count its bytes under."""
        self._next_request += 1
        return self._next_request

    def add(self, n: int, request: int = 0) -> int:
        """Counts up to `n` bytes of `request` (never past the cap) and returns how many were counted."""
        n = max(0, min(n, self.remaining))
        if n:
            at = self.elapsed
            if self.first_at is None:
                self.first_at = at
            self.total += n
            self._points.append((at, self.total))
            span = self._requests.get(request)
            if span is None:
                self._requests[request] = [at, at, n, n]
            else:
                span[1] = at
                span[3] += n
        return n

    def mark(self, request: int = 0) -> None:
        """The end of a request without new bytes: an upload's answer arrived (the server has what was sent)."""
        span = self._requests.get(request)
        if span is not None:
            span[1] = max(span[1], self.elapsed)

    def _active(self) -> List[Tuple[float, float]]:
        """The union of the requests' spans, in time order."""
        merged: List[List[float]] = []
        for first, last, *_ in sorted(self._requests.values()):
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        return [(a, b) for a, b in merged]

    @property
    def active(self) -> float:
        """Seconds during which at least one request moved data."""
        return sum(b - a for a, b in self._active())

    def mbps(self) -> Optional[float]:
        """Mbps over the active time, without the ramp-up and the tail; None when nothing moved."""
        if not self.total:
            return None
        spans = self._active()

        def on_active(t: float) -> float:  # seconds of active time up to the moment t
            return sum(min(b, t) - a for a, b in spans if a < t)

        if self.ramp <= 0:
            steady = self._steady_upload()
            if steady is not None:
                return steady
        if self.ramp > 0:
            # the part between the first `ramp` and the last `tail` of the bytes, from counted moment to counted moment
            low, high = self.total * self.ramp, self.total * (1 - self.tail)
            start = next(((at, c) for at, c in self._points if c >= low), None)
            end = next(((at, c) for at, c in self._points if c >= high), None)
            if start and end and end[0] > start[0]:
                later = sum(n for first, _, n, _ in self._requests.values() if start[0] < first <= end[0])
                moved, seconds = end[1] - start[1] - later, on_active(end[0]) - on_active(start[0])
                if moved > 0 and seconds > 0:
                    return moved * 8 / seconds / 1e6
        active = sum(b - a for a, b in spans)
        if active > 0:
            # all of it. A download's bytes counted at a moment arrived before it: the first chunk of every request is
            # left out. An upload's are counted as they are handed over, before they are sent: all of them count, up
            # to the answer.
            moved = self.total - (sum(n for _, _, n, _ in self._requests.values()) if self.ramp > 0 else 0)
            if moved > 0:
                return moved * 8 / active / 1e6
        elapsed = self.elapsed  # everything arrived at once: the plain average since the start is all there is
        return self.total * 8 / elapsed / 1e6 if elapsed > 0 else None

    def _steady_upload(self) -> Optional[float]:
        """An upload's speed while the streams were all sending: the rate the bytes were handed to the sockets between
        the first UPLOAD_SETTLE of the flow (the empty socket buffers take the first bytes in a burst) and its end (the
        target time, when no new request starts). Then the buffers are full, and the socket takes a byte only when the
        server has acknowledged one: the rate it takes them at is the line's. None when the flow lasted under
        STEADY_MIN_S (it may all have fit in the buffers; the average up to the answers says more then), and without a
        target time.

        Not the plain average up to the answers: the requests that run when the time is up finish one by one, and one
        that the server answers late (Cloudflare does, now and then, by seconds) would stretch the time and read low."""
        points = self._points
        if len(points) < 2 or self.target is None:  # (a direction without a target time has no steady part to find)
            return None
        first = points[0][0]
        end = min(points[-1][0], first + self.target)
        if end - first < STEADY_MIN_S:
            return None
        settle = first + (end - first) * UPLOAD_SETTLE
        start = next((p for p in points if p[0] >= settle), None)
        last = [p for p in points if p[0] <= end][-1]
        if start is None or last[0] <= start[0]:
            return None
        return (last[1] - start[1]) * 8 / (last[0] - start[0]) / 1e6

    def transfer(self, server: Optional[str]) -> Transfer:
        return Transfer(self.total, self.elapsed, self.mbps(), server, round(self.active, 3))


def request_size(cap_bytes: int, streams: int = STREAMS, rounds: int = 1, max_request: int = MAX_REQUEST,
                 unit: int = 1) -> int:
    """The size of one request of a direction: the cap spread over `rounds` rounds of the streams, in whole `unit`s (a
    LibreSpeed server counts in MiB), at most `max_request` and at least one unit (0: the cap is less than a unit).

    The streams keep asking for requests of this size until the direction is done (run_streams), never for more than
    is left of the cap. Downloads take one round: every request waits for its answer to start (40 ms to a second at
    Cloudflare), so as few as the limit allows. Uploads take UPLOAD_ROUNDS, so that each request is answered within a
    second or two: an upload is timed up to the answers (its bytes are counted as they are handed to the socket)."""
    units = cap_bytes // unit
    if units <= 0:
        return 0
    per = max(1, max_request // unit)
    return min(per, max(1, units // (streams * rounds))) * unit


class SpeedProvider:
    """The interface. `name` is the setting's value. `server` is the label of the server last used."""

    name = ""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.server: Optional[str] = None
        self.meter: Optional[Meter] = None  # the direction running now, or the last one

    def _meter(self, cap_bytes: int, time_limit_s: float, ramp: float = RAMP_FRACTION,
               target_s: Optional[float] = None) -> Meter:
        self.meter = Meter(cap_bytes, time_limit_s, self.clock, ramp, target_s=target_s)
        return self.meter

    def moved(self) -> int:
        """The bytes of the direction running now (or the last one): what an interrupted test still has to count."""
        return self.meter.total if self.meter is not None else 0

    async def download(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        """Moves at most `cap_bytes` down and stops after `time_limit_s`, or once data has flowed for `target_s`."""
        raise NotImplementedError

    async def upload(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


def refused(status: int, who: str) -> ProviderError:
    """The reason for an HTTP error answer. A 403 or 429 is the provider refusing (a rate limit, most likely)."""
    if status in (403, 429):
        return ProviderError(f"{who} refused the test (HTTP {status}, probably a rate limit); try another provider "
                             "or a longer interval")
    if 300 <= status < 400:
        return ProviderError(f"{who} answered with a redirect (HTTP {status}), which is not followed")
    return ProviderError(f"{who} answered HTTP {status}")


PROXY_NOTE = ("a proxy is set for Pengumon, but the speed test connects directly: a test through a proxy would measure "
              "the proxy, not your line")


def _proxy_set(exc: BaseException) -> bool:
    """Whether the environment sets a proxy (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, any case) for the request that failed,
    one NO_PROXY does not exempt: the speed test's transport never uses it, so a network that lets traffic out only
    through a proxy refuses the test."""
    proxies = urllib.request.getproxies_environment()
    try:
        url = exc.request.url  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        return bool(proxies.get("https") or proxies.get("http") or proxies.get("all"))
    if not (proxies.get(url.scheme) or proxies.get("all")):
        return False
    return not urllib.request.proxy_bypass_environment(url.host, proxies)


def failure(exc: BaseException, who: str) -> ProviderError:
    """A short reason for a transport error (never the URL: it may hold a token; nor the proxy's address)."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) and _proxy_set(exc):
        return ProviderError(f"could not connect to {who}; {PROXY_NOTE}")
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                        asyncio.TimeoutError, TimeoutError)):
        return ProviderError(f"{who} did not answer in time")
    if isinstance(exc, httpx.ConnectError):
        return ProviderError(f"could not connect to {who}: {exc.__class__.__name__}")
    if isinstance(exc, httpx.HTTPError):
        return ProviderError(f"{who}: {exc.__class__.__name__}")
    return ProviderError(f"{who}: {exc.__class__.__name__}: {exc}")


async def payload(meter: Meter, size: int, request: int = 0) -> AsyncIterator[bytes]:
    """`size` bytes of upload body, counted as they are handed to the transport; ends early (the request is then
    abandoned) when the cap is reached or the time is up."""
    left = size
    while left > 0:
        if meter.over_time:
            raise _Stop()
        block = _BLOCK[:min(left, len(_BLOCK))]
        counted = meter.add(len(block), request)
        if counted < len(block):
            raise _Stop()
        left -= len(block)
        yield block
        await asyncio.sleep(0)  # let the other streams run between blocks


class _Stop(Exception):
    """Ends an upload body early (cap or time reached)."""


# A request: (meter, size) -> the response is read, or the body sent, inside; returns when that request is done.
Step = Callable[[Meter, int], Awaitable[None]]


async def run_streams(step: Step, meter: Meter, size: int, who: str, streams: int = STREAMS, unit: int = 1,
                      finish: bool = False) -> None:
    """Runs `streams` workers that each ask for requests of `size` bytes (request_size), one after the other, and do
    `step` for each, until the direction is done: the cap is reached (nothing is asked beyond it: the last requests get
    what is left, in whole `unit`s), the time is up, or data has flowed for the meter's target time.

    Then the requests still running are cut off: at once for a download (its step also stops reading when the meter
    is done), after up to FINISH_GRACE_S with `finish` (an upload: its requests are short, and a request answered is
    one whose bytes the server has). A request that stalls (Cloudflare now and then stops serving one) is cut off with
    them, so it never holds a direction past its time; a request still waiting CUT_GRACE_S (real time) after the time
    limit is cut off in any case.

    A refusal (a ProviderError, from an HTTP status: a rate limit, say) fails the direction. So does a transport error
    (a reset, a timeout) unless the direction still moved data for its whole target time on the other streams: the
    speed is that of the data that flowed, so a stream that dropped out costs nothing then. A failed direction keeps
    the bytes that moved (ProviderError.moved), which count toward the data cap."""
    errors: List[BaseException] = []
    dropped: List[BaseException] = []  # transport errors of a stream
    finished = 0  # requests that completed
    asked = 0  # bytes asked for so far

    async def worker() -> None:
        nonlocal finished, asked
        while not meter.done:
            n = min(size, (meter.cap - asked) // unit * unit)
            if n <= 0:
                return
            asked += n
            try:
                await step(meter, n)
                finished += 1
            except _Stop:
                return
            except asyncio.CancelledError:
                raise
            except ProviderError as e:
                errors.append(e)
                return
            except Exception as e:  # noqa: BLE001 - every transport error is a reason
                if not (meter.done and not finish) and not meter.over_time:
                    dropped.append(e)
                return

    if size <= 0:
        raise ProviderError(f"the data cap is too small to ask {who} for anything")
    if size >= meter.cap / streams:  # (one request per stream) the speed leaves out the last request's worth: it may lag
        meter.tail = max(meter.tail, min(size, meter.cap) / meter.cap)
    loop = asyncio.get_running_loop()
    give_up = loop.time() + meter.limit + CUT_GRACE_S
    pending = {asyncio.ensure_future(worker()) for _ in range(max(1, min(streams, meter.cap // size)))}
    tasks = set(pending)
    cut_at: Optional[float] = None
    try:
        while pending:
            _, pending = await asyncio.wait(pending, timeout=WATCH_S)
            now = loop.time()
            if cut_at is None and meter.done:
                cut_at = now + (FINISH_GRACE_S if finish and not meter.over_time else 0.0)
            if now >= give_up or (cut_at is not None and now >= cut_at):
                break
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if dropped and not errors and not meter.reached:
        errors = dropped
    if errors:
        error = failure(errors[0], who)
        if (finished or meter.total) and not isinstance(errors[0], ProviderError):
            # a transport error of one stream (a refusal says enough by itself)
            error = ProviderError(f"{error.reason} on one of the streams; the others alone would read low")
        error.moved = meter.total
        raise error
    if not meter.total:
        raise ProviderError(f"{who} did not answer in time" if pending else f"{who} sent no data")
