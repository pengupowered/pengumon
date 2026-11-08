"""The SNMP driver and its BER codec: a fake v2c agent on a local UDP port (the real client, socket and all), and the
decoder against random, truncated and corrupted datagrams. Every value is made up."""

import asyncio
import random
import time

import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.drivers import DRIVERS
from pengumon.drivers import snmp_ber as ber
from pengumon.drivers.snmp import (
    HR_PROCESSOR_LOAD, HR_STORAGE_DESCR, HR_STORAGE_RAM, HR_STORAGE_SIZE, HR_STORAGE_TYPE, HR_STORAGE_UNITS,
    HR_STORAGE_USED, IF_DESCR, IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS, IF_IN_OCTETS, IF_NAME, IF_OUT_OCTETS,
    INET_CIDR_ROUTE_DEFAULT_IFINDEX, IP_ROUTE_DEFAULT_IFINDEX, SYS_DESCR, SYS_UPTIME, UCD_CPU_BUSY, UCD_CPU_IDLE,
    SnmpDriver, match_interface, model_of,
)
from pengumon.secrets import CredentialSources, password_env_var

from device_helpers import Clock

COMMUNITY = "s3cret-ro"
HR_STORAGE_OTHER = (1, 3, 6, 1, 2, 1, 25, 2, 1, 1)


# --- a fake agent ---------------------------------------------------------------------------------------------------

