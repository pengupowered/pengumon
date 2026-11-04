"""Discovery's ownership inference on recorded-style networks of several shapes (tests/discovery_fakes.py): where
the home ends, which hops are the provider's and which the internet's, with what confidence and why."""

import httpx
import pytest

from discovery_fakes import ASUS_LOGIN, XFINITY_LOGIN, by_address, discover, kinds, three_paths, trace
from pengumon.engine.discovery.engine import DiscoveryOptions
from pengumon.engine.discovery.infer import latency_step
from pengumon.engine.discovery.traces import merge_paths

SERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
NO_ASN = DiscoveryOptions(asn_lookup=False)


def roles(result):
    return [(h["address"], h["category"], h["confidence"]) for h in result.hops if h.get("group") is None]


def assert_internet_group(result):
    group = [h for h in result.hops if h.get("group") == "internet"]
    assert [(h["id"], h["name"], h["address"]) for h in group] == [
        ("cloudflare_dns", "Cloudflare", "1.1.1.1"), ("google_dns", "Google", "8.8.8.8"), ("quad9_dns", "Quad9", "9.9.9.9")]
    assert all(h["category"] == "public" for h in group)


async def test_isp_all_in_one_router_is_one_home_hop_named_by_its_page():
    """An ISP's combined modem-router: one private hop (its page says which), then the provider, then the internet."""
    traces = three_paths(shared=[("10.0.0.1", 0.7), ("203.0.113.1", 9.0), ("203.0.113.9", 10.0)],
                         to_1111=[("198.51.100.7", 12.0)])
    result = await discover(
        traces, gateway="10.0.0.1", pages={"10.0.0.1": XFINITY_LOGIN},
        names={"203.0.113.1": "po-1.cr1.city.example.net", "203.0.113.9": "be-2.core1.example.net",
               "198.51.100.7": "edge1.example.org"},
        asns={"203.0.113.1": (64500, "EXAMPLE-NET - Example Networks, LLC, ZZ"),
              "203.0.113.9": (64500, "EXAMPLE-NET - Example Networks, LLC, ZZ"),
              "198.51.100.7": (64511, "EXAMPLE-ORG - Example Org, ZZ")})
    assert roles(result) == [("10.0.0.1", "gateway", "high"), ("203.0.113.1", "isp", "high"),
                             ("203.0.113.9", "isp", "high"), ("198.51.100.7", "public", "high")]
    hops = by_address(result)
    assert hops["10.0.0.1"]["suggested_driver"] == "xfinity_xb" and hops["10.0.0.1"]["name"] == "Gateway"
    assert {"address_class", "device_page", "latency_step"} <= kinds(hops["10.0.0.1"])
    assert hops["203.0.113.1"]["name"] == "Provider hop 1 (example.net)"
    assert hops["203.0.113.9"]["name"] == "Provider hop 2 (example.net)"
    assert hops["198.51.100.7"]["name"] == "Internet hop 1 (example.org)"
    assert {"asn", "rdns", "divergence"} <= kinds(hops["203.0.113.9"])
    assert {"asn", "rdns", "divergence"} <= kinds(hops["198.51.100.7"])
    assert_internet_group(result)
    assert [h["position"] for h in result.hops] == list(range(len(result.hops)))
    assert all(h["status"] == "new" for h in result.hops)


async def test_router_and_isp_gateway_double_nat_are_two_home_hops():
    traces = three_paths(shared=[("192.168.88.1", 0.6), ("192.168.1.254", 1.0), ("203.0.113.1", 8.0),
                                 ("203.0.113.2", 9.0)])
    result = await discover(traces, gateway="192.168.88.1", pages={"192.168.88.1": ASUS_LOGIN}, options=NO_ASN)
    assert roles(result) == [("192.168.88.1", "local", "high"), ("192.168.1.254", "gateway", "high"),
                             ("203.0.113.1", "isp", "high"), ("203.0.113.2", "isp", "likely")]
    hops = by_address(result)
    assert hops["192.168.88.1"]["name"] == "Router" and hops["192.168.88.1"]["suggested_driver"] == "asuswrt"
    assert hops["192.168.1.254"]["name"] == "Gateway"
    assert any("double NAT" in e["text"] for e in hops["192.168.1.254"]["evidence"])


