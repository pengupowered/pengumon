"""The parts of discovery on their own: address classes, the registrable-domain heuristic, Team Cymru answers,
the trace merge and divergence point, the web-page check (fingerprints, redirects, size caps) and UPnP (SSDP answers,
descriptions, SOAP, hostile XML)."""

import socket
import asyncio

import httpx
import pytest

from discovery_fakes import ASUS_FRONT, ASUS_LOGIN, LUCI_INDEX, XFINITY_LOGIN, FakeDns, three_paths, trace
from icmp_fakes import FakeBackend, FakePath
from pengumon.drivers import upnp_wire
from pengumon.engine.discovery import devices, upnp
from pengumon.engine.discovery.addresses import address_class, registrable_domain, same_subnet
from pengumon.engine.discovery.lookups import (
    Lookups, origin_name, parse_as_name_txt, parse_origin_txt, short_operator,
)
from pengumon.engine.discovery.traces import merge_paths, run_traces
from pengumon.engine.mtr import PROBES_PER_HOP, MtrEngine

SERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


# --- addresses ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("address, cls", [
    ("192.168.1.1", "private"), ("10.1.2.3", "private"), ("172.31.0.1", "private"), ("172.32.0.1", "public"),
    ("100.64.0.1", "cgnat"), ("100.127.255.254", "cgnat"), ("100.128.0.1", "public"), ("169.254.1.1", "link_local"),
    ("127.0.0.1", "loopback"), ("192.0.0.1", "special"), ("224.0.0.1", "special"), ("0.1.2.3", "special"),
    ("203.0.113.9", "public"), ("198.51.100.1", "public"), ("1.1.1.1", "public"),
    ("fd00::1", "private"), ("fe80::1", "link_local"), ("2001:db8::1", "public"), ("2606:4700::1111", "public"),
    ("::ffff:192.168.1.1", "special"), ("router.lan", None), ("", None),
])
def test_address_class(address, cls):
    assert address_class(address) == cls


def test_same_subnet():
    assert same_subnet("192.168.88.1", "192.168.88.2")
    assert not same_subnet("192.168.88.1", "192.168.1.254")
    assert not same_subnet("192.168.1.1", "fd00::1") and not same_subnet(None, "192.168.1.1")


@pytest.mark.parametrize("name, domain", [
    ("be-1.core1.city.example.net", "example.net"), ("Example.NET.", "example.net"), ("example.net", "example.net"),
    ("ae1.cr1.lon.example.co.uk", "example.co.uk"), ("host.example.com.au", "example.com.au"),
    ("r1.example.ne.jp", "example.ne.jp"), ("co.uk", "co.uk"), ("a.example.de", "example.de"),
    ("router", None), ("192.0.2.1", None), ("1.2.0.192.in-addr.arpa", None), ("bad_label.example.net", None),
    ("", None), (None, None), ("-x.example.net", None), ("a\n.example.net", None),
])
def test_registrable_domain(name, domain):
    assert registrable_domain(name) == domain


# --- Team Cymru -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, asn", [
    ("64500 | 203.0.113.0/24 | ZZ | arin | 2001-01-01", 64500),
    ('"64500 | 203.0.113.0/24 | ZZ | arin | 2001-01-01"', 64500),
    ("64500 64501 | 203.0.113.0/24 | ZZ | arin | 2001-01-01", 64500),  # announced by two: the first
    ("4294967295 | 203.0.113.0/24 | ZZ | arin |", 4294967295),
    ("", None), ("hello", None), ("64500", None), ("AS64500 | 203.0.113.0/24 | ZZ", None),
    ("0 | 203.0.113.0/24 | ZZ | arin |", None), ("4294967296 | 203.0.113.0/24 | ZZ | arin |", None),
    ("99999999999 | x | y", None), ("-1 | x | y", None), (" | 203.0.113.0/24 | ZZ", None),
    ("64500 | " + "x" * 600, None), (None, None), (b"64500 | x | y", None),
])
def test_cymru_origin_answers(text, asn):
    assert parse_origin_txt(text) == asn


