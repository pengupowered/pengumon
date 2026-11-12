"""Driver HTTP (pengumon.drivers.http) and the Xfinity XB driver's session handling and errors."""

import asyncio
import functools
import gzip
import logging
import time
import tracemalloc
import zlib
from contextlib import contextmanager

import httpx
import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.drivers import http as http_module
from pengumon.drivers import xfinity_xb as xfinity_module
from pengumon.drivers.http import DeviceHttp, host_of
from pengumon.drivers.xfinity_xb import SESSION_LIFETIME_SECONDS, XfinityXbDriver


class FakeSpan:
    def __init__(self, name, attributes):
        self.name, self.attributes, self.status = name, dict(attributes), None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status


class FakeTracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name, attributes=None, **_):
        self.spans.append(FakeSpan(name, attributes or {}))
        yield self.spans[-1]


@pytest.fixture
def tracer(monkeypatch):
    fake = FakeTracer()
    monkeypatch.setattr(http_module, "tracer", fake)
    return fake


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url, host", [
    ("http://192.168.100.1", "192.168.100.1"), ("https://router.lan:8443/x", "router.lan"), ("192.168.1.1", "192.168.1.1"),
])
def test_host_of(url, host):
    assert host_of(url) == host


async def test_requests_get_a_span_and_a_debug_line_without_query_or_body(tracer, caplog):
    http = DeviceHttp("http://192.168.100.1/", lambda: None, driver="test",
                      client=_client(lambda r: httpx.Response(200, text="hello")))
    with caplog.at_level(logging.DEBUG, logger="pengumon.drivers.test"):
        res = await http.post("login.cgi", params={"token": "s3cret"}, data={"password": "hunter2"})
    assert res.text == "hello"
    span = tracer.spans[0]
    assert span.name == "POST /login.cgi" and span.status is None
    assert span.attributes == {"http.request.method": "POST", "url.path": "/login.cgi",  # (no address: exported over OTLP)
                               "http.response.status_code": 200}
    record = caplog.records[0]
    context = getattr(record, "pengumon_context")
    assert record.getMessage() == "request" and context["status"] == 200 and context["bytes"] == 5
    assert context["path"] == "/login.cgi" and "host" not in context  # (exported over OTLP: no address)
    assert "s3cret" not in repr(context) and "hunter2" not in repr(context) and "s3cret" not in repr(span.attributes)


async def test_error_status_marks_the_span(tracer):
    http = DeviceHttp("http://192.168.100.1", lambda: None, driver="test",
                      client=_client(lambda r: httpx.Response(502)))
    assert (await http.get("appGet.cgi")).status_code == 502
    assert tracer.spans[0].status is not None and tracer.spans[0].attributes["http.response.status_code"] == 502


async def test_transport_errors_are_logged_and_raised(tracer, caplog):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    http = DeviceHttp("http://192.168.100.1", lambda: None, driver="test", client=_client(handler))
    with caplog.at_level(logging.DEBUG, logger="pengumon.drivers.test"), pytest.raises(httpx.ConnectError):
        await http.get("status")
    assert tracer.spans[0].attributes["error.type"] == "ConnectError"
    context = getattr(caplog.records[0], "pengumon_context")
    assert caplog.records[0].getMessage() == "request failed" and context["detail"] == "connection refused"


async def test_client_is_created_once_and_again_after_close():
    made = []

    def factory():
        made.append(_client(lambda r: httpx.Response(204)))
        return made[-1]

    http = DeviceHttp("http://192.168.100.1", factory, driver="test")
    await http.get("a")
    await http.get("b")
    assert len(made) == 1
    await http.aclose()
    assert made[0].is_closed
    await http.get("c")
    assert len(made) == 2
    await http.aclose()


# --- Xfinity XB driver -----------------------------------------------------------------------------------

PAGE = """<table>
<tr><th class="row-label">Downstream</th></tr>
<tr><th class="row-label">Channel ID</th><td>1</td></tr>
<tr><th class="row-label">Power Level</th><td>1.0 dBmV</td></tr>
<tr><th class="row-label">SNR</th><td>38.0 dB</td></tr>
</table>"""
LOGIN_PAGE = "<html>Please Login First <form action='check.jst'></form></html>"