async def test_mesh_satellite_and_router_on_one_network_are_both_local():
    traces = three_paths(shared=[("192.168.88.2", 3.0), ("192.168.88.1", 3.4), ("203.0.113.1", 12.0)])
    result = await discover(traces, options=NO_ASN)
    assert roles(result)[:3] == [("192.168.88.2", "local", "likely"), ("192.168.88.1", "local", "high"),
                                 ("203.0.113.1", "isp", "high")]
    assert [h["name"] for h in result.hops[:2]] == ["Router", "Router 2"]


async def test_cgnat_hops_are_the_providers():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("100.64.0.1", 7.0), ("100.64.3.2", 8.0),
                                 ("203.0.113.5", 9.0)])
    result = await discover(traces, options=NO_ASN)
    assert roles(result) == [("192.168.1.1", "local", "high"), ("100.64.0.1", "isp", "high"),
                             ("100.64.3.2", "isp", "high"), ("203.0.113.5", "isp", "high")]
    first = by_address(result)["100.64.0.1"]
    assert {"address_class", "latency_step", "divergence"} <= kinds(first)


async def test_a_provider_without_names_is_told_apart_by_where_the_paths_part():
    """No rDNS, no ASN lookup: hops all three traces share are the provider's, past the split the internet's."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0), ("203.0.113.2", 9.0)],
                         to_1111=[("198.51.100.1", 11.0), ("198.51.100.2", 12.0)])
    result = await discover(traces, options=NO_ASN)
    assert roles(result) == [("192.168.1.1", "local", "high"), ("203.0.113.1", "isp", "high"),
                             ("203.0.113.2", "isp", "likely"), ("198.51.100.1", "public", "likely"),
                             ("198.51.100.2", "public", "likely")]
    assert result.dns.asked == [(q, "PTR") for q, _ in result.dns.asked]  # no ASN query at all
    assert not any("cymru" in q for q, _ in result.dns.asked)


async def test_silent_hops_are_left_out_and_unanswered_evidence_comes_from_the_neighbours():
    """Hops 2 and 4 never answer (left out: they cannot be probed). Only one trace answers, so no divergence; the
    hop with no evidence of its own between two provider hops is the provider's, the one between the provider and
    another network is unknown."""
    path = trace([("192.168.1.1", 0.6), (None, None), ("203.0.113.1", 9.0), ("203.0.113.2", 9.5),
                  ("203.0.113.3", 10.0), (None, None), ("203.0.113.4", 11.0), ("198.51.100.5", 13.0),
                  ("1.1.1.1", 14.0)])
    names = {"203.0.113.1": "a.example.net", "203.0.113.3": "b.example.net", "198.51.100.5": "c.example.org"}
    result = await discover({"1.1.1.1": path}, names=names, options=NO_ASN)
    assert roles(result) == [("192.168.1.1", "local", "high"), ("203.0.113.1", "isp", "high"),
                             ("203.0.113.2", "isp", "likely"), ("203.0.113.3", "isp", "likely"),
                             ("203.0.113.4", "isp", "unknown"), ("198.51.100.5", "public", "likely")]
    hops = by_address(result)
    assert kinds(hops["203.0.113.2"]) >= {"neighbours"}
    assert any("do not agree" in e["text"] for e in hops["203.0.113.4"]["evidence"])
    assert any("2 hops on the path did not answer" in n for n in result.notes)
    assert any("Only one trace answered" in n for n in result.notes)
    assert None not in {h["address"] for h in result.hops}


async def test_a_host_without_ipv6_gets_the_ipv4_path_only_and_a_note():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0)])
    result = await discover(traces, options=NO_ASN, ipv6="Network is unreachable")
    assert result.traced == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]  # no IPv6 trace
    assert all(":" not in h["address"] for h in result.hops)
    assert {h.get("group") for h in result.hops} == {None, "internet"}
    assert {h["family"] for h in result.hops} == {4}
    assert any("no IPv6 route to the internet (Network is unreachable)" in n for n in result.notes)


V6_TRACE = trace([("fe80::1", 0.5), ("2001:db8:1::1", 9.0), ("2001:db8:2::1", 11.0), ("2606:4700:4700::1111", 12.0)])


async def test_a_host_with_ipv6_gets_an_internet_ipv6_group_after_the_ipv4_chain():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0)])
    traces["2606:4700:4700::1111"] = V6_TRACE
    result = await discover(traces, options=NO_ASN, ipv6=None)
    v4 = [h for h in result.hops if h["family"] == 4]
    v6 = [h for h in result.hops if h["family"] == 6]
    assert [h["address"] for h in v4] == ["192.168.1.1", "203.0.113.1", "1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert result.hops[:len(v4)] == v4  # the IPv4 path stays the main chain; the IPv6 group comes after it
    assert [(h["id"], h["name"], h["address"], h["group"], h["category"], h["confidence"]) for h in v6] == [
        ("cloudflare_dns_v6", "Cloudflare (IPv6)", "2606:4700:4700::1111", "internet_v6", "public", "high"),
        ("google_dns_v6", "Google (IPv6)", "2001:4860:4860::8888", "internet_v6", "public", "high"),
        ("quad9_dns_v6", "Quad9 (IPv6)", "2620:fe::fe", "internet_v6", "public", "high")]
    assert all("This host has IPv6" in h["evidence"][0]["text"] for h in v6)
    assert "reached it in 4 hops" in v6[0]["evidence"][0]["text"]
    assert not any("IPv6" in n for n in result.notes)
    assert result.traced == ["1.1.1.1", "8.8.8.8", "9.9.9.9", "2606:4700:4700::1111"]
    # the IPv6 path's own hops are not added
    assert not any(h["address"].startswith(("fe80", "2001:db8")) for h in result.hops)


async def test_ipv6_servers_are_proposed_even_when_the_ipv6_trace_gets_nothing():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0)])
    result = await discover(traces, options=NO_ASN, ipv6=None)
    v6 = [h for h in result.hops if h["family"] == 6]
    assert len(v6) == 3 and "did not reach it" in v6[0]["evidence"][0]["text"]
    assert any("IPv6 trace got no answer" in n for n in result.notes)


async def test_the_latency_step_puts_private_hops_past_the_access_line_on_the_provider_side():
    """Satellite: the ground station has a private address, but it sits behind a 600 ms jump."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("192.168.100.1", 2.0), ("172.16.0.1", 610.0),
                                 ("203.0.113.1", 630.0)])
    result = await discover(traces, options=NO_ASN)
    assert roles(result) == [("192.168.1.1", "local", "likely"), ("192.168.100.1", "gateway", "high"),
                             ("172.16.0.1", "isp", "high"), ("203.0.113.1", "isp", "high")]
    step = [e for e in by_address(result)["172.16.0.1"]["evidence"] if e["kind"] == "latency_step"]
    assert step and "+608 ms" in step[0]["text"]


