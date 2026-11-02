"""Windows ICMP backend: IcmpSendEcho2 from iphlpapi.dll, called through ctypes.

`ping.exe` and `tracert.exe` use this API and run for standard users; raw sockets would need Administrators. The
call is synchronous (no Event, no ApcRoutine), so each probe occupies one worker thread of a small pool for up to
`timeout`. asyncio's Windows loop (Proactor) has no `add_reader`, which rules out a socket-based design anyway.

Design decisions:
- One ICMP handle per worker thread (created lazily in that thread). Microsoft does not document whether one handle
  may be used by several threads at once, so no sharing is assumed. `close()` waits for the workers to finish
  (bounded by the longest timeout in flight), then closes the handles.
- The API matches reply to request itself (id and sequence are chosen by the OS), so other processes' pings can
  never be mistaken for ours.
- Only the first four fields of ICMP_ECHO_REPLY are read (Address, Status, RoundTripTime, DataSize). They sit at the
  same offsets in the native and the 32-bit-pointer layout, so the parse does not depend on pointer width. The
  buffer is sized for the larger layout.
- rtt: RoundTripTime is whole milliseconds, which hides every LAN hop. The call is also timed with perf_counter_ns
  from just before the call to just after it (queueing in the pool is not counted). The smaller non-zero of the two
  is reported: the OS value is a floor on what really happened, and the stopwatch also includes thread wake-up and
  call overhead, so neither is trusted alone. `resolution_ms` stays 1.0 because that is what the OS promises; the
  float is better than that on a quiet machine but not guaranteed.
- Calls beyond `workers` in flight wait in the executor's queue, and their timeout starts when they reach a worker.
- Two pools: the prober's probes (TTL 64) run on `workers` threads, probes with a lower TTL (traceroute: 45 requests
  at once) on `trace_workers` other threads, so a trace queues behind itself and never delays a prober round.
  mtr.py lets one trace run at a time app-wide, so the trace pool is also the bound on all trace threads.
- close() does not wait for the workers: it refuses new probes, closes the handles of idle threads at once, and each
  busy thread closes its own handle when its call returns. Probes in flight or queued fail with OSError.

IPv6: `Icmp6CreateFile` and `Icmp6SendEcho2` (source in6addr_any, destination a sockaddr_in6 with its scope id), the
same pools, threads, buffer and close rules, with a second handle per thread. The reply is an ICMPV6_ECHO_REPLY:
Address is an IPV6_ADDRESS_EX, which ipexport.h packs to 1 byte (port 0, flowinfo 2, the address at 6..22, scope id
22..26: 26 bytes), so Status sits at 28 and RoundTripTime at 32 in either bitness (no pointers). The IPv6 statuses
share the IPv4 numbers where they mean the same (IP_HOP_LIMIT_EXCEEDED is 11013, IP_DEST_NO_ROUTE 11002, ...) and add
IP_DEST_UNREACHABLE 11040, IP_TIME_EXCEEDED 11041 and IP_DEST_SCOPE_MISMATCH 11045; IP_PACKET_TOO_BIG (11009) is
counted as unreachable, as on the other OSes.
"""

import asyncio
import ctypes
import errno
import ipaddress
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pengumon.engine.icmp_base import IcmpUnavailable, ProbeResult
from pengumon.engine.icmp_packet import address_key, address_text, ip_literal, sockaddr

log = logging.getLogger(__name__)

# IP_STATUS values from ipexport.h
IP_SUCCESS = 0
IP_BUF_TOO_SMALL = 11001
IP_DEST_NET_UNREACHABLE = 11002
IP_DEST_HOST_UNREACHABLE = 11003
IP_DEST_PROT_UNREACHABLE = 11004
IP_DEST_PORT_UNREACHABLE = 11005
IP_REQ_TIMED_OUT = 11010
IP_TTL_EXPIRED_TRANSIT = 11013  # IP_HOP_LIMIT_EXCEEDED for IPv6
IP_TTL_EXPIRED_REASSEM = 11014  # IP_REASSEMBLY_TIME_EXCEEDED
IP_PACKET_TOO_BIG = 11009
IP_DEST_UNREACHABLE = 11040  # IPv6 only
IP_TIME_EXCEEDED = 11041  # IPv6 only
IP_DEST_SCOPE_MISMATCH = 11045  # IPv6 only

