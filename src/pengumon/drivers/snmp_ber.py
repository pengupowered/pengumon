"""SNMPv2c messages (RFC 3416) in BER, and a UDP client that sends GET and GETBULK requests and walks tables.

Only what the snmp driver needs, and strict: every length is checked against the bytes that are actually there,
only the definite length form of at most four bytes is read, integers and OIDs have bounded sizes, and anything
malformed raises DriverError("protocol"). A datagram is at most 64 KiB, so decoding one never allocates more than a
small multiple of that. No SNMPv1 or v3.
"""

import asyncio
import os
import socket
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from pengumon.capabilities import DriverError

Oid = Tuple[int, ...]

# Universal and SNMP application tags.
INTEGER, OCTET_STRING, NULL, OBJECT_IDENTIFIER, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
IP_ADDRESS, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW = 0x80, 0x81, 0x82
# PDU tags.
GET_REQUEST, GET_NEXT_REQUEST, RESPONSE, GET_BULK_REQUEST = 0xA0, 0xA1, 0xA2, 0xA5

VERSION_2C = 1
MAX_DATAGRAM = 65535
MAX_OID_ARCS = 128  # RFC 2578: an OID has at most 128 sub-identifiers
MAX_ARC = 0xFFFFFFFF
MAX_VARBINDS = 2048
ERROR_STATUS = {1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess",
                7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
                12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed", 15: "undoFailed",
                16: "authorizationError", 17: "notWritable", 18: "inconsistentName"}
TOO_BIG = 1

_UNSIGNED_MAX = {COUNTER32: 0xFFFFFFFF, GAUGE32: 0xFFFFFFFF, TIMETICKS: 0xFFFFFFFF, COUNTER64: 0xFFFFFFFFFFFFFFFF}


def _malformed(what: str) -> DriverError:
    return DriverError("protocol", f"malformed SNMP answer: {what}")


class AgentError(DriverError):
    """The agent answered with an error-status (tooBig, genErr, ...)."""

    def __init__(self, status: int):
        super().__init__("protocol", f"the agent answered {ERROR_STATUS.get(status, 'error')} ({status})")
        self.status = status


@dataclass(frozen=True)
class Value:
    """A variable's value: its BER tag and the decoded content (int for the integer types, bytes for strings and
    addresses, an Oid, or None for NULL and the exceptions noSuchObject / noSuchInstance / endOfMibView)."""

    tag: int
    value: Any = None

    @property
    def missing(self) -> bool:
        return self.tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW)

    def number(self, *tags: int) -> Optional[int]:
        """The value when it is an integer of one of `tags` (any integer type when none are given), else None."""
        if isinstance(self.value, int) and self.tag in (tags or (INTEGER, COUNTER32, GAUGE32, TIMETICKS, COUNTER64)):
            return self.value
        return None

    def text(self) -> Optional[str]:
        return self.value.decode("utf-8", "replace") if self.tag == OCTET_STRING else None


@dataclass(frozen=True)
class Response:
    request_id: int
    error_status: int
    error_index: int
    varbinds: Tuple[Tuple[Oid, Value], ...]
    community: bytes


# --- encoding -------------------------------------------------------------------------------------------------------

def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _length(len(content)) + content


def _int(value: int) -> bytes:
    return _tlv(INTEGER, value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True))


def encode_oid(oid: Sequence[int]) -> bytes:
    if len(oid) < 2 or oid[0] > 2 or (oid[0] < 2 and oid[1] > 39) or not all(0 <= a <= MAX_ARC for a in oid):
        raise ValueError(f"not an OID: {oid!r}")
    out = bytearray()
    for arc in (oid[0] * 40 + oid[1], *oid[2:]):
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out += bytes(reversed(chunk))
    return _tlv(OBJECT_IDENTIFIER, bytes(out))


def encode_request(pdu: int, community: bytes, request_id: int, oids: Sequence[Oid], a: int = 0, b: int = 0) -> bytes:
    """A v2c request message. `a`/`b` are error-status/index (0) for GET and GETNEXT, non-repeaters and
    max-repetitions for GETBULK."""
    varbinds = b"".join(_tlv(SEQUENCE, encode_oid(oid) + _tlv(NULL, b"")) for oid in oids)
    body = _int(request_id) + _int(a) + _int(b) + _tlv(SEQUENCE, varbinds)
    return _tlv(SEQUENCE, _int(VERSION_2C) + _tlv(OCTET_STRING, community) + _tlv(pdu, body))


# --- decoding -------------------------------------------------------------------------------------------------------