class Gateway:
    """A gateway that wants a login first; `password` is the one it accepts."""

    def __init__(self, password="pw", status_after_login=200, page=PAGE):
        self.password = password
        self.status_after_login = status_after_login
        self.page = page
        self.logged_in = False
        self.paths = []

    def __call__(self, request):
        self.paths.append(request.url.path)
        if request.url.path.endswith("check.jst"):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("password") != self.password:
                return httpx.Response(200, text="Incorrect password")
            self.logged_in = True
            return httpx.Response(200, text="ok")
        if not self.logged_in:
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(self.status_after_login, text=self.page)


def _driver(gateway, password="pw"):
    return XfinityXbDriver("http://192.168.100.1", "admin", password, client=_client(gateway))


async def test_logs_in_when_the_session_is_gone_and_keeps_it():
    gateway = Gateway()
    driver = _driver(gateway)
    result = await driver.poll()
    assert gateway.paths == ["/check.jst", "/network_setup.jst"]  # no recent read: the login comes first
    reading = result.readings[Capability.ACCESS_LINE]
    assert reading.technology == "docsis" and reading.channels[0].snr_db == 38.0
    gateway.paths.clear()
    await driver.poll()
    assert gateway.paths == ["/network_setup.jst"]  # the session is reused


async def test_wrong_password_is_an_auth_error_naming_the_host():
    with pytest.raises(DriverError) as raised:
        await _driver(Gateway(password="right"), password="wrong").poll()
    assert raised.value.kind == "auth" and not raised.value.transport
    assert raised.value.message == "Authentication failed on gateway (192.168.100.1). Please verify password."


async def test_a_page_without_channels_is_an_error_not_a_healthy_modem():
    with pytest.raises(DriverError) as raised:
        await _driver(Gateway(page="<html>Downstream: none locked</html>")).poll()
    assert raised.value.kind == "protocol" and "no DOCSIS channel table" in raised.value.message


async def test_an_error_status_after_login_is_reported():
    with pytest.raises(DriverError) as raised:
        await _driver(Gateway(status_after_login=503)).poll()
    assert raised.value.message == "the gateway's status page answered HTTP 503"


def test_missing_configuration_and_credentials():
    driver = XfinityXbDriver("http://192.168.100.1", "admin", None)
    assert driver.missing_configuration() == "no password" and driver.host == "192.168.100.1"
    driver.set_credentials("admin", "pw")
    assert driver.missing_configuration() is None


class CookieGateway:
    """A gateway whose session lives in a cookie, as a real one does; check.jst records the passwords tried."""

    def __init__(self, passwords=("pw", "pw2")):
        self.passwords = passwords
        self.paths = []
        self.logins = []

    def __call__(self, request):
        self.paths.append(request.url.path)
        if request.url.path.endswith("check.jst"):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.logins.append(form.get("password"))
            if form.get("password") not in self.passwords:
                return httpx.Response(200, text="Incorrect password")
            return httpx.Response(200, text="ok", headers={"set-cookie": "DUKSID=abc; Path=/"})
        if "DUKSID=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(200, text=PAGE)


async def test_new_credentials_clear_the_session_so_the_next_poll_logs_in_again():
    gateway = CookieGateway()
    driver = _driver(gateway)
    await driver.poll()
    gateway.paths.clear()
    await driver.poll()
    assert gateway.paths == ["/network_setup.jst"] and gateway.logins == ["pw"]  # the session is reused

    gateway.paths.clear()
    driver.set_credentials("admin", "pw2")
    await driver.poll()
    assert gateway.paths == ["/check.jst", "/network_setup.jst"]
    assert gateway.logins == ["pw", "pw2"]  # logged in again, with the new password


async def test_a_login_answer_that_is_no_known_refusal_but_leaves_the_login_page_is_an_auth_error():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("check.jst"):
            return httpx.Response(200, text="Sorry, try again later")  # wording _login does not know
        return httpx.Response(200, text=LOGIN_PAGE)

    with pytest.raises(DriverError) as raised:
        await _driver(handler).poll()
    assert raised.value.kind == "auth" and not raised.value.transport
    assert "login page" in raised.value.message and "192.168.100.1" in raised.value.message
    assert paths == ["/check.jst", "/network_setup.jst"]


def _login_transport_error(request):
    if request.url.path.endswith("check.jst"):
        raise httpx.ConnectError("connection refused")
    return httpx.Response(200, text=LOGIN_PAGE)


