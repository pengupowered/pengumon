"""SNMPv3 messages (RFC 3412) with the User-based Security Model (RFC 3414) for the snmp driver's GET and GETBULK.

Security levels noAuthNoPriv, authNoPriv and authPriv. Authentication: HMAC-SHA-96 (usmHMACSHAAuthProtocol, RFC 3414)
and HMAC-SHA-2 (RFC 7860: SHA-224, -256, -384, -512, truncated to 16, 24, 32 and 48 bytes). Privacy: AES-128 in CFB
mode (RFC 3826), from the `cryptography` package. No MD5 and no DES: both are broken, and every agent that has
SHA-1 has them only as the older choice.

How a request goes (Usm.request):

- Discovery: the first request is preceded by an empty probe (noAuthNoPriv, no user, no engine id); the agent's
  Report (usmStatsUnknownEngineIDs) carries its snmpEngineID, and its boots and time, which are used as a first guess
  only: they are not authenticated.
- Keys: each password is turned into a key once (1 MiB of hashing, RFC 3414 A.2) and localized once per engine id;
  only the keys of the latest engine id are kept (a forged probe answer may name any number of them).
- Every authenticated answer is checked before anything in it is used: the msgID, the HMAC over the whole datagram,
  then the time window (RFC 3414 3.2.7: the agent's boots must not go back, and its time not more than 150 s behind
  what this client expects); only then is the scoped PDU decrypted and decoded, and the request id, the context and
  the security level matched. An answer that fails the HMAC or the window is dropped (it may be forged or replayed)
  and the request waits on, as is a datagram that does not decode, or an unauthenticated one whose scoped PDU does not
  (anyone can send one); if no good answer comes, the error says why the others were dropped.
- Reports: notInTimeWindows (authenticated: the agent's own boots and time) resynchronizes and the request is sent
  once more; unknownEngineIDs (the agent was replaced) discovers again, once; the others (unknown user, wrong digest,
  unsupported security level, decryption error, unknown context) become DriverError("auth") with what to check.
  net-snmp drops a request it cannot decrypt, or one for a context it does not have, without a word: an authPriv (or
  context) request that gets no answer from an agent that still answers the probe is reported as a likely wrong
  privacy password (or context name).

The decoder is as strict as snmp_ber's: lengths, sizes (engine id 5-32 bytes, user name at most 32, the
authentication and privacy parameters exactly their protocol's size) and integer ranges are checked, and anything
malformed raises DriverError("protocol") (Usm.unwrap drops what nothing vouches for instead). Error messages never
hold a password or a key.
"""

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple, Union

from cryptography.hazmat.decrepit.ciphers.modes import CFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from pengumon.capabilities import DriverError
from pengumon.drivers.snmp_ber import (
    GET_REQUEST, MAX_DATAGRAM, OCTET_STRING, REPORT, RESPONSE, SEQUENCE, Exchange, Oid, Response, _decode_int,
    _expect, _int, _malformed, _tlv, decode_pdu, encode_pdu,
)

VERSION_3 = 3
USM_MODEL = 3
FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 0x01, 0x02, 0x04
MSG_MAX_SIZE = 65507  # what this client accepts: the largest UDP payload over IPv4
MIN_MSG_MAX_SIZE = 484
MAX_INT = 0x7FFFFFFF
MIN_ENGINE_ID, MAX_ENGINE_ID = 5, 32  # SnmpEngineID (RFC 3411)
MAX_USER_NAME = 32  # SnmpAdminString (SIZE(1..32)) of usmUserName
MAX_CONTEXT_NAME = 32
MIN_PASSWORD = 8  # RFC 3414 11.2; net-snmp refuses shorter passwords, so an agent has none
TIME_WINDOW = 150  # seconds (RFC 3414 2.2.3)
SALT_SIZE = 8  # RFC 3826 msgPrivacyParameters
AES_KEY_SIZE = 16
PADDING_TOLERATED = 15  # bytes after a decrypted scoped PDU (an agent that pads to the block size)
PASSWORD_TO_KEY_BYTES = 1048576  # RFC 3414 A.2

SECURITY_LEVELS = ("noAuthNoPriv", "authNoPriv", "authPriv")


@dataclass(frozen=True)
class AuthProtocol:
    name: str  # as net-snmp names it (snmpget -a)
    hash: str  # hashlib name
    mac_length: int  # bytes of the HMAC sent (msgAuthenticationParameters)