def encode_value(tag, value) -> bytes:
    if tag == ber.INTEGER:
        return ber._int(value)
    if tag in (ber.COUNTER32, ber.GAUGE32, ber.TIMETICKS, ber.COUNTER64):
        return ber._tlv(tag, value.to_bytes(value.bit_length() // 8 + 1, "big"))
    if tag == ber.OBJECT_IDENTIFIER:
        return ber.encode_oid(value)
    if tag in (ber.OCTET_STRING, ber.IP_ADDRESS, ber.OPAQUE):
        return ber._tlv(tag, value.encode() if isinstance(value, str) else value)
    return ber._tlv(tag, b"")  # NULL and the exceptions


def encode_response(community: bytes, request_id: int, varbinds, error_status=0, error_index=0) -> bytes:
    body = b"".join(ber._tlv(ber.SEQUENCE, ber.encode_oid(oid) + encode_value(tag, v)) for oid, (tag, v) in varbinds)
    pdu = ber._int(request_id) + ber._int(error_status) + ber._int(error_index) + ber._tlv(ber.SEQUENCE, body)
    return ber._tlv(ber.SEQUENCE, ber._int(1) + ber._tlv(ber.OCTET_STRING, community) + ber._tlv(ber.RESPONSE, pdu))


def decode_request(data: bytes):
    """(community, pdu tag, request id, a, b, oids) of a request the client sent."""
    start, end = ber._expect(data, 0, len(data), ber.SEQUENCE, "message")
    version, pos = ber._decode_int(data, start, end, "version")
    assert version == 1
    c_start, c_end = ber._expect(data, pos, end, ber.OCTET_STRING, "community")
    tag, p_start, p_end = ber._read(data, c_end, end)
    request_id, pos = ber._decode_int(data, p_start, p_end, "id")
    a, pos = ber._decode_int(data, pos, p_end, "a")
    b, pos = ber._decode_int(data, pos, p_end, "b")
    vb_start, vb_end = ber._expect(data, pos, p_end, ber.SEQUENCE, "varbinds")
    oids, pos = [], vb_start
    while pos < vb_end:
        s, e = ber._expect(data, pos, vb_end, ber.SEQUENCE, "varbind")
        o_start, o_end = ber._expect(data, s, e, ber.OBJECT_IDENTIFIER, "name")
        oids.append(ber.decode_oid(data[o_start:o_end]))
        pos = e
    return data[c_start:c_end], tag, request_id, a, b, oids


class FakeAgent(asyncio.DatagramProtocol):
    """A v2c agent serving `mib` (oid -> (tag, value)): GET and GETBULK, silence for a wrong community. `drop`
    requests are ignored first; `reply` (request bytes -> datagrams) replaces the answer when set."""

    def __init__(self, mib, community=COMMUNITY):
        self.mib = dict(mib)
        self.community = community.encode()
        self.drop = 0
        self.requests = []
        self.reply = None
        self.too_big_above = None  # GETBULK with more repetitions than this answers tooBig

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        community, tag, request_id, a, b, oids = decode_request(data)
        self.requests.append((tag, oids, b))
        if community != self.community:
            return
        if self.drop:
            self.drop -= 1
            return
        if self.reply is not None:
            for datagram in self.reply(data, request_id):
                self.transport.sendto(datagram, addr)
            return
        if tag == ber.GET_REQUEST:
            varbinds = [(oid, self.mib.get(oid, (ber.NO_SUCH_INSTANCE, None))) for oid in oids]
        elif tag == ber.GET_BULK_REQUEST:
            if self.too_big_above is not None and b > self.too_big_above:
                self.transport.sendto(encode_response(community, request_id, [], error_status=ber.TOO_BIG), addr)
                return
            ordered = sorted(self.mib)
            after = [oid for oid in ordered if oid > oids[0]][:b]
            varbinds = [(oid, self.mib[oid]) for oid in after] or [(oids[0], (ber.END_OF_MIB_VIEW, None))]
        else:
            raise AssertionError(f"unexpected PDU 0x{tag:02x}")
        self.transport.sendto(encode_response(community, request_id, varbinds), addr)


async def start_agent(mib, **kwargs):
    loop = asyncio.get_running_loop()
    transport, agent = await loop.create_datagram_endpoint(lambda: FakeAgent(mib, **kwargs),
                                                           local_addr=("127.0.0.1", 0))
    return agent, transport.get_extra_info("sockname")[1]


def row(column, index, tag, value):
    return column + (index,), (tag, value)


def router_mib(*, uptime=500_000, hc=True, rx=10_000_000_000, tx=2_000_000_000, route=True, processors=(10, 30),
               available=True):
    """A small router: lo (1), eth0 (2, LAN), eth1 (3, WAN, the default route), RAM 1 GiB in KiB units."""
    mib = dict([
        (SYS_DESCR, (ber.OCTET_STRING, "Example Router ER-1 fw 2.0\r\nbuild 42")),
        (SYS_UPTIME, (ber.TIMETICKS, uptime)),
        row(IF_DESCR, 1, ber.OCTET_STRING, "lo"), row(IF_DESCR, 2, ber.OCTET_STRING, "LAN port"),
        row(IF_DESCR, 3, ber.OCTET_STRING, "Internet port"),
        row(IF_IN_OCTETS, 3, ber.COUNTER32, rx % 2 ** 32), row(IF_OUT_OCTETS, 3, ber.COUNTER32, tx % 2 ** 32),
        row(HR_STORAGE_TYPE, 1, ber.OBJECT_IDENTIFIER, HR_STORAGE_RAM),
        row(HR_STORAGE_DESCR, 1, ber.OCTET_STRING, "Physical memory"),
        row(HR_STORAGE_UNITS, 1, ber.INTEGER, 1024), row(HR_STORAGE_SIZE, 1, ber.INTEGER, 1_048_576),
        row(HR_STORAGE_USED, 1, ber.INTEGER, 838_861),  # 80 %, buffers and cache included
        row(HR_STORAGE_TYPE, 6, ber.OBJECT_IDENTIFIER, HR_STORAGE_OTHER),
        row(HR_STORAGE_DESCR, 6, ber.OCTET_STRING, "Memory buffers"),
        row(HR_STORAGE_UNITS, 6, ber.INTEGER, 1024), row(HR_STORAGE_USED, 6, ber.INTEGER, 104_858),
        row(HR_STORAGE_TYPE, 7, ber.OBJECT_IDENTIFIER, HR_STORAGE_OTHER),
        row(HR_STORAGE_DESCR, 7, ber.OCTET_STRING, "Cached memory"),
        row(HR_STORAGE_UNITS, 7, ber.INTEGER, 1024), row(HR_STORAGE_USED, 7, ber.INTEGER, 209_715),
    ])
    if available:  # 70 % available: 30 % in use
        mib.update([row(HR_STORAGE_TYPE, 11, ber.OBJECT_IDENTIFIER, HR_STORAGE_OTHER),
                    row(HR_STORAGE_DESCR, 11, ber.OCTET_STRING, "Available memory"),
                    row(HR_STORAGE_UNITS, 11, ber.INTEGER, 1024), row(HR_STORAGE_SIZE, 11, ber.INTEGER, 734_003)])
    if hc:
        mib.update([row(IF_NAME, 1, ber.OCTET_STRING, "lo"), row(IF_NAME, 2, ber.OCTET_STRING, "eth0"),
                    row(IF_NAME, 3, ber.OCTET_STRING, "eth1"),
                    row(IF_HC_IN_OCTETS, 3, ber.COUNTER64, rx), row(IF_HC_OUT_OCTETS, 3, ber.COUNTER64, tx)])
    if route:
        mib[IP_ROUTE_DEFAULT_IFINDEX] = (ber.INTEGER, 3)
    for i, load in enumerate(processors):
        mib[HR_PROCESSOR_LOAD + (196608 + i,)] = (ber.INTEGER, load)
    return mib


def snmp_driver(port, community=COMMUNITY, clock=None, **options):
    options.setdefault("timeout_seconds", 0.2)
    options.setdefault("retries", 0)
    cfg = DeviceConfig(id="sw", driver="snmp", url=f"snmp://127.0.0.1:{port}", options=options)
    secrets = CredentialSources(env={password_env_var("sw"): community} if community else {}, secrets_dir=None,
                                store={})
    return build_device(cfg, None, secrets, clock=clock or Clock(), wall_clock=lambda: 1_700_000_000.0)


# --- configuration --------------------------------------------------------------------------------------------------

def test_registered_and_takes_snmp_urls_only():
    assert DRIVERS["snmp"] is SnmpDriver
    cfg = DeviceConfig(id="sw", driver="snmp", url="snmp://192.0.2.2:1161")
    assert cfg.poll_interval_seconds == 10.0
    for url, message in (("http://192.0.2.2", "url must start with snmp://"),
                         ("snmp://public@192.0.2.2", "must not contain credentials"),
                         ("snmp://192.0.2.2/x", "must not contain a path")):
        with pytest.raises(ValueError, match=message):
            DeviceConfig(id="sw", driver="snmp", url=url)
    with pytest.raises(ValueError, match="url must start with http:// or https://"):
        DeviceConfig(id="r", driver="asuswrt", url="snmp://192.0.2.2")


def test_the_community_string_is_the_password():
    device = snmp_driver(161, community=None)
    assert device.driver.missing_configuration() == "no community string (the device's password)"
    device.set_credentials("ignored", "other")
    assert device.driver.missing_configuration() is None and device.driver.snmp.community == b"other"
    assert device.driver.host == "127.0.0.1" and device.driver.snmp.port == 161


@pytest.mark.parametrize("options", [{"retries": 9}, {"timeout_seconds": 0}, {"wan_interface": ""}, {"x": 1}])
def test_options_are_checked(options):
    with pytest.raises(ValueError, match="options for driver snmp"):
        DeviceConfig(id="sw", driver="snmp", url="snmp://192.0.2.2", options=options)


# --- the driver -----------------------------------------------------------------------------------------------------

async def test_first_poll_finds_the_layout_and_later_polls_are_one_get():
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port)
    try:
        result = await device.driver.poll()
        discovery = len(agent.requests)
        wan = result.readings[Capability.WAN_COUNTERS]
        assert (wan.rx_bytes, wan.tx_bytes, wan.counter_bits) == (10_000_000_000, 2_000_000_000, None)  # 64-bit
        assert wan.read_at == 1_700_000_000.0
        system = result.readings[Capability.ROUTER_SYSTEM]
        assert (system.cpu_pct, system.mem_pct, system.client_count) == (20.0, 30.0, None)
        assert system.model == "Example Router ER-1 fw 2.0"
        assert result.missing == {}
        await device.driver.poll()
        assert len(agent.requests) == discovery + 1 and agent.requests[-1][0] == ber.GET_REQUEST
    finally:
        await device.aclose()


async def test_a_device_snapshot_takes_the_reported_model_unless_config_names_one():
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port)
    try:
        snapshot = await device.poll(force=True)
        assert snapshot.status == "healthy" and snapshot.model_name == "Example Router ER-1 fw 2.0"
        assert snapshot.mem_usage_pct == 30.0 and snapshot.wan_rx_bytes == 10_000_000_000
    finally:
        await device.aclose()
    cfg = DeviceConfig(id="sw", driver="snmp", url=f"snmp://127.0.0.1:{port}", model="Core switch")
    device = build_device(cfg, None, CredentialSources(env={password_env_var("sw"): COMMUNITY}, secrets_dir=None,
                                                       store={}))
    try:
        assert (await device.poll(force=True)).model_name == "Core switch"
    finally:
        await device.aclose()


