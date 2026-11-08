"""A lean HTTP/1.1 transport for the speed test's bulk transfers: the CPU a test costs, and what the client can measure.

Through httpx's own transport a download costs about 11 to 13 ms of CPU per MB once it comes from a real line (data
arrives a few kilobytes at a time, and every arrival goes through the event loop, the TLS layer, the HTTP parser and the
stream wrappers): one core is spent at about 700 Mbps, and the test measured the client instead of the line. This
transport reads in batches instead: it waits `batch_s` (a few milliseconds) between reads and then takes everything the
socket holds, decrypting it with `ssl.SSLObject` (OpenSSL, in C). The kernel keeps the data meanwhile (a few hundred
kilobytes at most), so the line is not slowed, and a megabyte costs a few reads instead of a hundred. Measured on
loopback TLS with a server that sends 16 KiB every millisecond per stream (8 streams, about 1 Gbps): 13.1 ms per MB for
the old path, about 3 for this one (docs/speedtest.md, "How fast the client is").

It is an `httpx.AsyncBaseTransport`, so the providers use the usual httpx client (and the tests their mock transports):

* HTTP/1.1 only, `http` and `https` (TLS verified against httpx's CA bundle, with SNI); connections are kept and reused
  per host after a response was read to its end; one that is closed early (the cap, or the time limit) is dropped. A
  GET that finds its kept connection closed by the server is sent once more on a new one.
* Bodies with a Content-Length, chunked, or up to the close. Request bodies are sent as the stream yields them (the
  upload payload counts its bytes as they are handed over, as before).
* The timeouts of the request (connect, read, write) apply, and raise httpx's own timeout errors; other socket and TLS
  errors are httpx.ConnectError / ReadError / WriteError, a malformed answer is RemoteProtocolError.
* No proxies (environment proxies are not used with a custom transport), no HTTP/2, no compression (the providers ask
  for none; a compressed answer is passed through as it came, and httpx decodes it).
"""

import asyncio
import socket
import ssl
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx

BATCH_S = 0.005  # the wait between two reads of a response body: what one read can collect
READ_SIZE = 1 << 20  # what one read takes from the socket at most
RECORD_SIZE = 64 * 1024  # what one decryption takes out at most (a TLS record holds 16 KiB)
HEAD_MAX = 64 * 1024  # a response head larger than this is refused
DEFAULT_TIMEOUT = 10.0
ADDRESS_WAIT_S = 2.0  # how long a connection to one of several addresses may take before the next one is tried

Key = Tuple[str, str, int]


def _timeouts(request: httpx.Request) -> Dict[str, Optional[float]]:
    timeout = request.extensions.get("timeout") or {}
    return {k: timeout.get(k, DEFAULT_TIMEOUT) for k in ("connect", "read", "write", "pool")}