def test_cymru_as_name_answers():
    good = "64500 | ZZ | arin | 2001-01-01 | EXAMPLE-NET - Example Networks, LLC, ZZ"
    assert parse_as_name_txt(good, 64500) == "EXAMPLE-NET - Example Networks, LLC, ZZ"
    assert parse_as_name_txt(good, 64501) is None  # an answer about another AS
    assert parse_as_name_txt("64500 | ZZ | arin", 64500) is None
    assert parse_as_name_txt("64500 | ZZ | arin | 2001 | \x00\x01", 64500) is None
    assert parse_as_name_txt("x" * 600, 64500) is None
    assert short_operator("EXAMPLE-NET - Example Networks, LLC, ZZ") == "Example Networks"
    assert short_operator("EXAMPLE-NET, ZZ") == "EXAMPLE-NET"
    assert short_operator(None) is None


def test_origin_query_names_only_for_public_addresses():
    assert origin_name("203.0.113.9") == "9.113.0.203.origin.asn.cymru.com"
    assert origin_name("2001:db8::1").endswith(".8.b.d.0.1.0.0.2.origin6.asn.cymru.com")
    for address in ("192.168.1.1", "10.0.0.1", "100.64.0.1", "169.254.0.1", "127.0.0.1", "192.0.0.1", "fd00::1", "x"):
        assert origin_name(address) is None


async def test_asn_lookups_never_send_a_private_address_and_ignore_bad_answers():
    dns = FakeDns(asns={"203.0.113.1": (64500, "EXAMPLE-NET - Example Networks, ZZ")},
                  raw={"2.113.0.203.origin.asn.cymru.com": ["not | an answer"],
                       "3.113.0.203.origin.asn.cymru.com": ["0 | 203.0.113.0/24 | ZZ"]})
    found = await Lookups(dns).asns(["192.168.1.1", "100.64.0.1", "203.0.113.1", "203.0.113.2", "203.0.113.3"])
    assert {a: (i.number, i.operator) for a, i in found.items()} == {"203.0.113.1": (64500, "Example Networks")}
    asked = [q for q, _ in dns.asked]
    assert not any(q.startswith(("1.1.168.192", "1.0.64.100")) for q in asked)
    assert "AS64500.asn.cymru.com" in asked and len([q for q in asked if q.startswith("AS")]) == 1


async def test_a_failing_resolver_is_no_answer():
    async def broken(qname, rdtype):
        raise RuntimeError("boom")

    lookups = Lookups(broken)
    assert await lookups.reverse_names(["203.0.113.1"]) == {}
    assert await lookups.asns(["203.0.113.1"]) == {}


async def test_reverse_names_are_cleaned_and_bounded():
    dns = FakeDns(names={"203.0.113.1": "Be-1.Core.Example.NET", "203.0.113.2": "bad name"})
    in_flight, peak = 0, 0

    async def slow(qname, rdtype):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return await dns(qname, rdtype)

    names = await Lookups(slow, concurrency=3).reverse_names([f"203.0.113.{n}" for n in range(1, 20)])
    assert names == {"203.0.113.1": "be-1.core.example.net"}
    assert peak == 3


# --- traces ---------------------------------------------------------------------------------------------------------

def test_merge_keeps_the_path_to_1111_and_finds_where_the_paths_part():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0), ("203.0.113.2", 9.0)],
                         to_1111=[("198.51.100.1", 11.0)])
    path = merge_paths(traces, SERVERS)
    assert [h.address for h in path.hops] == ["192.168.1.1", "203.0.113.1", "203.0.113.2", "198.51.100.1"]
    assert path.divergence == 2 and path.traced_to == "1.1.1.1"
    assert path.reached == {s: True for s in SERVERS}
    assert path.hops[0].paths == tuple(SERVERS) and path.hops[3].paths == ("1.1.1.1",)