async def test_cellular_carrier_private_hops_past_the_jump_are_the_carriers():
    traces = three_paths(shared=[("192.168.12.1", 1.0), (None, None), ("10.164.8.1", 30.0), ("203.0.113.1", 45.0)])
    result = await discover(traces, options=NO_ASN)
    assert roles(result) == [("192.168.12.1", "local", "high"), ("10.164.8.1", "isp", "high"),
                             ("203.0.113.1", "isp", "high")]


async def test_a_device_page_keeps_a_hop_past_the_jump_in_the_home():
    """A slow powerline or Wi-Fi bridge between two home routers: the jump is inside the home, and the router past
    it serves its web page, so it stays at home."""
    traces = three_paths(shared=[("192.168.88.1", 0.6), ("192.168.1.1", 9.0), ("203.0.113.1", 15.0)])
    result = await discover(traces, pages={"192.168.1.1": ASUS_LOGIN}, options=NO_ASN)
    assert [r[:2] for r in roles(result)] == [("192.168.88.1", "local"), ("192.168.1.1", "gateway"),
                                              ("203.0.113.1", "isp")]


def _ssdp_answer(host):
    return (host, (f"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
                   f"LOCATION: http://{host}:5000/rootDesc.xml\r\nUSN: uuid:x\r\n\r\n").encode())


def _igd_lan(host, external):
    from test_discovery_parts import IGD_DESCRIPTION, soap_answer

    def page(request):
        if request.method == "GET" and request.url.path == "/rootDesc.xml":
            return httpx.Response(200, content=IGD_DESCRIPTION)
        if request.method == "POST":
            action = request.headers["soapaction"].strip('"').split("#")[1]
            fields = {"GetExternalIPAddress": {"NewExternalIPAddress": external},
                      "GetStatusInfo": {"NewConnectionStatus": "Connected", "NewUptime": "3600"},
                      "GetCommonLinkProperties": {"NewWANAccessType": "Cable",
                                                  "NewLayer1UpstreamMaxBitRate": "20000000",
                                                  "NewLayer1DownstreamMaxBitRate": "300000000"}}[action]
            return httpx.Response(200, content=soap_answer(action, fields))
        raise httpx.ConnectError("refused", request=request)

    return {host: page}