class _Connection:
    """One connection: a non-blocking socket, and for https an SSLObject over memory BIOs."""

    def __init__(self, key: Key, sock: socket.socket, tls: Optional[ssl.SSLObject], incoming: Optional[ssl.MemoryBIO],
                 outgoing: Optional[ssl.MemoryBIO]):
        self.key = key
        self.sock = sock
        self.tls, self._in, self._out = tls, incoming, outgoing
        self.plain = bytearray()  # received and decrypted, not yet used
        self.eof = False
        self.closed = False
        self.used = False  # it has carried a request (a kept connection the server may have closed since)
        self._buffer = bytearray(READ_SIZE)
        self._view = memoryview(self._buffer)
        self._record = bytearray(RECORD_SIZE)
        self._record_view = memoryview(self._record)

    @classmethod
    async def open(cls, key: Key, context: Optional[ssl.SSLContext], timeout: Optional[float],
                   request: httpx.Request) -> "_Connection":
        scheme, host, port = key
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                error: Optional[BaseException] = None
                for n, (family, kind, proto, _, address) in enumerate(infos):
                    sock = socket.socket(family, kind, proto)
                    sock.setblocking(False)
                    try:  # an address that does not answer soon gives way to the next (an IPv6 route that is broken)
                        last = n == len(infos) - 1  # (the last one has what is left of the connect timeout)
                        await asyncio.wait_for(loop.sock_connect(sock, address), None if last else ADDRESS_WAIT_S)
                    except (OSError, TimeoutError) as e:
                        sock.close()
                        error = e
                        continue
                    break
                else:
                    raise error or OSError(f"no address for {host}")
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if scheme != "https":
                    return cls(key, sock, None, None, None)
                incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                tls = context.wrap_bio(incoming, outgoing, server_hostname=host)
                conn = cls(key, sock, tls, incoming, outgoing)
                try:
                    await conn._handshake()
                except BaseException:
                    conn.close()
                    raise
                return conn
        except TimeoutError:
            raise httpx.ConnectTimeout(f"connecting to {host} timed out", request=request) from None
        except (OSError, ssl.SSLError) as e:
            raise httpx.ConnectError(f"{e.__class__.__name__}: {e}", request=request) from None

    async def _handshake(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                self.tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush()
                n = await loop.sock_recv_into(self.sock, self._view)
                if not n:
                    raise ConnectionResetError("the server closed the connection during the TLS handshake")
                self._in.write(self._view[:n])
        await self._flush()

    async def _flush(self) -> None:
        if self._out is not None and self._out.pending:
            await asyncio.get_running_loop().sock_sendall(self.sock, self._out.read())

    async def send(self, data: bytes) -> None:
        if self.tls is None:
            await asyncio.get_running_loop().sock_sendall(self.sock, data)
            return
        self.tls.write(data)
        await self._flush()

    def _take(self, n: int) -> None:
        """Puts `n` received bytes (in the read buffer) where the reader finds them: decrypted for TLS."""
        if self.tls is None:
            self.plain += self._view[:n]
            return
        self._in.write(self._view[:n])

    def _decrypt(self) -> None:
        if self.tls is None:
            return
        while True:
            try:
                n = self.tls.read(RECORD_SIZE, self._record)  # into a buffer: read(n) alone allocates n bytes each time
            except ssl.SSLWantReadError:
                return
            except ssl.SSLZeroReturnError:
                self.eof = True
                return
            if not n:
                self.eof = True
                return
            self.plain += self._record_view[:n]

    async def receive(self, wait: float, timeout: Optional[float]) -> None:
        """Waits `wait` seconds (to let data collect), then reads everything the socket holds (waiting for the first
        byte up to `timeout`), into `plain`. Sets `eof` at the end of the stream."""
        loop = asyncio.get_running_loop()
        if wait:
            await asyncio.sleep(wait)
        before = len(self.plain)
        while len(self.plain) == before and not self.eof:
            async with asyncio.timeout(timeout):
                n = await loop.sock_recv_into(self.sock, self._view)
            if not n:
                self.eof = True
                break
            self._take(n)
            while True:  # whatever else is there now, without waiting
                try:
                    n = self.sock.recv_into(self._view)
                except (BlockingIOError, InterruptedError):
                    break
                if not n:
                    self.eof = True
                    break
                self._take(n)
            self._decrypt()
            if self.tls is not None and self._out.pending:  # (a TLS 1.3 key update asks for an answer)
                await self._flush()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass


class _Body(httpx.AsyncByteStream):
    """A response body as it arrives, in batches. The connection goes back to the pool when it was read to its end."""

    def __init__(self, transport: "BulkTransport", conn: _Connection, length: Optional[int], chunked: bool,
                 keep: bool, read_timeout: Optional[float], request: httpx.Request):
        self.transport, self.conn, self.length, self.chunked = transport, conn, length, chunked
        self.keep, self.timeout, self.request = keep, read_timeout, request
        self.done = False

    async def _more(self, wait: float) -> None:
        try:
            await self.conn.receive(wait, self.timeout)
        except TimeoutError:
            raise httpx.ReadTimeout("the server sent nothing in time", request=self.request) from None
        except (OSError, ssl.SSLError) as e:
            raise httpx.ReadError(f"{e.__class__.__name__}: {e}", request=self.request) from None

    async def _line(self) -> bytes:
        conn = self.conn
        while b"\r\n" not in conn.plain:
            if conn.eof or len(conn.plain) > HEAD_MAX:
                raise httpx.RemoteProtocolError("a malformed chunked body", request=self.request)
            await self._more(0)
        line, _, rest = bytes(conn.plain).partition(b"\r\n")
        conn.plain = bytearray(rest)
        return line

    async def _counted(self, n: Optional[int]) -> AsyncIterator[bytes]:
        """`n` bytes (None: up to the end of the stream), in the pieces they arrive in."""
        conn = self.conn
        left = n
        while left is None or left > 0:
            if conn.plain:
                take = len(conn.plain) if left is None else min(left, len(conn.plain))
                piece = bytes(conn.plain[:take])
                del conn.plain[:take]
                if left is not None:
                    left -= take
                yield piece
                continue
            if conn.eof:
                if left is None:
                    return
                raise httpx.RemoteProtocolError("the server closed the connection before the end of the body",
                                                request=self.request)
            await self._more(self.transport.batch_s)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.chunked:
            while True:
                size_line = (await self._line()).split(b";", 1)[0].strip()
                try:
                    size = int(size_line, 16)
                except ValueError:
                    raise httpx.RemoteProtocolError("a malformed chunk size", request=self.request) from None
                if size == 0:
                    while await self._line():  # the trailers, up to the empty line
                        pass
                    break
                async for piece in self._counted(size):
                    yield piece
                await self._line()  # the CRLF after the chunk
        else:
            async for piece in self._counted(self.length):
                yield piece
        self.done = True

    async def aclose(self) -> None:
        if self.done and self.keep and not self.conn.eof:
            self.transport._release(self.conn)
        else:
            self.conn.close()


class BulkTransport(httpx.AsyncBaseTransport):
    def __init__(self, ssl_context: Optional[ssl.SSLContext] = None, batch_s: float = BATCH_S):
        self._context = ssl_context
        self.batch_s = batch_s
        self._idle: Dict[Key, List[_Connection]] = {}
        self._all: List[_Connection] = []

    def _ssl(self) -> ssl.SSLContext:
        if self._context is None:
            self._context = httpx.create_ssl_context()
        return self._context

    def _release(self, conn: _Connection) -> None:
        if not conn.closed:
            self._idle.setdefault(conn.key, []).append(conn)

    async def _connection(self, key: Key, timeouts: Dict[str, Optional[float]], request: httpx.Request) -> _Connection:
        idle = self._idle.get(key) or []
        while idle:
            conn = idle.pop()
            if not conn.closed and not conn.eof and not conn.plain:
                return conn
            conn.close()
        conn = await _Connection.open(key, self._ssl() if key[0] == "https" else None, timeouts["connect"], request)
        self._all = [c for c in self._all if not c.closed] + [conn]
        return conn

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise httpx.UnsupportedProtocol(f"no {url.scheme!r} here", request=request)
        key = (url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80))
        timeouts = _timeouts(request)
        conn = await self._connection(key, timeouts, request)
        reused = conn.used
        conn.used = True
        try:
            return await self._exchange(conn, request, timeouts)
        except (httpx.WriteError, httpx.RemoteProtocolError):
            conn.close()
            # a kept connection the server had closed meanwhile: a request without a body is sent once more, on a new
            # one (a body that was streamed cannot be sent again)
            if not reused or request.method not in ("GET", "HEAD"):
                raise
        except BaseException:
            conn.close()
            raise
        conn = await self._connection(key, timeouts, request)
        conn.used = True
        try:
            return await self._exchange(conn, request, timeouts)
        except BaseException:
            conn.close()
            raise

    async def _exchange(self, conn: _Connection, request: httpx.Request,
                        timeouts: Dict[str, Optional[float]]) -> httpx.Response:
        headers = list(request.headers.raw)
        if not any(k.lower() == b"host" for k, _ in headers):
            headers.insert(0, (b"Host", request.url.netloc))
        chunked = any(k.lower() == b"transfer-encoding" and b"chunked" in v.lower() for k, v in headers)
        head = b"%s %s HTTP/1.1\r\n" % (request.method.encode("ascii"), request.url.raw_path)
        head += b"".join(b"%s: %s\r\n" % (k, v) for k, v in headers) + b"\r\n"
        try:
            async with asyncio.timeout(timeouts["write"]):
                await conn.send(head)
            async for block in request.stream:  # type: ignore[union-attr]
                if not block:
                    continue
                async with asyncio.timeout(timeouts["write"]):
                    await conn.send(b"%x\r\n%s\r\n" % (len(block), block) if chunked else block)
            if chunked:
                async with asyncio.timeout(timeouts["write"]):
                    await conn.send(b"0\r\n\r\n")
        except TimeoutError:
            raise httpx.WriteTimeout("sending took too long", request=request) from None
        except (OSError, ssl.SSLError) as e:
            raise httpx.WriteError(f"{e.__class__.__name__}: {e}", request=request) from None

        body = _Body(self, conn, None, False, False, timeouts["read"], request)
        while b"\r\n\r\n" not in conn.plain:
            if len(conn.plain) > HEAD_MAX:
                raise httpx.RemoteProtocolError("the answer's head is too long", request=request)
            if conn.eof:
                raise httpx.RemoteProtocolError("the server closed the connection without an answer", request=request)
            await body._more(0)
        raw, _, rest = bytes(conn.plain).partition(b"\r\n\r\n")
        conn.plain = bytearray(rest)
        lines = raw.split(b"\r\n")
        parts = lines[0].split(b" ", 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/1.") or not parts[1].isdigit():
            raise httpx.RemoteProtocolError("not an HTTP/1.x answer", request=request)
        status = int(parts[1])
        answer: List[Tuple[bytes, bytes]] = []
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if not sep:
                raise httpx.RemoteProtocolError("a malformed header line", request=request)
            answer.append((name.strip(), value.strip()))
        fields = {k.lower(): v for k, v in answer}
        length: Optional[int] = None
        is_chunked = b"chunked" in fields.get(b"transfer-encoding", b"").lower()
        if not is_chunked and b"content-length" in fields:
            try:
                length = int(fields[b"content-length"])
            except ValueError:
                length = -1
            if length < 0:
                raise httpx.RemoteProtocolError("a malformed Content-Length", request=request)
        if request.method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            length, is_chunked = 0, False
        closes = b"close" in fields.get(b"connection", b"").lower() or parts[0] == b"HTTP/1.0"
        keep = not closes and (is_chunked or length is not None)
        body.length, body.chunked, body.keep = length, is_chunked, keep
        return httpx.Response(status, headers=answer, stream=body, request=request,
                              extensions={"http_version": parts[0], "reason_phrase": parts[2] if len(parts) > 2 else b""})

    async def aclose(self) -> None:
        for conn in self._all:
            conn.close()
        self._all, self._idle = [], {}
