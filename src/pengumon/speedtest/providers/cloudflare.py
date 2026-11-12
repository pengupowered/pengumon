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
from typing import Callable, Dict, Optional, Tuple

import httpx

from pengumon.speedtest.providers.base import HttpProvider, ProviderError, refused

BASE = "https://speed.cloudflare.com"
WHO = "Cloudflare"


def _refused(status: int) -> ProviderError:
    """A 403 or 429 is Cloudflare's rate limit for this address (the requests this provider makes are all under the
    per-request limits); anything else is the plain answer."""
    if status in (403, 429):
        return ProviderError(f"Cloudflare refused the test (HTTP {status}): a rate limit for this address, which can "
                             "last an hour; try again later, or try another provider")
    return refused(status, WHO)


class CloudflareProvider(HttpProvider):
    name = "cloudflare"
    DOWN_PATH = "/__down"
    UP_PATH = "/__up"
    _who = WHO

    def __init__(self, clock: Callable[[], float] = time.perf_counter, transport: Optional[httpx.AsyncBaseTransport] = None,
                 base: str = BASE):
        super().__init__(base, clock, transport)

    def _label(self, response: httpx.Response) -> None:
        colo = response.headers.get("colo") or response.headers.get("cf-meta-colo") or ""
        self.server = colo.strip()[:16] or self.server or WHO

    def _server(self) -> Optional[str]:
        return self.server or WHO

    def _download_request(self, size: int) -> Optional[Tuple[Dict[str, int], int]]:
        return {"bytes": size}, size

    def _refused(self, status: int) -> ProviderError:
        return _refused(status)