async def test_the_speed_test_reads_uptime_and_the_wan_counters_in_one_get_with_the_polls_layout():
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port)
    try:
        assert await device.driver.read_wan_counters() is None and agent.requests == []   # no layout yet
        await device.driver.poll()
        before = len(agent.requests)
        counters = await device.driver.read_wan_counters()
        assert (counters.rx_bytes, counters.tx_bytes, counters.counter_bits) == (10_000_000_000, 2_000_000_000, None)
        assert len(agent.requests) == before + 1
        tag, oids, _ = agent.requests[-1]
        assert tag == ber.GET_REQUEST and oids == [SYS_UPTIME, IF_HC_IN_OCTETS + (3,), IF_HC_OUT_OCTETS + (3,),
                                                    IF_IN_OCTETS + (3,), IF_OUT_OCTETS + (3,)]
        agent.mib[SYS_UPTIME] = (ber.TIMETICKS, 100)                  # the agent restarted: not taken as a wrap ...
        del agent.mib[IF_HC_IN_OCTETS + (3,)]
        agent.mib[IF_IN_OCTETS + (3,)] = (ber.COUNTER32, 7)
        counters = await device.driver.read_wan_counters()
        assert (counters.rx_bytes, counters.counter_bits) == (7, None)
        assert device.driver._uptime == 500_000                        # ... and the polls' own check is left alone
    finally:
        await device.aclose()


