"""HTTPS with your own certificate: PENGUMON_TLS_CERT and PENGUMON_TLS_KEY name a PEM certificate (chain) and its key.

Read once, at start, by pengumon.main: both or neither, and both must load, or the start stops with a message that
says which. With them the server speaks HTTPS only (uvicorn's ssl options) and the session cookie is marked Secure.
Pengumon never makes a certificate itself: a self-signed one warns in every browser (docs/security.md).
"""

import os
import ssl
from pathlib import Path
from typing import Mapping, Optional, Tuple

CERT_ENV = "PENGUMON_TLS_CERT"
KEY_ENV = "PENGUMON_TLS_KEY"


def _value(name: str, env: Mapping[str, str]) -> str:
    return (env.get(name) or "").strip()


def tls_requested(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when either variable is set (the start then serves HTTPS, or stops: tls_files)."""
    env = os.environ if env is None else env
    return bool(_value(CERT_ENV, env) or _value(KEY_ENV, env))


def tls_files(env: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, str]]:
    """(certificate, key) paths, or None when neither variable is set. Raises ValueError, in words for the log, when
    only one is set or the pair cannot be loaded (a missing file, a key that does not match, a key with a passphrase)."""
    env = os.environ if env is None else env
    cert, key = _value(CERT_ENV, env), _value(KEY_ENV, env)
    if not cert and not key:
        return None
    if not cert or not key:
        missing, given = (CERT_ENV, KEY_ENV) if not cert else (KEY_ENV, CERT_ENV)
        raise ValueError(f"{given} is set but {missing} is not: HTTPS needs both the certificate and its key "
                         "(or neither, for plain HTTP)")
    for name, path in ((CERT_ENV, cert), (KEY_ENV, key)):
        if not Path(path).is_file():
            raise ValueError(f"{name} names {path!r}, which is not a file this account can see")
    try:
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key, password=lambda: b"")
    except (ssl.SSLError, OSError) as e:
        raise ValueError(f"The certificate {cert!r} and key {key!r} cannot be used together: {e.__class__.__name__}"
                         f"{f' ({e.reason})' if getattr(e, 'reason', None) else ''}. Both must be PEM files, the key "
                         "without a passphrase") from None
    return cert, key
