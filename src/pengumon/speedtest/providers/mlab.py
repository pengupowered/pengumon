"""M-Lab's NDT7 (https://github.com/m-lab/ndt-server/blob/main/spec/ndt7-protocol.md) over WebSocket.

M-Lab publishes every result with the client's public IP address as open data; the setting says so, and this provider
is opt-in. The nearest server comes from the locate service (`locate.measurementlab.net/v2/nearest/ndt/ndt7`), whose
answer carries the wss URLs of the download and upload tests with a short-lived access token in the query string (kept
as given, never logged). ndt7 is a single stream by design, so there is one WebSocket per direction.

* download: the server sends binary messages (random data, 8 KiB growing to at most 16 MiB; text messages are its
  measurements and are ignored); the client counts them, and closes at the cap or the time limit.
* upload: the client sends binary messages, 8 KiB growing to 256 KiB (the spec allows 1 KiB to 16 MiB; the scaling is
  optional) and closes at the cap or the time limit. The server's text messages are read and dropped.

Cap slack: a download message arrives whole, so the cap can be passed by up to one message (the server picks the size; it
grows only as the transfer does, to about a sixteenth of the bytes sent so far, so it is under a tenth of the cap in
practice). The count includes the whole last message: it is what was received. An upload message is never larger than
what is left of the cap.

The locate answer is untrusted input: only wss URLs on a Measurement Lab domain are used, and a redirect from the server
is not followed (`websockets` would follow one to any secure host otherwise; see `NoRedirect`).
"""

import asyncio
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import httpx
import websockets
from websockets.asyncio.client import connect

from pengumon import __version__
from pengumon.speedtest.providers.base import (RAMP_FRACTION, STALL_GRACE_S, TIMEOUTS, USER_AGENT, Meter, ProviderError,
                                               SpeedProvider, Transfer, failure)

LOCATE_URL = "https://locate.measurementlab.net/v2/nearest/ndt/ndt7"
SUBPROTOCOL = "net.measurementlab.ndt.v7"
DOWNLOAD_KEY, UPLOAD_KEY = "wss:///ndt/v7/download", "wss:///ndt/v7/upload"
MAX_MESSAGE = 1 << 24  # the spec: a limit smaller than this is not allowed
FIRST_SIZE, MAX_SEND = 1 << 13, 1 << 18
TRUSTED_DOMAINS = ("measurement-lab.org", "measurementlab.net")
WHO = "M-Lab"


class NoRedirect(connect):
    """`websockets`' connect, without following redirects: the server was checked against the trusted domains before
    the test, and a redirect could send the test (and its access token) anywhere. A 3xx is then a failed handshake."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


class MLabProvider(SpeedProvider):
    name = "mlab"

    def __init__(self, clock: Callable[[], float] = time.monotonic, transport: Optional[httpx.AsyncBaseTransport] = None,
                 locate_url: str = LOCATE_URL, connect_fn: Callable[..., Any] = NoRedirect, trusted_only: bool = True):
        super().__init__(clock)
        self.locate_url, self._connect, self._trusted_only = locate_url, connect_fn, trusted_only
        self._client = httpx.AsyncClient(timeout=TIMEOUTS, follow_redirects=False, transport=transport,
                                         headers={"User-Agent": USER_AGENT})
        self._urls: Optional[Dict[str, str]] = None

    async def _locate(self) -> Dict[str, str]:
        if self._urls is not None:
            return self._urls
        try:
            response = await self._client.get(self.locate_url, params={"client_name": "pengumon",
                                                                       "client_version": __version__})
        except httpx.HTTPError as e:
            raise failure(e, "the M-Lab locate service")
        if response.status_code != 200:
            reason = f"the M-Lab locate service answered HTTP {response.status_code}"
            raise ProviderError(reason + (" (probably a rate limit); try another provider"
                                          if response.status_code in (403, 429) else ""))
        try:
            results = response.json()["results"]
            first = results[0]
            urls = {k: first["urls"][k] for k in (DOWNLOAD_KEY, UPLOAD_KEY)}
            machine = str(first.get("machine") or "")
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("the M-Lab locate service found no server") from None
        for url in urls.values():
            self._check(url)
        self._urls = urls
        self.server = machine[:80] or WHO
        return urls

    def _check(self, url: Any) -> None:
        parts = urlsplit(url) if isinstance(url, str) else None
        host = (parts.hostname or "") if parts else ""
        allowed = parts is not None and host and (
            (parts.scheme == "wss" and any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS))
            or (not self._trusted_only and parts.scheme in ("ws", "wss")))
        if not allowed:
            raise ProviderError("the M-Lab locate service named a server that is not M-Lab's; not using it")

    async def _session(self, key: str, cap_bytes: int, time_limit_s: float, drive,
                       target_s: Optional[float] = None) -> Transfer:
        url = (await self._locate())[key]
        meter = self._meter(cap_bytes, time_limit_s, ramp=RAMP_FRACTION if key == DOWNLOAD_KEY else 0.0,
                            target_s=target_s)
        try:
            async with asyncio.timeout(time_limit_s + STALL_GRACE_S):
                async with self._connect(url, subprotocols=[SUBPROTOCOL], additional_headers={"User-Agent": USER_AGENT},
                                         max_size=MAX_MESSAGE, open_timeout=8, ping_interval=None,
                                         close_timeout=3) as ws:
                    meter.started = self.clock()  # the test proper starts when the handshake is done
                    await drive(ws, meter)
        except websockets.exceptions.ConnectionClosed:
            pass  # the spec: an abrupt end is not a failed test; what moved before it counts
        except (TimeoutError, asyncio.TimeoutError):
            if not meter.total:
                raise ProviderError("M-Lab did not answer in time") from None
        except (OSError, websockets.exceptions.WebSocketException) as e:
            if not meter.total:
                raise ProviderError(f"could not run the test at M-Lab: {e.__class__.__name__}") from None
        if not meter.total:
            raise ProviderError("M-Lab sent no data" if key == DOWNLOAD_KEY else "M-Lab took no data")
        return meter.transfer(self.server)

    async def download(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        async def drive(ws, meter: Meter) -> None:
            while not meter.done:
                message = await ws.recv()
                if isinstance(message, bytes):
                    meter.add(len(message))  # the whole message: it was received (see the module docstring)
            await ws.close()

        return await self._session(DOWNLOAD_KEY, cap_bytes, time_limit_s, drive, target_s)

    async def upload(self, cap_bytes: int, time_limit_s: float, target_s: Optional[float] = None) -> Transfer:
        async def drive(ws, meter: Meter) -> None:
            async def drain() -> None:  # the server's measurements: read and dropped
                async for _ in ws:
                    pass

            reader = asyncio.ensure_future(drain())
            try:
                size, block = FIRST_SIZE, bytes(MAX_SEND)
                while not meter.done:
                    if size < MAX_SEND and meter.total >= size * 16:
                        size *= 2
                    n = min(size, meter.remaining)
                    await ws.send(block[:n])
                    meter.add(n)
                await ws.close()
                meter.mark()  # the closing handshake is done: the server has read everything sent
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

        return await self._session(UPLOAD_KEY, cap_bytes, time_limit_s, drive, target_s)

    async def aclose(self) -> None:
        await self._client.aclose()
