"""Device credentials: where each device's username and password come from, and how new ones are stored.

Sources, highest first (the first one that has a value wins, separately for the username and the password):

  password                                          username
  1. env PENGUMON_DEVICE_<ID>_PASSWORD              1. env PENGUMON_DEVICE_<ID>_USERNAME
  2. file /run/secrets/pengumon_<id>_password       2. data/secrets.json devices.<id>.username
  3. data/secrets.json devices.<id>.password        3. `username` in the device's config (default "admin")

<ID> is the id upper-cased with '-' as '_'. Everything is read once, at startup (SecretStore keeps secrets.json; the
secret files are read when a device's credentials are resolved). Credentials entered in the dashboard are written
under devices.<id>.

Passwords are never logged or returned: only the name of the source that supplied them.
"""

import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple, Union

from pengumon import paths
from pengumon.devices.settings import DeviceConfig, credential_env_name
from pengumon.db.migrations import LEGACY_MODEM_ID, LEGACY_ROUTER_ID
from pengumon.observability import get_logger

_log = get_logger("pengumon.secrets")

SECRETS_DIR = Path("/run/secrets")  # Podman / Docker secrets
SECRETS_PATH = paths.secrets_path()  # <data directory>/secrets.json
SECRET_FILE_MAX_BYTES = 4096  # a password file larger than this is a mistake (a key, a log), not a password


def password_env_var(device_id: str) -> str:
    return f"PENGUMON_DEVICE_{credential_env_name(device_id)}_PASSWORD"


def username_env_var(device_id: str) -> str:
    return f"PENGUMON_DEVICE_{credential_env_name(device_id)}_USERNAME"


def secret_file_name(device_id: str) -> str:
    return f"pengumon_{device_id}_password"


def webhook_secret_env_var(name: str) -> str:
    return "PENGUMON_WEBHOOK_SECRET_" + "".join(c if c.isalnum() else "_" for c in name).upper()


def resolve_webhook_secret(name: str, *, env: Optional[Mapping[str, str]] = None,
                           secrets_dir: Optional[Path] = SECRETS_DIR,
                           store: Optional["SecretStore"] = None) -> Optional[str]:
    """The value of a secret a webhook header names ("secret:<name>" in config): the environment variable
    PENGUMON_WEBHOOK_SECRET_<NAME>, else the file <secrets dir>/pengumon_webhook_<name>, else data/secrets.json
    webhooks.<name>. None when none has it. Never logged."""
    env = os.environ if env is None else env
    if env.get(webhook_secret_env_var(name)):
        return env[webhook_secret_env_var(name)]
    if secrets_dir is not None:
        found = _read_secret_file(secrets_dir / f"pengumon_webhook_{name}", f"webhook:{name}")
        if found is not None:
            return found[0]
    hooks = (store or SecretStore()).data().get("webhooks")
    return _text(hooks.get(name)) if isinstance(hooks, dict) else None


@dataclass(frozen=True)
class Credentials:
    """A device's login and where each half came from. `password_source` is None when no source has a password."""

    username: str
    password: Optional[str] = field(repr=False)
    username_source: str = "config"
    password_source: Optional[str] = None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def describe(self) -> str:
        """For logs: where the credentials came from, never what they are."""
        if self.password_source is None:
            return "no password configured"
        if self.username_source == self.password_source:
            return f"credentials from {self.password_source}"
        return f"password from {self.password_source}, username from {self.username_source}"


def credential_updates(device_id: str, username: str, password: str) -> dict:
    """What SecretStore.write merges in for new credentials."""
    return {"devices": {device_id: {"username": username, "password": password}}}


# One-shot conversion of the flat keys the builds before `devices:` wrote (modem_username, modem_password,
# router_username, router_password) to devices.modem / devices.router. Remove after the owner's first start.
FLAT_KEYS: Mapping[str, str] = {
    f"{device_id}_{key}": (device_id, key)
    for device_id in (LEGACY_MODEM_ID, LEGACY_ROUTER_ID) for key in ("username", "password")
}


