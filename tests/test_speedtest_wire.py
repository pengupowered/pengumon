"""The speed test's bulk transport (speedtest/providers/wire.py) over real loopback sockets: HTTP/1.1 bodies by length,
chunked and to the close, kept connections, TLS with a throwaway certificate, timeouts, and the batched reads that
keep its CPU cost down. Nothing leaves the machine."""

import asyncio
import shutil
import socket
import ssl
import subprocess

import httpx
import pytest

from pengumon.speedtest.providers.wire import BulkTransport


class Server:
    """An HTTP/1.1 server on 127.0.0.1: GET /n/<bytes> answers that many bytes in 1 KiB writes (`chunked` in chunks,
    `close` without a length, then closes); GET /stall sends its head and nothing more; POST reads the body by its
    Content-Length (or chunked) and answers its size. `connections` counts the connections it accepted."""

    def __init__(self):
        self.connections = 0
        self.bodies = []
        self.release = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, target, _ = lines[0].split(" ")
                headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
                if method == "POST":
                    if headers.get("transfer-encoding") == "chunked":
                        body = b""
                        while True:
                            size = int((await reader.readuntil(b"\r\n")).strip(), 16)
                            body += (await reader.readexactly(size + 2))[:size]
                            if not size:
                                break
                    else:
                        body = await reader.readexactly(int(headers["content-length"]))
                    self.bodies.append(body)
                    answer = str(len(body)).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(answer), answer))
                    await writer.drain()
                    continue
                if target == "/stall":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                    await writer.drain()
                    await self.release.wait()
                    return
                kind, size = target.split("/")[1], int(target.split("/")[2])
                if kind == "chunked":
                    writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nX-Kind: chunked\r\n\r\n")
                elif kind == "close":
                    writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % size)
                left = size
                while left > 0:
                    piece = b"x" * min(1024, left)
                    writer.write(b"%x\r\n%s\r\n" % (len(piece), piece) if kind == "chunked" else piece)
                    left -= len(piece)
                    await writer.drain()
                if kind == "chunked":
                    writer.write(b"0\r\nX-Trailer: 1\r\n\r\n")
                await writer.drain()
                if kind == "close":
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def serve(ssl_context=None):
    server = Server()
    listening = await asyncio.start_server(server.handle, "127.0.0.1", 0, ssl=ssl_context)
    return server, listening, listening.sockets[0].getsockname()[1]


async def get(client, url):
    pieces = []
    async with client.stream("GET", url) as response:
        async for piece in response.aiter_bytes():
            pieces.append(piece)
    return response, pieces


async def test_bodies_by_length_chunked_and_to_the_close_arrive_whole_on_a_kept_connection():
    server, listening, port = await serve()
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        base = f"http://127.0.0.1:{port}"
        for path in ("/n/300000", "/chunked/70000", "/n/5"):
            response, pieces = await get(client, base + path)
            assert response.status_code == 200 and sum(map(len, pieces)) == int(path.split("/")[-1])
        assert response.http_version == "HTTP/1.1"
        assert server.connections == 1                                 # one connection, kept between the requests
        _, pieces = await get(client, base + "/close/4000")
        assert sum(map(len, pieces)) == 4000
        assert server.connections == 1                                 # (asked on the kept connection ...)
        await get(client, base + "/n/10")
        assert server.connections == 2                                 # ... which an answer that closes ends
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_reads_are_batched_into_few_large_pieces():
    """1 MB written 1 KiB at a time arrives in pieces far larger than the writes: what keeps the CPU per MB down."""
    server, listening, port = await serve()
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        _, pieces = await get(client, f"http://127.0.0.1:{port}/n/1000000")
        assert sum(map(len, pieces)) == 1_000_000 and len(pieces) < 1_000_000 / 1024 / 4
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_body_closed_early_drops_its_connection_and_the_next_request_opens_another():
    server, listening, port = await serve()
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        async with client.stream("GET", f"http://127.0.0.1:{port}/n/5000000") as response:
            async for _ in response.aiter_bytes():
                break                                                  # the cap: stop reading
        _, pieces = await get(client, f"http://127.0.0.1:{port}/n/100")
        assert sum(map(len, pieces)) == 100 and server.connections == 2
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_streamed_upload_is_sent_by_length_or_chunked():
    server, listening, port = await serve()
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)

    async def body():
        for _ in range(10):
            yield b"y" * 65536

    try:
        response = await client.post(f"http://127.0.0.1:{port}/up", content=body(),
                                     headers={"Content-Length": str(655360)})
        assert response.status_code == 200 and response.text == "655360"
        response = await client.post(f"http://127.0.0.1:{port}/up", content=body())      # no length: chunked
        assert response.text == "655360" and server.bodies[1] == b"y" * 655360
        assert server.connections == 1
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_server_that_stops_sending_is_a_read_timeout_and_a_closed_port_a_connect_error():
    server, listening, port = await serve()
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=httpx.Timeout(5.0, read=0.3))
    try:
        with pytest.raises(httpx.ReadTimeout):
            await get(client, f"http://127.0.0.1:{port}/stall")
    finally:
        server.release.set()
        await client.aclose()
        listening.close()
        await listening.wait_closed()
    # (Windows takes about 2 s to refuse a connection to a closed local port: the limit leaves it room to)
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=10.0)
    try:
        with pytest.raises(httpx.ConnectError):
            await get(client, f"http://127.0.0.1:{port}/n/1")         # nothing listens there any more
    finally:
        await client.aclose()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool to make a test certificate")