async def test_32_bit_counters_wrap_and_an_agent_restart_starts_a_new_baseline():
    mib = router_mib(hc=False, rx=2 ** 32 + 5)
    agent, port = await start_agent(mib)
    device = snmp_driver(port)
    try:
        wan = (await device.driver.poll()).readings[Capability.WAN_COUNTERS]
        assert (wan.rx_bytes, wan.counter_bits) == (5, 32)
        agent.mib[SYS_UPTIME] = (ber.TIMETICKS, 100)  # restarted
        requests = len(agent.requests)
        wan = (await device.driver.poll()).readings[Capability.WAN_COUNTERS]
        assert wan.counter_bits is None  # a counter that went back now is a reset, not a wrap
        assert len(agent.requests) > requests + 1  # the layout was found again
        agent.mib[SYS_UPTIME] = (ber.TIMETICKS, 200)
        assert (await device.driver.poll()).readings[Capability.WAN_COUNTERS].counter_bits == 32
    finally:
        await device.aclose()


async def test_the_wan_interface_by_name_by_description_or_from_the_cidr_route_table():
    mib = router_mib(route=False)
    mib[INET_CIDR_ROUTE_DEFAULT_IFINDEX + (2, 1, 4, 192, 0, 2, 1)] = (ber.INTEGER, 2)
    agent, port = await start_agent(mib)
    for options, rx in (({}, None), ({"wan_interface": "eth1"}, 10_000_000_000),
                        ({"wan_interface": "internet PORT"}, 10_000_000_000)):
        device = snmp_driver(port, **options)
        try:
            result = await device.driver.poll()
        finally:
            await device.aclose()
        if rx is None:  # the default route is on eth0 (2), which has no counters in this MIB
            assert "no octet counters for eth0" in result.missing[Capability.WAN_COUNTERS]
        else:
            assert result.readings[Capability.WAN_COUNTERS].rx_bytes == rx


async def test_no_wan_interface_says_what_to_configure_and_keeps_cpu_and_memory():
    agent, port = await start_agent(router_mib(route=False))
    for options, message in (({}, "set options.wan_interface"),
                             ({"wan_interface": "wan9"}, "no interface named 'wan9'")):
        device = snmp_driver(port, **options)
        try:
            result = await device.driver.poll()
        finally:
            await device.aclose()
        assert message in result.missing[Capability.WAN_COUNTERS]
        assert result.readings[Capability.ROUTER_SYSTEM].mem_pct == 30.0
    assert "it has: lo, eth0, eth1" in result.missing[Capability.WAN_COUNTERS]