def convert_flat_keys(data: dict) -> Optional[dict]:
    """`data` with the flat keys moved under devices.<id> (a value already there wins) and removed; None when it
    has none. Remove after the owner's first start."""
    if not any(key in data for key in FLAT_KEYS):
        return None
    converted = {k: v for k, v in data.items() if k not in FLAT_KEYS}
    devices = converted.get("devices")
    devices = dict(devices) if isinstance(devices, dict) else {}
    for flat, (device_id, key) in FLAT_KEYS.items():
        value = _text(data.get(flat))
        if value is None:
            continue
        entry = devices.get(device_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if not _text(entry.get(key)):
            entry[key] = value
        devices[device_id] = entry
    converted["devices"] = devices
    return converted


class SecretStore:
    """data/secrets.json: read once (then kept), written atomically with mode 0600 (update_secrets), merged
    one device entry at a time. One instance per app (AppState.secret_store), so every reader and writer uses the
    same path, and tests point it elsewhere by passing a path."""

    def __init__(self, path: Optional[Path] = None, *, data: Optional[dict] = None):
        """`path` defaults to SECRETS_PATH (looked up now); `data` preloads the content instead of reading
        the file (tests)."""
        self.path = Path(path) if path is not None else SECRETS_PATH
        self._data = data
        self._problem: Optional[str] = None

    def data(self) -> dict:
        """The parsed file, read on first use; {} when it is missing or unusable (logged, without its content)."""
        if self._data is None:
            data, self._problem = _read_store(self.path)
            self._data = self._convert(data)
        return self._data

    @property
    def problem(self) -> Optional[str]:
        """Why the file could not be used when it was read ("is not valid JSON"), else None (also for a missing file).
        The sign-in reads it: a damaged file must not open a dashboard that had a password (pengumon.access)."""
        self.data()
        return self._problem

    def _convert(self, data: dict) -> dict:
        """Remove after the owner's first start: the flat keys moved under devices.<id> and the file rewritten."""
        converted = convert_flat_keys(data)
        if converted is None:
            return data
        moved = ", ".join(key for key in FLAT_KEYS if key in data)
        try:
            write_secrets(converted, self.path)
        except OSError as e:
            _log.warning("Credentials file could not be rewritten with its flat keys under devices.<id>; using them "
                         "from memory", path=str(self.path), error=type(e).__name__)
            return converted
        _log.info(f"Moved {moved} in {self.path} under devices.modem / devices.router", path=str(self.path))
        return converted

    def write(self, updates: dict) -> None:
        """Merges `updates` into the file and into what data() returns. Raises SecretsFileUnusable (nothing
        written) when the file on disk cannot be read or parsed."""
        self.data()  # converted before the merge, so the write never brings back what the conversion removed
        update_secrets(updates, self.path)
        _merge(self.data(), json.loads(json.dumps(updates)))

    def label(self, key: str) -> str:
        """A source name for logs: "data/secrets.json devices.router.password"."""
        return f"{self.path} {key}"

    def get(self, path: Tuple[str, ...]) -> Optional[str]:
        """The text at `path` ("ntfy", "url"), or None when it is missing, empty or not text. Never log it."""
        node: object = self.data()
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        return _text(node)

    def remove(self, path: Tuple[str, ...]) -> None:
        """Removes the entry at `path` from the file and from what data() returns (`write` can only merge)."""
        self.change({}, [path])

    def change(self, updates: dict, removals: Sequence[Tuple[str, ...]] = ()) -> None:
        """Merges `updates` and removes `removals` (paths; a parent left empty goes too) in one atomic write of the
        file, then updates what data() returns. Nothing is written when neither changes anything, nor when the file
        on disk cannot be read or parsed (SecretsFileUnusable)."""
        memory = json.loads(json.dumps(self.data()))  # converted before the change, as in write()
        _merge(memory, json.loads(json.dumps(updates)))
        for path in removals:
            _remove_path(memory, tuple(path))
        if memory == self.data():
            return
        on_disk = _read_for_write(self.path)
        _merge(on_disk, json.loads(json.dumps(updates)))
        for path in removals:
            _remove_path(on_disk, tuple(path))
        write_secrets(on_disk, self.path)
        self._data = memory


class CredentialSources:
    """The credential sources of one process: the environment, the secrets directory and the SecretStore.
    `resolve` picks each device's credentials from them; `outranking` says which source would override credentials
    saved to the store."""

    def __init__(self, *, env: Optional[Mapping[str, str]] = None, secrets_dir: Optional[Path] = SECRETS_DIR,
                 secrets_file: Optional[Path] = None, store: Union["SecretStore", dict, None] = None):
        """`env` defaults to os.environ; `secrets_dir` None skips the secret files; `store` is the app's SecretStore
        (or, for tests, the parsed content as a dict); without one, `secrets_file` (default SECRETS_PATH)."""
        self._env = os.environ if env is None else env
        self._dir = secrets_dir
        if isinstance(store, SecretStore):
            self.store = store
        else:
            self.store = SecretStore(secrets_file, data=store)
        self._files: Dict[str, Optional[Tuple[str, str]]] = {}  # device id -> (password, source): read once

    @classmethod
    def none(cls) -> "CredentialSources":
        """No sources at all: every device has the username of its config and no password (tests, tools)."""
        return cls(env={}, secrets_dir=None, store={})

    def resolve(self, device: DeviceConfig) -> Credentials:
        password, password_source = self._password_above_store(device.id) or self._stored(device.id, "password")
        username, username_source = (self._from_env(username_env_var(device.id))
                                     or self._stored(device.id, "username"))
        if not username:
            username, username_source = device.username, "config"
        return Credentials(username=username, password=password, username_source=username_source,
                           password_source=password_source)

    def outranking(self, device: DeviceConfig) -> Optional[str]:
        """The source that ranks above secrets.json and supplies the password (else the username) of `device`: when
        there is one, credentials saved to the store are overridden by it at the next start."""
        found = self._password_above_store(device.id) or self._from_env(username_env_var(device.id))
        return found[1] if found is not None else None

    def _from_env(self, name: str) -> Optional[Tuple[str, str]]:
        return (self._env[name], f"environment {name}") if self._env.get(name) else None

    def _password_above_store(self, device_id: str) -> Optional[Tuple[str, str]]:
        return self._from_env(password_env_var(device_id)) or self._secret_file(device_id)

    def _secret_file(self, device_id: str) -> Optional[Tuple[str, str]]:
        if self._dir is None:
            return None
        if device_id not in self._files:
            self._files[device_id] = _read_secret_file(self._dir / secret_file_name(device_id), device_id)
        return self._files[device_id]

    def _stored(self, device_id: str, key: str) -> Tuple[Optional[str], Optional[str]]:
        devices = self.store.data().get("devices")
        entry = devices.get(device_id) if isinstance(devices, dict) else None
        value = _text(entry.get(key)) if isinstance(entry, dict) else None
        return (value, self.store.label(f"devices.{device_id}.{key}")) if value else (None, None)


def _read_secret_file(path: Path, device_id: str) -> Optional[Tuple[str, str]]:
    """(password, source) from a Podman/Docker secret file, or None. Only a regular file of at most
    SECRET_FILE_MAX_BYTES is read (a FIFO would block the start, a huge file is not a password); exactly one trailing
    "\n" or "\r\n" is removed (the one `echo` or an editor adds). Problems are logged with the path, never the
    content."""
    def skip(problem: str) -> None:
        _log.warning(f"Secret file {problem}; trying the next credential source", device=device_id, path=str(path))

    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.warning("Secret file could not be read; trying the next credential source", device=device_id,
                     path=str(path), error=type(e).__name__)
        return None
    if not stat.S_ISREG(info.st_mode):
        skip("is not a regular file")
        return None
    if info.st_size > SECRET_FILE_MAX_BYTES:
        skip(f"is larger than {SECRET_FILE_MAX_BYTES} bytes")
        return None
    try:
        # O_NONBLOCK: if the file was swapped for a FIFO since the stat, opening it must not hang the start.
        # O_BINARY (Windows): no newline translation, so the file is read as written.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                skip("is not a regular file")
                return None
            raw = f.read(SECRET_FILE_MAX_BYTES + 1)
    except OSError as e:
        _log.warning("Secret file could not be read; trying the next credential source", device=device_id,
                     path=str(path), error=type(e).__name__)
        return None
    if len(raw) > SECRET_FILE_MAX_BYTES:
        skip(f"is larger than {SECRET_FILE_MAX_BYTES} bytes")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        skip("is not UTF-8 text")
        return None
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        skip("is empty")
        return None
    return text, str(path)


def _text(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


# What the page says when a save is refused for that reason (the log line names the path and the problem).
SECRETS_FILE_DAMAGED = ("The secrets file (secrets.json) is damaged, so it was not overwritten and nothing was saved. "
                        "Fix the file, or move it away to start over, then save again.")


class SecretsFileUnusable(OSError):
    """secrets.json exists but cannot be read or parsed, so a write would erase what it holds: nothing was written.
    An OSError, so every caller that handles a failed write refuses the save; the message never holds the content."""

    def __init__(self, path: Path, problem: str):
        super().__init__(f"{path} {problem}; it was not overwritten")
        self.path, self.problem = path, problem


def _read_store(path: Path) -> Tuple[Dict, Optional[str]]:
    """(secrets.json parsed, None), or ({}, problem) when the file exists but cannot be read or parsed ("is not valid
    JSON"); ({}, None) when it is missing. The one reader of the file: the store's first read and every write use it.
    Problems are logged with the path, never the content."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        _log.warning("Credentials file could not be read; credentials stored there are not used", path=str(path),
                     error=type(e).__name__)
        return {}, "could not be read"
    try:
        data = json.loads(raw)
    except ValueError as e:
        _log.warning("Credentials file is not valid JSON; credentials stored there are not used", path=str(path),
                     error=f"{type(e).__name__} at line {getattr(e, 'lineno', '?')}")
        return {}, "is not valid JSON"
    if not isinstance(data, dict):
        _log.warning("Credentials file is not a JSON object; credentials stored there are not used", path=str(path))
        return {}, "is not a JSON object"
    try:
        # Windows has no permission bits (st_mode is 0o666 or 0o444); the profile's ACL protects the file there
        mode = stat.S_IMODE(path.stat().st_mode) if sys.platform != "win32" else 0
    except OSError:
        mode = 0
    if mode & 0o077:
        _log.warning(f"Credentials file is readable by other users (mode {mode:04o}); pengumon writes it 0600",
                     path=str(path))
    return data, None


def _read_for_write(path: Path) -> Dict:
    """secrets.json as it is on disk now, to merge a change into ({} when missing). Raises SecretsFileUnusable when
    the file exists but cannot be read or parsed: a write would replace every credential in it."""
    data, problem = _read_store(path)
    if problem is not None:
        _log.error(f"Credentials file {problem}: nothing was saved, so the credentials in it are kept. Fix the file "
                   "(or move it away to start over), then save again", path=str(path))
        raise SecretsFileUnusable(path, problem)
    return data


# --- the Settings page's secret slots ----------------------------------------------------------------------------------
#
# A slot is a named place in secrets.json that the Settings page writes and never reads back (pengumon.settings.registry
# SECRET_SLOTS): "ntfy.url" and "ntfy.token" (PENGUMON_NOTIFY_URL / _TOKEN win while set), "webhook.<id>.url" (a
# webhook's address; it can hold a token) and "webhook_header.<name>" (a header secret, the existing webhooks.<name>;
# PENGUMON_WEBHOOK_SECRET_<NAME> and /run/secrets/pengumon_webhook_<name> win, as resolve_webhook_secret reads them).

SLOT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # a header secret's name (notify.settings._SECRET_NAME)
WEBHOOK_SLOT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # a webhook id (notify.settings.WEBHOOK_ID)


def slot_path(slot: str) -> Optional[Tuple[str, ...]]:
    """Where a slot lives in secrets.json, or None for a name that is no slot."""
    if slot in ("ntfy.url", "ntfy.token"):
        return tuple(slot.split("."))
    if slot.startswith("webhook.") and slot.endswith(".url"):
        webhook = slot[len("webhook."):-len(".url")]
        return ("webhook_urls", webhook) if WEBHOOK_SLOT_ID.fullmatch(webhook) else None
    if slot.startswith("webhook_header."):
        name = slot[len("webhook_header."):]
        return ("webhooks", name) if SLOT_NAME.fullmatch(name) else None
    return None


def slot_env_var(slot: str) -> Optional[str]:
    """The variable that wins over a slot while it is set (and refuses a write to it), or None."""
    from pengumon.settings.env import SECRET_ENV
    if slot in SECRET_ENV:
        return SECRET_ENV[slot]
    if slot.startswith("webhook_header."):
        return webhook_secret_env_var(slot[len("webhook_header."):])
    return None


def resolve_slot(slot: str, store: "SecretStore", *, env: Optional[Mapping[str, str]] = None,
                 secrets_dir: Optional[Path] = SECRETS_DIR) -> Tuple[Optional[str], Optional[str]]:
    """(value, source) of a slot: source "env", "secret_file" or "secrets_file", or (None, None) when no source
    has it. Never log the value."""
    from pengumon.settings.env import env_value
    variable = slot_env_var(slot)
    if variable is not None:
        value = env_value(variable, env)
        if value is not None:
            return value, "env"
    if slot.startswith("webhook_header.") and secrets_dir is not None:
        name = slot[len("webhook_header."):]
        found = _read_secret_file(secrets_dir / f"pengumon_webhook_{name}", f"webhook:{name}")
        if found is not None:
            return found[0], "secret_file"
    path = slot_path(slot)
    value = store.get(path) if path is not None else None
    return (value, "secrets_file") if value is not None else (None, None)


def _remove_path(data: dict, path: Tuple[str, ...]) -> None:
    """Deletes data[path...]; a mapping left empty by it is removed too."""
    if not path:
        return
    parent = data.get(path[0]) if len(path) > 1 else data
    if len(path) == 1:
        data.pop(path[0], None)
        return
    if isinstance(parent, dict):
        _remove_path(parent, path[1:])
        if not parent:
            data.pop(path[0], None)


# --- writing the credentials file -----------------------------------------------------------------------------------

def _merge(into: dict, updates: dict) -> None:
    """Merges `updates` into `into`, recursing into mappings present in both (so one device's entry under
    "devices" leaves the others alone); any other value is replaced."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def update_secrets(updates: dict, path: Optional[Path] = None) -> None:
    """Merges `updates` into the credentials file (write_secrets). Raises SecretsFileUnusable, writing nothing, when
    the file exists but cannot be read or parsed."""
    path = Path(path) if path else SECRETS_PATH
    data = _read_for_write(path)
    _merge(data, updates)
    write_secrets(data, path)


def write_secrets(data: dict, path: Optional[Path] = None) -> None:
    """Replaces the credentials file with `data`. Written to a temp file with mode 0600 and moved into place, so a
    crash never leaves a half-written file and the passwords are never world-readable."""
    path = Path(path) if path else SECRETS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as sf:
            json.dump(data, sf, indent=2)
            sf.flush()
            os.fsync(sf.fileno())
        os.chmod(tmp, 0o600)  # O_CREAT's mode is ignored if a stale temp file existed
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
