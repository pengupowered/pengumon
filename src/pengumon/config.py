"""Configuration: config.yaml read into one AppConfig.

AppConfig has one line per section. Each section's model lives next to the feature that reads it (the prober's in
engine/prober_settings.py, the devices' in devices/settings.py, ...), so changing a feature's settings edits that
feature's files only; the section models import nothing of the app, which keeps this module free of import cycles.
Only the server and database sections, which belong to no single feature, are defined here. Credentials are never
part of the config (pengumon.secrets).
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from pengumon import paths
from pengumon.clients.settings import PrivacyConfig
from pengumon.devices.settings import DeviceConfig
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.discovery.settings import DiscoverySettings
from pengumon.engine.load_settings import LineConfig
from pengumon.engine.netinfo import default_dns_probes, default_targets, get_default_gateway_ip
from pengumon.engine.prober_settings import DnsProbeConfig, ProberConfig
from pengumon.models.probes import TargetConfig
from pengumon.notify_settings import NotificationsConfig
from pengumon.observability import get_logger
from pengumon.services.settings import ServiceChecksConfig
from pengumon.web.settings import DashboardConfig

_log = get_logger("pengumon.config")


class ServerConfig(BaseModel):
    host: str = paths.DEFAULT_HOST  # PENGUMON_HOST wins; see pengumon.paths.listen_address
    port: int = paths.DEFAULT_PORT  # PENGUMON_PORT wins
    log_level: str = "info"


class DatabaseConfig(BaseModel):
    db_path: Optional[str] = None  # unset: <data directory>/pengumon.db; a relative path is relative to the cwd (legacy)
    retention_days: int = 7
    rollup_retention_days: int = 400  # per-minute rollups (raw samples: retention_days)


# Models that may carry a credential (or a URL someone put one in) never echo their input in validation errors: the
# error text ends up in the startup log and the crash output.
_SECRET_SAFE = ConfigDict(hide_input_in_errors=True)


class AppConfig(BaseModel):
    model_config = _SECRET_SAFE

    server: ServerConfig = Field(default_factory=ServerConfig)
    prober: ProberConfig = Field(default_factory=ProberConfig)
    targets: List[TargetConfig] = Field(default_factory=list)
    dns_probes: List[DnsProbeConfig] = Field(default_factory=list)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    devices: List[DeviceConfig] = Field(default_factory=list)  # the modems and routers to read; none by default
    line: LineConfig = Field(default_factory=LineConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    service_checks: ServiceChecksConfig = Field(default_factory=ServiceChecksConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    _targets_are_defaults: bool = PrivateAttr(default=False)  # load_config filled in default_targets()

    @property
    def targets_from_file(self) -> bool:
        """Whether `targets` came from the config (not the defaults). Only read before the stored path replaces
        them: once the path has been imported or applied it lives in the database (pengumon.engine.path)."""
        return bool(self.targets) and not self._targets_are_defaults

    @model_validator(mode="after")
    def _unique_device_ids(self):
        seen_ids: Dict[str, str] = {}
        seen_env: Dict[str, str] = {}
        for device in self.devices:
            if device.id in seen_ids:
                raise ValueError(f"devices: id {device.id!r} is used twice; every device needs its own id")
            env = device.credential_env_name
            if env in seen_env:
                raise ValueError(f"devices: ids {seen_env[env]!r} and {device.id!r} share the credential variables "
                                 f"PENGUMON_DEVICE_{env}_*; rename one")
            seen_ids[device.id] = seen_env[env] = device.id
        return self


def read_config_file(path: Path) -> dict:
    """The config file's top-level mapping; {} when the file does not exist."""
    return _read_yaml(path) if path.exists() else {}


def _read_yaml(path: Path) -> dict:
    """The file's YAML. A syntax error is reported by position only: PyYAML's own message quotes the offending line,
    which may hold a password."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(e, "problem", None) or type(e).__name__
        raise ValueError(f"{path} is not valid YAML{where}: {problem}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping (sections such as targets:, devices:)")
    return data


def load_server_config(config_path: Optional[str] = None) -> ServerConfig:
    """Only the `server` section of the config file (the listen address is needed before the app starts)."""
    path = Path(config_path) if config_path is not None else paths.config_path()
    return ServerConfig(**(read_config_file(path).get("server") or {}))


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file or return defaults. Credentials are not part of it: pengumon.secrets reads
    them when the devices are built."""
    if config_path is None:
        path = paths.config_path()
    else:
        path = Path(config_path)
    raw = read_config_file(path)
    config = AppConfig(**raw)

    if not config.targets:
        gateway = get_default_gateway_ip()
        config.targets = default_targets(gateway)
        config._targets_are_defaults = True
        _log.info("No targets configured; probing the defaults", targets=",".join(t.id for t in config.targets),
                  gateway=gateway)

    # left out: the default lookups; an empty list: none (a config that watches one server needs no DNS lookups)
    if raw.get("dns_probes") is None:
        config.dns_probes = default_dns_probes()

    return config
