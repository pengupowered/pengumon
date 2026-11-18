"""The clients' opaque row keys (the /clients and traffic routes' `key`): the same for the same client, also after a restart,
and no way back to the MAC.

A key is a salted hash of the client's driver key (a MAC in normal form for a router). Without the salt, which never leaves the
server, a MAC cannot be recovered from a key. The salt is kept in the database (meta KEY_SALT_META; load_key_salt at
start), so a page opened before an update, or an hour the Internet use card asked for then, still opens the same device. Until
the start loads it (and in tests), a random one.
"""

import hashlib
import secrets

KEY_SALT_META = "client_key_salt"
_KEY_SALT = secrets.token_bytes(16)


async def load_key_salt(repository) -> None:
    """Puts the stored salt in force, storing a new random one the first time (or when the stored one is unusable)."""
    global _KEY_SALT
    stored = await repository.get_meta(KEY_SALT_META)
    try:
        salt = bytes.fromhex(stored) if stored else b""
    except ValueError:
        salt = b""
    if len(salt) != 16:
        salt = secrets.token_bytes(16)
        await repository.set_meta(KEY_SALT_META, salt.hex())
    _KEY_SALT = salt


def client_key(key: str) -> str:
    """A client's opaque row key (twelve hex digits)."""
    return hashlib.sha256(_KEY_SALT + key.encode()).hexdigest()[:12]