async def test_a_transport_error_during_the_login_propagates_as_an_httpx_error():
    with pytest.raises(httpx.ConnectError):
        await _driver(_login_transport_error).poll()


async def test_a_transport_error_during_the_login_becomes_a_communication_error_snapshot():
    from device_helpers import build_modem
    modem = build_modem(password="pw", client=_client(_login_transport_error))
    snap = await modem.poll(force=True)
    assert snap.status == "error" and snap.error_kind == "unreachable"
    assert snap.error_message.startswith("Failed to communicate with modem:")


class TickingClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _timed_driver(gateway, clock, password="pw"):
    return XfinityXbDriver("http://192.168.100.1", "admin", password, client=_client(gateway), monotonic=clock)


async def test_a_poll_after_a_recent_read_tries_the_cookie_first_and_an_old_one_logs_in_first():
    gateway, clock = CookieGateway(), TickingClock()
    driver = _timed_driver(gateway, clock)
    await driver.poll()
    assert gateway.paths == ["/check.jst", "/network_setup.jst"]  # nothing read yet: the login comes first (2)
    gateway.paths.clear()
    clock.now += SESSION_LIFETIME_SECONDS - 1
    await driver.poll()
    assert gateway.paths == ["/network_setup.jst"]  # the cookie is fresh (1)
    gateway.paths.clear()
    clock.now += SESSION_LIFETIME_SECONDS + 1
    await driver.poll()
    assert gateway.paths == ["/check.jst", "/network_setup.jst"]  # older than a session lasts: login first (2)


async def test_a_short_lived_session_inside_the_lifetime_still_costs_three_requests():
    gateway, clock = Gateway(), TickingClock()
    driver = _timed_driver(gateway, clock)
    await driver.poll()
    gateway.paths.clear()
    gateway.logged_in = False  # the gateway dropped the session early
    clock.now += 10
    await driver.poll()
    assert gateway.paths == ["/network_setup.jst", "/check.jst", "/network_setup.jst"]


async def test_a_refused_login_costs_one_request_and_a_failed_poll_does_not_count_as_a_read():
    gateway, clock = Gateway(password="right"), TickingClock()
    driver = _timed_driver(gateway, clock, password="wrong")
    with pytest.raises(DriverError):
        await driver.poll()
    assert gateway.paths == ["/check.jst"]
    gateway.paths.clear()
    clock.now += 5
    with pytest.raises(DriverError):
        await driver.poll()
    assert gateway.paths == ["/check.jst"]  # still logs in first: there was never a good read


_REAL_CLIENT = httpx.AsyncClient


def _real_client_over(monkeypatch, handler):
    """Makes the driver's own `_new_client` (its redirect rule, its hooks) talk to `handler` instead of the network."""
    real = _REAL_CLIENT
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_the_login_is_never_followed_to_another_host(monkeypatch):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"Location": "http://203.0.113.9/check.jst"})  # 307 re-sends the form

    _real_client_over(monkeypatch, handler)
    driver = XfinityXbDriver("http://192.168.100.1", "admin", "pw")
    with pytest.raises(DriverError) as raised:
        await driver.poll()
    assert raised.value.kind == "protocol"
    assert hosts == ["192.168.100.1"]  # the other host never got the user name and password


