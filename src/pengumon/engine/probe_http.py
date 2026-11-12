"""An HTTP(S) probe: the time from the start of the connection to the server's status line.

Raw asyncio streams rather than httpx, on purpose: httpx pools connections, reads every header, follows the proxy
variables of the environment and hides the phase timings. Here "no connection reuse, no proxy, no cookies, no body" is
true by construction:

- The request is one fixed text (request_bytes): no Cookie, no Authorization, no Accept-Encoding.
- Only the status line is read, at most STATUS_LINE_MAX bytes, then the transport is aborted: headers and body are never
  read, and a redirect (3xx) is an answer, not followed.
- TLS is always verified against the system store; there is no switch to turn that off.
- The status code and the timings are all that leave this module; response bytes never reach an error text.

Which addresses may be probed is decided by the caller (engine.probe_rules). Errors follow engine.probe_tcp: a refusal,
a timeout and the failures listed in the table below are answers (`SocketAnswer`); any other OSError is raised.
"""

import asyncio
import re
import ssl
import time
from typing import Optional

from pengumon import __version__
from pengumon.engine.probe_tcp import SocketAnswer, connect_socket, kernel_rtt_ms, refused

USER_AGENT = f"Pengumon/{__version__} (path probe)"
STATUS_LINE_MAX = 1024
_STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?\r?\n")
_TIMEOUT_WORDS = {"connect": "TCP handshake", "tls": "TLS handshake", "answer": "waiting for the HTTP answer"}

_context: Optional[ssl.SSLContext] = None
_context_lock: Optional[asyncio.Lock] = None


async def default_context() -> ssl.SSLContext:
    """The TLS context of every probe, made once: certificates verified against the system store, host name checked,
    HTTP/1.1 only. Loading the store blocks for tens of ms on old hardware, so it runs in a thread."""
    global _context, _context_lock
    if _context is None:
        if _context_lock is None:
            _context_lock = asyncio.Lock()
        async with _context_lock:
            if _context is None:
                context = await asyncio.to_thread(ssl.create_default_context)
                context.set_alpn_protocols(["http/1.1"])
                _context = context
    return _context


def host_header(host: str, port: Optional[int], scheme: str) -> str:
    """The Host header value: the hop's address (an IPv6 literal in brackets), with the port when it is not the scheme's."""
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default = 443 if scheme == "https" else 80
    return shown if port is None or port == default else f"{shown}:{port}"


def request_bytes(verb: str, host_header_value: str, path: str) -> bytes:
    """Exactly the request that is sent and nothing else."""
    return (f"{verb} {path} HTTP/1.1\r\nHost: {host_header_value}\r\nUser-Agent: {USER_AGENT}\r\nAccept: */*\r\n"
            "Connection: close\r\n\r\n").encode("ascii")


def _certificate_words(e: ssl.SSLCertVerificationError) -> str:
    message = (e.verify_message or "").lower()
    if "expired" in message:
        return "TLS: certificate expired"
    if "mismatch" in message:
        return "TLS: certificate is for another name"
    return "TLS: certificate not trusted"


async def http_status(address: str, version: int, host: str, port: Optional[int], scheme: str, path: str, verb: str,
                      timeout: float, *, ssl_context: Optional[ssl.SSLContext] = None,
                      kernel_rtt=kernel_rtt_ms) -> SocketAnswer:
    """One request to `address` (already resolved; `host` is the hop's own address for the Host header and the
    certificate check). rtt_ms runs from just before the connection starts to the status line; connect_ms is the
    handshake inside it. The name lookup is not timed."""
    https = scheme == "https"
    connect_port = port if port is not None else (443 if https else 80)
    phase = "connect"
    writer = None
    sock = None
    try:
        context = (ssl_context or await default_context()) if https else None
        async with asyncio.timeout(timeout):
            start = time.perf_counter()
            sock, connect_ms = await connect_socket(address, version, connect_port, kernel_rtt=kernel_rtt)
            phase = "tls" if https else "answer"
            reader, writer = await asyncio.open_connection(sock=sock, ssl=context, server_hostname=host if https else None,
                                                           limit=STATUS_LINE_MAX)
            sock = None  # the transport owns it now
            phase = "answer"
            writer.write(request_bytes(verb, host_header(host, port, scheme), path))
            line = await reader.readuntil(b"\n")
            rtt_ms = (time.perf_counter() - start) * 1000
    except ConnectionRefusedError:
        return refused(connect_port)
    except TimeoutError:
        return SocketAnswer("timeout", error=f"No answer in time ({_TIMEOUT_WORDS[phase]})")
    except ssl.SSLCertVerificationError as e:
        return SocketAnswer("tls", error=_certificate_words(e))
    except ssl.SSLError as e:
        return SocketAnswer("tls", error=f"TLS handshake failed: {e.reason or e}")
    except (asyncio.LimitOverrunError, ValueError):
        return SocketAnswer("bad_answer", error=f"Not an HTTP answer (no status line in the first {STATUS_LINE_MAX} bytes)")
    except asyncio.IncompleteReadError:
        return SocketAnswer("bad_answer", error="The server closed the connection without answering")
    except ConnectionError:
        if phase == "connect":
            raise
        return SocketAnswer("bad_answer", error="The server closed the connection without answering")
    finally:
        if writer is not None:
            writer.transport.abort()  # never read headers or body, leave nothing lingering
        elif sock is not None:
            sock.close()
    match = _STATUS_LINE.fullmatch(line)
    if match is None:
        return SocketAnswer("bad_answer", error="Not an HTTP answer")
    status = int(match.group(1))
    connect_ms = min(connect_ms, rtt_ms)
    if status >= 500:
        return SocketAnswer("http_error", rtt_ms=rtt_ms, connect_ms=connect_ms, status=status,
                            error=f"HTTP {status} from the server")
    return SocketAnswer("reply", rtt_ms=rtt_ms, connect_ms=connect_ms, status=status)