_UNREACHABLE = frozenset(
    {IP_DEST_NET_UNREACHABLE, IP_DEST_HOST_UNREACHABLE, IP_DEST_PROT_UNREACHABLE, IP_DEST_PORT_UNREACHABLE}
)
_TTL_EXCEEDED = frozenset({IP_TTL_EXPIRED_TRANSIT, IP_TTL_EXPIRED_REASSEM})
_UNREACHABLE6 = _UNREACHABLE | {IP_DEST_UNREACHABLE, IP_DEST_SCOPE_MISMATCH, IP_PACKET_TOO_BIG}
_TTL_EXCEEDED6 = _TTL_EXCEEDED | {IP_TIME_EXCEEDED}
AF_INET6_WINDOWS = 23  # ws2def.h; sockaddr_in6.sin6_family on Windows

# Win32 errors that mean "cannot send from here" (the interface raises OSError for those)
ERROR_NETWORK_UNREACHABLE = 1231
ERROR_HOST_UNREACHABLE = 1232

PAYLOAD = b"pengumon-icmp-probe-32-bytes...."  # 32 bytes, like ping.exe
assert len(PAYLOAD) == 32
_ERROR_ROOM = 8  # the docs: room for the 8 bytes of an ICMP error message
_IO_STATUS_BLOCK_ROOM = 16  # two pointers on 64-bit
MAX_WORKERS = 32
TRACE_WORKERS = 24  # a 45-request trace runs in two waves
PROBE_TTL = 64  # the prober's TTL; a lower one is a trace probe
_MAX_TIMEOUT_MS = 60_000


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class ICMP_ECHO_REPLY(ctypes.Structure):
    """Layout for a process of the same bitness as Windows (the case here)."""

    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint32),
        ("DataSize", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("Data", ctypes.c_void_p),
        ("Options", IP_OPTION_INFORMATION),
    ]


class _IP_OPTION_INFORMATION32(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_uint32),
    ]


class ICMP_ECHO_REPLY32(ctypes.Structure):
    """Same record with 32-bit pointers (what a 32-bit caller gets); used only to size the buffer safely."""

    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint32),
        ("DataSize", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("Data", ctypes.c_uint32),
        ("Options", _IP_OPTION_INFORMATION32),
    ]


class IPV6_ADDRESS_EX(ctypes.Structure):
    _pack_ = 1  # ipexport.h wraps it in pshpack1.h (Wine's copy: #pragma pack(push,1))
    _fields_ = [
        ("sin6_port", ctypes.c_uint16),
        ("sin6_flowinfo", ctypes.c_uint32),
        ("sin6_addr", ctypes.c_uint8 * 16),  # USHORT[8] in the header: the address bytes in network order
        ("sin6_scope_id", ctypes.c_uint32),
    ]


class ICMPV6_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", IPV6_ADDRESS_EX),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint),
    ]


class SOCKADDR_IN6(ctypes.Structure):
    _fields_ = [
        ("sin6_family", ctypes.c_uint16),
        ("sin6_port", ctypes.c_uint16),
        ("sin6_flowinfo", ctypes.c_uint32),
        ("sin6_addr", ctypes.c_uint8 * 16),
        ("sin6_scope_id", ctypes.c_uint32),
    ]


# The documented minimum, plus room for the IP options a reply may carry (up to 40 bytes; IcmpSendEcho2 answers
# IP_BUF_TOO_SMALL, error 122, when they don't fit) and then some: 1.5 KB, as most ping implementations allow.
_MINIMUM_REPLY_BUFFER = (
    max(ctypes.sizeof(ICMP_ECHO_REPLY), ctypes.sizeof(ICMP_ECHO_REPLY32), ctypes.sizeof(ICMPV6_ECHO_REPLY))
    + len(PAYLOAD)
    + _ERROR_ROOM
    + _IO_STATUS_BLOCK_ROOM
)
REPLY_BUFFER_SIZE = max(1536, _MINIMUM_REPLY_BUFFER)


def parse_reply(buf: Any) -> tuple[str, int, int]:
    """(responder address, Status, RoundTripTime ms) from the first ICMP_ECHO_REPLY in `buf` (bytes-like)."""
    raw = bytes(buf)[:16]
    if len(raw) < 16:
        raise ValueError("reply buffer too short")
    address = str(ipaddress.IPv4Address(raw[0:4]))  # network byte order in memory, whatever the CPU
    status = int.from_bytes(raw[4:8], sys.byteorder)
    rtt = int.from_bytes(raw[8:12], sys.byteorder)
    return address, status, rtt