async def test_a_redirect_to_https_on_the_same_host_is_followed_but_https_never_goes_down_to_http(monkeypatch):
    gateway = Gateway()
    seen = []

    def to_https(request):
        seen.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(307, headers={"Location": str(request.url.copy_with(scheme="https"))})
        return gateway(request)

    _real_client_over(monkeypatch, to_https)
    assert (await XfinityXbDriver("http://192.168.100.1", "admin", "pw").poll()).readings
    assert seen[0].startswith("http://") and seen[1].startswith("https://192.168.100.1/")

    seen.clear()

    def to_http(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://192.168.100.1/check.jst"})

    _real_client_over(monkeypatch, to_http)
    with pytest.raises(DriverError):
        await XfinityXbDriver("https://192.168.100.1", "admin", "pw").poll()
    assert seen == ["https://192.168.100.1/check.jst"]


async def test_redirects_are_followed_by_hand_through_the_bounded_reader(monkeypatch):
    """A 307 re-sends the login form to the same host, a 302 turns the request into a GET, a relative Location is
    resolved, and the session cookie set on a hop is kept."""
    gateway = Gateway()
    seen = []

    def hops(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/check.jst" and len(seen) == 1:
            return httpx.Response(307, headers={"Location": "/login/check.jst"})
        if request.url.path == "/login/check.jst":
            assert request.content  # the form came along
            return gateway(httpx.Request("POST", "http://192.168.100.1/check.jst", content=request.content))
        if request.url.path == "/network_setup.jst" and gateway.logged_in and len(seen) == 3:
            return httpx.Response(302, headers={"Location": "/moved/network_setup.jst"})
        return gateway(request)

    _real_client_over(monkeypatch, hops)
    assert (await XfinityXbDriver("http://192.168.100.1", "admin", "pw").poll()).readings
    assert seen == [("POST", "/check.jst"), ("POST", "/login/check.jst"), ("GET", "/network_setup.jst"),
                    ("GET", "/moved/network_setup.jst")]


async def test_a_redirect_loop_ends_after_a_few_hops(monkeypatch):
    seen = []

    def loop(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/again"})

    _real_client_over(monkeypatch, loop)
    with pytest.raises(DriverError, match="redirected more than") as raised:
        await XfinityXbDriver("http://192.168.100.1", "admin", "pw").poll()
    assert raised.value.kind == "protocol" and len(seen) == xfinity_module.MAX_REDIRECTS + 1


async def test_a_redirect_answer_is_read_through_the_bounded_reader_not_by_httpx(monkeypatch):
    """Httpx read each redirect's body itself, decoded, without a limit; a gzip bomb as the body of a 302 inflated
    to its full size. Now the hop is read like any answer: up to the limit, and the poll fails."""
    def bomb(request):
        return httpx.Response(302, headers={"Location": "/next", "content-encoding": "gzip"},
                              stream=Pieces([gzip_bomb()]))

    _real_client_over(monkeypatch, bomb)
    async def poll():
        with pytest.raises(DriverError) as raised:
            await XfinityXbDriver("http://192.168.100.1", "admin", "pw").poll()
        return raised.value

    error, peak = await _peak(poll())
    assert error.kind == "protocol"
    assert peak < 8 * 1024 * 1024  # (the unbounded read inflated all 64 MiB)


# --- bounded bodies: the cap counts decoded bytes, and only that much is ever decoded -----------------

class Pieces(httpx.AsyncByteStream):
    """A streamed body, handed over piece by piece as the client reads it; `served` counts what was taken."""

    def __init__(self, pieces):
        self.pieces = pieces
        self.served = 0

    async def __aiter__(self):
        for piece in self.pieces:
            self.served += len(piece)
            yield piece


@functools.lru_cache(maxsize=None)
def gzip_bomb(mib=64):
    """`mib` MiB of zeros as gzip: about a thousandth of that."""
    z = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    zeros = bytes(1 << 20)
    return b"".join([z.compress(zeros) for _ in range(mib)] + [z.flush()])


def _streamed(body_pieces, headers=None, seen=None):
    stream = Pieces(body_pieces)

    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, headers=headers or {}, stream=stream)
    return handler, stream


async def _peak(coroutine):
    """(result, peak bytes allocated while it ran)."""
    tracemalloc.start()
    try:
        result = await coroutine
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def test_read_capped_asks_for_no_compression_and_inflates_a_compressed_answer_only_up_to_the_cap():
    seen = []
    handler, _ = _streamed([gzip_bomb()], {"content-encoding": "gzip"}, seen)
    (status, _, body, truncated), peak = await _peak(
        http_module.read_capped(_client(handler), "GET", "http://192.0.2.1/desc.xml", limit=64 * 1024))
    assert status == 200 and truncated and body == bytes(64 * 1024)
    assert peak < 8 * 1024 * 1024  # (httpx's decoder inflated the whole piece: 64 MiB)
    assert seen[0].headers["accept-encoding"] == "identity"


async def test_read_capped_stops_reading_a_compressed_answer_that_decodes_to_nothing_more():
    handler, stream = _streamed([gzip.compress(b"hello")] + [b"x" * 65536] * 1000, {"content-encoding": "gzip"})
    _, _, body, truncated = await http_module.read_capped(_client(handler), "GET", "http://192.0.2.1/")
    assert body == b"hello" and truncated
    assert stream.served < 300 * 1024  # not the 64 MB that follow the end of the gzip stream


@pytest.mark.parametrize("encoding, data", [
    ("gzip", gzip.compress(b"hello world")), ("deflate", zlib.compress(b"hello world")),
    ("deflate", zlib.compress(b"hello world")[2:-4]),  # raw deflate, without the zlib header (httpx takes it too)
    ("identity", b"hello world"), ("x-unknown", b"hello world"),  # (an unknown coding is left as it is, as httpx does)
], ids=["gzip", "deflate", "raw-deflate", "identity", "unknown"])
async def test_read_capped_decodes_what_httpx_decoded(encoding, data):
    handler, _ = _streamed([data[:3], data[3:]], {"content-encoding": encoding})
    _, _, body, truncated = await http_module.read_capped(_client(handler), "GET", "http://192.0.2.1/")
    assert body == b"hello world" and not truncated


@pytest.mark.parametrize("encoding, data", [("gzip", b"not gzip at all"), ("gzip, gzip", gzip.compress(gzip.compress(b"x")))],
                         ids=["corrupt", "twice"])
async def test_read_capped_refuses_what_does_not_decode_and_double_compression(encoding, data):
    handler, _ = _streamed([data], {"content-encoding": encoding})
    with pytest.raises(httpx.DecodingError):
        await http_module.read_capped(_client(handler), "GET", "http://192.0.2.1/")


async def test_capped_body_can_refuse_any_compression():
    response = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Pieces([gzip.compress(b"x")]))
    with pytest.raises(httpx.DecodingError, match="compressed"):
        http_module.CappedBody(response, 10, compressed=False)
    plain = http_module.CappedBody(httpx.Response(200, stream=Pieces([b"abc", b"def"])), 4, compressed=False)
    assert await plain.read() == b"abcd" and plain.truncated


async def test_request_reads_a_compressed_answer_into_memory_decoded_and_asks_for_none(tracer):
    seen = []
    handler, _ = _streamed([gzip.compress(b"hello")], {"content-encoding": "gzip", "set-cookie": "sid=abc; Path=/"},
                           seen)
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test", client=_client(handler))
    res = await http.post("login.cgi", data={"a": "b"}, headers={"User-Agent": "test-agent"})
    assert res.status_code == 200 and res.text == "hello" and res.content == b"hello"
    assert "content-encoding" not in res.headers and res.cookies.get("sid") == "abc"
    assert seen[0].headers["accept-encoding"] == "identity" and seen[0].headers["user-agent"] == "test-agent"
    assert tracer.spans[0].attributes["http.response.status_code"] == 200


async def test_request_refuses_an_answer_longer_than_its_limit_without_reading_it_all(tracer):
    handler, stream = _streamed([b"x" * 65536] * 100)
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test", client=_client(handler))
    with pytest.raises(DriverError) as raised:
        await http.get("ubus", limit=128 * 1024)
    assert raised.value.kind == "protocol" and "more than 128 KiB" in raised.value.message
    assert stream.served <= 256 * 1024


async def test_request_bounds_a_compressed_answer_after_decoding(tracer):
    handler, _ = _streamed([gzip_bomb()], {"content-encoding": "gzip"})
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test", client=_client(handler))
    tracemalloc.start()
    try:
        with pytest.raises(DriverError) as raised:
            await http.get("network_setup.jst")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert raised.value.kind == "protocol" and "more than 1024 KiB" in raised.value.message
    assert peak < 16 * 1024 * 1024  # (httpx inflated all of it: 64 MiB)


# --- the gateway's channel table (Linear in the page, whatever it holds) -------------------------------------

FULL_PAGE = """<html><body><div class="module">
<table class="data"><thead><tr><th class="row-label">Downstream</th><th colspan="3">Channel Bonding Value</th></tr></thead>
<tbody>
<tr><th class="row-label "><div class="netWidth">Channel ID</div></th><td><div class="netWidth">1</div></td><td>2</td><td>3</td></tr>
<tr><th class="row-label">Lock Status</th><td>Locked</td><td>Locked</td><td>Locked</td></tr>
<tr><th class="row-label">Frequency</th><td>531 MHz</td><td>537000000 Hz</td><td>543 MHz</td></tr>
<tr><th class="row-label">SNR</th><td>38.5 dB</td><td>38.9 dB</td><td>NA</td></tr>
<tr><th class="row-label">Power Level</th><td>2.1 dBmV</td><td>-1.4 dBmV</td><td>0.3 dBmV</td></tr>
<tr><th class="row-label">Modulation</th><td>256 QAM</td><td>256 QAM</td><td>OFDM</td></tr>
</tbody></table>
<table class="data"><thead><tr><th class="row-label">Upstream</th></tr></thead>
<tr><td class="row-label">Channel ID</td><td>1</td><td>2</td></tr>
<tr><td class="row-label">Lock Status</td><td>Locked</td><td>Locked</td></tr>
<tr><td class="row-label">Frequency</td><td>17 MHz</td><td>23700000 Hz</td></tr>
<tr><td class="row-label">Symbol Rate</td><td>5120</td><td>5120</td></tr>
<tr><td class="row-label">Power Level</td><td>44.0 dBmV</td><td>45.5 dBmV</td></tr>
<tr><td class="row-label">Modulation</td><td>QAM</td><td>QAM</td></tr>
</table>
<table class="data"><thead><tr><th class="row-label">CM Error Codewords</th></tr></thead>
<tr><th class="row-label">Channel ID</th><td>1</td><td>2</td><td>3</td></tr>
<tr><th class="row-label">Unerrored Codewords</th><td>1000</td><td>2000</td><td>3000</td></tr>
<tr><th class="row-label">Correctable Codewords</th><td>10</td><td>20</td><td>30</td></tr>
<tr><th class="row-label">Uncorrectable Codewords</th><td>1</td><td>0</td><td>7</td></tr>
</table></div></body></html>"""

VERTICAL_PAGE = """<table>
<tr><th>Channel</th><th>Lock Status</th><th>Modulation</th><th>Frequency</th><th>Power</th><th>SNR</th>
<th>Corrected</th><th>Uncorrectables</th></tr>
<tr><td>1</td><td>Locked</td><td>QAM256</td><td>531000000 Hz</td><td>-2.1 dBmV</td><td>38.5 dB</td><td>10</td><td>1</td></tr>
<tr><td>2</td><td>Locked</td><td>QAM256</td><td>537 MHz</td><td>-1.4 dBmV</td><td>38.9 dB</td><td>20</td><td>0</td></tr>
</table>
<table>
<tr><td>3</td><td>Locked</td><td>ATDMA</td><td>17000000 Hz</td><td>5120 Ksym/sec</td><td>44.0 dBmV</td></tr>
</table>"""


def _summary(channels):
    return [(c.direction, c.channel_id, c.frequency_mhz, c.power_dbmv, c.snr_db, c.modulation, c.correcteds,
             c.uncorrectables, c.unerrored) for c in channels]


def test_the_channel_table_is_read_as_before():
    assert _summary(XfinityXbDriver._parse_channels(FULL_PAGE)) == [
        ("downstream", 1, 531.0, 2.1, 38.5, "256 QAM", 10, 1, 1000),
        ("downstream", 2, 537.0, -1.4, 38.9, "256 QAM", 20, 0, 2000),
        ("downstream", 3, 543.0, 0.3, None, "OFDM", 30, 7, 3000),
        ("upstream", 1, 17.0, 44.0, None, "QAM", 0, 0, None),
        ("upstream", 2, 23.7, 45.5, None, "QAM", 0, 0, None),
    ]


def test_the_vertical_layout_is_read_row_by_row():
    assert _summary(XfinityXbDriver._parse_channels(VERTICAL_PAGE)) == [
        ("downstream", 1, 531.0, -2.1, 38.5, "QAM256", 10, 1, None),
        ("downstream", 2, 537.0, -1.4, 38.9, "QAM256", 20, 0, None),
        ("upstream", 3, 17.0, 44.0, None, "ATDMA", 0, 0, None),
    ]


@pytest.mark.parametrize("page", [
    "<tr><td>1</td><td>x</td>" * 400,  # the fallback's patterns: 7 s before (cubic in the rows)
    "<tr" * 50000,  # row starts without ">" (quadratic before)
    "<tr><td>1</td><td>x</td>" * 20000,  # 480 KB: as long as a page that is read
    "<tr><td>" * 60000,  # cells that never close
    "<tr><th class='row-label'>Downstream</th></tr><tr><th class='row-label'>Channel ID</th>" + "<td>1<b" * 80000,
], ids=["fallback-10kb", "row-starts", "fallback-480kb", "open-cells", "open-tags"])
def test_a_page_that_is_no_channel_table_is_parsed_in_linear_time(page):
    started = time.perf_counter()
    assert XfinityXbDriver._parse_channels(page) == []
    assert time.perf_counter() - started < 2.0  # (loaded machine: well under a tenth of that here)


def test_the_channels_and_their_modulation_text_are_capped():
    n = 300
    page = ("<table><tr><th class='row-label'>Downstream</th></tr>"
            "<tr><th class='row-label'>Channel ID</th>" + "".join(f"<td>{i}</td>" for i in range(1, n + 1)) + "</tr>"
            "<tr><th class='row-label'>Power Level</th>" + "<td>1.0 dBmV</td>" * n + "</tr>"
            "<tr><th class='row-label'>Modulation</th>" + "<td>QAM {} 256</td>".format("x" * 100) * n + "</tr></table>")
    channels = XfinityXbDriver._parse_channels(page)
    assert len(channels) == 128 and [c.channel_id for c in channels[:3]] == [1, 2, 3]
    assert all(len(c.modulation) <= 16 for c in channels) and channels[0].modulation.startswith("QAM x")


# --- a total deadline: httpx's timeouts are per read, a drip-fed answer must still end -------------------------

class Drip(httpx.AsyncByteStream):
    """An answer that sends one byte every `gap` seconds, for ever (well inside each read timeout)."""

    def __init__(self, gap):
        self.gap = gap

    async def __aiter__(self):
        while True:
            await asyncio.sleep(self.gap)
            yield b"x"


def _dripping_client(gap=0.02, read=0.05):
    return httpx.AsyncClient(timeout=httpx.Timeout(read), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Drip(gap))))


