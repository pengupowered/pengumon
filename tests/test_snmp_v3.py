"""SNMPv3 with USM (drivers/snmp_v3.py): known-answer tests for the keys and ciphers, exchanges recorded from a real
net-snmp 5.9 agent replayed byte for byte, and a fake v3 agent on a local UDP port for discovery, time
synchronization, every refusal and every forged, replayed or malformed answer. Every value is made up; the recordings
come from a throwaway agent on a documentation network (engine id "example-engine", no host data in its views)."""

import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

import pytest

from pengumon.capabilities import Capability, DriverError
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.drivers import snmp_ber as ber
from pengumon.drivers import snmp_v3 as v3
from pengumon.drivers.snmp import IF_NAME, SYS_DESCR, SYS_UPTIME, SnmpDriver, SnmpOptions, Unconfigured
from pengumon.secrets import CredentialSources, password_env_var, privacy_password_env_var

from device_helpers import Clock
from test_snmp import encode_value, router_mib

RECORDINGS = Path(__file__).parent / "snmpv3"
ENGINE = bytes.fromhex("80001f88046578616d706c652d656e67696e65")  # net-snmp's text form of "example-engine"
SYS_NAME = (1, 3, 6, 1, 2, 1, 1, 5, 0)
AUTH_PW, PRIV_PW = "example-auth-pw", "example-priv-pw"


# --- known answers --------------------------------------------------------------------------------------------------

def test_rfc_3414_a32_password_to_key_and_localization_sha1():
    ku = v3.password_to_key(b"maplesyrup", "sha1")
    assert ku.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    kul = v3.localize_key(ku, bytes.fromhex("000000000000000000000002"), "sha1")
    assert kul.hex() == "6695febc9288e36282235fc7151f128497b38f3f"


# The keys net-snmp 5.9.4 stored for the throwaway agent's users (usmUser lines of /var/lib/snmp/snmpd.conf): the
# localized authentication key, and the AES key (the first 16 bytes of the privacy password's localized key).
NET_SNMP_KEYS = {
    "SHA": ("b1986004f891d9286ccb8bb65949d8ce31ad3d1e", "42eaad7e64a283f2e2492e0760d60cdd"),
    "SHA-224": ("310181bcd13aa86cae7ad39e85a1641619dfa4228a461dd28dfd1495", "db24c85827f06e9eafc9f65fc018eb0c"),
    "SHA-256": ("ed41aa87a59afb4987dbaaddba2d4ecadf994f31d8079fe6708729d01404c89e", "4f72ad6126179e25b766428bf99132b7"),
    "SHA-384": ("4e5f9376f85c6adda66ebebb942bbb2a6b2dff8af2a77a43673bf0fe931afc1ef39f032224d0de3c6eac7bfc727e0a77",
                "cbfc921bc87ee691c99d8de8425d90b0"),
    "SHA-512": ("0689ea39ab0205adfe0db34bdcc3301102339396936bc78402996de17ef2c30ff60f442325b10099f86c5049441ff6ddd057a8"
                "07b907e2f9d2c5f2ed5fa2a13b", "5ca3cf1fa2ccbeec4146a997affb874e"),
}


@pytest.mark.parametrize("protocol", sorted(NET_SNMP_KEYS))
def test_localized_keys_match_net_snmp_for_every_sha(protocol):
    name = {"SHA": "sha"}.get(protocol, "sha" + protocol[4:])
    usm = v3.Usm("p", "authPriv", auth_protocol=protocol, auth_password=f"example-auth-{name}",
                 priv_password=f"example-priv-{name}")
    auth, priv = usm.keys(ENGINE)
    assert (auth.hex(), priv.hex()) == NET_SNMP_KEYS[protocol]


def test_hmac_lengths_and_rfc_4231_vectors_truncated():
    # RFC 4231 test case 2 (key "Jefe"), truncated to the RFC 7860 lengths; HMAC-SHA-96 is RFC 2202's case 2 cut to 12
    message = b"what do ya want for nothing?"
    expected = {
        "SHA": "effcdf6ae5eb2fa2d27416d5",
        "SHA-224": "a30e01098bc6dbbf45690f3a7e9e6d0f",
        "SHA-256": "5bdcc146bf60754e6a042426089575c75a003f089d273983",
        "SHA-384": "af45d2e376484031617f78d2b58a6b1b9c7ef464f5a01b47e42ec3736322445e",
        "SHA-512": "164b7a7bfcf819e2e395fbe73b56e0a387bd64222e831fd610270cd7ea2505549758bf75c05a994a6d034f65f8f0e6fd",
    }
    for name, protocol in v3.AUTH_PROTOCOLS.items():
        mac = v3.hmac_of(b"Jefe", message, protocol)
        assert (len(mac), mac.hex()) == (protocol.mac_length, expected[name])
    assert [p.mac_length for p in v3.AUTH_PROTOCOLS.values()] == [12, 16, 24, 32, 48]


def test_rfc_3826_iv_is_boots_time_and_salt():
    salt = bytes(range(9, 17))
    assert v3.aes_iv(0x01020304, 0x05060708, salt) == bytes(range(1, 17))
    assert v3.aes_iv(1, 11, salt).hex() == "000000010000000b" + salt.hex()
    with pytest.raises(ValueError):
        v3.aes_iv(1, 1, b"short")


def test_aes_cfb128_nist_vector_and_no_padding():
    # NIST SP 800-38A F.3.13, CFB128-AES128.Encrypt (two blocks): a 128-bit segment, not CFB8
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
    cipher = v3.aes_cfb(key, iv, plain, encrypt=True)
    assert cipher.hex() == "3b3fd92eb72dad20333449f8e83cfb4ac8a64537a0b3a93fcde3cdad9f1ce58b"
    assert v3.aes_cfb(key, iv, cipher, encrypt=False) == plain
    assert len(v3.aes_cfb(key, iv, plain[:21], encrypt=True)) == 21  # a scoped PDU of any length, no padding