def parse_reply6(buf: Any) -> tuple[str, int, int]:
    """(responder address, Status, RoundTripTime ms) from the ICMPV6_ECHO_REPLY at the start of `buf` (bytes-like):
    the packed IPV6_ADDRESS_EX (address at 6..22, scope id at 22..26), Status at 28, RoundTripTime at 32."""
    raw = bytes(buf)[:36]
    if len(raw) < 36:
        raise ValueError("reply buffer too short")
    scope_id = int.from_bytes(raw[22:26], sys.byteorder)
    address = address_text(raw[6:22], scope_id)
    status = int.from_bytes(raw[28:32], sys.byteorder)
    rtt = int.from_bytes(raw[32:36], sys.byteorder)
    return address, status, rtt


def sockaddr_in6(host: str = "::", scope_id: int = 0) -> SOCKADDR_IN6:
    """A Windows struct sockaddr_in6 for `host` (an IPv6 literal without its zone)."""
    sa = SOCKADDR_IN6(sin6_family=AF_INET6_WINDOWS, sin6_scope_id=scope_id)
    sa.sin6_addr[:] = list(ipaddress.IPv6Address(host).packed)
    return sa


def _load_api() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise IcmpUnavailable("The Windows ICMP backend only works on Windows.")
    try:
        from ctypes import wintypes

        dll = ctypes.WinDLL("iphlpapi", use_last_error=True)  # type: ignore[attr-defined]
        dll.IcmpCreateFile.restype = wintypes.HANDLE
        dll.IcmpCreateFile.argtypes = []
        dll.IcmpCloseHandle.restype = wintypes.BOOL
        dll.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        dll.IcmpSendEcho2.restype = wintypes.DWORD
        dll.IcmpSendEcho2.argtypes = [
            wintypes.HANDLE,  # IcmpHandle
            wintypes.HANDLE,  # Event
            ctypes.c_void_p,  # ApcRoutine
            ctypes.c_void_p,  # ApcContext
            wintypes.DWORD,  # DestinationAddress
            ctypes.c_void_p,  # RequestData
            wintypes.WORD,  # RequestSize
            ctypes.c_void_p,  # RequestOptions
            ctypes.c_void_p,  # ReplyBuffer
            wintypes.DWORD,  # ReplySize
            wintypes.DWORD,  # Timeout
        ]
        try:  # IPv6: in every Windows since Vista; without it only IPv6 probes fail
            dll.Icmp6CreateFile.restype = wintypes.HANDLE
            dll.Icmp6CreateFile.argtypes = []
            dll.Icmp6SendEcho2.restype = wintypes.DWORD
            dll.Icmp6SendEcho2.argtypes = [
                wintypes.HANDLE,  # IcmpHandle
                wintypes.HANDLE,  # Event
                ctypes.c_void_p,  # ApcRoutine
                ctypes.c_void_p,  # ApcContext
                ctypes.c_void_p,  # SourceAddress (struct sockaddr_in6 *)
                ctypes.c_void_p,  # DestinationAddress (struct sockaddr_in6 *)
                ctypes.c_void_p,  # RequestData
                wintypes.WORD,  # RequestSize
                ctypes.c_void_p,  # RequestOptions
                ctypes.c_void_p,  # ReplyBuffer
                wintypes.DWORD,  # ReplySize
                wintypes.DWORD,  # Timeout
            ]
        except AttributeError:
            pass
    except (OSError, AttributeError) as e:
        raise IcmpUnavailable(f"iphlpapi.dll does not provide the ICMP API: {e}") from e
    return dll, ctypes.get_last_error  # type: ignore[attr-defined]