async def test_a_upnp_gateway_with_a_public_address_is_the_edge_of_the_home():
    """Without UPnP the private second hop would be a home gateway (double NAT); the router reports a public
    internet address, so that hop is the provider's."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("10.20.0.1", 1.0), ("203.0.113.1", 2.0)])
    plain = await discover(traces, options=NO_ASN)
    assert [r[:2] for r in roles(plain)][:2] == [("192.168.1.1", "local"), ("10.20.0.1", "gateway")]
    result = await discover(traces, options=NO_ASN, ssdp=[_ssdp_answer("192.168.1.1")],
                            pages=_igd_lan("192.168.1.1", "203.0.113.200"))
    assert [r[:2] for r in roles(result)][:3] == [("192.168.1.1", "local"), ("10.20.0.1", "isp"), ("203.0.113.1", "isp")]
    router = by_address(result)["192.168.1.1"]
    upnp = [e["text"] for e in router["evidence"] if e["kind"] == "upnp"]
    assert upnp and "public: it is the edge of your network" in upnp[0] and "line type Cable" in upnp[0]
    assert "203.0.113.200" not in str(result.hops) + str(result.notes)  # the external address is never shown


async def test_a_upnp_gateway_with_a_private_address_keeps_the_next_router_at_home():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("10.20.0.1", 9.0), ("203.0.113.1", 12.0)])
    plain = await discover(traces, options=NO_ASN)
    assert [r[:2] for r in roles(plain)][:2] == [("192.168.1.1", "local"), ("10.20.0.1", "isp")]
    result = await discover(traces, options=NO_ASN, ssdp=[_ssdp_answer("192.168.1.1")],
                            pages=_igd_lan("192.168.1.1", "10.20.0.7"))
    assert [r[:2] for r in roles(result)][:2] == [("192.168.1.1", "local"), ("10.20.0.1", "gateway")]


async def test_a_cable_modem_off_the_path_is_proposed_at_the_edge():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    modem_page = "<html><head><title>Cable Modem Status</title></head></html>"
    result = await discover(traces, options=NO_ASN, pages={"192.168.100.1": modem_page})
    assert [(h["address"], h["category"], h["name"]) for h in result.hops[:3]] == [
        ("192.168.1.1", "local", "Router"), ("192.168.100.1", "gateway", "Cable modem"),
        ("203.0.113.1", "isp", "Provider hop 1")]
    modem = result.hops[1]
    assert modem["confidence"] == "likely" and "Cable Modem Status" in modem["evidence"][0]["text"]
    assert result.lan.hosts().count("192.168.100.1") == 1  # one GET of its front page, nothing else


async def test_asn_decides_over_names_and_the_provider_continues_past_the_split():
    """Paths part inside the provider's network; its AS says the hops past the split are still the provider's."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 8.0)],
                         to_1111=[("203.0.113.50", 9.0), ("198.51.100.9", 11.0)])
    asns = {"203.0.113.1": (64500, "EXAMPLE-NET - Example Networks, ZZ"),
            "203.0.113.50": (64500, "EXAMPLE-NET - Example Networks, ZZ"),
            "198.51.100.9": (64511, "OTHER - Other Transit, ZZ")}
    result = await discover(traces, asns=asns)
    assert roles(result) == [("192.168.1.1", "local", "high"), ("203.0.113.1", "isp", "high"),
                             ("203.0.113.50", "isp", "likely"), ("198.51.100.9", "public", "high")]
    assert by_address(result)["203.0.113.50"]["name"] == "Provider hop 2 (Example Networks)"
    # only public hops were sent to Team Cymru
    origins = [q for q, t in result.dns.asked if q.endswith(".origin.asn.cymru.com")]
    assert sorted(origins) == sorted(f"{'.'.join(reversed(a.split('.')))}.origin.asn.cymru.com" for a in asns)


async def test_no_trace_answers_the_draft_still_has_the_gateway_and_the_servers():
    result = await discover({}, gateway="192.168.1.1", options=NO_ASN)
    assert [(h["address"], h["category"]) for h in result.hops] == [
        ("192.168.1.1", "local"), ("1.1.1.1", "public"), ("8.8.8.8", "public"), ("9.9.9.9", "public")]
    assert any("No trace got an answer" in n for n in result.notes)


