"""HTTP for drivers: one persistent client per device, and a span plus a debug line for every exchange.

`PENGUMON_LOG_LEVELS=pengumon.drivers=debug` shows each request a driver makes (method, path, status, duration,
size) without the query string or any body: login requests carry credentials.

Every body is read through `CappedBody`: up to a limit, counted after decoding, and decoded only that far. Requests
ask for no compression (`Accept-Encoding: identity`), but a device or anyone answering for an http:// address can
send gzip anyway, and a few hundred KB of it inflate to gigabytes: httpx's own decoder inflates every piece whole.
"""

import asyncio
import ssl
import time
import zlib
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import SpanKind, Status, StatusCode

from pengumon.capabilities import DriverError
from pengumon.observability import get_logger
from pengumon.observability.telemetry import tracer


Verify = Union[bool, ssl.SSLContext]
BODY_MAX = 64 * 1024  # the most `read_capped` reads of a body unless asked for another limit
REQUEST_MAX = 1024 * 1024  # the most `DeviceHttp.request` reads of an answer unless asked for another limit
# Compressed bytes read past the limit before a body counts as too long: a compressed stream can decode to nothing (or
# go on after its end), and must not be read for ever.
RAW_SLACK = 64 * 1024
_WBITS = {"gzip": zlib.MAX_WBITS | 16, "x-gzip": zlib.MAX_WBITS | 16, "deflate": zlib.MAX_WBITS}
# httpx's timeouts are per read: an answer sent one byte just inside each of them never ends. A whole exchange (or the
# reading of an answer) gets this many read timeouts in all, plus the connect timeout for an exchange.
TOTAL_READ_TIMEOUTS = 3
_DROPPED_HEADERS = (b"content-encoding", b"content-length", b"transfer-encoding")


def total_deadline(timeout: Union[httpx.Timeout, float, None]) -> Optional[float]:
    """The most seconds a whole exchange may take for a client (or request) timeout: the connect timeout plus
    TOTAL_READ_TIMEOUTS read timeouts. None when the read timeout is off (a caller that wants no limit says so)."""
    if timeout is None:
        return None
    if not isinstance(timeout, httpx.Timeout):
        timeout = httpx.Timeout(timeout)
    if timeout.read is None:
        return None
    return (timeout.connect or 0.0) + TOTAL_READ_TIMEOUTS * timeout.read


class _Inflate:
    """A gzip or deflate decoder whose output is bounded on every call (zlib's max_length)."""

    def __init__(self, encoding: str):
        self._deflate = encoding == "deflate"
        self._first = True
        self._z = zlib.decompressobj(_WBITS[encoding])

    def decode(self, data: bytes, room: int) -> bytes:
        """At most `room` (at least 1) bytes decoded from `data`. When it gives `room` bytes, the rest of `data` may be
        left undecoded: the reader stops there. Anything after the end of the compressed stream is ignored."""
        if self._z.eof:  # (zlib would keep it all in unused_data)
            return b""
        first, self._first = self._first, False
        try:
            return self._z.decompress(data, room)
        except zlib.error as e:
            if first and self._deflate:  # "deflate" sent raw, without the zlib header (httpx takes it too)
                self._z = zlib.decompressobj(-zlib.MAX_WBITS)
                return self.decode(data, room)
            raise httpx.DecodingError(f"a compressed answer that does not decode ({e})") from None