async def test_memory_without_an_available_row_leaves_out_buffers_and_cache():
    agent, port = await start_agent(router_mib(available=False))
    device = snmp_driver(port)
    try:
        assert (await device.driver.poll()).readings[Capability.ROUTER_SYSTEM].mem_pct == 50.0  # 80 - 10 - 20
    finally:
        await device.aclose()


async def test_cpu_from_the_ucd_tick_counters_without_hr_processor_load():
    mib = router_mib(processors=())
    ticks = {oid: 0 for oid in (*UCD_CPU_BUSY, *UCD_CPU_IDLE)}

    def set_ticks(busy, idle):
        ticks[UCD_CPU_BUSY[0]], ticks[UCD_CPU_IDLE[0]] = busy, idle
        agent.mib.update({oid: (ber.COUNTER32, v) for oid, v in ticks.items()})

    agent, port = await start_agent(mib)
    clock = Clock()
    set_ticks(2 ** 32 - 100, 1000)  # the busy counter wraps before the next reading
    device = snmp_driver(port, clock=clock)
    try:
        assert (await device.driver.poll()).readings[Capability.ROUTER_SYSTEM].cpu_pct is None  # needs two readings
        clock.now += 10
        set_ticks(300, 1600)  # 400 busy (across the wrap), 600 idle
        assert (await device.driver.poll()).readings[Capability.ROUTER_SYSTEM].cpu_pct == 40.0
        clock.now += 10  # the agent has not refreshed its counters: the last percentage stands
        assert (await device.driver.poll()).readings[Capability.ROUTER_SYSTEM].cpu_pct == 40.0
    finally:
        await device.aclose()


async def test_a_vanished_row_finds_the_layout_again():
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port)
    try:
        await device.driver.poll()
        del agent.mib[IF_IN_OCTETS + (3,)]  # the agent renumbered its interfaces: eth1 is now 4
        for column, value in ((IF_IN_OCTETS, 7), (IF_OUT_OCTETS, 8), (IF_HC_IN_OCTETS, 7), (IF_HC_OUT_OCTETS, 8)):
            agent.mib[column + (4,)] = (ber.COUNTER64 if column in (IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS)
                                        else ber.COUNTER32, value)
        agent.mib[IP_ROUTE_DEFAULT_IFINDEX] = (ber.INTEGER, 4)
        wan = (await device.driver.poll()).readings[Capability.WAN_COUNTERS]
        assert (wan.rx_bytes, wan.tx_bytes) == (7, 8)
    finally:
        await device.aclose()


async def test_a_wrong_community_is_a_timeout_after_the_retries():
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port, community="wrong", retries=2, timeout_seconds=0.1)
    try:
        snapshot = await device.poll(force=True)
    finally:
        await device.aclose()
    assert snapshot.status == "error" and snapshot.error_kind == "timeout"
    assert "wrong community" in snapshot.error_message and len(agent.requests) == 3


def test_match_interface_and_model():
    names, descrs = {1: "lo", 3: "eth1"}, {1: "lo", 3: "WAN"}
    assert match_interface("eth1", names, descrs) == 3 and match_interface("wan", names, descrs) == 3
    assert match_interface("eth9", names, descrs) is None
    assert model_of("  ") is None and model_of("A\x00B\nsecond line") == "A B"


# --- the client -----------------------------------------------------------------------------------------------------

async def test_retries_after_a_lost_answer_and_ignores_answers_to_other_requests():
    agent, port = await start_agent({SYS_UPTIME: (ber.TIMETICKS, 42)})
    client = ber.SnmpClient("127.0.0.1", port, COMMUNITY, timeout=0.2, retries=1)
    try:
        agent.drop = 1
        assert (await client.get([SYS_UPTIME]))[0][1].value == 42 and len(agent.requests) == 2
        stale = encode_response(COMMUNITY.encode(), 1, [(SYS_UPTIME, (ber.TIMETICKS, 1))])
        agent.reply = lambda data, rid: [stale, encode_response(COMMUNITY.encode(), rid,
                                                                [(SYS_UPTIME, (ber.TIMETICKS, 43))])]
        assert (await client.get([SYS_UPTIME]))[0][1].value == 43
    finally:
        await client.aclose()


