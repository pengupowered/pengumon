"""Where Pengumon keeps its files and which address it listens on: the one module everything else asks.

Data directory (database, secrets.json, sessions.json, backups/):
    PENGUMON_DATA, else the operating system's per-user location: %LOCALAPPDATA%\\Pengumon (Windows),
    ~/Library/Application Support/Pengumon (macOS), $XDG_STATE_HOME/pengumon or ~/.local/state/pengumon (other).
Config file:
    PENGUMON_CONFIG, else <data directory>/config.yaml. A missing file means the defaults.
Database:
    `database.db_path` in the config, else <data directory>/pengumon.db. A configured relative path keeps its old
    meaning, relative to the working directory (legacy: the container's config said `data/pengumon.db` with the
    working directory /app); new configs leave the line out.
Listen address:
    PENGUMON_HOST / PENGUMON_PORT, else `server.host` / `server.port` in the config, else 127.0.0.1 and 8000.

Nothing here creates a directory except ensure_data_dir(); the callers that write do that.
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Tuple

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DB_FILE = "pengumon.db"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def default_data_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """The operating system's per-user location for Pengumon's data (not created)."""
    env = _env(env)
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        return Path(base) / "Pengumon" if base else Path.home() / "AppData" / "Local" / "Pengumon"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Pengumon"
    state = env.get("XDG_STATE_HOME")
    base_dir = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return base_dir / "pengumon"


def data_dir_source(env: Optional[Mapping[str, str]] = None) -> str:
    return "PENGUMON_DATA" if _env(env).get("PENGUMON_DATA") else "default for this system"


def data_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """PENGUMON_DATA, else the system default. Not created (see ensure_data_dir)."""
    configured = _env(env).get("PENGUMON_DATA")
    return Path(configured).expanduser() if configured else default_data_dir(env, platform)


def ensure_data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """Creates the data directory if it is missing (mode 0700 where the system has modes) and returns it."""
    path = data_dir(env)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    configured = _env(env).get("PENGUMON_CONFIG")
    return Path(configured).expanduser() if configured else data_dir(env) / "config.yaml"


def config_source(env: Optional[Mapping[str, str]] = None) -> str:
    return "PENGUMON_CONFIG" if _env(env).get("PENGUMON_CONFIG") else "<data directory>/config.yaml"


def db_path(configured: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """The database file: the configured path as it is (a relative one is relative to the working directory), else
    <data directory>/pengumon.db."""
    return str(configured) if configured else str(data_dir(env) / DB_FILE)


def secrets_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "secrets.json"


def sessions_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "sessions.json"


def backups_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "backups"


def listen_address(server=None, env: Optional[Mapping[str, str]] = None) -> Tuple[str, int, str, str]:
    """(host, port, where the host came from, where the port came from). `server` is the config's ServerConfig (or
    None for no config); only a value the file actually sets counts, not the model's default."""
    env = _env(env)
    set_in_file = getattr(server, "model_fields_set", set())
    host, host_from = DEFAULT_HOST, "default"
    if env.get("PENGUMON_HOST"):
        host, host_from = env["PENGUMON_HOST"], "PENGUMON_HOST"
    elif "host" in set_in_file:
        host, host_from = server.host, "server.host in the config"
    port, port_from = DEFAULT_PORT, "default"
    if env.get("PENGUMON_PORT"):
        try:
            port, port_from = int(env["PENGUMON_PORT"]), "PENGUMON_PORT"
        except ValueError:
            raise ValueError(f"PENGUMON_PORT must be a number, not {env['PENGUMON_PORT']!r}") from None
    elif "port" in set_in_file:
        port, port_from = server.port, "server.port in the config"
    return host, port, host_from, port_from
