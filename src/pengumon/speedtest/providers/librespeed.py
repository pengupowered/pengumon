"""A LibreSpeed server of the owner's (librespeed-php or the Go backend): `GET {base}/garbage.php?ckSize=<MiB>` and
`POST {base}/empty.php`. Only that server sees anything. `ckSize` is a whole number of MiB, so the cap is divided into
one request per stream of whole MiB (base.plan_requests with a MiB unit; what is left over, less than a MiB per stream,
is not used); the reader cuts the body off at the amount asked for in any case. The server's label is the URL's host.
"""

import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from pengumon.speedtest.providers.base import (TIMEOUTS, UPLOAD_ROUNDS, USER_AGENT, Meter, ProviderError,
                                               SpeedProvider, Transfer, payload, refused, request_size, run_streams)
from pengumon.speedtest.providers.wire import BulkTransport

MIB = 1024 * 1024


class LibreSpeedProvider(SpeedProvider):
    name = "librespeed"

    def __init__(self, base_url: str, clock: Callable[[], float] = time.perf_counter,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__(clock)
        self.base = base_url.rstrip("/")
        self.server = urlsplit(self.base).hostname
        self._who = self.server or "the LibreSpeed server"
        self._client = httpx.AsyncClient(timeout=TIMEOUTS, follow_redirects=False,
                                         transport=transport if transport is not None else BulkTransport(),
                                         headers={"User-Agent": USER_AGENT})

    async def download(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        meter = self._meter(cap_bytes, time_limit_s, target_s=target_s)
        who = self._who

        async def step(meter: Meter, size: int) -> None:
            key = meter.request()
            mib = size // MIB
            if mib < 1:
                return  # less than one MiB of the cap is left: nothing can be asked for
            want = min(size, mib * MIB)
            async with self._client.stream("GET", f"{self.base}/garbage.php", params={"ckSize": mib}) as response:
                if response.status_code != 200:
                    raise self._refused(response.status_code)
                async for data in response.aiter_bytes():
                    want -= meter.add(min(len(data), want), key)
                    if want <= 0 or meter.done:
                        return

        await run_streams(step, meter, request_size(cap_bytes, unit=MIB), who, unit=MIB)
        return meter.transfer(self.server)

    async def upload(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        meter = self._meter(cap_bytes, time_limit_s, ramp=0.0, target_s=target_s)
        who = self._who

        async def step(meter: Meter, size: int) -> None:
            key = meter.request()
            response = await self._client.post(f"{self.base}/empty.php", content=payload(meter, size, key),
                                               headers={"Content-Length": str(size),
                                                        "Content-Type": "application/octet-stream"})
            if response.status_code >= 300:
                raise self._refused(response.status_code)
            meter.mark(key)  # the answer: the server has the whole body

        await run_streams(step, meter, request_size(cap_bytes, rounds=UPLOAD_ROUNDS), who, finish=True)
        return meter.transfer(self.server)

    def _refused(self, status: int):
        """The reason for an error answer; a 404 most likely means the address is not the folder of the backend."""
        if status == 404:
            return ProviderError(f"{self._who} answered HTTP 404: is the address the folder that holds garbage.php? "
                                 f"For the official Docker image it is http://{urlsplit(self.base).netloc}/backend")
        return refused(status, self._who)

    async def aclose(self) -> None:
        await self._client.aclose()