async def test_a_malformed_answer_or_one_for_other_variables_is_a_protocol_error():
    agent, port = await start_agent({})
    client = ber.SnmpClient("127.0.0.1", port, COMMUNITY, timeout=0.5, retries=0)
    try:
        agent.reply = lambda data, rid: [b"\x30\x84\xff\xff\xff\xff"]
        with pytest.raises(DriverError) as err:
            await client.get([SYS_UPTIME])
        assert err.value.kind == "protocol"
        agent.reply = lambda data, rid: [encode_response(COMMUNITY.encode(), rid, [(SYS_DESCR, (ber.NULL, None))])]
        with pytest.raises(DriverError, match="do not match"):
            await client.get([SYS_UPTIME])
        agent.reply = lambda data, rid: [encode_response(COMMUNITY.encode(), rid, [], error_status=5)]
        with pytest.raises(DriverError, match="genErr"):
            await client.get([SYS_UPTIME])
    finally:
        await client.aclose()


async def test_walks_end_at_the_table_and_halve_the_repetitions_on_too_big():
    mib = {IF_NAME + (i,): (ber.OCTET_STRING, f"port{i}") for i in range(1, 61)}
    mib[IF_HC_IN_OCTETS + (1,)] = (ber.COUNTER64, 1)
    agent, port = await start_agent(mib)
    agent.too_big_above = 10
    client = ber.SnmpClient("127.0.0.1", port, COMMUNITY, timeout=0.5, retries=0, max_repetitions=40)
    try:
        rows = await client.walk(IF_NAME)
        assert [oid[-1] for oid, _ in rows] == list(range(1, 61))
        assert [b for _, _, b in agent.requests[:3]] == [40, 20, 10]
        assert len(await client.walk(IF_NAME, max_rows=5)) == 5
        assert await client.walk(HR_PROCESSOR_LOAD) == []  # a table the agent does not have
    finally:
        await client.aclose()


async def test_a_walk_that_goes_backwards_is_a_protocol_error_not_a_loop():
    agent, port = await start_agent({})
    agent.reply = lambda data, rid: [encode_response(COMMUNITY.encode(), rid,
                                                     [(IF_NAME + (1,), (ber.OCTET_STRING, "a")),
                                                      (IF_NAME + (1,), (ber.OCTET_STRING, "a"))])]
    client = ber.SnmpClient("127.0.0.1", port, COMMUNITY, timeout=0.5, retries=0)
    try:
        with pytest.raises(DriverError, match="backwards"):
            await client.walk(IF_NAME)
    finally:
        await client.aclose()


async def test_an_unreachable_port_is_an_unreachable_error():
    agent, port = await start_agent({})
    agent.transport.close()  # nothing listens there any more: the kernel answers port unreachable
    client = ber.SnmpClient("127.0.0.1", port, COMMUNITY, timeout=0.5, retries=0)
    try:
        with pytest.raises(DriverError) as err:
            await client.get([SYS_UPTIME])
        assert err.value.kind in ("unreachable", "timeout") and err.value.transport
    finally:
        await client.aclose()


# --- the codec ------------------------------------------------------------------------------------------------------

def test_encoding_matches_known_bytes():
    # GET sysUpTime.0 with community "public", request id 1 (as net-snmp's snmpget sends it)
    expected = bytes.fromhex("302602010104067075626c6963a019020101020100020100300e300c06082b060102010103000500")
    assert ber.encode_request(ber.GET_REQUEST, b"public", 1, [SYS_UPTIME]) == expected
    assert ber.encode_oid((1, 3, 6, 1, 4, 1, 2021, 4294967295)) == bytes.fromhex("060c2b060104018f658fffffff7f")
    assert ber.decode_oid(bytes.fromhex("2b060104018f658fffffff7f")) == (1, 3, 6, 1, 4, 1, 2021, 4294967295)
    assert ber._int(128) == b"\x02\x02\x00\x80" and ber._int(-5) == b"\x02\x01\xfb"


