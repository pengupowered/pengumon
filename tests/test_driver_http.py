"""Driver HTTP (pengumon.drivers.http) and the Xfinity XB driver's session handling and errors."""

import logging
from contextlib import contextmanager

import httpx
import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.drivers import http as http_module
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
    assert span.attributes == {"http.request.method": "POST", "server.address": "192.168.100.1", "url.path": "/login.cgi",
                               "http.response.status_code": 200}
    record = caplog.records[0]
    context = getattr(record, "pengumon_context")
    assert record.getMessage() == "request" and context["status"] == 200 and context["bytes"] == 5
    assert context["path"] == "/login.cgi" and context["host"] == "192.168.100.1"
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