AUTH_PROTOCOLS: Dict[str, AuthProtocol] = {p.name: p for p in (
    AuthProtocol("SHA", "sha1", 12),          # usmHMACSHAAuthProtocol, HMAC-SHA-96 (RFC 3414)
    AuthProtocol("SHA-224", "sha224", 16),    # usmHMAC128SHA224AuthProtocol (RFC 7860)
    AuthProtocol("SHA-256", "sha256", 24),    # usmHMAC192SHA256AuthProtocol
    AuthProtocol("SHA-384", "sha384", 32),    # usmHMAC256SHA384AuthProtocol
    AuthProtocol("SHA-512", "sha512", 48),    # usmHMAC384SHA512AuthProtocol
)}
PRIV_PROTOCOLS = ("AES",)  # usmAesCfb128Protocol (RFC 3826)

# usmStats (RFC 3414 5), snmpMPDStats (RFC 3412 5) and the context counters (RFC 3413 4.1.2): what a Report says.
USM_STATS = (1, 3, 6, 1, 6, 3, 15, 1, 1)
UNSUPPORTED_SEC_LEVELS = USM_STATS + (1, 0)
NOT_IN_TIME_WINDOWS = USM_STATS + (2, 0)
UNKNOWN_USER_NAMES = USM_STATS + (3, 0)
UNKNOWN_ENGINE_IDS = USM_STATS + (4, 0)
WRONG_DIGESTS = USM_STATS + (5, 0)
DECRYPTION_ERRORS = USM_STATS + (6, 0)
UNKNOWN_SECURITY_MODELS = (1, 3, 6, 1, 6, 3, 11, 2, 1, 1, 0)
INVALID_MSGS = (1, 3, 6, 1, 6, 3, 11, 2, 1, 2, 0)
UNKNOWN_PDU_HANDLERS = (1, 3, 6, 1, 6, 3, 11, 2, 1, 3, 0)
UNAVAILABLE_CONTEXTS = (1, 3, 6, 1, 6, 3, 12, 1, 4, 0)
UNKNOWN_CONTEXTS = (1, 3, 6, 1, 6, 3, 12, 1, 5, 0)


# --- keys and ciphers -----------------------------------------------------------------------------------------------

def password_to_key(password: bytes, hash_name: str) -> bytes:
    """Ku: the hash of the password repeated to 1 MiB (RFC 3414 A.2.2; RFC 7860 4 for SHA-2)."""
    if not password:
        raise ValueError("an empty password has no key")
    repeats, rest = divmod(PASSWORD_TO_KEY_BYTES, len(password))
    return hashlib.new(hash_name, password * repeats + password[:rest]).digest()


def localize_key(key: bytes, engine_id: bytes, hash_name: str) -> bytes:
    """Kul: the key localized to one engine, H(Ku || snmpEngineID || Ku) (RFC 3414 2.6)."""
    return hashlib.new(hash_name, key + engine_id + key).digest()


def hmac_of(key: bytes, message: bytes, protocol: AuthProtocol) -> bytes:
    """msgAuthenticationParameters for `message` (whose own are zeros): the HMAC, truncated."""
    return hmac.new(key, message, protocol.hash).digest()[:protocol.mac_length]


def aes_iv(boots: int, engine_time: int, salt: bytes) -> bytes:
    """The AES-CFB IV: snmpEngineBoots, snmpEngineTime (4 bytes each, big-endian) and the 8-byte salt (RFC 3826
    3.1.2.1); boots and time are the msgAuthoritativeEngineBoots and Time of the message it encrypts."""
    if len(salt) != SALT_SIZE:
        raise ValueError("the salt is 8 bytes")
    return boots.to_bytes(4, "big") + engine_time.to_bytes(4, "big") + salt