def _read(data: bytes, pos: int, end: int) -> Tuple[int, int, int]:
    """(tag, content start, content end) of the TLV at `pos`, which must lie within `end`."""
    if pos + 2 > end:
        raise _malformed("truncated header")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise _malformed("multi-byte tag")
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4:
            raise _malformed("indefinite or oversized length")
        if pos + count > end:
            raise _malformed("truncated length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if length > end - pos:
        raise _malformed("length beyond the data")
    return tag, pos, pos + length


def _expect(data: bytes, pos: int, end: int, tag: int, what: str) -> Tuple[int, int]:
    got, start, stop = _read(data, pos, end)
    if got != tag:
        raise _malformed(f"{what}: tag 0x{got:02x}, expected 0x{tag:02x}")
    return start, stop


def _integer(content: bytes, signed: bool) -> int:
    if not content or len(content) > 9:  # (leading 0x00 / 0xFF bytes that are not needed are tolerated)
        raise _malformed("integer of a bad size")
    value = int.from_bytes(content, "big", signed=True)
    if not signed and value < 0:
        raise _malformed("negative unsigned value")
    return value


def _decode_int(data: bytes, pos: int, end: int, what: str) -> Tuple[int, int]:
    start, stop = _expect(data, pos, end, INTEGER, what)
    value = _integer(data[start:stop], True)
    if not -0x80000000 <= value <= 0x7FFFFFFF:
        raise _malformed(f"{what} out of range")
    return value, stop


def decode_oid(content: bytes) -> Oid:
    if not content or len(content) > MAX_OID_ARCS * 5:
        raise _malformed("OID of a bad size")
    arcs: List[int] = []
    arc, started = 0, False
    for byte in content:
        started = True
        arc = (arc << 7) | (byte & 0x7F)
        if arc > MAX_ARC and arcs:  # the first one also holds the second arc (x * 40 + y)
            raise _malformed("OID arc too large")
        if not byte & 0x80:
            arcs.append(arc)
            arc, started = 0, False
            if len(arcs) > MAX_OID_ARCS:
                raise _malformed("OID too long")
    if started:
        raise _malformed("OID ends inside an arc")
    first = arcs[0]
    if first > 2 * 40 + MAX_ARC:
        raise _malformed("OID arc too large")
    head = (first // 40, first % 40) if first < 80 else (2, first - 80)
    return head + tuple(arcs[1:])


def _value(tag: int, content: bytes) -> Value:
    if tag == INTEGER:
        value = _integer(content, True)
        if not -0x80000000 <= value <= 0x7FFFFFFF:
            raise _malformed("INTEGER out of range")
        return Value(tag, value)
    if tag in _UNSIGNED_MAX:
        value = _integer(content, False)
        if value > _UNSIGNED_MAX[tag]:
            raise _malformed("counter out of range")
        return Value(tag, value)
    if tag == OBJECT_IDENTIFIER:
        return Value(tag, decode_oid(content))
    if tag in (NULL, NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW):
        if content:
            raise _malformed("NULL with content")
        return Value(tag, None)
    if tag == IP_ADDRESS and len(content) != 4:
        raise _malformed("IpAddress not four bytes")
    if tag & 0x20:  # constructed: no SNMP value is
        raise _malformed(f"constructed value 0x{tag:02x}")
    return Value(tag, bytes(content))  # OCTET STRING, IpAddress, Opaque, and types this client does not interpret


def decode_response(data: bytes) -> Response:
    """The Response PDU of a v2c message. Raises DriverError("protocol") for anything else or anything malformed."""
    if len(data) > MAX_DATAGRAM:
        raise _malformed("datagram too large")
    start, end = _expect(data, 0, len(data), SEQUENCE, "message")
    if end != len(data):
        raise _malformed("bytes after the message")
    version, pos = _decode_int(data, start, end, "version")
    if version != VERSION_2C:
        raise _malformed(f"SNMP version {version}, not v2c")
    c_start, c_end = _expect(data, pos, end, OCTET_STRING, "community")
    community = bytes(data[c_start:c_end])
    tag, p_start, p_end = _read(data, c_end, end)
    if p_end != end:
        raise _malformed("bytes after the PDU")
    if tag != RESPONSE:
        raise _malformed(f"PDU 0x{tag:02x} is not a Response")
    request_id, pos = _decode_int(data, p_start, p_end, "request-id")
    error_status, pos = _decode_int(data, pos, p_end, "error-status")
    error_index, pos = _decode_int(data, pos, p_end, "error-index")
    vb_start, vb_end = _expect(data, pos, p_end, SEQUENCE, "varbind list")
    if vb_end != p_end:
        raise _malformed("bytes after the varbind list")
    varbinds: List[Tuple[Oid, Value]] = []
    pos = vb_start
    while pos < vb_end:
        if len(varbinds) >= MAX_VARBINDS:
            raise _malformed("too many variables")
        s, e = _expect(data, pos, vb_end, SEQUENCE, "varbind")
        o_start, o_end = _expect(data, s, e, OBJECT_IDENTIFIER, "name")
        oid = decode_oid(data[o_start:o_end])
        v_tag, v_start, v_end = _read(data, o_end, e)
        if v_end != e:
            raise _malformed("bytes after a value")
        varbinds.append((oid, _value(v_tag, data[v_start:v_end])))
        pos = e
    return Response(request_id, error_status, error_index, tuple(varbinds), community)


# --- transport ------------------------------------------------------------------------------------------------------

class _Protocol(asyncio.DatagramProtocol):
    """Hands each datagram to the request waiting for it (one at a time per client)."""

    def __init__(self):
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.waiting: Optional[asyncio.Future] = None
        self.request_id: Optional[int] = None
        self.community = b""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if self.waiting is None or self.waiting.done():
            return  # a late answer to a request that timed out
        try:
            response = decode_response(data)
        except DriverError as err:
            self.waiting.set_exception(err)
            return
        if response.request_id != self.request_id or response.community != self.community:
            return  # an answer to an earlier try, or not for us
        self.waiting.set_result(response)

    def error_received(self, exc: Exception) -> None:
        if self.waiting is not None and not self.waiting.done():
            self.waiting.set_exception(DriverError("unreachable", f"{type(exc).__name__}: {exc}", transport=True))

    def connection_lost(self, exc) -> None:
        if self.waiting is not None and not self.waiting.done():
            self.waiting.set_exception(DriverError("unreachable", "the UDP socket closed", transport=True))


class SnmpClient:
    """GET, GETBULK and walks against one agent. Requests go one at a time; each try waits `timeout` seconds and
    is sent `retries` more times before the request fails with DriverError("timeout"). The socket is opened on
    first use and kept."""

    def __init__(self, host: str, port: int, community: str, *, timeout: float = 2.0, retries: int = 1,
                 max_repetitions: int = 25):
        self.host = host
        self.port = port
        self.community = community.encode("utf-8")
        self.timeout = timeout
        self.retries = retries
        self.max_repetitions = max_repetitions
        self._protocol: Optional[_Protocol] = None
        self._lock = asyncio.Lock()
        self.requests = 0  # sent datagrams, for tests and logs

    async def _endpoint(self) -> _Protocol:
        if self._protocol is not None and self._protocol.transport is not None and \
                not self._protocol.transport.is_closing():
            return self._protocol
        loop = asyncio.get_running_loop()
        try:
            _, protocol = await loop.create_datagram_endpoint(_Protocol, remote_addr=(self.host, self.port))
        except socket.gaierror as e:
            raise DriverError("unreachable", f"cannot resolve {self.host}: {e}", transport=True) from None
        except OSError as e:
            raise DriverError("unreachable", f"cannot reach {self.host}:{self.port}: {e}", transport=True) from None
        self._protocol = protocol
        return protocol

    async def request(self, pdu: int, oids: Sequence[Oid], a: int = 0, b: int = 0) -> Response:
        async with self._lock:
            protocol = await self._endpoint()
            request_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
            message = encode_request(pdu, self.community, request_id, oids, a, b)
            loop = asyncio.get_running_loop()
            for _ in range(self.retries + 1):
                future = loop.create_future()
                protocol.waiting, protocol.request_id, protocol.community = future, request_id, self.community
                protocol.transport.sendto(message)
                self.requests += 1
                try:
                    response = await asyncio.wait_for(future, self.timeout)
                except asyncio.TimeoutError:
                    continue
                finally:
                    protocol.waiting = None
                if response.error_status:
                    raise AgentError(response.error_status)
                return response
        raise DriverError("timeout", f"no SNMP answer from {self.host}:{self.port} after {self.retries + 1} tries "
                                     "(SNMP off, a firewall, or a wrong community string: agents ignore those)",
                          transport=True)

    async def get(self, oids: Sequence[Oid]) -> List[Tuple[Oid, Value]]:
        """The values of `oids`, in order (a missing one is noSuchObject / noSuchInstance)."""
        response = await self.request(GET_REQUEST, oids)
        if len(response.varbinds) != len(oids) or any(o != want for (o, _), want in zip(response.varbinds, oids)):
            raise _malformed("the variables do not match the request")
        return list(response.varbinds)

    async def walk(self, prefix: Oid, max_rows: int = 1024) -> List[Tuple[Oid, Value]]:
        """Every variable under `prefix` (GETBULK), in order; at most `max_rows`. An agent whose answers do not
        move forward is a protocol error, so a walk always ends."""
        rows: List[Tuple[Oid, Value]] = []
        last = prefix
        repetitions = self.max_repetitions
        while len(rows) < max_rows:
            try:
                response = await self.request(GET_BULK_REQUEST, [last], 0, min(repetitions, max_rows - len(rows)))
            except AgentError as err:
                if err.status == TOO_BIG and repetitions > 1:
                    repetitions = max(1, repetitions // 2)
                    continue
                raise
            if not response.varbinds:
                break
            for oid, value in response.varbinds:
                if value.tag == END_OF_MIB_VIEW or oid[:len(prefix)] != prefix:
                    return rows
                if oid <= last:
                    raise _malformed("a walk went backwards")
                rows.append((oid, value))
                last = oid
                if len(rows) >= max_rows:
                    break
        return rows

    async def aclose(self) -> None:
        if self._protocol is not None and self._protocol.transport is not None:
            self._protocol.transport.close()
        self._protocol = None