class WindowsIcmpBackend:
    name = "windows-icmpapi"
    resolution_ms = 1.0

    def __init__(self, *, workers: int = MAX_WORKERS, trace_workers: int = TRACE_WORKERS, _dll: Any = None, _get_last_error: Any = None):
        """`_dll` and `_get_last_error` are for tests: an object with IcmpCreateFile / IcmpSendEcho2 /
        IcmpCloseHandle, and a function returning the thread's last Win32 error."""
        if not 1 <= workers <= 256 or not 1 <= trace_workers <= 256:
            raise ValueError("workers must be 1..256")
        if _dll is None:
            _dll, _get_last_error = _load_api()
        self._dll = _dll
        self._last_error = _get_last_error
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pengumon-icmp")
        self._trace_pool = ThreadPoolExecutor(max_workers=trace_workers, thread_name_prefix="pengumon-icmp-trace")
        self._local = threading.local()
        self._busy: set[Any] = set()  # handles of threads inside IcmpSendEcho2
        self._handles: list[Any] = []
        self._lock = threading.Lock()
        self._closed = False
        self._logged: set[int] = set()
        # Fail now (not on the first probe) if the stack has no ICMP support.
        probe_handle = self._dll.IcmpCreateFile()
        if not self._valid(probe_handle):
            err = self._last_error()
            raise IcmpUnavailable(f"IcmpCreateFile failed (Win32 error {err}).")
        self._dll.IcmpCloseHandle(probe_handle)

    @staticmethod
    def _valid(handle: Any) -> bool:
        return handle not in (None, 0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)

    def _handle(self, version: int = 4) -> Any:
        attr = "handle" if version == 4 else "handle6"
        h = getattr(self._local, attr, None)
        if h is None:
            if version == 4:
                h = self._dll.IcmpCreateFile()
                if not self._valid(h):
                    raise OSError(self._last_error(), "IcmpCreateFile failed")
            else:
                create = getattr(self._dll, "Icmp6CreateFile", None)
                h = create() if create is not None else None
                if not self._valid(h):
                    err = self._last_error() if create is not None else 0
                    # no IPv6 stack here: what the prober reads as "IPv6 unavailable on this host"
                    raise OSError(errno.EAFNOSUPPORT, f"Icmp6CreateFile failed (Win32 error {err}): no IPv6 here")
            with self._lock:
                if self._closed:
                    self._dll.IcmpCloseHandle(h)
                    raise OSError("backend is closed")
                self._handles.append(h)
            setattr(self._local, attr, h)
        return h

    async def probe(self, address: str, *, timeout: float, ttl: int = 64) -> ProbeResult:
        if not isinstance(address, str):
            raise ValueError("address must be an IP literal string")
        text, version = ip_literal(address)  # ValueError for names
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 255:
            raise ValueError("ttl must be 1..255")
        timeout_ms = max(1, min(_MAX_TIMEOUT_MS, int(timeout * 1000 + 0.999)))
        if self._closed:
            raise OSError("backend is closed")
        if version == 4:
            # IPAddr is the network-order bytes read as a ULONG
            dest: Any = int.from_bytes(ipaddress.IPv4Address(text).packed, sys.byteorder)
        else:
            host, _port, _flow, scope_id = sockaddr(text, 6)  # OSError for a zone that names no interface
            dest = (host, scope_id)
        loop = asyncio.get_running_loop()
        pool = self._pool if ttl >= PROBE_TTL else self._trace_pool
        return await loop.run_in_executor(pool, self._probe_blocking, dest, ttl, timeout_ms)

    def _probe_blocking(self, dest: Any, ttl: int, timeout_ms: int) -> ProbeResult:
        version = 4 if isinstance(dest, int) else 6
        handle = self._handle(version)  # OSError if the backend is closed
        with self._lock:
            if self._closed:
                raise OSError("backend is closed")
            self._busy.add(handle)
        try:
            result = self._send(handle, dest, ttl, timeout_ms)
        finally:
            with self._lock:
                self._busy.discard(handle)
                closed = self._closed
                if closed and handle in self._handles:
                    self._handles.remove(handle)
                else:
                    closed = False
            if closed:  # close() ran meanwhile and left this handle to us
                self._dll.IcmpCloseHandle(handle)
                setattr(self._local, "handle" if version == 4 else "handle6", None)
        if self._closed:
            raise OSError("backend is closed")
        return result

    def _send(self, handle: Any, dest: Any, ttl: int, timeout_ms: int) -> ProbeResult:
        if not isinstance(dest, int):
            return self._send6(handle, dest, ttl, timeout_ms)
        options = IP_OPTION_INFORMATION(Ttl=ttl)
        buf = ctypes.create_string_buffer(REPLY_BUFFER_SIZE)
        request = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
        t0 = time.perf_counter_ns()
        count = self._dll.IcmpSendEcho2(
            handle, None, None, None, dest, request, len(PAYLOAD), ctypes.pointer(options), buf,
            REPLY_BUFFER_SIZE, timeout_ms,
        )  # fmt: skip
        elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
        if not count:
            return self._from_error(self._last_error(), elapsed_ms)
        responder, status, os_ms = parse_reply(buf.raw)
        return self._from_status(status, responder, os_ms, elapsed_ms, dest)

    def _send6(self, handle: Any, dest: tuple, ttl: int, timeout_ms: int) -> ProbeResult:
        host, scope_id = dest
        options = IP_OPTION_INFORMATION(Ttl=ttl)  # the hop limit
        source = sockaddr_in6()  # in6addr_any: the stack picks the source
        target = sockaddr_in6(host, scope_id)
        buf = ctypes.create_string_buffer(REPLY_BUFFER_SIZE)
        request = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
        t0 = time.perf_counter_ns()
        count = self._dll.Icmp6SendEcho2(
            handle, None, None, None, ctypes.byref(source), ctypes.byref(target), request, len(PAYLOAD),
            ctypes.pointer(options), buf, REPLY_BUFFER_SIZE, timeout_ms,
        )  # fmt: skip
        elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
        if not count:
            return self._from_error(self._last_error(), elapsed_ms, v6=True)
        responder, status, os_ms = parse_reply6(buf.raw)
        if status == IP_SUCCESS:
            if address_key(responder) != address_key(host):
                self._note(status)  # the OS matched the reply, but someone else answered: not the target alive
                return ProbeResult("timeout")
            return ProbeResult("reply", responder, _rtt(os_ms, elapsed_ms))
        if status in _TTL_EXCEEDED6:
            return ProbeResult("ttl_exceeded", responder, _rtt(os_ms, elapsed_ms))
        if status in _UNREACHABLE6:
            return ProbeResult("unreachable", responder, _rtt(os_ms, elapsed_ms))
        if status != IP_REQ_TIMED_OUT:
            self._note(status)
        return ProbeResult("timeout")

    def _from_error(self, err: int, elapsed_ms: float, v6: bool = False) -> ProbeResult:
        if err in (ERROR_NETWORK_UNREACHABLE, ERROR_HOST_UNREACHABLE):
            raise OSError(err, "no route to host")
        if err == IP_REQ_TIMED_OUT:
            return ProbeResult("timeout")
        if err in (_UNREACHABLE6 if v6 else _UNREACHABLE):
            return ProbeResult("unreachable", None, _rtt(0, elapsed_ms))
        if 11000 <= err < 11100:  # some other IP_STATUS: lost, but say what it was (once)
            self._note(err)
            return ProbeResult("timeout")
        raise OSError(err, f"IcmpSendEcho2 failed (Win32 error {err})")

    def _from_status(self, status: int, responder: str, os_ms: int, elapsed_ms: float, dest: int) -> ProbeResult:
        if status == IP_SUCCESS:
            if responder != str(ipaddress.IPv4Address(dest.to_bytes(4, sys.byteorder))):
                self._note(status)  # the OS matched the reply, but someone else answered: not the target alive
                return ProbeResult("timeout")
            return ProbeResult("reply", responder, _rtt(os_ms, elapsed_ms))
        if status in _TTL_EXCEEDED:
            return ProbeResult("ttl_exceeded", responder, _rtt(os_ms, elapsed_ms))
        if status in _UNREACHABLE:
            return ProbeResult("unreachable", responder, _rtt(os_ms, elapsed_ms))
        if status != IP_REQ_TIMED_OUT:
            self._note(status)
        return ProbeResult("timeout")

    def _note(self, status: int) -> None:
        with self._lock:
            if status in self._logged or len(self._logged) >= 64:
                return
            self._logged.add(status)
        log.warning("IcmpSendEcho2 reported IP_STATUS %d; counted as a timeout", status)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Not waiting: a call can take up to its timeout. Queued probes still run, find the backend closed and raise
        # OSError at once (cancelling them would surface as CancelledError in the callers).
        self._pool.shutdown(wait=False)
        self._trace_pool.shutdown(wait=False)
        with self._lock:
            idle = [h for h in self._handles if h not in self._busy]
            self._handles = [h for h in self._handles if h in self._busy]  # closed by their threads when done
        for h in idle:
            self._dll.IcmpCloseHandle(h)


def _rtt(os_ms: int, elapsed_ms: float) -> float:
    """The smaller non-zero of the OS's whole-millisecond figure and our stopwatch (see the module docstring)."""
    candidates = [v for v in (float(os_ms), elapsed_ms) if v > 0]
    return min(candidates) if candidates else 0.001


__all__ = ["WindowsIcmpBackend"]