async def test_https_with_a_certificate_the_client_trusts_and_not_with_one_it_does_not(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=30)
    served = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    served.load_cert_chain(str(cert), str(key))
    server, listening, port = await serve(served)
    client = httpx.AsyncClient(transport=BulkTransport(ssl.create_default_context(cafile=str(cert))), timeout=5.0)
    try:
        for _ in range(2):
            _, pieces = await get(client, f"https://127.0.0.1:{port}/n/2000000")
            assert sum(map(len, pieces)) == 2_000_000
        response = await client.post(f"https://127.0.0.1:{port}/up", content=b"z" * 300_000)
        assert response.text == "300000" and server.connections == 1
    finally:
        await client.aclose()
    stranger = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)          # the public CAs: not this one
    try:
        with pytest.raises(httpx.ConnectError):
            await get(stranger, f"https://127.0.0.1:{port}/n/1")
    finally:
        await stranger.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_get_on_a_kept_connection_the_server_closed_meanwhile_is_sent_again_on_a_new_one():
    server, listening, port = await serve()
    transport = BulkTransport()
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    try:
        await get(client, f"http://127.0.0.1:{port}/n/10")
        (kept,) = transport._idle[("http", "127.0.0.1", port)]
        kept.sock.shutdown(socket.SHUT_RD)                             # as if the server had closed it while idle
        kept.eof = False
        _, pieces = await get(client, f"http://127.0.0.1:{port}/n/10")
        assert sum(map(len, pieces)) == 10 and server.connections == 2
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_an_address_that_does_not_answer_soon_gives_way_to_the_next(monkeypatch):
    from pengumon.speedtest.providers import wire
    server, listening, port = await serve()
    loop = asyncio.get_running_loop()
    connect = loop.sock_connect

    async def addresses(host, port, **kw):                           # a broken IPv6-like address first, then loopback
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    async def sock_connect(sock, address):
        if address[0] == "192.0.2.1":
            await asyncio.Event().wait()                             # never answers
        return await connect(sock, address)

    monkeypatch.setattr(loop, "getaddrinfo", addresses)
    monkeypatch.setattr(loop, "sock_connect", sock_connect)
    monkeypatch.setattr(wire, "ADDRESS_WAIT_S", 0.05)
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        _, pieces = await asyncio.wait_for(get(client, f"http://speed.example.com:{port}/n/10"), 2.0)
        assert sum(map(len, pieces)) == 10 and server.connections == 1
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_certificate_for_another_name_is_refused_even_from_a_trusted_issuer(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "1", "-subj", "/CN=other.example", "-addext", "subjectAltName=DNS:other.example",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=30)
    served = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    served.load_cert_chain(str(cert), str(key))
    server, listening, port = await serve(served)
    client = httpx.AsyncClient(transport=BulkTransport(ssl.create_default_context(cafile=str(cert))), timeout=5.0)
    try:
        with pytest.raises(httpx.ConnectError):
            await get(client, f"https://127.0.0.1:{port}/n/1")
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_a_negative_content_length_is_a_protocol_error():
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: -5\r\n\r\nhello")
        await writer.drain()
        writer.close()

    listening = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = listening.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        with pytest.raises(httpx.RemoteProtocolError):
            await get(client, f"http://127.0.0.1:{port}/")
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def answering(raw: bytes):
    """A server on 127.0.0.1 that answers every connection's first request with `raw` and closes. Returns (the
    listening server, its port, the requests it received)."""
    received = []

    async def answer(reader, writer):
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(raw)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    listening = await asyncio.start_server(answer, "127.0.0.1", 0)
    return listening, listening.sockets[0].getsockname()[1], received