def _decoder(headers: httpx.Headers, compressed: bool) -> Optional[_Inflate]:
    """The decoder for a response's Content-Encoding: None for none (identity; unknown codings are left as they are,
    as httpx does). Raises httpx.DecodingError for more than one coding, and for any when `compressed` is False."""
    codings = [c.strip().lower() for c in headers.get("content-encoding", "").split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if codings and not compressed:
        raise httpx.DecodingError(f"the answer came compressed ({', '.join(codings)[:40]}), and is read only as sent")
    known = [c for c in codings if c in _WBITS]
    if len(known) > 1:
        raise httpx.DecodingError("an answer compressed more than once is not read")
    return _Inflate(known[0]) if known else None


class CappedBody:
    """The body of a streamed response, decoded, `limit` bytes at most: the rest is never read nor decoded, and
    `truncated` says there was more. Raises httpx.DecodingError for a body that does not decode, or (constructor) one
    that is compressed when `compressed` is False (the speed test counts bytes as sent)."""

    def __init__(self, response: httpx.Response, limit: int, *, compressed: bool = True):
        self.response, self.limit = response, limit
        self.size = 0  # decoded bytes given out
        self.truncated = False
        self._decoder = _decoder(response.headers, compressed)

    async def pieces(self) -> AsyncIterator[bytes]:
        """The body in the pieces it arrives in (decoded), `limit` bytes in all at most."""
        try:  # already in memory and decoded: a test's mock transport (a real transport's stream is not read yet)
            chunks: Any = _one(self.response.content)
            decoder = None
        except httpx.ResponseNotRead:
            chunks = self.response.aiter_raw()
            decoder = self._decoder
        raw = 0
        async for chunk in chunks:
            raw += len(chunk)
            room = self.limit - self.size + 1  # one byte more than the limit says the body is longer
            piece = chunk[:room] if decoder is None else decoder.decode(chunk, room)
            if len(piece) >= room:
                self.truncated = True
                piece = piece[:room - 1]
            self.size += len(piece)
            if piece:
                yield piece
            if self.truncated or raw > self.limit + RAW_SLACK:
                self.truncated = True
                return

    async def read(self, deadline: Optional[float] = None) -> bytes:
        """The whole body (up to the limit). It must arrive within `deadline` seconds in all, by default
        TOTAL_READ_TIMEOUTS times the read timeout of the request (none when that is off): an answer dripped a byte
        at a time inside each read timeout otherwise holds the caller for as long as it likes. Raises
        httpx.ReadTimeout when it does not."""
        if deadline is None:
            try:
                timeout = self.response.request.extensions.get("timeout") or {}
            except RuntimeError:  # (a response built without a request)
                timeout = {}
            if timeout.get("read") is not None:
                deadline = TOTAL_READ_TIMEOUTS * timeout["read"]
        try:
            async with asyncio.timeout(deadline):
                return b"".join([piece async for piece in self.pieces()])
        except TimeoutError:
            raise httpx.ReadTimeout("the answer did not arrive in time") from None


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _identity(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """A request's keyword arguments with `Accept-Encoding: identity` added to its headers."""
    headers = httpx.Headers(kwargs.get("headers"))
    headers["Accept-Encoding"] = "identity"
    return {**kwargs, "headers": headers}


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


def new_client(verify: Verify, *, timeout: float = 6.0, headers: Optional[Dict[str, str]] = None,
               event_hooks: Optional[Dict[str, list]] = None) -> httpx.AsyncClient:
    """The httpx client every driver that talks HTTP to its device builds (`DeviceHttp`'s `factory` calls it): the
    device's `verify`, a timeout, and never a redirect followed by httpx itself (a redirect's body would be read without
    a limit: a driver that follows redirects does it by hand through `DeviceHttp.request`). What differs per driver is
    given: the timeout, the headers (the user agent) and the event hooks. Requests ask for no compression
    (`_identity`), whatever the client."""
    return httpx.AsyncClient(timeout=timeout, verify=verify, follow_redirects=False, headers=headers,
                             event_hooks=event_hooks)


def host_of(url: str) -> str:
    """The host part of a base URL ("http://192.168.100.1:8080/x" -> "192.168.100.1")."""
    return urlsplit(url if "://" in url else f"http://{url}").hostname or url


async def read_capped(client: httpx.AsyncClient, method: str, url: str, limit: int = BODY_MAX, **kwargs):
    """(status, headers, body bytes, truncated) of one request, asking for no compression: the body (decoded) cut at
    `limit` bytes (the rest is never read) and `truncated` True when it was longer."""
    async with client.stream(method, url, **_identity(kwargs)) as response:
        body = CappedBody(response, limit)
        data = await body.read()
        return response.status_code, response.headers, data, body.truncated


class DeviceHttp:
    """Wraps the driver's AsyncClient. The client is created on first use by `factory` (or given, for tests) and
    kept for the process lifetime: one TCP connection and one cookie jar instead of a new one per poll."""

    def __init__(self, base_url: str, factory: Callable[[], httpx.AsyncClient], *, driver: str,
                 client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self.host = host_of(base_url)
        self._factory = factory
        self._client = client
        self.device_id: Optional[str] = None  # set by the Device that owns the driver: the spans name the device by it
        # (no host in the context: the records are exported over OTLP, which leaves out the device's address)
        self._log = get_logger(f"pengumon.drivers.{driver}")

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
        # (the spans are exported over OTLP, which leaves out the device's address: it is named by its id, as the logs do)
        attributes = {"http.request.method": method, "url.path": url_path}
        if self.device_id:
            attributes["device.id"] = self.device_id
        started = time.perf_counter()
        with tracer.start_as_current_span(f"{method} {url_path}", kind=SpanKind.CLIENT,
                                          attributes=attributes) as span:
            try:
                try:
                    async with asyncio.timeout(total_deadline(self.client.timeout)):
                        status, size = await send(url)
                except TimeoutError:  # (not httpx's own timeouts: those are httpx.TimeoutException)
                    raise httpx.ReadTimeout("the exchange did not finish in time") from None
            except httpx.HTTPError as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                span.set_attribute("error.type", type(exc).__name__)
                self._log.debug("request failed", method=method, path=url_path, error=type(exc).__name__,
                                detail=str(exc).replace(self.host, "the device") or None, duration_ms=round((time.perf_counter() - started) * 1000))
                raise
            span.set_attribute("http.response.status_code", status)
            if status >= 400:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
            self._log.debug("request", method=method, path=url_path, status=status, bytes=size,
                            duration_ms=round((time.perf_counter() - started) * 1000))

    async def request(self, method: str, path: str, limit: int = REQUEST_MAX, **kwargs) -> httpx.Response:
        """One request, its answer read into memory (decoded) up to `limit` bytes: a longer one is not read further
        and raises DriverError("protocol")."""
        done: List[httpx.Response] = []
        truncated: List[bool] = []

        async def send(url: str) -> Tuple[int, int]:
            async with self.client.stream(method, url, **_identity(kwargs)) as response:
                body = CappedBody(response, limit)
                content = await body.read()
            truncated.append(body.truncated)
            # The body is decoded: the rebuilt answer carries no Content-Encoding (httpx would decode it again)
            headers = [(k, v) for k, v in response.headers.raw if k.lower() not in _DROPPED_HEADERS]
            done.append(httpx.Response(response.status_code, headers=headers, content=content,
                                       request=response.request, history=response.history))
            return response.status_code, len(content)

        await self._exchange(method, path, send)
        if truncated[0]:
            raise DriverError("protocol", f"{self.host} answered {method} {urlsplit(path).path or '/'} with more than "
                                          f"{limit // 1024} KiB: not read")
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
