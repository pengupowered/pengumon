"""HTTP for drivers: one persistent client per device, and a span plus a debug line for every exchange.

`PENGUMON_LOG_LEVELS=pengumon.drivers=debug` shows each request a driver makes (method, path, status, duration,
size) without the query string or any body: login requests carry credentials.
"""

import ssl
import time
from typing import Callable, Optional, Union
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import SpanKind, Status, StatusCode

from pengumon.observability import get_logger
from pengumon.observability.telemetry import tracer


Verify = Union[bool, ssl.SSLContext]


def tls_verify(tls) -> Verify:
    """httpx's `verify` for `tls:` settings (devices.settings): an SSL context trusting `ca_file` when one is given
    (it still checks the host name against the certificate's subjectAltName), else True (certifi's bundle, or
    SSL_CERT_FILE / SSL_CERT_DIR) or False (no check). Raises ValueError naming the file when it cannot be used."""
    if tls.ca_file:
        try:
            return ssl.create_default_context(cafile=tls.ca_file)
        except (OSError, ssl.SSLError) as e:
            raise ValueError(f"tls.ca_file '{tls.ca_file}' cannot be used: {type(e).__name__}: {e}") from None
    return bool(tls.verify)


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

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = path if "://" in path else f"{self.base_url}/{path.lstrip('/')}"
        url_path = urlsplit(url).path or "/"
        attributes = {"http.request.method": method, "server.address": self.host, "url.path": url_path}
        started = time.perf_counter()
        with tracer.start_as_current_span(f"{method} {url_path}", kind=SpanKind.CLIENT,
                                          attributes=attributes) as span:
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                span.set_attribute("error.type", type(exc).__name__)
                self._log.debug("request failed", method=method, path=url_path, error=type(exc).__name__,
                                detail=str(exc) or None, duration_ms=round((time.perf_counter() - started) * 1000))
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {response.status_code}"))
            self._log.debug("request", method=method, path=url_path, status=response.status_code,
                            bytes=len(response.content), duration_ms=round((time.perf_counter() - started) * 1000))
            return response

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
