"""HTTP for drivers: one persistent client per device, and a span plus a debug line for every exchange.

`PENGUMON_LOG_LEVELS=pengumon.drivers=debug` shows each request a driver makes (method, path, status, duration,
size) without the query string or any body: login requests carry credentials.
"""

import ssl
import time
from typing import Awaitable, Callable, List, Optional, Tuple, Union
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import SpanKind, Status, StatusCode

from pengumon.observability import get_logger
from pengumon.observability.telemetry import tracer


Verify = Union[bool, ssl.SSLContext]
BODY_MAX = 64 * 1024  # the most `read_capped` reads of a body unless asked for another limit


def tls_verify(tls) -> Verify:
    """httpx's `verify` for `tls:` settings (devices.settings): an SSL context trusting `ca_file` when one is given
    (it still checks the host name against the certificate's subjectAltName), else True (certifi's bundle, or
    SSL_CERT_FILE / SSL_CERT_DIR) or False (no check). Raises ValueError naming the file, and why in plain words,
    when it cannot be used (the Settings page shows the text)."""
    if tls.ca_file:
        try:
            return ssl.create_default_context(cafile=tls.ca_file)
        except (OSError, ssl.SSLError) as e:
            raise ValueError(f"CA file '{tls.ca_file}' cannot be used: {_ca_file_problem(e)}") from None
    return bool(tls.verify)


def _ca_file_problem(error: Exception) -> str:
    if isinstance(error, ssl.SSLError):  # before OSError: SSLError is one
        return "it holds no certificate in PEM form"
    if isinstance(error, FileNotFoundError):
        return "there is no such file"
    if isinstance(error, IsADirectoryError):
        return "it is a folder, not a file"
    if isinstance(error, PermissionError):
        return "Pengumon is not allowed to read it"
    return "it cannot be read"


def device_verify(cfg) -> Verify:
    """`verify` for a device (its DeviceConfig), built once at startup. An unusable CA file stops the start only
    for an enabled device with an https:// URL, the one case where it would fail every poll; for a disabled or
    plain-HTTP device it is a warning, and the device verifies against the default bundle (never not at all)."""
    try:
        return tls_verify(cfg.tls)
    except ValueError as e:
        if cfg.enabled and urlsplit(cfg.url).scheme == "https":
            raise
        get_logger("pengumon.drivers", device=cfg.id).warning(
            f"{e}; device {cfg.id} is {'disabled' if not cfg.enabled else 'reached over plain HTTP'}, so it starts "
            "anyway and verifies HTTPS (a redirect) against the default CA bundle", ca_file=cfg.tls.ca_file)
        return True


def host_of(url: str) -> str:
    """The host part of a base URL ("http://192.168.100.1:8080/x" -> "192.168.100.1")."""
    return urlsplit(url if "://" in url else f"http://{url}").hostname or url


async def read_capped(client: httpx.AsyncClient, method: str, url: str, limit: int = BODY_MAX, **kwargs):
    """(status, headers, body bytes) of one request, the body cut at `limit` bytes (the rest is never read) and
    `truncated` True when it was longer."""
    async with client.stream(method, url, **kwargs) as response:
        body = bytearray()
        truncated = False
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > limit:
                truncated = True
                break
        return response.status_code, response.headers, bytes(body[:limit]), truncated


class DeviceHttp:
    """Wraps the driver's AsyncClient. The client is created on first use by `factory` (or given, for tests) and
    kept for the process lifetime: one TCP connection and one cookie jar instead of a new one per poll."""

    def __init__(self, base_url: str, factory: Callable[[], httpx.AsyncClient], *, driver: str,
                 client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self.host = host_of(base_url)
        self._factory = factory
        self._client = client
        self._log = get_logger(f"pengumon.drivers.{driver}", host=self.host)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._factory()
        return self._client

    @property
    def cookies(self) -> httpx.Cookies:
        return self.client.cookies

    async def _exchange(self, method: str, path: str, send: Callable[[str], Awaitable[Tuple[int, int]]]) -> None:
        """One request under its span and debug line. `send(url)` makes it and returns (status code, body size)."""
        url = path if "://" in path else f"{self.base_url}/{path.lstrip('/')}"
        url_path = urlsplit(url).path or "/"
        attributes = {"http.request.method": method, "server.address": self.host, "url.path": url_path}
        started = time.perf_counter()
        with tracer.start_as_current_span(f"{method} {url_path}", kind=SpanKind.CLIENT,
                                          attributes=attributes) as span:
            try:
                status, size = await send(url)
            except httpx.HTTPError as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                span.set_attribute("error.type", type(exc).__name__)
                self._log.debug("request failed", method=method, path=url_path, error=type(exc).__name__,
                                detail=str(exc) or None, duration_ms=round((time.perf_counter() - started) * 1000))
                raise
            span.set_attribute("http.response.status_code", status)
            if status >= 400:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
            self._log.debug("request", method=method, path=url_path, status=status, bytes=size,
                            duration_ms=round((time.perf_counter() - started) * 1000))

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        done: List[httpx.Response] = []

        async def send(url: str) -> Tuple[int, int]:
            done.append(await self.client.request(method, url, **kwargs))
            return done[0].status_code, len(done[0].content)

        await self._exchange(method, path, send)
        return done[0]

    async def request_capped(self, method: str, path: str, limit: int = BODY_MAX, **kwargs
                             ) -> Tuple[int, httpx.Headers, bytes, bool]:
        """Like `request`, but the body is read only up to `limit` bytes (the rest never is): (status, headers,
        body, truncated). For devices whose answers are small and fixed in shape."""
        done: List[Tuple[int, httpx.Headers, bytes, bool]] = []

        async def send(url: str) -> Tuple[int, int]:
            done.append(await read_capped(self.client, method, url, limit, **kwargs))
            return done[0][0], len(done[0][2])

        await self._exchange(method, path, send)
        return done[0]

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    def clear_session(self) -> None:
        """Forgets the session cookies (new credentials must be tested, not an old session reused)."""
        if self._client is not None and not self._client.is_closed:
            self._client.cookies.clear()

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