def test_the_total_deadline_is_the_connect_timeout_plus_three_read_timeouts():
    assert http_module.total_deadline(httpx.Timeout(6.0)) == 24.0
    assert http_module.total_deadline(httpx.Timeout(5.0, connect=1.0)) == 16.0
    assert http_module.total_deadline(2.0) == 8.0
    assert http_module.total_deadline(None) is None and http_module.total_deadline(httpx.Timeout(None)) is None


async def test_a_drip_fed_answer_to_a_device_request_ends_at_the_total_deadline(tracer):
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test", client=_dripping_client())
    started = time.perf_counter()
    with pytest.raises(httpx.ReadTimeout):
        await http.get("status")
    assert time.perf_counter() - started < 2
    assert tracer.spans[0].attributes["error.type"] == "ReadTimeout"


async def test_a_drip_fed_capped_answer_ends_at_the_total_deadline(tracer):
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test", client=_dripping_client())
    with pytest.raises(httpx.ReadTimeout):
        await http.request_capped("GET", "status")


async def test_the_bounded_reader_stops_a_drip_by_itself_and_takes_a_deadline():
    async with _dripping_client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await http_module.read_capped(client, "GET", "http://192.0.2.1/x")
        async with client.stream("GET", "http://192.0.2.1/x") as response:
            with pytest.raises(httpx.ReadTimeout):
                await http_module.CappedBody(response, 1024).read(deadline=0.05)


async def test_a_quick_answer_is_not_cut_by_the_deadline(tracer):
    http = DeviceHttp("http://192.0.2.1", lambda: None, driver="test",
                      client=_client(lambda r: httpx.Response(200, text="hello")))
    assert (await http.get("status")).text == "hello"


async def test_the_spans_name_the_device_by_its_id_not_its_address(tracer):
    http = DeviceHttp("http://192.168.100.1", lambda: None, driver="test", client=_client(lambda r: httpx.Response(200)))
    http.device_id = "modem"
    await http.get("status")
    assert tracer.spans[0].attributes["device.id"] == "modem"
    assert "192.168.100.1" not in repr(tracer.spans[0].attributes)


def test_a_device_names_its_drivers_http_by_its_id():
    from pengumon.devices.device import Device
    http = DeviceHttp("http://192.168.100.1", lambda: None, driver="test")

    class Driver:
        name, host = "test", "192.168.100.1"
    driver = Driver()
    driver.http = http
    Device("modem", driver, object(), interval_seconds=10.0)
    assert http.device_id == "modem"