def test_a_load_balanced_hop_does_not_move_the_divergence_point_early():
    """At TTL 3 the trace to 8.8.8.8 met another router of the provider; the paths still share TTL 4."""
    t1 = trace([("192.168.1.1", 1), ("203.0.113.1", 8), ("203.0.113.2", 9), ("203.0.113.4", 10),
                ("198.51.100.1", 11), ("1.1.1.1", 12)])
    t8 = trace([("192.168.1.1", 1), ("203.0.113.1", 8), ("203.0.113.3", 9), ("203.0.113.4", 10),
                ("198.51.100.8", 11), ("8.8.8.8", 12)])
    t9 = trace([("192.168.1.1", 1), ("203.0.113.1", 8), ("203.0.113.2", 9), ("203.0.113.4", 10), ("9.9.9.9", 11)])
    path = merge_paths({"1.1.1.1": t1, "8.8.8.8": t8, "9.9.9.9": t9}, SERVERS)
    assert path.divergence == 3


def test_merge_falls_back_to_the_next_server_and_says_what_answered():
    t8 = trace([("192.168.1.1", 1), ("203.0.113.1", 8), (None, None)])
    path = merge_paths({"1.1.1.1": [], "8.8.8.8": t8, "9.9.9.9": []}, SERVERS)
    assert path.traced_to == "8.8.8.8" and path.divergence is None
    assert path.reached == {s: False for s in SERVERS} and path.answered == ["8.8.8.8"]
    assert [h.address for h in path.hops] == ["192.168.1.1", "203.0.113.1", None]


def test_paths_that_share_nothing_part_before_the_first_hop():
    t1 = trace([("192.0.2.1", 1), ("1.1.1.1", 2)])
    t8 = trace([("198.51.100.1", 1), ("8.8.8.8", 2)])
    assert merge_paths({"1.1.1.1": t1, "8.8.8.8": t8}, SERVERS).divergence == -1
    assert merge_paths({}, SERVERS).hops == []


async def test_the_three_traces_run_one_after_the_other_through_the_icmp_backend():
    backend = FakeBackend(path=FakePath(["192.168.1.1", "203.0.113.1"]), delay=0.01)
    progress = []
    traces = await run_traces(lambda host: MtrEngine(host, max_hops=4, timeout=0.05, backend=backend).run_trace(),
                              SERVERS, progress.append)
    assert [h.ip for h in traces["8.8.8.8"]] == ["192.168.1.1", "203.0.113.1", "8.8.8.8"]
    sent = [address for address, _ttl, _timeout in backend.sent]
    assert sent == ["1.1.1.1"] * 4 * PROBES_PER_HOP + ["8.8.8.8"] * 4 * PROBES_PER_HOP + ["9.9.9.9"] * 4 * PROBES_PER_HOP
    assert backend.max_in_flight <= 4 * PROBES_PER_HOP  # never two traces at once
    assert progress == ["Tracing the path to 1.1.1.1 (1 of 3)", "Tracing the path to 8.8.8.8 (2 of 3)",
                        "Tracing the path to 9.9.9.9 (3 of 3)"]


# --- web pages ------------------------------------------------------------------------------------------------------

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.parametrize("body, driver", [
    (XFINITY_LOGIN, "xfinity_xb"), ("<html><div id='home_loggedout'></div></html>", "xfinity_xb"),
    (ASUS_LOGIN, "asuswrt"), (ASUS_FRONT, "asuswrt"), (LUCI_INDEX, "openwrt"),
    ("<html><script>location.href='/Main_Login.php';</script></html>", None),
    ("<html><head><title>OpenWrt - LuCI</title></head></html>", "openwrt"),
    ("<html><head><title>Welcome</title></head></html>", None),
])
async def test_fingerprints(body, driver):
    async with _client(lambda r: httpx.Response(200, text=body)) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert (page.fingerprint.driver if page.fingerprint else None) == driver


