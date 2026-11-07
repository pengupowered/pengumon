"""The upnp_igd driver against recorded UPnP answers (made-up values) through httpx.MockTransport. Nothing here
touches a real device: the unicast search runs against a socket this test opens on loopback."""

import asyncio
import socket

import httpx
import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.devices.wan import WanRates
from pengumon.drivers import DRIVERS
from pengumon.drivers.upnp_igd import UpnpIgdDriver, ask_host

HOST = "192.168.88.1"
DESCRIPTION_URL = f"http://{HOST}:5000/rootDesc.xml"
COMMON = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"
IP_CONN = "urn:schemas-upnp-org:service:WANIPConnection:1"
DESCRIPTION = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Example-Mesh router</friendlyName>
  <deviceList><device><deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
    <serviceList><service><serviceType>{COMMON}</serviceType><controlURL>/ctl/CmnIfCfg</controlURL></service></serviceList>
    <deviceList><device><deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
      <serviceList><service><serviceType>{IP_CONN}</serviceType><controlURL>/ctl/IPConn</controlURL></service></serviceList>
    </device></deviceList>
  </device></deviceList>
</device></root>""".encode()


def soap(action: str, **fields) -> httpx.Response:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return httpx.Response(200, content=(
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:{action}Response xmlns:u="urn:x">{body}</u:{action}Response></s:Body></s:Envelope>').encode())


class FakeIgd:
    """The router: counters and uptime a test sets, every request recorded as (method, path, action)."""

    def __init__(self, rx=1000, tx=500, uptime=3600, description=DESCRIPTION):
        self.rx, self.tx, self.uptime, self.description = rx, tx, uptime, description
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        action = request.headers.get("soapaction", "").strip('"').split("#")[-1] or None
        self.requests.append((request.method, request.url.path, action))
        if request.method == "GET":
            return httpx.Response(200, content=self.description)
        return {"GetTotalBytesReceived": lambda: soap(action, NewTotalBytesReceived=self.rx),
                "GetTotalBytesSent": lambda: soap(action, NewTotalBytesSent=self.tx),
                "GetStatusInfo": lambda: soap(action, NewConnectionStatus="Connected", NewUptime=self.uptime),
                }[action]()

    @property
    def actions(self):
        return [a for _, _, a in self.requests if a]


def driver(igd, url=DESCRIPTION_URL, locate=None, clock=lambda: 1_700_000_000.0):
    cfg = DeviceConfig(id="gw", driver="upnp_igd", url=url)
    device = build_device(cfg, None, client=httpx.AsyncClient(transport=httpx.MockTransport(igd)), wall_clock=clock)
    if locate:
        device.driver._locate = locate
    return device.driver


def test_registered_without_a_password_and_polled_every_30_seconds():
    assert DRIVERS["upnp_igd"] is UpnpIgdDriver
    cfg = DeviceConfig(id="gw", driver="upnp_igd", url=DESCRIPTION_URL)
    assert cfg.poll_interval_seconds == 30.0
    device = build_device(cfg, None)
    assert device.needs_sign_in is False and device.has_credentials is True
    assert device.driver.offers == {Capability.WAN_COUNTERS}


async def test_a_poll_reads_the_two_counters_and_the_uptime_and_nothing_else():
    igd = FakeIgd()
    result = await driver(igd).poll()
    wan = result.readings[Capability.WAN_COUNTERS]
    assert (wan.rx_bytes, wan.tx_bytes, wan.counter_bits, wan.read_at) == (1000, 500, 32, 1_700_000_000.0)
    assert igd.actions == ["GetTotalBytesReceived", "GetTotalBytesSent", "GetStatusInfo"]  # never the external IP
    assert [r[:2] for r in igd.requests if r[0] == "GET"] == [("GET", "/rootDesc.xml")]


async def test_the_description_is_read_once_and_again_after_a_failed_poll():
    igd = FakeIgd()
    gw = driver(igd)
    await gw.poll()
    await gw.poll()
    assert [r[0] for r in igd.requests].count("GET") == 1
    igd.rx = "oops"
    with pytest.raises(DriverError, match="without WAN byte counters"):
        await gw.poll()
    igd.rx = 2000
    await gw.poll()
    assert [r[0] for r in igd.requests].count("GET") == 2


@pytest.mark.parametrize("figure", ["1000\u00b2", "\u0661\u0660\u0660\u0660"])  # "²", Arabic-Indic digits
async def test_a_counter_that_is_not_plain_digits_fails_the_poll_and_the_description_is_read_again(figure):
    igd = FakeIgd()
    gw = driver(igd)
    await gw.poll()
    igd.rx = figure
    with pytest.raises(DriverError, match="without WAN byte counters"):
        await gw.poll()
    igd.rx, igd.uptime = 2000, "3700\u00b2"  # an uptime like it: not read, so no restart is claimed
    wan = (await gw.poll()).readings[Capability.WAN_COUNTERS]
    assert (wan.rx_bytes, wan.counter_bits) == (2000, 32)
    assert [r[0] for r in igd.requests].count("GET") == 2  # the description again after the failed poll


async def test_a_bare_host_is_asked_where_its_description_is():
    igd, asked = FakeIgd(), []

    async def locate(host):
        asked.append(host)
        return DESCRIPTION_URL

    result = await driver(igd, url=f"http://{HOST}", locate=locate).poll()
    assert asked == [HOST] and result.readings[Capability.WAN_COUNTERS].rx_bytes == 1000
    assert igd.requests[0][:2] == ("GET", "/rootDesc.xml")

    async def elsewhere(host):
        return "http://198.51.100.9:5000/rootDesc.xml"  # an answer that points off the configured host

    for answer in (elsewhere, lambda host: asyncio.sleep(0, result=None)):
        with pytest.raises(DriverError, match="did not tell where its UPnP description is") as err:
            await driver(FakeIgd(), url=f"http://{HOST}", locate=answer).poll()
        assert err.value.kind == "unsupported"


async def test_the_unicast_search_takes_an_answer_from_the_asked_host_only():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    port = server.getsockname()[1]
    loop = asyncio.get_running_loop()

    async def answer():
        data, addr = await loop.sock_recvfrom(server, 2048)
        assert b"M-SEARCH" in data and b"InternetGatewayDevice" in data
        reply = (b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
                 b"LOCATION: http://127.0.0.1:5000/rootDesc.xml\r\n\r\n")
        await loop.sock_sendto(server, reply, addr)

    task = asyncio.create_task(answer())
    try:
        assert await ask_host("127.0.0.1", wait=2.0, port=port) == "http://127.0.0.1:5000/rootDesc.xml"
    finally:
        await task
        server.close()
    assert await ask_host("127.0.0.1", wait=0.1, port=port) is None  # nobody listens: no answer, no error


@pytest.mark.parametrize("what", ["off-host control URL", "redirect", "doctype", "not an IGD"])
async def test_only_the_configured_host_is_contacted_and_unsafe_answers_are_refused(what):
    igd = FakeIgd()
    expected = "UPnP internet gateway description"
    if what == "off-host control URL":
        igd.description = DESCRIPTION.replace(b"/ctl/CmnIfCfg", b"http://198.51.100.66/ctl/CmnIfCfg")
        expected = "offers no WAN traffic counters"
    elif what == "doctype":
        igd.description = b'<?xml version="1.0"?><!DOCTYPE root [<!ENTITY a "x">]>' + DESCRIPTION.split(b"?>", 1)[1]
    elif what == "not an IGD":
        igd.description = b"<root><device><deviceType>urn:x:device:Printer:1</deviceType></device></root>"
    gw = driver(igd)
    if what == "redirect":
        gw = driver(lambda request: httpx.Response(302, headers={"location": "http://198.51.100.66/x"}))
        expected = "redirect"
    with pytest.raises(DriverError, match=expected):
        await gw.poll()
    assert all(r[0] in ("GET", "POST") and r[1] != "/ctl/CmnIfCfg" for r in igd.requests)


async def test_a_32_bit_counter_that_goes_back_is_a_wrap_but_a_restart_is_not():
    igd = FakeIgd(rx=2**32 - 1_000_000, tx=0, uptime=500)
    now = [1000.0]
    gw, rates = driver(igd, clock=lambda: now[0]), WanRates()

    async def rate(ts):
        now[0] = ts
        return rates.update(ts, (await gw.poll()).readings[Capability.WAN_COUNTERS])

    assert await rate(1000.0) == (None, None)
    igd.rx, igd.uptime = 4_000_000, 530  # wrapped: 1 MB to the top, 4 MB past it
    assert await rate(1030.0) == (pytest.approx(5_000_000 * 8 / 30 / 1e6, abs=0.01), 0.0)
    igd.rx, igd.uptime = 12_000, 20  # uptime went back: the router restarted, its counters started again
    assert await rate(1060.0) == (None, None) and rates.resets == 1


async def test_a_router_without_uptime_has_a_wrap_assumed_and_wider_counters_are_read_as_wider():
    igd = FakeIgd(rx=2**32 - 10, tx=0, description=DESCRIPTION.replace(IP_CONN.encode(), b"urn:x:service:Other:1"))
    gw = driver(igd)
    first = (await gw.poll()).readings[Capability.WAN_COUNTERS]
    assert first.counter_bits == 32 and "GetStatusInfo" not in igd.actions
    igd.rx = 6 * 2**32  # no 32-bit counter holds this
    wide = (await gw.poll()).readings[Capability.WAN_COUNTERS]
    assert (wide.rx_bytes, wide.counter_bits) == (6 * 2**32, None)