async def test_a_body_of_many_tiny_chunks_costs_little_and_arrives_in_few_pieces():
    """100000 one-byte chunks (600 kB on the wire): every chunk of a read goes into one piece. Re-copying the buffer
    for every line made this quadratic: the event loop (the whole app) was held for seconds, and for minutes with a
    few MB, by a server that sends such a body (a LibreSpeed server is the owner's choice of any host)."""
    n = 100_000
    listening, port, _ = await answering(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"1\r\nx\r\n" * n
                                         + b"0\r\n\r\n")
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=10.0)
    loop = asyncio.get_running_loop()
    try:
        began = loop.time()
        _, pieces = await get(client, f"http://127.0.0.1:{port}/")
        took = loop.time() - began
        assert b"".join(pieces) == b"x" * n
        assert len(pieces) < 100 and took < 3.0, (len(pieces), took)       # (before: one piece per chunk, ~8 s)
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


@pytest.mark.parametrize("body", [
    b"-1\r\n\r\n0\r\n\r\n",                        # a negative size (int() takes it)
    b"0x5\r\nhello\r\n0\r\n\r\n",                  # a "0x" prefix (int(..., 16) takes it)
    b"1_0\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n",     # an underscore (int() takes it)
    b"\r\nhello\r\n0\r\n\r\n",                     # no size at all
    b"5\r\nhello, and more\r\n0\r\n\r\n",          # a chunk longer than its size
    b"5\r\nhel",                                   # closed in the middle of a chunk
    b"1" * (70 * 1024),                            # a size line with no end
], ids=["negative", "0x prefix", "underscore", "no size", "longer than its size", "closed mid-chunk",
        "size line with no end"])  # (short ids: Windows caps PYTEST_CURRENT_TEST, which holds the id, at 32,767 chars)
async def test_a_malformed_chunked_body_is_a_protocol_error(body):
    listening, port, _ = await answering(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        with pytest.raises(httpx.RemoteProtocolError):
            await get(client, f"http://127.0.0.1:{port}/")
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


async def test_chunk_extensions_and_trailers_are_dropped_and_the_body_arrives_whole():
    listening, port, _ = await answering(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                                         b"5;name=value\r\nhello\r\nA \r\n, world!!!\r\n"
                                         b"0\r\nX-One: 1\r\nX-Two: 2\r\n\r\n")
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        _, pieces = await get(client, f"http://127.0.0.1:{port}/")
        assert b"".join(pieces) == b"hello, world!!!"
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


@pytest.mark.parametrize("method, headers", [
    ("GET", {"X-Token": "abc\r\nX-Injected: 1"}),                # a CR LF in a value starts a header of its own
    ("GET", {"X-Token": "abc\nGET /other HTTP/1.1"}),            # (a bare LF too)
    ("GET", {"X-Token": "abc\x00"}),
    ("GET", {"X Token": "abc"}),                                 # a name that is not a token
    ("GET /other HTTP/1.1\r\nX:", {}),                           # a method that is not a token
])
async def test_a_request_head_that_would_break_out_of_its_line_is_refused_before_anything_is_sent(method, headers):
    """httpx leaves these checks to h11, which this transport replaces; its URL already refuses control characters."""
    listening, port, received = await answering(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        with pytest.raises(httpx.LocalProtocolError):
            await client.request(method, f"http://127.0.0.1:{port}/", headers=headers)
        assert received == []
        assert (await client.get(f"http://127.0.0.1:{port}/", headers={"X-Token": "abc"})).status_code == 200
        assert len(received) == 1                                       # (a plain one goes through)
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()


@pytest.mark.parametrize("status", [b"2000", b"20", b"9" * 5000], ids=["4 digits", "2 digits", "5000 digits"])
async def test_a_status_that_is_not_three_digits_is_a_protocol_error(status):
    """Not a ValueError out of int() (5000 digits are past Python's limit), whose text would become the test's reason."""
    listening, port, _ = await answering(b"HTTP/1.1 " + status + b" OK\r\nContent-Length: 0\r\n\r\n")
    client = httpx.AsyncClient(transport=BulkTransport(), timeout=5.0)
    try:
        with pytest.raises(httpx.RemoteProtocolError):
            await client.get(f"http://127.0.0.1:{port}/")
    finally:
        await client.aclose()
        listening.close()
        await listening.wait_closed()