async def test_snmp_is_asked_only_when_ticked():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    off = await discover(traces, options=NO_ASN, snmp={"192.168.1.1": "Linux router 5.15"})
    assert "SNMP" not in str(off.hops)
    on = await discover(traces, options=DiscoveryOptions(asn_lookup=False, snmp=True),
                        snmp={"192.168.1.1": "Linux router 5.15"})
    router = by_address(on)["192.168.1.1"]
    assert router["suggested_driver"] == "snmp" and "Linux router 5.15" in str(router["evidence"])


async def test_device_checks_off_fetch_nothing():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    result = await discover(traces, options=DiscoveryOptions(asn_lookup=False, device_checks=False),
                            pages={"192.168.1.1": ASUS_LOGIN, "192.168.100.1": "<title>x</title>"})
    assert result.lan.requests == []
    assert by_address(result)["192.168.1.1"]["suggested_driver"] is None


async def test_a_limited_host_gets_one_front_page_get_and_nothing_else():
    """The one-off real run limits the cable gateway: one GET of / at most, no UPnP description, no SNMP."""
    traces = three_paths(shared=[("10.0.0.1", 0.6), ("203.0.113.1", 9.0)])
    pages = _igd_lan("10.0.0.1", "203.0.113.200")
    result = await discover(traces, options=DiscoveryOptions(asn_lookup=False, snmp=True), pages=pages,
                            ssdp=[_ssdp_answer("10.0.0.1")], limited={"10.0.0.1"}, snmp={"10.0.0.1": "x"})
    assert [(r.method, r.url.path) for r in result.lan.requests if r.url.host == "10.0.0.1"] == [("GET", "/")]
    assert "SNMP" not in str(result.hops)


@pytest.mark.parametrize("rtts, expected", [
    ([0.6, 9.0, 10.0], (0, 1)),  # the access line after the router
    ([0.6, 1.0, 8.0], (1, 2)),  # double NAT: after the second home box
    ([0.6, 0.9, 1.3], None),  # nothing significant (all under 1 ms apart)
    ([3.0, 3.4, 4.1], None),  # Wi-Fi host: no hop is much slower than the one before
])
def test_latency_step_finds_the_largest_significant_jump(rtts, expected):
    path = merge_paths({"1.1.1.1": trace([(f"192.0.2.{n}", r) for n, r in enumerate(rtts, 1)])}, SERVERS)
    step = latency_step(path, len(rtts) - 1)
    assert (None if step is None else (step.before, step.after)) == expected


async def test_a_device_claiming_to_be_a_gateway_off_the_path_is_never_asked():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    pages = {**_igd_lan("192.168.1.1", "203.0.113.200"), **_igd_lan("192.168.1.66", "203.0.113.201")}
    result = await discover(traces, options=NO_ASN, ssdp=[_ssdp_answer("192.168.1.66")], pages=pages)
    assert "192.168.1.66" not in result.lan.hosts()  # no description, no SOAP
    assert not result.facts.igds