def aes_cfb(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    """AES-128 in CFB mode with a 128-bit segment (RFC 3826): the length stays the same, no padding."""
    cipher = Cipher(algorithms.AES(key[:AES_KEY_SIZE]), CFB(iv))
    worker = cipher.encryptor() if encrypt else cipher.decryptor()
    return worker.update(data) + worker.finalize()


# --- messages -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """An SNMPv3 message with USM security parameters, as received. `auth_at` is where the content of
    msgAuthenticationParameters starts in the datagram (zeroed to check the HMAC); `data` is the scoped PDU (its whole
    BER encoding) when the message is not encrypted, else the encrypted octets."""

    msg_id: int
    flags: int
    engine_id: bytes
    boots: int
    time: int
    user: bytes
    auth_params: bytes
    priv_params: bytes
    auth_at: int
    data: bytes


def encode_message(msg_id: int, flags: int, engine_id: bytes, boots: int, engine_time: int, user: bytes,
                   auth_params: bytes, priv_params: bytes, msg_data: bytes) -> bytes:
    """An SNMPv3 message; `msg_data` is the scoped PDU's encoding, or the encrypted PDU as an OCTET STRING."""
    header = _tlv(SEQUENCE, _int(msg_id) + _int(MSG_MAX_SIZE) + _tlv(OCTET_STRING, bytes([flags])) + _int(USM_MODEL))
    usm = _tlv(SEQUENCE, _tlv(OCTET_STRING, engine_id) + _int(boots) + _int(engine_time) + _tlv(OCTET_STRING, user)
               + _tlv(OCTET_STRING, auth_params) + _tlv(OCTET_STRING, priv_params))
    return _tlv(SEQUENCE, _int(VERSION_3) + header + _tlv(OCTET_STRING, usm) + msg_data)


def encode_scoped_pdu(context_engine_id: bytes, context_name: bytes, pdu: bytes) -> bytes:
    return _tlv(SEQUENCE, _tlv(OCTET_STRING, context_engine_id) + _tlv(OCTET_STRING, context_name) + pdu)


def _unsigned(data: bytes, pos: int, end: int, what: str, low: int = 0) -> Tuple[int, int]:
    value, pos = _decode_int(data, pos, end, what)
    if value < low:
        raise _malformed(f"{what} out of range")
    return value, pos


def _octets(data: bytes, pos: int, end: int, what: str, most: int) -> Tuple[bytes, int, int]:
    """(content, content start, next position) of an OCTET STRING of at most `most` bytes."""
    start, stop = _expect(data, pos, end, OCTET_STRING, what)
    if stop - start > most:
        raise _malformed(f"{what} too long")
    return bytes(data[start:stop]), start, stop


def decode_message(data: bytes) -> Message:
    """An SNMPv3 message with USM parameters. Raises DriverError("protocol") for anything else or anything malformed.
    Nothing is verified here: see Usm."""
    if len(data) > MAX_DATAGRAM:
        raise _malformed("datagram too large")
    start, end = _expect(data, 0, len(data), SEQUENCE, "message")
    if end != len(data):
        raise _malformed("bytes after the message")
    version, pos = _decode_int(data, start, end, "version")
    if version != VERSION_3:
        raise _malformed(f"SNMP version {version}, not v3")
    h_start, h_end = _expect(data, pos, end, SEQUENCE, "header")
    msg_id, pos = _unsigned(data, h_start, h_end, "msgID")
    _, pos = _unsigned(data, pos, h_end, "msgMaxSize", MIN_MSG_MAX_SIZE)
    flags_octets, _, pos = _octets(data, pos, h_end, "msgFlags", 1)
    if len(flags_octets) != 1:
        raise _malformed("msgFlags not one byte")
    flags = flags_octets[0]
    if flags & FLAG_PRIV and not flags & FLAG_AUTH:
        raise _malformed("privacy without authentication")
    model, pos = _decode_int(data, pos, h_end, "msgSecurityModel")
    if model != USM_MODEL:
        raise _malformed(f"security model {model}, not USM")
    if pos != h_end:
        raise _malformed("bytes after the header")
    s_start, s_end = _expect(data, h_end, end, OCTET_STRING, "security parameters")
    u_start, u_end = _expect(data, s_start, s_end, SEQUENCE, "USM parameters")
    if u_end != s_end:
        raise _malformed("bytes after the USM parameters")
    engine_id, _, pos = _octets(data, u_start, u_end, "msgAuthoritativeEngineID", MAX_ENGINE_ID)
    if len(engine_id) < MIN_ENGINE_ID:
        raise _malformed("msgAuthoritativeEngineID too short")
    boots, pos = _unsigned(data, pos, u_end, "msgAuthoritativeEngineBoots")
    engine_time, pos = _unsigned(data, pos, u_end, "msgAuthoritativeEngineTime")
    user, _, pos = _octets(data, pos, u_end, "msgUserName", MAX_USER_NAME)
    auth_params, auth_at, pos = _octets(data, pos, u_end, "msgAuthenticationParameters", 64)
    priv_params, _, pos = _octets(data, pos, u_end, "msgPrivacyParameters", 64)
    if pos != u_end:
        raise _malformed("bytes after the USM parameters")
    if (flags & FLAG_AUTH and not auth_params) or (flags & FLAG_PRIV and not priv_params):
        raise _malformed("security parameters missing for msgFlags")
    if flags & FLAG_PRIV:
        d_start, d_end = _expect(data, s_end, end, OCTET_STRING, "encryptedPDU")
        msg_data = bytes(data[d_start:d_end])
    else:
        _, d_end = _expect(data, s_end, end, SEQUENCE, "scopedPDU")
        msg_data = bytes(data[s_end:d_end])
    if d_end != end:
        raise _malformed("bytes after the scoped PDU")
    return Message(msg_id, flags, engine_id, boots, engine_time, user, auth_params, priv_params, auth_at, msg_data)


def decode_scoped_pdu(data: bytes, padding: int = 0) -> Tuple[bytes, bytes, Response]:
    """(contextEngineID, contextName, the Response or Report PDU) of a scoped PDU; at most `padding` bytes may follow
    it (a decrypted one)."""
    start, end = _expect(data, 0, len(data), SEQUENCE, "scopedPDU")
    if len(data) - end > padding:
        raise _malformed("bytes after the scoped PDU")
    context_engine_id, _, pos = _octets(data, start, end, "contextEngineID", MAX_ENGINE_ID)
    context_name, _, pos = _octets(data, pos, end, "contextName", MAX_CONTEXT_NAME)
    return context_engine_id, context_name, decode_pdu(data, pos, end, b"", (RESPONSE, REPORT))


# --- the security model ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    """A Report the agent answered with: what its first variable counts, and whether the Report was authenticated."""

    oid: Optional[Oid]
    authenticated: bool


def report_error(report: Report, *, user: str, level: str, auth: Optional[AuthProtocol], context: str) -> DriverError:
    """The DriverError for a Report that ends a request: what the agent refused and what to check."""
    oid = report.oid
    if oid == UNKNOWN_USER_NAMES:
        return DriverError("auth", f"the agent has no SNMPv3 user named {user!r} (unknownUserNames): check the user "
                                   "name")
    if oid == WRONG_DIGESTS:
        return DriverError("auth", "the agent refused the authentication (wrongDigests): wrong authentication "
                                   f"password, or the user does not use {auth.name if auth else 'this protocol'}")
    if oid == UNSUPPORTED_SEC_LEVELS:
        return DriverError("auth", f"the agent's user {user!r} does not support the security level {level} "
                                   "(unsupportedSecLevels): set the level the user was created with")
    if oid == DECRYPTION_ERRORS:
        return DriverError("auth", "the agent could not decrypt the request (decryptionErrors): wrong privacy "
                                   "password, or the user does not use AES")
    if oid == NOT_IN_TIME_WINDOWS:
        return DriverError("auth", "the agent keeps answering notInTimeWindows after a resynchronization")
    if oid in (UNKNOWN_CONTEXTS, UNAVAILABLE_CONTEXTS):
        return DriverError("auth", f"the agent has no context named {context!r}: check options.context_name")
    if oid == UNKNOWN_ENGINE_IDS:
        return DriverError("protocol", "the agent keeps reporting an unknown engine id after a new discovery")
    if oid in (UNKNOWN_SECURITY_MODELS, INVALID_MSGS, UNKNOWN_PDU_HANDLERS):
        return DriverError("protocol", f"the agent refused the SNMPv3 message (Report {'.'.join(map(str, oid))})")
    named = ".".join(map(str, oid)) if oid else "without a variable"
    return DriverError("protocol", f"the agent answered with a Report ({named})")


class Usm:
    """The SNMPv3 side of one SnmpClient (its `security`): one user, its security level and protocols, the keys,
    and what is known of the agent's engine (its id, and its boots and time as its authenticated messages said).

    `auth_password` / `priv_password` are needed by the levels that use them (the driver checks that first); they are
    kept only as keys. `clock` (monotonic seconds) times the agent's engine time; `random_bytes` makes the msgIDs and
    the first salt (tests replay recorded exchanges)."""

    def __init__(self, user: str, level: str, *, auth_protocol: str = "SHA", auth_password: Optional[str] = None,
                 priv_protocol: str = "AES", priv_password: Optional[str] = None, context_name: str = "",
                 clock: Callable[[], float] = time.monotonic, random_bytes: Callable[[int], bytes] = os.urandom):
        if level not in SECURITY_LEVELS:
            raise ValueError(f"unknown security level {level!r}")
        self.user = user.encode("utf-8")
        self.level = level
        self.auth = AUTH_PROTOCOLS[auth_protocol] if level != "noAuthNoPriv" else None
        if level == "authPriv" and priv_protocol not in PRIV_PROTOCOLS:
            raise ValueError(f"unknown privacy protocol {priv_protocol!r}")
        self.privacy = level == "authPriv"
        self.context_name = context_name.encode("utf-8")
        self._auth_ku = password_to_key(auth_password.encode("utf-8"), self.auth.hash) if self.auth else None
        self._priv_ku = (password_to_key(priv_password.encode("utf-8"), self.auth.hash) if self.privacy else None)
        self._keys: Dict[bytes, Tuple[bytes, bytes]] = {}  # engine id -> (auth key, priv key): the latest one only
        self._clock = clock
        self._random = random_bytes
        self._salt = int.from_bytes(random_bytes(SALT_SIZE), "big")
        self.engine_id: Optional[bytes] = None
        self._forget_time()
        self._dropped: Optional[str] = None  # why an answer was dropped during the current request
        self._dropped_kind = "auth"  # the DriverError kind if that ends it: "protocol" when it did not decode
        self.discoveries = 0  # probes answered, for tests and logs

    @property
    def flags(self) -> int:
        return FLAG_REPORTABLE | (FLAG_AUTH if self.auth else 0) | (FLAG_PRIV if self.privacy else 0)

    def _forget_time(self) -> None:
        # What the agent's authenticated messages said (RFC 3414 2.3: snmpEngineBoots, snmpEngineTime and
        # latestReceivedEngineTime, and when it was learnt); the probe's unauthenticated values are a guess for sending.
        self._boots, self._time, self._latest, self._at = 0, 0, 0, self._clock()
        self._synced = False
        self._guess: Optional[Tuple[int, int, float]] = None

    def keys(self, engine_id: bytes) -> Tuple[bytes, bytes]:
        """(authentication key, privacy key) localized to `engine_id`: computed once per engine id, and kept for the
        latest one only."""
        if engine_id not in self._keys:
            auth = localize_key(self._auth_ku, engine_id, self.auth.hash) if self.auth else b""
            priv = localize_key(self._priv_ku, engine_id, self.auth.hash)[:AES_KEY_SIZE] if self.privacy else b""
            self._keys = {engine_id: (auth, priv)}
        return self._keys[engine_id]

    def engine_clock(self) -> Tuple[int, int]:
        """(boots, time) of the agent's engine now, as this client knows it, for an outgoing message."""
        now = self._clock()
        if self._synced:
            return self._boots, min(MAX_INT, self._time + max(0, int(now - self._at)))
        if self._guess is not None:
            boots, engine_time, at = self._guess
            return boots, min(MAX_INT, engine_time + max(0, int(now - at)))
        return 0, 0

    def in_time_window(self, boots: int, engine_time: int) -> bool:
        """RFC 3414 3.2.7 b, for an authenticated message from the agent: takes its boots and time when they are newer
        than what is known, then whether the message is inside the window (boots not back, time at most 150 s
        behind)."""
        now = self._clock()
        if not self._synced or boots > self._boots or (boots == self._boots and engine_time > self._latest):
            self._boots, self._time, self._latest, self._at, self._synced = boots, engine_time, engine_time, now, True
        if self._boots == MAX_INT or boots < self._boots:
            return False
        expected = self._time + max(0, int(now - self._at))
        return not (boots == self._boots and engine_time < expected - TIME_WINDOW)

    # --- outgoing ---

    def _next_salt(self) -> bytes:
        self._salt = (self._salt + 1) & 0xFFFFFFFFFFFFFFFF
        return self._salt.to_bytes(SALT_SIZE, "big")

    def probe(self, msg_id: int, request_id: int) -> bytes:
        """The discovery probe: an empty GET, noAuthNoPriv, no user and no engine id (RFC 3414 4)."""
        scoped = encode_scoped_pdu(b"", b"", encode_pdu(GET_REQUEST, request_id, []))
        return encode_message(msg_id, FLAG_REPORTABLE, b"", 0, 0, b"", b"", b"", scoped)

    def wrap(self, msg_id: int, pdu: bytes) -> bytes:
        """The request message for `pdu`: scoped with the agent's engine id and the context, encrypted and
        authenticated as the security level says."""
        engine_id = self.engine_id
        boots, engine_time = self.engine_clock() if self.auth else (0, 0)
        auth_key, priv_key = self.keys(engine_id)
        msg_data = encode_scoped_pdu(engine_id, self.context_name, pdu)
        priv_params = b""
        if self.privacy:
            priv_params = self._next_salt()
            encrypted = aes_cfb(priv_key, aes_iv(boots, engine_time, priv_params), msg_data, encrypt=True)
            msg_data = _tlv(OCTET_STRING, encrypted)
        zeros = bytes(self.auth.mac_length) if self.auth else b""
        message = encode_message(msg_id, self.flags, engine_id, boots, engine_time, self.user, zeros, priv_params,
                                 msg_data)
        if not self.auth:
            return message
        at = decode_message(message).auth_at
        return message[:at] + hmac_of(auth_key, message, self.auth) + message[at + len(zeros):]

    # --- incoming ---

    def _drop(self, why: str) -> None:
        self._dropped, self._dropped_kind = why, "auth"
        return None

    def _drop_malformed(self, err: DriverError) -> None:
        """A datagram nothing vouches for that does not decode: dropped, so that anyone able to send one cannot fail
        the request. Recorded unless a more telling drop (a failed HMAC, the time window) already was."""
        if self._dropped is None:
            self._dropped, self._dropped_kind = f"it was malformed ({err.message})", "protocol"
        return None

    def unwrap_probe(self, data: bytes, msg_id: int) -> Optional[Tuple[Report, Message]]:
        """The probe's answer (an unauthenticated Report, with the engine id), or None when the datagram is not it (or
        does not decode)."""
        try:
            message = decode_message(data)
            if message.msg_id != msg_id or message.flags & FLAG_AUTH:
                return None
            _, _, pdu = decode_scoped_pdu(message.data)
        except DriverError as err:
            return self._drop_malformed(err)
        if pdu.pdu != REPORT:
            return None
        return Report(pdu.varbinds[0][0] if pdu.varbinds else None, False), message

    def unwrap(self, data: bytes, msg_id: int, request_id: int) -> Union[Response, Report, None]:
        """What a datagram answers to the request `msg_id`: the Response, a Report, or None when it is not the answer
        (another request's, or dropped: failed authentication, outside the time window, or a datagram that does not
        decode and that no HMAC vouches for). Raises DriverError("protocol") for an authenticated message whose content
        is malformed (the agent's own). Nothing in an authenticated message is used before its HMAC and time window are
        checked."""
        try:
            message = decode_message(data)
        except DriverError as err:
            return self._drop_malformed(err)
        if message.msg_id != msg_id:
            return None
        authenticated = bool(message.flags & FLAG_AUTH)
        if authenticated:
            if self.auth is None or message.user != self.user or message.engine_id != self.engine_id:
                return None  # not an answer to this user's request
            auth_key, priv_key = self.keys(message.engine_id)
            if len(message.auth_params) != self.auth.mac_length:
                return self._drop(f"its authentication code is not the size {self.auth.name} gives")
            at, size = message.auth_at, self.auth.mac_length
            zeroed = data[:at] + bytes(size) + data[at + size:]
            if not hmac.compare_digest(hmac_of(auth_key, zeroed, self.auth), message.auth_params):
                return self._drop("it failed authentication (wrong HMAC): forged, or the agent uses other keys")
            if not self.in_time_window(message.boots, message.time):
                return self._drop("it was outside the time window (replayed, or the agent's clock went back)")
            if message.flags & FLAG_PRIV:
                if not self.privacy:
                    return None
                if len(message.priv_params) != SALT_SIZE:
                    raise _malformed("msgPrivacyParameters not 8 bytes")
                plain = aes_cfb(priv_key, aes_iv(message.boots, message.time, message.priv_params), message.data,
                                encrypt=False)
                context_engine_id, context_name, pdu = decode_scoped_pdu(plain, PADDING_TOLERATED)
            else:
                context_engine_id, context_name, pdu = decode_scoped_pdu(message.data)
        else:
            try:
                context_engine_id, context_name, pdu = decode_scoped_pdu(message.data)
            except DriverError as err:
                return self._drop_malformed(err)
        if pdu.pdu == REPORT:
            oid = pdu.varbinds[0][0] if pdu.varbinds else None
            if oid == NOT_IN_TIME_WINDOWS and self.auth and not authenticated:
                return self._drop("an unauthenticated notInTimeWindows Report cannot set the agent's time")
            return Report(oid, authenticated)
        if message.flags & (FLAG_AUTH | FLAG_PRIV) != self.flags & (FLAG_AUTH | FLAG_PRIV):
            # a Response comes at the request's security level (RFC 3412 7.2.10): nothing vouches for this one
            return self._drop("it came at another security level than the request")
        if (pdu.request_id != request_id or message.user != self.user or message.engine_id != self.engine_id
                or context_engine_id != self.engine_id or context_name != self.context_name):
            return None
        return pdu

    # --- a request ---

    async def _discover(self, exchange: Exchange, next_id: Callable[[], int], *, tries: Optional[int] = None) -> None:
        msg_id = next_id()
        report, message = await exchange(self.probe(msg_id, next_id()), lambda data: self.unwrap_probe(data, msg_id),
                                         tries=tries)
        if report.oid != UNKNOWN_ENGINE_IDS:
            raise report_error(report, user=self.user.decode("utf-8", "replace"), level=self.level, auth=self.auth,
                               context=self.context_name.decode("utf-8", "replace"))
        self.discoveries += 1
        if message.engine_id != self.engine_id:
            self.engine_id = message.engine_id
            self._forget_time()
        self._guess = (message.boots, message.time, self._clock())

    async def request(self, exchange: Exchange, next_id: Callable[[], int], pdu: int, oids: Sequence[Oid],
                      a: int = 0, b: int = 0) -> Response:
        """One GET or GETBULK through `exchange` (SnmpClient's), discovering the engine first when it is not known.
        Raises DriverError for a Report that ends it, or when no good answer came (saying why others were dropped)."""
        resynced = rediscovered = False
        while True:
            if self.engine_id is None:
                await self._discover(exchange, next_id)
            msg_id, request_id = next_id(), next_id()
            self._dropped, self._dropped_kind = None, "auth"
            try:
                answer = await exchange(self.wrap(msg_id, encode_pdu(pdu, request_id, oids, a, b)),
                                        lambda data: self.unwrap(data, msg_id, request_id))
            except DriverError as err:
                if err.kind != "timeout":
                    raise
                raise await self._no_answer(err, exchange, next_id) from None
            if isinstance(answer, Response):
                return answer
            if answer.oid == NOT_IN_TIME_WINDOWS and answer.authenticated and not resynced:
                resynced = True  # (unwrap took the agent's boots and time from it)
                continue
            if answer.oid == UNKNOWN_ENGINE_IDS and not rediscovered:
                rediscovered = True
                self.engine_id = None
                continue
            raise report_error(answer, user=self.user.decode("utf-8", "replace"), level=self.level, auth=self.auth,
                               context=self.context_name.decode("utf-8", "replace"))

    async def _no_answer(self, timeout: DriverError, exchange: Exchange, next_id: Callable[[], int]) -> DriverError:
        """The error for a request no good answer came to."""
        if self._dropped is not None:
            if "time window" in self._dropped:
                self.engine_id = None  # the next request starts over (an agent reset to boots 1 keeps its id)
            return DriverError(self._dropped_kind, f"the agent's answer was dropped: {self._dropped}")
        # net-snmp drops, without an answer, a request it cannot decrypt and one for a context it does not have
        causes = (["a wrong privacy password"] if self.privacy else []) + (
            [f"a context the agent does not have ({self.context_name.decode('utf-8', 'replace')!r}, "
             "options.context_name)"] if self.context_name else [])
        if not causes:
            return timeout
        try:
            await self._discover(exchange, next_id, tries=1)
        except DriverError:
            return timeout
        return DriverError("auth", f"the agent answers its discovery probe, but not to these requests: most likely "
                                   f"{' or '.join(causes)} (net-snmp drops those without an answer)")