def test_keys_are_computed_once_per_password_and_engine(monkeypatch):
    calls = {"ku": 0, "kul": 0}
    real_ku, real_kul = v3.password_to_key, v3.localize_key
    monkeypatch.setattr(v3, "password_to_key", lambda *a: calls.__setitem__("ku", calls["ku"] + 1) or real_ku(*a))
    monkeypatch.setattr(v3, "localize_key", lambda *a: calls.__setitem__("kul", calls["kul"] + 1) or real_kul(*a))
    usm = v3.Usm("u", "authPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, priv_password=PRIV_PW)
    usm.engine_id = ENGINE
    for n in range(20):
        usm.wrap(n, ber.encode_pdu(ber.GET_REQUEST, n, [SYS_UPTIME]))
    assert calls == {"ku": 2, "kul": 2}  # the two passwords once, each localized once for the engine
    usm.keys(ENGINE[:-1] + b"\x00")
    assert calls == {"ku": 2, "kul": 4}


def test_only_the_latest_engines_keys_are_kept():
    # a forged probe answer may name any engine id: the keys localized for each must not pile up
    usm = v3.Usm("u", "authPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, priv_password=PRIV_PW)
    for n in range(50):
        usm.keys(ENGINE[:-1] + bytes([n]))
    assert list(usm._keys) == [ENGINE[:-1] + bytes([49])]
    assert usm.keys(ENGINE) == User("authPriv").keys() and list(usm._keys) == [ENGINE]


# --- replayed exchanges with net-snmp ---------------------------------------------------------------------------------

def _recordings():
    return sorted(p.name for p in RECORDINGS.glob("*.json"))


class Replay(asyncio.DatagramProtocol):
    """Plays a recorded agent: each datagram received must be the next one recorded as sent (byte for byte: ids,
    salts, HMACs and ciphertext), and is answered with the datagrams recorded after it."""

    def __init__(self, datagrams):
        self.datagrams = list(datagrams)
        self.problems = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not self.datagrams or self.datagrams[0] != ["out", data.hex()]:
            self.problems.append(f"unexpected request {data.hex()}")
            return
        self.datagrams.pop(0)
        while self.datagrams and self.datagrams[0][0] == "in":
            self.transport.sendto(bytes.fromhex(self.datagrams.pop(0)[1]), addr)


async def start_replay(datagrams):
    loop = asyncio.get_running_loop()
    transport, replay = await loop.create_datagram_endpoint(lambda: Replay(datagrams), local_addr=("127.0.0.1", 0))
    return replay, transport.get_extra_info("sockname")[1]


def recorded_usm(rec):
    return v3.Usm(rec["user"], rec["level"], auth_protocol=rec["auth_protocol"] or "SHA",
                  auth_password=rec["auth_password"], priv_password=rec["priv_password"],
                  clock=lambda: rec["clock"], random_bytes=random.Random("usm-" + rec["seed"]).randbytes)


def test_the_recordings_cover_every_level_and_protocol():
    assert {n for n in _recordings() if not n.startswith("driver")} == {
        "noAuthNoPriv-none.json",
        *(f"{level}-{p.lower()}.json" for level in ("authNoPriv", "authPriv") for p in v3.AUTH_PROTOCOLS)}


@pytest.mark.parametrize("name", [n for n in _recordings() if not n.startswith("driver")])
async def test_a_recorded_net_snmp_exchange_replays_byte_for_byte(name):
    rec = json.loads((RECORDINGS / name).read_text())
    replay, port = await start_replay(rec["datagrams"])
    usm = recorded_usm(rec)
    client = ber.SnmpClient("127.0.0.1", port, "", timeout=1.0, retries=0, security=usm,
                            random_bytes=random.Random("client-" + rec["seed"]).randbytes)
    try:
        values = await client.get([SYS_DESCR, SYS_NAME, SYS_UPTIME])
        rows = await client.walk(IF_NAME, max_rows=8)
    finally:
        await client.aclose()
        replay.transport.close()
    assert replay.problems == [] and replay.datagrams == []
    assert {"sysDescr": values[0][1].text(), "sysName": values[1][1].text(),
            "ifName": [v.text() for _, v in rows]} == rec["answers"]
    assert usm.engine_id == ENGINE and usm.discoveries == 1
    if rec["level"] != "noAuthNoPriv":  # the agent's boots and time came from its authenticated answers
        assert usm.engine_clock() == (1, rec_engine_time(rec))


def rec_engine_time(rec):
    last = v3.decode_message(bytes.fromhex([d for d in rec["datagrams"] if d[0] == "in"][-1][1]))
    return last.time


async def test_a_recorded_driver_poll_over_authpriv_sha256_replays():
    rec = json.loads((RECORDINGS / "driver-poll-authpriv-sha-256.json").read_text())
    replay, port = await start_replay(rec["datagrams"])
    options = SnmpOptions(version="3", security_level="authPriv", auth_protocol="SHA-256", retries=0)
    driver = SnmpDriver(f"snmp://127.0.0.1:{port}", rec["auth_password"], options=options, username=rec["user"],
                        privacy_password=rec["priv_password"], wan_interface="eth0", timeout_seconds=1.0, retries=0,
                        clock=lambda: rec["clock"], wall_clock=lambda: 1_700_000_000.0)
    driver.snmp.security = recorded_usm(rec)
    driver.snmp._random = random.Random("client-" + rec["seed"]).randbytes
    try:
        result = await driver.poll()
    finally:
        await driver.aclose()
        replay.transport.close()
    assert replay.problems == [] and replay.datagrams == []
    assert sorted(c.value for c in result.readings) == rec["answers"]["readings"] == ["wan_counters"]
    assert result.readings[Capability.WAN_COUNTERS].rx_bytes > 0


async def test_a_tampered_recording_is_dropped_not_trusted():
    rec = json.loads((RECORDINGS / "authPriv-sha-256.json").read_text())
    datagrams = [list(d) for d in rec["datagrams"]]
    answer = bytearray.fromhex(datagrams[3][1])  # the answer to the GET: one bit of its encrypted PDU flipped
    answer[-3] ^= 0x01
    datagrams[3][1] = answer.hex()
    replay, port = await start_replay(datagrams)
    client = ber.SnmpClient("127.0.0.1", port, "", timeout=0.3, retries=0, security=recorded_usm(rec),
                            random_bytes=random.Random("client-" + rec["seed"]).randbytes)
    try:
        with pytest.raises(DriverError, match="failed authentication") as err:
            await client.get([SYS_DESCR, SYS_NAME, SYS_UPTIME])
    finally:
        await client.aclose()
        replay.transport.close()
    assert err.value.kind == "auth" and rec["auth_password"] not in str(err.value)


# --- a fake v3 agent --------------------------------------------------------------------------------------------------

class User:
    def __init__(self, level, protocol="SHA-256", auth=AUTH_PW, priv=PRIV_PW, engine=ENGINE):
        self.level = level
        self.auth = v3.AUTH_PROTOCOLS[protocol] if level != "noAuthNoPriv" else None
        h = self.auth.hash if self.auth else None
        self.auth_key = v3.localize_key(v3.password_to_key(auth.encode(), h), engine, h) if self.auth else b""
        self.priv_key = (v3.localize_key(v3.password_to_key(priv.encode(), h), engine, h)[:16]
                         if level == "authPriv" else b"")

    def keys(self):
        return self.auth_key, self.priv_key


LEVEL_FLAGS = {"noAuthNoPriv": 0, "authNoPriv": v3.FLAG_AUTH, "authPriv": v3.FLAG_AUTH | v3.FLAG_PRIV}


def agent_parse(data):
    """The fields of a request, as the agent reads them (the probe has no engine id, which the client's decoder
    refuses in an answer)."""
    s, e = ber._expect(data, 0, len(data), ber.SEQUENCE, "message")
    version, pos = ber._decode_int(data, s, e, "version")
    assert version == 3
    hs, he = ber._expect(data, pos, e, ber.SEQUENCE, "header")
    msg_id, p = ber._decode_int(data, hs, he, "msgID")
    _, p = ber._decode_int(data, p, he, "max size")
    fs, fe = ber._expect(data, p, he, ber.OCTET_STRING, "flags")
    ss, se = ber._expect(data, he, e, ber.OCTET_STRING, "security parameters")
    us, ue = ber._expect(data, ss, se, ber.SEQUENCE, "usm")
    es, ee = ber._expect(data, us, ue, ber.OCTET_STRING, "engine")
    boots, p = ber._decode_int(data, ee, ue, "boots")
    engine_time, p = ber._decode_int(data, p, ue, "time")
    ns, ne = ber._expect(data, p, ue, ber.OCTET_STRING, "user")
    as_, ae = ber._expect(data, ne, ue, ber.OCTET_STRING, "auth")
    ps, pe = ber._expect(data, ae, ue, ber.OCTET_STRING, "priv")
    flags = data[fs]
    if flags & v3.FLAG_PRIV:
        ds, de = ber._expect(data, se, e, ber.OCTET_STRING, "encrypted")
        msg_data = data[ds:de]
    else:
        msg_data = data[se:e]
    return dict(msg_id=msg_id, flags=flags, engine=data[es:ee], boots=boots, time=engine_time, user=data[ns:ne],
                auth=data[as_:ae], auth_at=as_, priv=data[ps:pe], data=msg_data)


def agent_scoped(data):
    """(contextEngineID, contextName, pdu tag, request id, a, b, oids) of a plaintext scoped PDU."""
    s, e = ber._expect(data, 0, len(data), ber.SEQUENCE, "scoped")
    cs, ce = ber._expect(data, s, e, ber.OCTET_STRING, "context engine")
    ns, ne = ber._expect(data, ce, e, ber.OCTET_STRING, "context name")
    tag, ps, pe = ber._read(data, ne, e)
    request_id, p = ber._decode_int(data, ps, pe, "id")
    a, p = ber._decode_int(data, p, pe, "a")
    b, p = ber._decode_int(data, p, pe, "b")
    vs, ve = ber._expect(data, p, pe, ber.SEQUENCE, "varbinds")
    oids, p = [], vs
    while p < ve:
        s2, e2 = ber._expect(data, p, ve, ber.SEQUENCE, "varbind")
        os_, oe = ber._expect(data, s2, e2, ber.OBJECT_IDENTIFIER, "name")
        oids.append(ber.decode_oid(data[os_:oe]))
        p = e2
    return data[cs:ce], data[ns:ne], tag, request_id, a, b, oids


def pdu_bytes(tag, request_id, varbinds):
    body = b"".join(ber._tlv(ber.SEQUENCE, ber.encode_oid(oid) + encode_value(t, v)) for oid, (t, v) in varbinds)
    return ber._tlv(tag, ber._int(request_id) + ber._int(0) + ber._int(0) + ber._tlv(ber.SEQUENCE, body))


class FakeV3Agent(asyncio.DatagramProtocol):
    """An SNMPv3 agent (the authoritative side of RFC 3414) serving `mib` to `users`: discovery, the time window,
    every Report, GET and GETBULK. Hooks for the tests: `on_decrypt_error` ("silent" as net-snmp, or "report"),
    `probe_clock` (the boots and time its probe answer gives), `scoped` (changes a Response's plaintext scoped PDU),
    `answer_clock` (the boots and time a Response says), `tamper` (changes the finished datagram), `extra` (datagrams
    sent before the answer)."""

    def __init__(self, mib, users, *, boots=5, engine_time=5000):
        self.mib = dict(mib)
        self.users = users
        self.boots, self.time = boots, engine_time
        self.requests = []  # (kind, user) per datagram received
        self.on_decrypt_error = "silent"
        self.probe_clock = None
        self.scoped = None
        self.answer_clock = None
        self.tamper = None
        self.extra = None
        self.contexts = {b""}  # a request for another context is dropped, as net-snmp does
        self.salt = 0

    def connection_made(self, transport):
        self.transport = transport

    def build(self, msg_id, flags, user_name, user, pdu, *, clock=None, scoped_hook=None, context=b""):
        boots, engine_time = clock or (self.boots, self.time)
        scoped = v3.encode_scoped_pdu(ENGINE, context, pdu)
        if scoped_hook:
            scoped = scoped_hook(scoped)
        priv = b""
        if flags & v3.FLAG_PRIV:
            self.salt += 1
            priv = self.salt.to_bytes(8, "big")
            scoped = ber._tlv(ber.OCTET_STRING, v3.aes_cfb(user.priv_key, v3.aes_iv(boots, engine_time, priv), scoped,
                                                           encrypt=True))
        zeros = bytes(user.auth.mac_length) if flags & v3.FLAG_AUTH else b""
        message = v3.encode_message(msg_id, flags, ENGINE, boots, engine_time, user_name, zeros, priv, scoped)
        if flags & v3.FLAG_AUTH:
            at = v3.decode_message(message).auth_at
            message = message[:at] + v3.hmac_of(user.auth_key, message, user.auth) + message[at + len(zeros):]
        return message

    def report(self, addr, req, oid, *, user=None, flags=0, request_id=0):
        pdu = pdu_bytes(ber.REPORT, request_id, [(oid, (ber.COUNTER32, 1))])
        self.transport.sendto(self.build(req["msg_id"], flags, req["user"], user, pdu), addr)

    def datagram_received(self, data, addr):
        req = agent_parse(data)
        if not req["engine"]:
            self.requests.append(("probe", None))
            boots, engine_time = self.probe_clock or (self.boots, self.time)
            _, _, _, request_id, _, _, _ = agent_scoped(req["data"])
            pdu = pdu_bytes(ber.REPORT, request_id, [(v3.UNKNOWN_ENGINE_IDS, (ber.COUNTER32, 1))])
            self.transport.sendto(self.build(req["msg_id"], 0, b"", None, pdu, clock=(boots, engine_time)), addr)
            return
        name = req["user"].decode()
        self.requests.append(("request", name))
        if req["engine"] != ENGINE:
            return self.report(addr, req, v3.UNKNOWN_ENGINE_IDS)
        user = self.users.get(name)
        if user is None:
            return self.report(addr, req, v3.UNKNOWN_USER_NAMES)
        flags = req["flags"] & (v3.FLAG_AUTH | v3.FLAG_PRIV)
        if flags > LEVEL_FLAGS[user.level]:
            return self.report(addr, req, v3.UNSUPPORTED_SEC_LEVELS)
        if flags & v3.FLAG_AUTH:
            size = user.auth.mac_length
            zeroed = data[:req["auth_at"]] + bytes(size) + data[req["auth_at"] + size:]
            if v3.hmac_of(user.auth_key, zeroed, user.auth) != req["auth"]:
                return self.report(addr, req, v3.WRONG_DIGESTS)
            if req["boots"] != self.boots or abs(req["time"] - self.time) > v3.TIME_WINDOW:
                return self.report(addr, req, v3.NOT_IN_TIME_WINDOWS, user=user, flags=v3.FLAG_AUTH)
        plain = req["data"]
        if flags & v3.FLAG_PRIV:
            plain = v3.aes_cfb(user.priv_key, v3.aes_iv(req["boots"], req["time"], req["priv"]), plain,
                               encrypt=False)
        try:
            _, context, tag, request_id, a, b, oids = agent_scoped(plain)
        except DriverError:
            if self.on_decrypt_error == "report":
                self.report(addr, req, v3.DECRYPTION_ERRORS)
            return  # net-snmp drops it without a word
        if context not in self.contexts:
            return
        if tag == ber.GET_REQUEST:
            varbinds = [(oid, self.mib.get(oid, (ber.NO_SUCH_INSTANCE, None))) for oid in oids]
        else:
            after = [oid for oid in sorted(self.mib) if oid > oids[0]][:b]
            varbinds = [(oid, self.mib[oid]) for oid in after] or [(oids[0], (ber.END_OF_MIB_VIEW, None))]
        if self.extra:
            for datagram in self.extra(req, user):
                self.transport.sendto(datagram, addr)
        message = self.build(req["msg_id"], flags, req["user"], user, pdu_bytes(ber.RESPONSE, request_id, varbinds),
                             clock=self.answer_clock, scoped_hook=self.scoped, context=context)
        self.transport.sendto(self.tamper(message) if self.tamper else message, addr)


async def start_v3_agent(mib=None, users=None, **kwargs):
    loop = asyncio.get_running_loop()
    users = users if users is not None else {"u": User("authPriv")}
    transport, agent = await loop.create_datagram_endpoint(
        lambda: FakeV3Agent(mib if mib is not None else {SYS_UPTIME: (ber.TIMETICKS, 42)}, users, **kwargs),
        local_addr=("127.0.0.1", 0))
    return agent, transport.get_extra_info("sockname")[1]


def v3_client(port, *, user="u", level="authPriv", protocol="SHA-256", auth=AUTH_PW, priv=PRIV_PW, clock=None,
              timeout=0.3, retries=0):
    usm = v3.Usm(user, level, auth_protocol=protocol, auth_password=auth, priv_password=priv,
                 clock=clock or Clock(0.0))
    return ber.SnmpClient("127.0.0.1", port, "", timeout=timeout, retries=retries, security=usm)


async def get_uptime(client):
    return (await asyncio.wait_for(client.get([SYS_UPTIME]), 5))[0][1].value


@pytest.mark.parametrize("level, protocol", [("noAuthNoPriv", "SHA"), *(
    (level, p) for level in ("authNoPriv", "authPriv") for p in v3.AUTH_PROTOCOLS)])
async def test_every_level_and_protocol_against_the_fake_agent(level, protocol):
    agent, port = await start_v3_agent(users={"u": User(level, protocol)})
    client = v3_client(port, level=level, protocol=protocol)
    try:
        assert await get_uptime(client) == 42
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()
    assert agent.requests == [("probe", None), ("request", "u"), ("request", "u")]  # one discovery, no resync


async def test_an_agent_whose_probe_gives_no_time_is_synchronized_by_its_authenticated_report():
    agent, port = await start_v3_agent()
    agent.probe_clock = (0, 0)
    client = v3_client(port)
    try:
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()
    assert agent.requests == [("probe", None), ("request", "u"), ("request", "u")]  # notInTimeWindows, then again
    assert client.security.engine_clock() == (5, 5000)


async def test_an_agent_restart_resynchronizes_and_an_engine_change_discovers_again():
    agent, port = await start_v3_agent()
    clock = Clock(0.0)
    client = v3_client(port, clock=clock)
    try:
        assert await get_uptime(client) == 42
        agent.boots, agent.time = 6, 3  # restarted: boots up, time from 0
        clock.now += 600
        assert await get_uptime(client) == 42
        assert client.security.engine_clock() == (6, 3)
        assert [kind for kind, _ in agent.requests] == ["probe", "request", "request", "request"]
        client.security.engine_id = ENGINE[:-1] + b"X"  # as if the agent had been replaced: unknownEngineIDs
        client.security._synced = False
        assert await get_uptime(client) == 42
        assert client.security.engine_id == ENGINE and client.security.discoveries == 2
    finally:
        await client.aclose()


async def test_the_engine_time_moves_with_the_clock_between_requests():
    agent, port = await start_v3_agent()
    clock = Clock(0.0)
    client = v3_client(port, clock=clock)
    try:
        await get_uptime(client)
        clock.now += 120
        agent.time += 120
        await get_uptime(client)
        assert len(agent.requests) == 3  # sent with time 5120: inside the agent's window, no resync
    finally:
        await client.aclose()


@pytest.mark.parametrize("change, kind, message", [
    ({"user": "nobody"}, "auth", "no SNMPv3 user named 'nobody'"),
    ({"auth": "wrong-auth-pw"}, "auth", "wrong authentication password, or the user does not use SHA-256"),
    ({"protocol": "SHA-512"}, "auth", "wrongDigests"),
    ({"level": "authPriv", "user": "n"}, "auth", "does not support the security level authPriv"),
])
async def test_refusals_become_auth_errors_that_name_what_to_check(change, kind, message):
    agent, port = await start_v3_agent(users={"u": User("authPriv"), "n": User("authNoPriv")})
    client = v3_client(port, **change)
    try:
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
    finally:
        await client.aclose()
    assert err.value.kind == kind and message in err.value.message
    assert "wrong-auth-pw" not in err.value.message and AUTH_PW not in err.value.message


async def test_a_wrong_privacy_password_is_named_whether_the_agent_reports_it_or_stays_silent():
    for mode, words in (("report", "decryptionErrors"), ("silent", "most likely a wrong privacy password")):
        agent, port = await start_v3_agent()
        agent.on_decrypt_error = mode
        client = v3_client(port, priv="wrong-priv-pw", timeout=0.2, retries=1)
        try:
            with pytest.raises(DriverError) as err:
                await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        finally:
            await client.aclose()
        assert err.value.kind == "auth" and words in err.value.message and "wrong-priv-pw" not in err.value.message
    assert [kind for kind, _ in agent.requests] == ["probe", "request", "request", "probe"]  # (silent: two tries)


async def test_an_unknown_context_is_named_when_the_agent_drops_the_request():
    agent, port = await start_v3_agent(users={"u": User("authNoPriv")})
    usm = v3.Usm("u", "authNoPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, context_name="nocontext",
                 clock=Clock(0.0))
    client = ber.SnmpClient("127.0.0.1", port, "", timeout=0.2, retries=0, security=usm)
    try:
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        assert err.value.kind == "auth" and "a context the agent does not have ('nocontext'" in err.value.message
        assert "privacy" not in err.value.message
        agent.contexts.add(b"nocontext")
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()


async def test_a_silent_agent_is_a_timeout_also_with_privacy():
    agent, port = await start_v3_agent()
    client = v3_client(port, timeout=0.1, retries=0)
    try:
        await get_uptime(client)
        agent.transport.close()
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
    finally:
        await client.aclose()
    assert err.value.kind in ("timeout", "unreachable") and err.value.transport


def _flip_hmac(message):
    at = v3.decode_message(message).auth_at
    return message[:at] + bytes([message[at] ^ 0x01]) + message[at + 1:]


async def test_an_answer_with_a_bad_hmac_is_dropped_and_the_genuine_one_still_taken():
    agent, port = await start_v3_agent()
    client = v3_client(port)
    try:
        await get_uptime(client)
        agent.tamper = _flip_hmac  # only forged answers: the request fails, naming why
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        assert err.value.kind == "auth" and "failed authentication (wrong HMAC)" in err.value.message
        agent.tamper = None  # a forged answer first (the right msgID, keys guessed), then the genuine one
        evil = User("authPriv", auth="forger-guess-pw", priv="forger-guess-pw")
        agent.extra = lambda req, user: [agent.build(req["msg_id"], req["flags"] & 3, req["user"], evil, pdu_bytes(
            ber.RESPONSE, 0, [(SYS_UPTIME, (ber.TIMETICKS, 666))]))]
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()


JUNK = (b"", b"junk", b"\x30\x03\x02\x01\x03", b"\x30\x84\x7f\xff\xff\xff")  # empty, not BER, a bare v3 header, a huge length


async def test_a_datagram_that_does_not_decode_is_dropped_and_the_genuine_answer_still_taken():
    # anyone who can send to the client's port could fail an authenticated request with one malformed datagram
    agent, port = await start_v3_agent()
    client = v3_client(port)
    try:
        await get_uptime(client)
        agent.extra = lambda req, user: list(JUNK)  # sent just before the genuine answer
        assert await get_uptime(client) == 42
        agent.extra = None
        agent.tamper = lambda message: b"junk"  # nothing but junk: a protocol error that says so, not an auth one
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        assert err.value.kind == "protocol" and "dropped: it was malformed" in err.value.message
    finally:
        await client.aclose()


def test_an_unauthenticated_answer_whose_scoped_pdu_does_not_decode_is_dropped():
    usm = v3.Usm("u", "authPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, priv_password=PRIV_PW)
    usm.engine_id = ENGINE
    plain = v3.encode_message(9, 0, ENGINE, 5, 5000, b"u", b"", b"", ber._tlv(ber.SEQUENCE, b"\x04\x01"))
    assert usm.unwrap(plain, 9, 7) is None and "malformed" in usm._dropped
    probe_answer = v3.encode_message(3, 0, ENGINE, 5, 5000, b"", b"", b"", ber._tlv(ber.SEQUENCE, b"\x04\x01"))
    assert usm.unwrap_probe(probe_answer, 3) is None and usm.unwrap_probe(b"junk", 3) is None


async def test_an_answer_outside_the_time_window_is_dropped():
    for clock_change in ((5, 5000 - 151), (4, 6000)):  # 151 s behind; boots gone back
        agent, port = await start_v3_agent()
        client = v3_client(port)
        try:
            await get_uptime(client)
            agent.answer_clock = clock_change
            with pytest.raises(DriverError) as err:
                await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
            assert err.value.kind == "auth" and "outside the time window" in err.value.message
            assert client.security.engine_id is None  # the next request discovers and synchronizes again
            agent.answer_clock = None
            assert await get_uptime(client) == 42
        finally:
            await client.aclose()
    agent, port = await start_v3_agent()
    client = v3_client(port)
    try:
        await get_uptime(client)
        agent.answer_clock = (5, 5000 - 150)  # at the edge: still inside
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()


async def test_an_unauthenticated_answer_to_an_authenticated_request_is_ignored():
    agent, port = await start_v3_agent()
    client = v3_client(port, timeout=0.2)
    try:
        await get_uptime(client)

        def plain(message):  # the same answer re-sent at noAuthNoPriv, so nothing vouches for it
            m = v3.decode_message(message)
            pdu = pdu_bytes(ber.RESPONSE, 0, [])
            return v3.encode_message(m.msg_id, 0, m.engine_id, m.boots, m.time, m.user, b"", b"",
                                     v3.encode_scoped_pdu(ENGINE, b"", pdu))
        agent.tamper = plain
        with pytest.raises(DriverError) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        assert err.value.kind == "auth" and "another security level" in err.value.message
    finally:
        await client.aclose()


@pytest.mark.parametrize("hook, why", [
    (lambda scoped: scoped[:-5], "beyond the data"),                  # truncated: the scoped PDU claims more
    (lambda scoped: scoped + bytes(16), "bytes after the scoped PDU"),  # more than a block of padding
    (lambda scoped: b"\x30\x84\x7f\xff\xff\xff" + scoped, "beyond the data"),
    (lambda scoped: bytes(len(scoped)), "tag 0x00"),
])
async def test_a_malformed_encrypted_pdu_is_a_protocol_error_never_a_crash_or_hang(hook, why):
    agent, port = await start_v3_agent()
    client = v3_client(port)
    try:
        await get_uptime(client)
        agent.scoped = hook
        with pytest.raises(DriverError, match=why) as err:
            await asyncio.wait_for(client.get([SYS_UPTIME]), 5)
        assert err.value.kind == "protocol"
    finally:
        await client.aclose()


async def test_up_to_a_block_of_padding_after_a_decrypted_pdu_is_tolerated():
    agent, port = await start_v3_agent()
    agent.scoped = lambda scoped: scoped + bytes(15)
    client = v3_client(port)
    try:
        assert await get_uptime(client) == 42
    finally:
        await client.aclose()


async def test_the_driver_polls_over_snmpv3_with_the_privacy_password_from_its_sources(tmp_path):
    agent, port = await start_v3_agent(router_mib(), users={"monitor": User("authPriv", "SHA-512")})
    cfg = DeviceConfig(id="sw", driver="snmp", url=f"snmp://127.0.0.1:{port}", username="monitor",
                       options={"version": "3", "auth_protocol": "SHA-512", "timeout_seconds": 0.3, "retries": 0})
    (tmp_path / "pengumon_sw_privacy_password").write_text(PRIV_PW + "\n")
    secrets = CredentialSources(env={password_env_var("sw"): AUTH_PW}, secrets_dir=tmp_path, store={})
    device = build_device(cfg, None, secrets, clock=Clock(), wall_clock=lambda: 1_700_000_000.0)
    try:
        assert device.driver.missing_configuration() is None and device.has_credentials
        snapshot = await device.poll(force=True)
        assert snapshot.status == "healthy" and snapshot.wan_rx_bytes == 10_000_000_000
        assert snapshot.model_name == "Example Router ER-1 fw 2.0"
        device.set_credentials("monitor", AUTH_PW, privacy_password="wrong-priv-pw")
        snapshot = await device.poll(force=True)
        assert snapshot.status == "error" and snapshot.error_kind == "auth"
        assert "privacy password" in snapshot.error_message
        device.set_credentials("monitor", AUTH_PW, privacy_password=PRIV_PW)
        assert (await device.poll(force=True)).status == "healthy"
    finally:
        await device.aclose()


# --- configuration --------------------------------------------------------------------------------------------------

def v3_driver(level="authPriv", password=AUTH_PW, privacy=PRIV_PW, username="monitor", **options):
    return SnmpDriver("snmp://192.0.2.2", password, username=username, privacy_password=privacy,
                      options=SnmpOptions(version="3", security_level=level, **options))


def test_snmpv3_options_and_what_is_missing():
    assert SnmpOptions().version == "2c"
    for options, message in (({"security_level": "authNoPriv"}, "security_level is for SNMPv3"),
                             ({"auth_protocol": "SHA", "context_name": "x"}, "auth_protocol, context_name are for"),
                             ({"version": "3", "auth_protocol": "MD5"}, "auth_protocol"),
                             ({"version": "3", "priv_protocol": "DES"}, "priv_protocol"),
                             ({"version": "3", "context_name": "é" * 17}, "longer than 32 bytes"),
                             ({"version": "1"}, "version")):
        with pytest.raises(ValueError, match=message):
            DeviceConfig(id="sw", driver="snmp", url="snmp://192.0.2.2", options=options)
    assert v3_driver().missing_configuration() is None
    assert v3_driver(privacy=None).missing_configuration() == "no SNMPv3 privacy password (security level authPriv needs one)"
    assert "privacy password is shorter than 8" in v3_driver(privacy="short").missing_configuration()
    assert v3_driver(password=None).missing_configuration() == "no SNMPv3 authentication password (the device's password)"
    assert "authentication password is shorter than 8" in v3_driver(password="short").missing_configuration()
    assert "user name" in v3_driver(username="x" * 33).missing_configuration()
    assert v3_driver("authNoPriv", privacy=None).missing_configuration() is None
    none = v3_driver("noAuthNoPriv", password=None, privacy=None)
    assert none.missing_configuration() is None and none.needs_sign_in is False
    assert none.snmp.security.flags == v3.FLAG_REPORTABLE
    unconfigured = v3_driver(privacy=None).snmp.security  # no keys until the configuration is complete
    assert isinstance(unconfigured, Unconfigured) and "privacy password" in unconfigured.why


def test_what_the_setup_dialog_asks_for():
    assert SnmpDriver("snmp://192.0.2.2", "c").sign_in == {"username": None, "password": "Community string",
                                                            "privacy_password": None}
    assert v3_driver().sign_in == {"username": "SNMPv3 user name", "password": "Authentication password",
                                   "privacy_password": "Privacy password"}
    assert v3_driver("authNoPriv").sign_in["privacy_password"] is None
    assert v3_driver().takes_privacy_password and not v3_driver("authNoPriv").takes_privacy_password
    assert not SnmpDriver("snmp://192.0.2.2", "c").takes_privacy_password


def test_new_credentials_make_new_keys_and_keep_the_privacy_password_when_none_is_given():
    driver = v3_driver(privacy=None)
    assert isinstance(driver.snmp.security, Unconfigured)
    driver.set_credentials("other", AUTH_PW, PRIV_PW)
    first = driver.snmp.security
    assert first is not None and first.user == b"other"
    driver.set_credentials("other", "another-auth-pw")
    assert driver.snmp.security is not first and driver._privacy_password == PRIV_PW
    v2c = SnmpDriver("snmp://192.0.2.2", "c")
    v2c.set_credentials("ignored", "community")
    assert v2c.snmp.security is None and v2c.snmp.community == b"community"


class Listener(asyncio.DatagramProtocol):
    """A loopback UDP port that records whatever reaches it and never answers."""

    def __init__(self):
        self.datagrams = []

    def datagram_received(self, data, addr):
        self.datagrams.append(data)


@pytest.mark.parametrize("change", [
    dict(privacy_password="short"),                   # the API took 1 character before 0.27's fix
    dict(username="x" * 33),                          # and up to 64 bytes of user name
    dict(password="short"),
])
async def test_incomplete_snmpv3_credentials_send_nothing_and_never_fall_back_to_v2c(change):
    # a poll under way when the credentials change must not send its next request as SNMPv2c, with the
    # authentication password as the community string
    loop = asyncio.get_running_loop()
    transport, listener = await loop.create_datagram_endpoint(Listener, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    driver = SnmpDriver(f"snmp://127.0.0.1:{port}", AUTH_PW, username="monitor", privacy_password=PRIV_PW,
                        timeout_seconds=0.2, retries=0,
                        options=SnmpOptions(version="3", security_level="authPriv", auth_protocol="SHA-256"))
    try:
        assert isinstance(driver.snmp.security, v3.Usm) and driver.snmp.community == b""
        credentials = {"username": "monitor", "password": AUTH_PW, "privacy_password": PRIV_PW, **change}
        driver.set_credentials(credentials["username"], credentials["password"], credentials["privacy_password"])
        assert driver.missing_configuration() and driver.snmp.community == b""
        for request in (driver.snmp.get([SYS_DESCR]), driver.snmp.walk(IF_NAME), driver.poll()):
            with pytest.raises(DriverError) as err:
                await asyncio.wait_for(request, 5)
            assert err.value.kind == "unconfigured"
            assert AUTH_PW not in err.value.message and PRIV_PW not in err.value.message
        await asyncio.sleep(0.3)
        assert listener.datagrams == []  # not a single datagram, v2c or v3
    finally:
        await driver.aclose()
        transport.close()


async def test_an_snmpv2c_client_without_a_community_sends_nothing():
    loop = asyncio.get_running_loop()
    transport, listener = await loop.create_datagram_endpoint(Listener, local_addr=("127.0.0.1", 0))
    client = ber.SnmpClient("127.0.0.1", transport.get_extra_info("sockname")[1], "", timeout=0.2, retries=0)
    try:
        with pytest.raises(DriverError) as err:
            await client.get([SYS_DESCR])
        await asyncio.sleep(0.1)
    finally:
        await client.aclose()
        transport.close()
    assert err.value.kind == "unconfigured" and client.requests == 0 and listener.datagrams == []


def test_the_credentials_check_the_api_asks_the_driver():
    driver = v3_driver()
    assert driver.credentials_problem("monitor", AUTH_PW, PRIV_PW) is None
    assert driver.credentials_problem("monitor", AUTH_PW) is None  # left out: the stored one is kept
    assert "privacy password is shorter than 8" in driver.credentials_problem("monitor", AUTH_PW, "seven-7")
    assert "authentication password is shorter than 8" in driver.credentials_problem("monitor", "seven-7", PRIV_PW)
    assert "1 to 32 bytes" in driver.credentials_problem("é" * 17, AUTH_PW, PRIV_PW)  # 34 bytes
    assert v3_driver("authNoPriv").credentials_problem("monitor", AUTH_PW) is None
    assert SnmpDriver("snmp://192.0.2.2", "c").credentials_problem("ignored", "c") is None  # v2c: any community


def test_the_privacy_password_sources_rank_like_the_password(tmp_path):
    cfg = DeviceConfig(id="core-sw", driver="snmp", url="snmp://192.0.2.2", options={"version": "3"})
    store = {"devices": {"core-sw": {"privacy_password": "from-store-1"}}}
    assert CredentialSources(env={}, secrets_dir=None, store=store).resolve(cfg).privacy_password == "from-store-1"
    (tmp_path / "pengumon_core-sw_privacy_password").write_text("from-file-1\n")
    found = CredentialSources(env={}, secrets_dir=tmp_path, store=store).resolve(cfg)
    assert (found.privacy_password, found.privacy_password_source) == ("from-file-1",
                                                                       str(tmp_path / "pengumon_core-sw_privacy_password"))
    env = {privacy_password_env_var("core-sw"): "from-env-1"}
    sources = CredentialSources(env=env, secrets_dir=tmp_path, store=store)
    assert sources.resolve(cfg).privacy_password == "from-env-1"
    assert privacy_password_env_var("core-sw") == "PENGUMON_DEVICE_CORE_SW_PRIVACY_PASSWORD"
    assert sources.outranking(cfg) == "environment PENGUMON_DEVICE_CORE_SW_PRIVACY_PASSWORD"
    assert "from-env-1" not in repr(sources.resolve(cfg))


async def test_an_authpriv_device_without_its_privacy_password_says_what_to_set():
    cfg = DeviceConfig(id="sw", driver="snmp", url="snmp://192.0.2.2", options={"version": "3"})
    device = build_device(cfg, None, CredentialSources(env={password_env_var("sw"): AUTH_PW}, secrets_dir=None,
                                                       store={}))
    snapshot = await device.poll(force=True)
    assert snapshot.status == "unconfigured"
    assert "PENGUMON_DEVICE_SW_PRIVACY_PASSWORD" in snapshot.error_message
    assert "pengumon_sw_privacy_password" in snapshot.error_message


# --- the decoder ----------------------------------------------------------------------------------------------------

def _answer(flags=v3.FLAG_AUTH | v3.FLAG_PRIV):
    user = User("authPriv")
    scoped = v3.encode_scoped_pdu(ENGINE, b"", pdu_bytes(ber.RESPONSE, 7, [(SYS_UPTIME, (ber.TIMETICKS, 42))]))
    priv = b""
    if flags & v3.FLAG_PRIV:
        priv = bytes(8)
        scoped = ber._tlv(ber.OCTET_STRING, v3.aes_cfb(user.priv_key, v3.aes_iv(5, 5000, priv), scoped, encrypt=True))
    zeros = bytes(24) if flags & v3.FLAG_AUTH else b""
    message = v3.encode_message(9, flags, ENGINE, 5, 5000, b"u", zeros, priv, scoped)
    if flags & v3.FLAG_AUTH:
        at = v3.decode_message(message).auth_at
        message = message[:at] + v3.hmac_of(user.auth_key, message, user.auth) + message[at + 24:]
    return message


def test_decoding_an_answer_checks_every_field():
    m = v3.decode_message(_answer())
    assert (m.msg_id, m.flags, m.engine_id, m.boots, m.time, m.user) == (9, 3, ENGINE, 5, 5000, b"u")
    assert len(m.auth_params) == 24 and m.priv_params == bytes(8)
    usm = v3.Usm("u", "authPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, priv_password=PRIV_PW)
    usm.engine_id = ENGINE
    response = usm.unwrap(_answer(), 9, 7)
    assert response.varbinds == ((SYS_UPTIME, ber.Value(ber.TIMETICKS, 42)),)
    assert usm.unwrap(_answer(), 10, 7) is None  # another msgID
    assert usm.unwrap(_answer(), 9, 8) is None  # another request id (checked after the HMAC and decryption)


def _with(message, old, new):
    assert message.count(old) == 1
    return message.replace(old, new)


@pytest.mark.parametrize("change, why", [
    (lambda m: m[:-1], "beyond the data"),
    (lambda m: m + b"\x00", "bytes after the message"),
    (lambda m: _with(m, b"\x02\x01\x03\x30", b"\x02\x01\x01\x30"), "version 1"),
    (lambda m: _with(m, b"\x04\x01\x03\x02\x01\x03", b"\x04\x01\x02\x02\x01\x03"), "privacy without authentication"),
    (lambda m: _with(m, b"\x04\x01\x03\x02\x01\x03", b"\x04\x01\x03\x02\x01\x02"), "security model 2"),
    (lambda m: _with(m, b"\x04\x13" + ENGINE, b"\x04\x04" + ENGINE[:4] + bytes(15)), "too short"),
    (lambda m: _with(m, b"\x02\x03\x00\xff\xe3", b"\x02\x03\x00\x01\x00"), "msgMaxSize out of range"),
])
def test_malformed_v3_headers(change, why):
    with pytest.raises(DriverError, match=why):
        v3.decode_message(change(_answer()))


def test_fuzz_v3_answers_only_raise_driver_errors_or_are_dropped():
    rng = random.Random(20261110)
    valid = _answer()
    usm = v3.Usm("u", "authPriv", auth_protocol="SHA-256", auth_password=AUTH_PW, priv_password=PRIV_PW)
    usm.engine_id = ENGINE
    started = time.perf_counter()
    for cut in range(len(valid)):  # nothing vouches for a datagram that does not decode: dropped, and why recorded
        usm._dropped = None
        assert usm.unwrap(valid[:cut], 9, 7) is None and usm._dropped.startswith("it was malformed")
    for _ in range(3000):
        corrupted = bytearray(valid)
        for _ in range(rng.randrange(1, 4)):
            corrupted[rng.randrange(len(corrupted))] = rng.randrange(256)
        for data in (bytes(corrupted), bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))):
            try:
                answer = usm.unwrap(data, 9, 7)
            except DriverError as err:
                assert err.kind == "protocol"
            else:  # a changed byte the HMAC covers is never accepted
                assert answer is None or data == valid or bytes(corrupted) == valid or isinstance(answer, v3.Report)
    assert time.perf_counter() - started < 30


def test_a_message_too_long_for_a_datagram_is_refused():
    with pytest.raises(DriverError, match="too large"):
        v3.decode_message(b"\x30\x83\x01\x00\x00" + bytes(65536))


def test_password_to_key_hashes_one_mebibyte():
    assert v3.password_to_key(b"abc", "sha256") == hashlib.sha256((b"abc" * 349526)[:1048576]).digest()
    with pytest.raises(ValueError):
        v3.password_to_key(b"", "sha1")