async def test_this_hosts_own_public_address_is_never_sent_to_the_asn_lookup():
    """The router's UPnP-reported external address is this home's public address: not a Team Cymru question."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.200", 9.0), ("203.0.113.1", 12.0)])
    result = await discover(traces, ssdp=[_ssdp_answer("192.168.1.1")], pages=_igd_lan("192.168.1.1", "203.0.113.200"))
    asked = [q for q, _t in result.dns.asked if q.endswith(".origin.asn.cymru.com")]
    assert "200.113.0.203.origin.asn.cymru.com" not in asked
    assert "1.113.0.203.origin.asn.cymru.com" in asked  # other public hops are still looked up


# --- gateways off the traced path (a modem in bridge mode, an ISP gateway) --------------------------------------------

OFF_PATH_EVIDENCE = "answers on your network but isn't on the traced path (a modem in bridge mode, or an ISP gateway)"


def test_the_gateway_addresses_are_private_only():
    from pengumon.engine.discovery.addresses import PRIVATE, address_class
    from pengumon.engine.discovery.engine import GATEWAY_ADDRESSES
    assert GATEWAY_ADDRESSES == ("10.0.0.1", "192.168.0.1", "192.168.1.1", "192.168.100.1")
    assert {address_class(a) for a in GATEWAY_ADDRESSES} == {PRIVATE}


async def test_a_gateway_address_off_the_path_that_answers_is_proposed_after_the_router():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    result = await discover(traces, options=NO_ASN, answers_ping={"10.0.0.1"},
                            pages={"10.0.0.1": XFINITY_LOGIN})
    assert [(h["address"], h["category"], h["name"]) for h in result.hops[:3]] == [
        ("192.168.1.1", "local", "Router"), ("10.0.0.1", "gateway", "Gateway"), ("203.0.113.1", "isp", "Provider hop 1")]
    gateway = result.hops[1]
    assert gateway["confidence"] == "likely" and gateway["status"] == "new"
    assert gateway["suggested_driver"] == "xfinity_xb"
    assert any(OFF_PATH_EVIDENCE in e["text"] for e in gateway["evidence"])
    # each address off the path got one ping and one GET of its front page, no HTTPS retry, nothing else
    assert result.pinged == ["10.0.0.1", "192.168.0.1", "192.168.100.1"]
    for address in ("10.0.0.1", "192.168.0.1", "192.168.100.1"):
        assert [(r.method, r.url.scheme, r.url.path) for r in result.lan.requests if r.url.host == address] == [
            ("GET", "http", "/")]


async def test_a_gateway_address_that_answers_ping_only_is_proposed_too():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    result = await discover(traces, options=NO_ASN, answers_ping={"192.168.100.1"})
    modem = by_address(result)["192.168.100.1"]
    assert (modem["category"], modem["name"], modem["confidence"]) == ("gateway", "Cable modem", "likely")
    assert [e["kind"] for e in modem["evidence"]] == ["off_path", "off_path"]
    assert "10.0.0.1" not in by_address(result)  # nothing answered there


async def test_gateway_addresses_on_the_path_or_on_this_hosts_own_network_are_not_asked():
    """192.168.1.1 is on the path (the router); 192.168.0.1 is this host's own network (beside the router, not in
    front of it): neither is pinged or fetched as an off-path gateway."""
    traces = three_paths(shared=[("192.168.0.7", 0.4), ("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    result = await discover(traces, options=NO_ASN, answers_ping={"192.168.0.1"}, pages={"192.168.0.1": "<p>x</p>"})
    assert result.pinged == ["10.0.0.1", "192.168.100.1"]
    assert "192.168.0.1" not in result.lan.hosts() and "192.168.0.1" not in by_address(result)
    # the router's page is fetched as a hop of the path (HTTP, then HTTPS), like the other private hop, not again
    assert result.lan.hosts().count("192.168.1.1") == result.lan.hosts().count("192.168.0.7") == 2


async def test_a_limited_host_among_the_gateway_addresses_gets_no_request_at_all():
    """A configured device (the modem) is read only by its driver, behind that driver's gate: the trace missing it
    does not make it an off-path gateway to ping or fetch."""
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    result = await discover(traces, options=NO_ASN, limited={"10.0.0.1"}, answers_ping={"10.0.0.1"},
                            pages={"10.0.0.1": XFINITY_LOGIN})
    assert "10.0.0.1" not in result.pinged
    assert "10.0.0.1" not in result.lan.hosts()
    assert "10.0.0.1" not in by_address(result)
    assert "192.168.100.1" in result.pinged  # the other gateway addresses are still asked


async def test_with_device_checks_off_only_the_background_runs_ask_the_gateway_addresses():
    traces = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0)])
    pages = {"192.168.1.1": ASUS_LOGIN, "192.168.100.1": "<title>Status</title>"}
    off = await discover(traces, options=DiscoveryOptions(asn_lookup=False, device_checks=False), pages=pages)
    assert off.lan.requests == [] and off.pinged == []
    background = await discover(traces, pages=pages, options=DiscoveryOptions(
        asn_lookup=False, device_checks=False, off_path_gateways=True))
    assert sorted(background.lan.hosts()) == ["10.0.0.1", "192.168.0.1", "192.168.100.1"]  # not the router's page
    assert background.pinged == ["10.0.0.1", "192.168.0.1", "192.168.100.1"]
    assert by_address(background)["192.168.100.1"]["category"] == "gateway"
    assert any("only the common gateway addresses" in n for n in background.notes)


async def test_the_gateway_addresses_share_the_device_check_budget():
    from pengumon.engine.discovery.engine import MAX_DEVICE_HOSTS
    home = [(f"172.16.{n}.1", 0.5 + n / 10) for n in range(1, 9)]  # eight private hops before the provider
    traces = three_paths(shared=home + [("203.0.113.1", 9.0)])
    result = await discover(traces, options=NO_ASN)
    asked = set(result.lan.hosts())
    assert len(asked) == MAX_DEVICE_HOSTS
    assert {"10.0.0.1", "192.168.0.1", "192.168.1.1", "192.168.100.1"} <= asked
