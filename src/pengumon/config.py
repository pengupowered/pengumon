"""Configuration: config.yaml read into one AppConfig.

AppConfig has one line per section. Each section's model lives next to the feature that reads it (the prober's in
engine/prober_settings.py, the devices' in devices/settings.py, ...), so changing a feature's settings edits that
feature's files only; the section models import nothing of the app, which keeps this module free of import cycles.
Only the server and database sections, which belong to no single feature, are defined here. Credentials are never
part of the config (pengumon.secrets).
"""

import os
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from pengumon import paths
from pengumon.clients.settings import PrivacyConfig
from pengumon.devices.levels import RouterLevelsConfig
from pengumon.devices.settings import DeviceConfig, unique_device_ids
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.discovery.settings import DiscoverySettings
from pengumon.engine.load_settings import LineConfig
from pengumon.engine.netinfo import default_dns_probes, default_targets, get_default_gateway_ip
from pengumon.engine.prober_settings import DnsProbeConfig, ProberConfig
from pengumon.lines.levels import LineSignalConfig
from pengumon.models.probes import TargetConfig
from pengumon.notify.settings import AlertsConfig, NotificationsConfig
from pengumon.observability import get_logger
from pengumon.observability.logs import parse_levels
from pengumon.reports.settings import ReportsConfig, UptimeConfig
from pengumon.services.settings import ServiceChecksConfig
from pengumon.settings import lenient
from pengumon.settings.general import GeneralConfig
from pengumon.web.settings import DashboardConfig, DisplayConfig

_log = get_logger("pengumon.config")


_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical"}


class ServerConfig(BaseModel):
    host: str = paths.DEFAULT_HOST  # PENGUMON_HOST wins; see pengumon.paths.listen_address
    port: int = paths.DEFAULT_PORT  # PENGUMON_PORT wins
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"  # PENGUMON_LOG_LEVEL wins
    # Per-logger levels, "pengumon.devices=debug,uvicorn.access=warning" (observability.logs.parse_levels);
    # PENGUMON_LOG_LEVELS wins
    log_levels: Optional[str] = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _level_name(cls, value):
        if isinstance(value, str):
            value = value.strip().lower()
            return _LEVEL_ALIASES.get(value, value)
        return value

    @field_validator("log_levels")
    @classmethod
    def _levels(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        parse_levels(value)
        return value.strip()


class DatabaseConfig(BaseModel):
    db_path: Optional[str] = None  # unset: <data directory>/pengumon.db; a relative path is relative to the cwd (legacy)
    retention_days: int = Field(7, ge=1, le=90)  # raw samples; line-capacity learning needs 7
    rollup_retention_days: int = Field(400, ge=30, le=1000)  # per-minute rollups, at least retention_days

    @model_validator(mode="after")
    def _rollups_outlive_raw(self):
        if self.rollup_retention_days < self.retention_days:
            raise ValueError("rollup_retention_days must be at least retention_days")
        return self


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
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    line_signal: LineSignalConfig = Field(default_factory=LineSignalConfig)
    router_levels: RouterLevelsConfig = Field(default_factory=RouterLevelsConfig)
    uptime: UptimeConfig = Field(default_factory=UptimeConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    _targets_are_defaults: bool = PrivateAttr(default=False)  # load_config filled in default_targets()

    @property
    def targets_from_file(self) -> bool:
        """Whether `targets` came from the config (not the defaults). Only read before the stored path replaces
        them: once the path has been imported or applied it lives in the database (pengumon.engine.path)."""
        return bool(self.targets) and not self._targets_are_defaults

    @model_validator(mode="after")
    def _unique_device_ids(self):
        unique_device_ids(self.devices)
        return self

    @model_validator(mode="after")
    def _wan_critical_above_busy(self):
        # The WAN gauge warns from the busy level, so its critical level must lie above it.
        if not self.router_levels.wan_critical_pct > self.classifier.busy_load_pct:
            raise ValueError("router_levels.wan_critical_pct must be above classifier.busy_load_pct")
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


# The keys read from config.yaml at every start, before the database opens; strictly: a bad one stops the start.
# Everything else is a setting, owned by the settings store once it was imported (pengumon.settings).
BOOTSTRAP_KEYS = ("server.host", "server.port", "database.db_path", "prober.interval_seconds")


def _bootstrap(raw: dict) -> None:
    """Validates the bootstrap keys of the file on their own; raises ValueError (the start fails) for a bad one."""
    for key in BOOTSTRAP_KEYS:
        section, name = key.split(".")
        part = raw.get(section)
        if isinstance(part, dict) and name in part:
            model = AppConfig.model_fields[section].annotation
            try:
                model.model_validate({name: part[name]})
            except ValidationError as e:
                raise ValueError(f"config file: {key} is not valid ({lenient.reason(e.errors()[0])})") from None
        elif part is not None and not isinstance(part, dict):
            raise ValueError(f"config file: {section}: must be a mapping")


def load_config(config_path: Optional[str] = None, *, lenient_rest: bool = False) -> AppConfig:
    """Load configuration from YAML file or return defaults. Credentials are not part of it: pengumon.secrets reads
    them when the devices are built.

    With `lenient_rest` (the app's start): the bootstrap keys (BOOTSTRAP_KEYS) and `targets` are validated strictly,
    and a refused value anywhere else is logged by its key and left at its default instead of stopping the start:
    those values are the settings store's (pengumon.settings), which validates them on its own."""
    if config_path is None:
        path = paths.config_path()
    else:
        path = Path(config_path)
    raw = read_config_file(path)
    if lenient_rest:
        _bootstrap(raw)
        rest = {k: v for k, v in raw.items() if k != "targets"}
        config, dropped = lenient.validate_leniently(AppConfig, rest)
        if dropped:
            _log.warning(f"config.yaml: {len(dropped)} value(s) refused and left at their defaults: "
                         f"{', '.join(lenient.removed_paths(dropped))}")
        if raw.get("targets") is not None:
            config.targets = AppConfig(targets=raw["targets"]).targets
    else:
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
