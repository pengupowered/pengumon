"""Cloudflare's speed test endpoints (speed.cloudflare.com): `GET /__down?bytes=N` and `POST /__up`.

Cloudflare sees the client's address and the transfer, as it does for any visit. Each direction runs STREAMS parallel
streams (base.plan_requests: the cap divided into equal requests up front, one per stream or more rounds to keep each
under 10 MB; no request asks for more than its share). An
upload request declares its Content-Length and streams a generated body; one that is cut off (the time limit) is
abandoned by closing its connection. The server's label is the Cloudflare data center (such as "FRA") from the `colo`
header (`cf-meta-colo` in older answers) when one is sent, else "Cloudflare".

Cloudflare's limits (seen 2026-11): a download request of 100000000 bytes or more is refused (403); requests of
10000000 bytes or more are rate-limited per address: after a few dozen within minutes, every one is refused (403 at
first, then 429) for more than half an hour, while smaller ones are still served. So no request asks for 10 MB or more
(base.MAX_REQUEST). Answers also start late (40 ms to a second, sometimes several) and a request is now and then slowed
to a trickle; the Meter leaves both out. A refusal fails the direction with a reason that says so.
"""

import time
from typing import Callable, Optional

import httpx

from pengumon.speedtest.providers.base import (TIMEOUTS, UPLOAD_ROUNDS, USER_AGENT, Meter, ProviderError,
                                               SpeedProvider, Transfer, payload, refused, request_size, run_streams)
from pengumon.speedtest.providers.wire import BulkTransport

BASE = "https://speed.cloudflare.com"
WHO = "Cloudflare"


def _refused(status: int) -> ProviderError:
    """A 403 or 429 is Cloudflare's rate limit for this address (the requests this provider makes are all under the
    per-request limits); anything else is the plain answer."""
    if status in (403, 429):
        return ProviderError(f"Cloudflare refused the test (HTTP {status}): a rate limit for this address, which can "
                             "last an hour; try again later, or try another provider")
    return refused(status, WHO)


class CloudflareProvider(SpeedProvider):
    name = "cloudflare"

    def __init__(self, clock: Callable[[], float] = time.monotonic, transport: Optional[httpx.AsyncBaseTransport] = None,
                 base: str = BASE):
        super().__init__(clock)
        self.base = base
        self._client = httpx.AsyncClient(timeout=TIMEOUTS, follow_redirects=False,
                                         transport=transport if transport is not None else BulkTransport(),
                                         headers={"User-Agent": USER_AGENT})

    def _label(self, response: httpx.Response) -> None:
        colo = response.headers.get("colo") or response.headers.get("cf-meta-colo") or ""
        self.server = colo.strip()[:16] or self.server or WHO

    async def download(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        meter = self._meter(cap_bytes, time_limit_s, target_s=target_s)

        async def step(meter: Meter, size: int) -> None:
            key = meter.request()
            want = size
            async with self._client.stream("GET", f"{self.base}/__down", params={"bytes": size}) as response:
                if response.status_code != 200:
                    raise _refused(response.status_code)
                self._label(response)
                async for data in response.aiter_bytes():
                    want -= meter.add(min(len(data), want), key)  # a response with more than asked for is cut off here
                    if want <= 0 or meter.done:
                        return  # leaving the block closes the connection with whatever is unread

        await run_streams(step, meter, request_size(cap_bytes), WHO)
        return meter.transfer(self.server or WHO)

    async def upload(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        meter = self._meter(cap_bytes, time_limit_s, ramp=0.0, target_s=target_s)

        async def step(meter: Meter, size: int) -> None:
            key = meter.request()
            response = await self._client.post(f"{self.base}/__up", content=payload(meter, size, key),
                                               headers={"Content-Length": str(size),
                                                        "Content-Type": "application/octet-stream"})
            if response.status_code >= 300:
                raise _refused(response.status_code)
            meter.mark(key)  # the answer: the server has the whole body
            self._label(response)

        await run_streams(step, meter, request_size(cap_bytes, rounds=UPLOAD_ROUNDS), WHO, finish=True)
        return meter.transfer(self.server or WHO)

    async def aclose(self) -> None:
        await self._client.aclose()
