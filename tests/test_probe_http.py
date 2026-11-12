"""The HTTP(S) probe (engine/probe_http.py) against scripted servers on loopback inside the test container."""

import asyncio
import shutil
import ssl
import subprocess
import time

import pytest

from pengumon.engine.probe_http import USER_AGENT, http_status, request_bytes


class Server:
    """A loopback server that records each request's bytes and answers with `reply(requests_so_far)` (bytes), then
    after `then` seconds closes (None: at once)."""

    def __init__(self, reply=b"HTTP/1.1 200 OK\r\n\r\n", then=None, tls=None):
        self.reply, self.then, self.tls = reply, then, tls
        self.requests = []
        self.connections = 0
        self.server = None
        self.port = None

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        except Exception:
            writer.close()
            return
        self.requests.append(data)
        reply = self.reply(data) if callable(self.reply) else self.reply
        if isinstance(reply, list):  # chunks with sleeps between: [bytes, seconds, bytes, ...]
            for part in reply:
                if isinstance(part, (int, float)):
                    await asyncio.sleep(part)
                else:
                    writer.write(part)
                    await writer.drain()
        elif reply:
            writer.write(reply)
            await writer.drain()
        if self.then:
            await asyncio.sleep(self.then)
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=self.tls)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def ask(server, scheme="http", path="/", verb="HEAD", timeout=1.0, host="127.0.0.1", port="server", **kw):
    return await http_status("127.0.0.1", 4, host, server.port if port == "server" else port, scheme, path, verb,
                             timeout, **kw)


async def test_the_request_is_exactly_the_fixed_text_even_with_a_proxy_in_the_environment(monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://198.51.100.1:9")
    async with Server() as server:
        a = await ask(server, path="/health", verb="GET")
    assert a.kind == "reply" and a.status == 200 and 0 < a.rtt_ms
    raw = server.requests[0]
    lines = raw.decode("ascii").split("\r\n")
    assert lines[0] == "GET /health HTTP/1.1"
    headers = dict(line.split(": ", 1) for line in lines[1:] if line)
    assert set(headers) == {"Host", "User-Agent", "Accept", "Connection"}
    assert headers["User-Agent"] == USER_AGENT and USER_AGENT.startswith("Pengumon/")
    assert headers["Host"] == f"127.0.0.1:{server.port}" and headers["Connection"] == "close"
    assert raw == request_bytes("GET", f"127.0.0.1:{server.port}", "/health")
    assert b"Cookie" not in raw and b"Authorization" not in raw


@pytest.mark.parametrize("status, kind", [(200, "reply"), (404, "reply"), (503, "http_error"), (502, "http_error")])
async def test_status_codes_below_500_are_answers_and_5xx_are_losses(status, kind):
    async with Server(reply=f"HTTP/1.1 {status} Whatever\r\nContent-Length: 0\r\n\r\n".encode()) as server:
        a = await ask(server)
    assert a.kind == kind and a.status == status
    assert (a.error == f"HTTP {status} from the server") == (kind == "http_error")
    assert a.connect_ms <= a.rtt_ms


async def test_a_redirect_is_an_answer_and_is_not_followed():
    async with Server() as other:
        async with Server(reply=f"HTTP/1.1 301 Moved\r\nLocation: http://127.0.0.1:{other.port}/\r\n\r\n".encode()) as first:
            a = await ask(first)
        assert a.kind == "reply" and a.status == 301
        await asyncio.sleep(0.05)
        assert other.connections == 0


async def test_an_answer_that_is_no_status_line_is_refused_with_fixed_words():
    async with Server(reply=b"garbage\n") as server:
        assert (await ask(server)).error == "Not an HTTP answer"
    async with Server(reply=b"x" * 2048, then=2) as server:
        a = await ask(server)
        assert a.kind == "bad_answer" and a.error == "Not an HTTP answer (no status line in the first 1024 bytes)"
    async with Server(reply=b"") as server:
        a = await ask(server)
        assert a.kind == "bad_answer" and "closed the connection" in a.error


async def test_a_status_line_over_the_limit_is_cut_off_whatever_follows():
    long_line = b"HTTP/1.1 200 " + b"A" * 2000 + b"\r\n"
    async with Server(reply=long_line) as server:
        assert (await ask(server)).kind == "bad_answer"


async def test_the_body_is_never_read():
    reply = [b"HTTP/1.1 200 OK\r\nContent-Length: 100000000\r\n\r\n", 2, b"x" * 100000]
    async with Server(reply=reply) as server:
        started = time.monotonic()
        a = await ask(server, verb="GET", timeout=1.0)
        assert a.kind == "reply" and time.monotonic() - started < 0.8


async def test_no_answer_in_time_names_the_phase():
    async with Server(reply=b"", then=3) as server:
        a = await ask(server, timeout=0.3)
    assert a.kind == "timeout" and a.error == "No answer in time (waiting for the HTTP answer)"


async def test_a_closed_port_is_refused_with_its_number():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    a = await http_status("127.0.0.1", 4, "127.0.0.1", port, "http", "/", "HEAD", 5.0)  # Windows reports a refusal after ~2 s; Linux at once
    assert (a.kind, a.error) == ("refused", f"Connection refused on port {port}")


# --- TLS ---------------------------------------------------------------------------------------------------------------

def _certificate(folder, name="127.0.0.1", san="IP:127.0.0.1", days="1"):
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", days, "-subj", f"/CN={name}", "-addext", f"subjectAltName={san}",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=30)
    return cert, key


def _server_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool for a test certificate")


@needs_openssl
async def test_a_self_signed_certificate_is_not_trusted_by_default_but_is_with_its_own_context(tmp_path):
    cert, key = _certificate(tmp_path)
    async with Server(tls=_server_context(cert, key)) as server:
        a = await ask(server, scheme="https")
        assert (a.kind, a.error) == ("tls", "TLS: certificate not trusted")
        trusting = ssl.create_default_context(cafile=str(cert))
        b = await ask(server, scheme="https", ssl_context=trusting)
        assert b.kind == "reply" and b.status == 200 and 0 < b.connect_ms <= b.rtt_ms


@needs_openssl
async def test_a_certificate_for_another_name_is_named(tmp_path):
    cert, key = _certificate(tmp_path, name="other.example", san="DNS:other.example")
    async with Server(tls=_server_context(cert, key)) as server:
        trusting = ssl.create_default_context(cafile=str(cert))
        a = await ask(server, scheme="https", ssl_context=trusting)
    assert (a.kind, a.error) == ("tls", "TLS: certificate is for another name")


async def test_a_server_that_does_not_speak_tls_is_a_tls_failure_not_a_crash():
    async with Server(reply=b"HTTP/1.1 200 OK\r\n\r\n") as server:
        a = await ask(server, scheme="https")
    assert a.kind in ("tls", "bad_answer", "timeout") and a.status is None
    assert "HTTP/1.1" not in (a.error or "")