def test_decoding_a_response_with_every_value_type():
    varbinds = [((1, 3, 6, 1, 2, 1, 1, 1, 0), (ber.OCTET_STRING, b"\xffbinary")),
                ((1, 3, 6, 1, 2, 1, 1, 3, 0), (ber.TIMETICKS, 2 ** 32 - 1)),
                ((1, 3, 6, 1, 2, 1, 1, 2, 0), (ber.OBJECT_IDENTIFIER, (1, 3, 6, 1, 4, 1, 8072))),
                ((1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6, 1), (ber.COUNTER64, 2 ** 64 - 1)),
                ((1, 3, 6, 1, 2, 1, 2, 2, 1, 7, 1), (ber.INTEGER, -5)),
                ((2, 999, 3), (ber.NO_SUCH_OBJECT, None)),
                ((1, 3, 6, 1, 2, 1, 4, 20, 1, 1, 192, 0, 2, 1), (ber.IP_ADDRESS, bytes([192, 0, 2, 1])))]
    response = ber.decode_response(encode_response(b"c", 77, varbinds))
    assert response.request_id == 77 and response.community == b"c" and response.error_status == 0
    assert [(oid, v.tag, v.value) for oid, v in response.varbinds] == [(o, t, v) for o, (t, v) in varbinds]
    assert response.varbinds[5][1].missing and response.varbinds[3][1].number(ber.COUNTER64) == 2 ** 64 - 1
    with pytest.raises(DriverError, match="IpAddress"):
        ber.decode_response(encode_response(b"c", 1, [((1, 3), (ber.IP_ADDRESS, b"\x01\x02"))]))


@pytest.mark.parametrize("data, why", [
    (b"", "truncated"),
    (b"\x30\x80\x00\x00", "indefinite"),
    (b"\x30\x85\x00\x00\x00\x00\x01", "oversized"),
    (b"\x30\x05\x02\x01\x01", "beyond"),
    (b"\x1f\x01\x00", "multi-byte tag"),
    (b"\x30\x03\x02\x01\x00", "version 0"),
])
def test_malformed_headers(data, why):
    with pytest.raises(DriverError, match=why):
        ber.decode_response(data)


def _valid_response() -> bytes:
    return encode_response(COMMUNITY.encode(), 123456, [
        (SYS_DESCR, (ber.OCTET_STRING, "Example Router")), (SYS_UPTIME, (ber.TIMETICKS, 123)),
        (IF_HC_IN_OCTETS + (3,), (ber.COUNTER64, 10 ** 12)), (HR_STORAGE_TYPE + (1,), (ber.OBJECT_IDENTIFIER,
                                                                                       HR_STORAGE_RAM)),
        (IF_IN_OCTETS + (3,), (ber.COUNTER32, 7)), (HR_PROCESSOR_LOAD + (1,), (ber.NO_SUCH_INSTANCE, None))])


def _decodes_or_refuses(data: bytes) -> None:
    try:
        response = ber.decode_response(data)
    except DriverError as err:
        assert err.kind == "protocol"
    else:
        assert isinstance(response, ber.Response)


def test_fuzz_truncated_answers_are_always_refused():
    valid = _valid_response()
    assert len(ber.decode_response(valid).varbinds) == 6
    for cut in range(len(valid)):
        with pytest.raises(DriverError):
            ber.decode_response(valid[:cut])


def test_fuzz_random_and_corrupted_datagrams_only_raise_driver_errors():
    rng = random.Random(20261102)
    valid = _valid_response()
    started = time.perf_counter()
    for _ in range(4000):
        _decodes_or_refuses(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200))))
        corrupted = bytearray(valid)
        for _ in range(rng.randrange(1, 4)):
            corrupted[rng.randrange(len(corrupted))] = rng.randrange(256)
        _decodes_or_refuses(bytes(corrupted))
        cut = bytearray(valid)
        del cut[rng.randrange(len(cut))]
        _decodes_or_refuses(bytes(cut))
        # structured garbage: a valid header with random content behind it
        body = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
        _decodes_or_refuses(b"\x30" + ber._length(len(body)) + body)
    assert time.perf_counter() - started < 30


def test_large_lengths_and_long_oids_are_bounded():
    huge = b"\x30\x84\x7f\xff\xff\xff" + b"\x00" * 16
    with pytest.raises(DriverError, match="beyond"):
        ber.decode_response(huge)
    with pytest.raises(DriverError):
        ber.decode_oid(b"\x2b" + b"\x01" * 200)  # more than 128 arcs
    with pytest.raises(DriverError, match="too large"):
        ber.decode_oid(b"\x2b\x90\x80\x80\x80\x00")  # an arc above 2^32 - 1
    with pytest.raises(DriverError, match="inside an arc"):
        ber.decode_oid(b"\x2b\x81")
    with pytest.raises(DriverError):
        ber.decode_response(b"\x30\x82\xff\xff" + b"\x00" * 70000)  # more than a datagram