async def test_a_page_title_and_server_header_are_kept_short_and_clean():
    body = "<html><head><title>\n  My &amp; Router\x07 </title></head></html>"
    async with _client(lambda r: httpx.Response(200, text=body, headers={"server": "httpd/2.0\x00"})) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert (page.title, page.server, page.status) == ("My & Router", "httpd/2.0", 200)
    assert "titled “My & Router”" in page.describe()


async def test_a_same_host_redirect_is_followed_to_the_login_page():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/Main_Login.asp"})
        return httpx.Response(200, text=ASUS_LOGIN)

    async with _client(handler) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert page.fingerprint.driver == "asuswrt"
    assert seen == ["http://192.168.1.1/", "http://192.168.1.1/Main_Login.asp"]


async def test_an_asus_front_page_that_redirects_to_its_login_page_is_recognised_without_following():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"location": "/Main_Login.asp"})

    async with _client(handler) as client:
        page = await devices.fetch_page("192.168.1.1", client, budget=1, fallback_https=False)
    assert seen == ["/"] and page.fingerprint.driver == "asuswrt"


async def test_a_redirect_to_another_host_is_not_followed():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "192.168.1.1":
            return httpx.Response(302, headers={"location": "http://login.example.com/portal"})
        return httpx.Response(200, text=ASUS_LOGIN)

    async with _client(handler) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert seen == ["192.168.1.1"]
    assert page.off_host_redirect and page.fingerprint is None and page.status == 302
    assert "redirects to another host (not followed)" in page.describe()


async def test_redirects_stop_after_a_few_and_a_redirect_to_https_on_the_same_host_is_fine():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": "https://192.168.1.1/"})
        return httpx.Response(302, headers={"location": f"/again{len(seen)}"})

    async with _client(handler) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert len(seen) == 1 + devices.MAX_REDIRECTS and page.status == 302


async def test_https_is_tried_when_nothing_listens_on_http_and_nothing_answering_is_none():
    def handler(request):
        if request.url.scheme == "http":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text="<title>Secure</title>")

    async with _client(handler) as client:
        page = await devices.fetch_page("192.168.1.1", client)
        assert (page.scheme, page.title) == ("https", "Secure")
        assert await devices.fetch_page("192.168.1.1", client, budget=1, fallback_https=False) is None

    def nothing(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    async with _client(nothing) as client:
        assert await devices.fetch_page("192.168.1.1", client) is None


async def test_a_budget_of_one_sends_one_request():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"location": "/login"})

    async with _client(handler) as client:
        page = await devices.fetch_page("10.0.0.1", client, budget=1, fallback_https=False)
    assert seen == ["/"] and page.status == 302


async def test_a_huge_page_is_read_only_up_to_the_cap():
    body = b"<html>" + b"x" * (devices.PAGE_MAX * 4) + b"<title>late</title>"
    async with _client(lambda r: httpx.Response(200, content=body)) as client:
        page = await devices.fetch_page("192.168.1.1", client)
    assert page.title is None


async def test_the_discovery_client_never_follows_redirects_or_reads_proxy_settings():
    async with devices.new_client() as client:
        assert client.follow_redirects is False and client.trust_env is False


# --- UPnP -----------------------------------------------------------------------------------------------------------

IGD_DESCRIPTION = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>Example Router</friendlyName>
    <manufacturer>Example Corp</manufacturer>
    <modelName>XR-1</modelName>
    <deviceList><device>
      <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
      <serviceList><service>
        <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
        <controlURL>/ctl/CmnIfCfg</controlURL>
      </service></serviceList>
      <deviceList><device>
        <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
        <serviceList><service>
          <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
          <controlURL>/ctl/IPConn</controlURL>
        </service>
        <service>
          <serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
          <controlURL>/ctl/L3F</controlURL>
        </service></serviceList>
      </device></deviceList>
    </device></deviceList>
  </device>
</root>"""


def soap_answer(action, fields):
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">{inner}'
            f'</u:{action}Response></s:Body></s:Envelope>').encode()


def ssdp(host, location=None, st="urn:schemas-upnp-org:device:InternetGatewayDevice:1", status="200 OK"):
    location = location or f"http://{host}:5000/rootDesc.xml"
    return f"HTTP/1.1 {status}\r\nCACHE-CONTROL: max-age=120\r\nST: {st}\r\nLOCATION: {location}\r\n\r\n".encode()


def test_ssdp_answers_count_only_with_a_description_on_the_answering_private_host():
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1")) == "http://192.168.1.1:5000/rootDesc.xml"
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", "http://192.168.1.99/d.xml")) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", "http://example.com/d.xml")) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", "https://192.168.1.1/d.xml")) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", "file:///etc/passwd")) is None
    assert upnp.parse_ssdp_answer("203.0.113.1", ssdp("203.0.113.1")) is None  # not from the LAN
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", st="upnp:rootdevice")) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1", status="404 Not Found")) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", ssdp("192.168.1.1") + b"x" * 4096) is None
    assert upnp.parse_ssdp_answer("192.168.1.1", b"\xff\xfe garbage") is None


def test_an_igd_description_lists_its_wan_services_from_the_whole_device_tree():
    d = upnp_wire.parse_description(IGD_DESCRIPTION)
    assert (d.friendly_name, d.manufacturer, d.model) == ("Example Router", "Example Corp", "XR-1")
    assert d.services == [("urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1", "/ctl/CmnIfCfg"),
                          ("urn:schemas-upnp-org:service:WANIPConnection:1", "/ctl/IPConn")]
    other = IGD_DESCRIPTION.replace(b"InternetGatewayDevice:1</deviceType>\n    <friendlyName>",
                                    b"MediaServer:1</deviceType>\n    <friendlyName>")
    assert upnp_wire.parse_description(other) is None


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<root><device><deviceType>&lol3;</deviceType></device></root>"""
EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<root><device><deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
<friendlyName>&xxe;</friendlyName></device></root>"""


@pytest.mark.parametrize("body", [
    BILLION_LAUGHS, EXTERNAL_ENTITY, b"<!doctype root><root/>", b"<root><device>", b"not xml at all",
    b"<root>" + b"<a>x</a>" * (upnp_wire.XML_MAX // 8) + b"</root>", "<root/>",
], ids=["billion-laughs", "external-entity", "doctype", "unclosed", "not-xml", "oversized", "str-body"])
def test_hostile_or_oversized_xml_is_refused(body):
    assert upnp_wire.safe_xml(body) is None
    assert upnp_wire.parse_description(body if isinstance(body, bytes) else b"") is None


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8"])
def test_a_doctype_is_refused_in_any_encoding(encoding):
    """The old guard looked for the bytes of "<!DOCTYPE" and missed a UTF-16 document, which the parser reads."""
    text = BILLION_LAUGHS.decode("ascii")
    if encoding != "utf-8":
        text = text.replace('<?xml version="1.0"?>', f'<?xml version="1.0" encoding="{encoding}"?>')
    body = text.encode(encoding)
    assert upnp_wire.safe_xml(body) is None
    assert upnp_wire.parse_soap_answer(body, "GetExternalIPAddress") == {}
    assert upnp_wire.parse_description(body) is None
    assert upnp_wire.safe_xml("<root/>".encode("utf-16")) is not None  # a UTF-16 document without a DTD is still fine


def test_soap_answers_keep_only_known_fields():
    body = soap_answer("GetExternalIPAddress", {"NewExternalIPAddress": "203.0.113.200", "Evil": "x"})
    assert upnp_wire.parse_soap_answer(body, "GetExternalIPAddress") == {"NewExternalIPAddress": "203.0.113.200"}
    fault = (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
             b'<faultstring>UPnPError</faultstring></s:Fault></s:Body></s:Envelope>')
    assert upnp_wire.parse_soap_answer(fault, "GetExternalIPAddress") == {}
    assert upnp_wire.parse_soap_answer(BILLION_LAUGHS, "GetExternalIPAddress") == {}


def _igd_handler(seen, description=IGD_DESCRIPTION, external="203.0.113.200"):
    def handler(request):
        seen.append((request.method, str(request.url), request.headers.get("soapaction")))
        if request.method == "GET":
            return httpx.Response(200, content=description)
        action = request.headers["soapaction"].strip('"').split("#")[1]
        fields = {"GetExternalIPAddress": {"NewExternalIPAddress": external},
                  "GetStatusInfo": {"NewConnectionStatus": "Connected", "NewUptime": "86400"},
                  "GetCommonLinkProperties": {"NewWANAccessType": "DSL", "NewLayer1UpstreamMaxBitRate": "10000000",
                                              "NewLayer1DownstreamMaxBitRate": "100000000",
                                              "NewPhysicalLinkStatus": "Up"}}[action]
        return httpx.Response(200, content=soap_answer(action, fields))
    return handler


async def test_an_igd_is_asked_only_the_read_only_actions_of_the_services_it_lists():
    seen = []
    async with _client(_igd_handler(seen)) as client:
        igd = await upnp.read_igd("http://192.168.1.1:5000/rootDesc.xml", client, "192.168.1.1")
    assert (igd.external_class, igd.connection_status, igd.uptime_s, igd.access_type, igd.up_bps, igd.down_bps,
            igd.link_status) == ("public", "Connected", 86400, "DSL", 10_000_000, 100_000_000, "Up")
    assert [(m, u.split(":5000")[1], a) for m, u, a in seen] == [
        ("GET", "/rootDesc.xml", None),
        ("POST", "/ctl/CmnIfCfg", '"urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1#GetCommonLinkProperties"'),
        ("POST", "/ctl/IPConn", '"urn:schemas-upnp-org:service:WANIPConnection:1#GetExternalIPAddress"'),
        ("POST", "/ctl/IPConn", '"urn:schemas-upnp-org:service:WANIPConnection:1#GetStatusInfo"'),
    ]  # nothing asked of Layer3Forwarding, nothing that changes the router


async def test_a_control_url_on_another_host_is_never_called():
    seen = []
    description = IGD_DESCRIPTION.replace(b"<controlURL>/ctl/IPConn</controlURL>",
                                          b"<controlURL>http://198.51.100.66/ctl/IPConn</controlURL>")
    async with _client(_igd_handler(seen, description)) as client:
        igd = await upnp.read_igd("http://192.168.1.1:5000/rootDesc.xml", client, "192.168.1.1")
    assert igd.external_ip is None
    assert all("198.51.100.66" not in url for _m, url, _a in seen)


async def test_an_oversized_description_is_not_parsed():
    seen = []
    huge = IGD_DESCRIPTION.replace(b"<friendlyName>", b"<!--" + b"x" * upnp_wire.XML_MAX + b"--><friendlyName>")
    async with _client(_igd_handler(seen, huge)) as client:
        assert await upnp.read_igd("http://192.168.1.1:5000/rootDesc.xml", client, "192.168.1.1") is None
    assert len(seen) == 1  # no SOAP call either


async def test_discover_igds_skips_hosts_it_must_leave_alone_and_answers_from_elsewhere():
    seen = []

    async def search():
        return [("192.168.1.1", ssdp("192.168.1.1")), ("192.168.1.1", ssdp("192.168.1.1")),
                ("10.0.0.1", ssdp("10.0.0.1")), ("192.168.1.7", ssdp("192.168.1.7", "http://203.0.113.5/x.xml"))]

    async with _client(_igd_handler(seen)) as client:
        found = await upnp.discover_igds(client, search, skip=lambda a: a == "10.0.0.1")
    assert [i.address for i in found] == ["192.168.1.1"]
    assert {httpx.URL(u).host for _m, u, _a in seen} == {"192.168.1.1"}


async def test_a_gateway_whose_answers_break_the_reading_does_not_fail_the_others():
    seen = []
    good = _igd_handler(seen)

    def handler(request):
        if request.url.host == "192.168.1.66":
            raise RuntimeError("an answer nothing expected")  # not an httpx error: read_igd does not catch it
        return good(request)

    async def search():
        return [("192.168.1.66", ssdp("192.168.1.66")), ("192.168.1.1", ssdp("192.168.1.1"))]

    async with _client(handler) as client:
        found = await upnp.discover_igds(client, search)
    assert [i.address for i in found] == ["192.168.1.1"]


@pytest.mark.parametrize("figure", ["86400\u00b2", "\u0668\u0666\u0664\u0660\u0660"])  # "²", Arabic-Indic digits
async def test_a_figure_that_is_not_plain_digits_is_left_out(figure):
    seen = []
    plain = _igd_handler(seen)

    def handler(request):
        response = plain(request)
        if request.method == "POST":
            return httpx.Response(200, content=response.content.replace(b">86400<", f">{figure}<".encode()))
        return response

    async with _client(handler) as client:
        igd = await upnp.read_igd("http://192.168.1.1:5000/rootDesc.xml", client, "192.168.1.1")
    assert igd.uptime_s is None and igd.down_bps == 100_000_000


async def test_discover_igds_asks_only_gateways_on_the_traced_path():
    seen = []

    async def search():
        return [("192.168.1.1", ssdp("192.168.1.1")), ("192.168.1.66", ssdp("192.168.1.66"))]

    async with _client(_igd_handler(seen)) as client:
        found = await upnp.discover_igds(client, search, allowed={"192.168.1.1"})
        assert [i.address for i in found] == ["192.168.1.1"]
        assert {httpx.URL(u).host for _m, u, _a in seen} == {"192.168.1.1"}  # the impostor was never fetched
        seen.clear()
        assert await upnp.discover_igds(client, search, allowed=set()) == []
    assert seen == []


def test_the_msearch_asks_for_an_internet_gateway():
    text = upnp_wire.msearch(upnp_wire.IGD_TYPES[0]).decode()
    assert text.startswith("M-SEARCH * HTTP/1.1\r\n") and "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1" in text
    assert 'MAN: "ssdp:discover"' in text and text.endswith("\r\n\r\n")


async def test_the_ssdp_search_listens_only_on_the_lan_interface_it_sends_from(monkeypatch):
    # CodeQL "binding a socket to all network interfaces": the answers are awaited on the address the multicast goes
    # out from, not on 0.0.0.0 (on a host with a public interface nothing should listen there).
    from pengumon.engine.discovery import upnp
    bound = []

    class Recording(socket.socket):
        def bind(self, address):
            bound.append(address)
            return super().bind(("127.0.0.1", 0))

        def sendto(self, data, address):
            return len(data)

    monkeypatch.setattr(upnp, "lan_source_address", lambda: "127.0.0.1")
    monkeypatch.setattr(upnp.socket, "socket", Recording)
    await upnp.ssdp_search(wait=0.05)
    assert bound and bound[0][0] == "127.0.0.1"


def test_the_lan_source_address_is_a_real_address_or_none():
    from pengumon.engine.discovery import upnp
    address = upnp.lan_source_address()
    assert address is None or (address != "0.0.0.0" and socket.inet_aton(address))


async def test_without_a_lan_address_there_is_no_ssdp_search_and_nothing_listens(monkeypatch):
    from pengumon.engine.discovery import upnp
    opened = []
    monkeypatch.setattr(upnp, "lan_source_address", lambda: None)
    monkeypatch.setattr(upnp.socket, "socket", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("socket opened")))
    assert await upnp.ssdp_search(wait=0.01) == []
    assert opened == []
