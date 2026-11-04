"""The devices' config section (`devices:`): one DeviceConfig per modem, gateway or router, with its `tls:` settings.

Only pydantic and pengumon.models.hosts are imported at module level. The driver registry is imported inside the
validators: the drivers import this package (devices), so a module-level import would be a cycle.
"""

from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from pengumon.models.hosts import ID_PATTERN, validate_host


class TlsConfig(BaseModel):
    """How a device's HTTPS certificate is checked. Only an https:// URL (or a redirect to one) uses it, so it
    changes nothing for a device reached over plain HTTP.

    `verify: true` checks the certificate against certifi's CA bundle (or SSL_CERT_FILE / SSL_CERT_DIR when set), as
    httpx does. `ca_file` checks it against that PEM file instead (a home CA, or the device's own self-signed
    certificate) and still checks the host name: the certificate's subjectAltName must cover the URL's host, which
    for a router is often an IP address (an IP SAN entry). Python 3.13 and later also set VERIFY_X509_STRICT, which
    refuses certificates that bend the X.509 rules (no SAN, a CA certificate without the key-usage extensions, ...),
    as some device-generated ones do. Pinning a certificate fingerprint is not supported yet."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    verify: bool = True  # false: accept any certificate (a self-signed one the device generated itself)
    ca_file: Optional[str] = None  # PEM CA (or the device's own certificate) to verify against instead of certifi's

    @model_validator(mode="after")
    def _ca_file_needs_verify(self):
        if self.ca_file and not self.verify:
            raise ValueError("tls.ca_file is set but tls.verify is false: a CA file is only used to verify; remove one")
        return self


def credential_env_name(device_id: str) -> str:
    """The <ID> in PENGUMON_DEVICE_<ID>_PASSWORD: upper case, '-' as '_' (environment names allow no '-')."""
    return device_id.upper().replace("-", "_")


class DeviceConfig(BaseModel):
    """One modem, gateway or router pengumon reads (`devices:` in config.yaml). The driver (pengumon.drivers) decides
    what it can report; credentials never go here (pengumon.secrets)."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(..., max_length=64)
    driver: str
    url: str = Field(..., max_length=255)
    enabled: bool = True
    poll_interval_seconds: Optional[float] = Field(default=None, gt=0)  # unset: the driver's default
    username: str = Field(default="admin", min_length=1, max_length=64)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    model: Optional[str] = Field(default=None, max_length=100)  # display name (the device may not report its model)
    options: Dict[str, Any] = Field(default_factory=dict)  # driver-specific, checked against the driver's options_model

    @model_validator(mode="before")
    @classmethod
    def _no_password_here(cls, data: Any) -> Any:
        if isinstance(data, dict) and "password" in data:
            raise ValueError(
                "devices: entries take no password; set PENGUMON_DEVICE_<ID>_PASSWORD, a /run/secrets/pengumon_<id>_password "
                "file, or enter it in the dashboard settings (stored in data/secrets.json)")
        return data

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        v = v.strip()
        if not ID_PATTERN.match(v):
            raise ValueError("Device ID must be 1-64 alphanumeric characters, hyphens, or underscores.")
        return v

    @field_validator("driver")
    @classmethod
    def _known_driver(cls, v: str) -> str:
        from pengumon.drivers import DRIVERS  # imported here: see the module docstring
        if v not in DRIVERS:
            raise ValueError(f"unknown driver {v!r}; available drivers: {', '.join(sorted(DRIVERS))}")
        return v

    @field_validator("url")
    @classmethod
    def _valid_url(cls, v: str, info: ValidationInfo) -> str:
        # The messages name the problem, never the value: a URL may carry a password (http://admin:pw@host, or a
        # scheme-less admin:pw@host that would otherwise be echoed as a bad scheme). The schemes are the driver's
        # (`url_schemes`, default http and https: snmp takes snmp://); `driver` is validated before `url`.
        from pengumon.drivers import DRIVERS
        driver = DRIVERS.get(info.data.get("driver") or "")
        schemes = tuple(getattr(driver, "url_schemes", ("http", "https")))
        v = v.strip()
        try:
            parts = urlsplit(v)
            hostname = parts.hostname
        except ValueError:
            raise ValueError("url is not a valid URL") from None
        if "@" in parts.netloc or ("@" in v and not hostname):
            raise ValueError("url must not contain credentials; see docs/devices.md for where device credentials go")
        if parts.scheme not in schemes or not hostname:
            raise ValueError(f"url must start with {' or '.join(f'{scheme}://' for scheme in schemes)} and name a host")
        try:
            parts.port
        except ValueError:
            raise ValueError("url has an invalid port") from None
        if parts.query or parts.fragment:
            raise ValueError("url must not contain a query or fragment")
        if "http" not in schemes and parts.path not in ("", "/"):
            raise ValueError("url must not contain a path")
        validate_host(hostname)
        return v

    @model_validator(mode="after")
    def _driver_settings(self):
        from pengumon.drivers import DRIVERS
        driver = DRIVERS[self.driver]
        try:
            driver.options_model.model_validate(self.options)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'options'}: {err['msg']}" for err in e.errors())
            raise ValueError(f"options for driver {self.driver}: {problems}") from None
        if self.poll_interval_seconds is None:
            self.poll_interval_seconds = driver.default_poll_seconds
        default_username = getattr(driver, "default_username", None)
        if default_username and "username" not in self.model_fields_set:  # openwrt: root, not admin
            self.username = default_username
        return self

    @property
    def credential_env_name(self) -> str:
        return credential_env_name(self.id)


def unique_device_ids(configs: Iterable[DeviceConfig]) -> None:
    """Raises ValueError when two devices share an id, or share the credential variables
    PENGUMON_DEVICE_<ID>_* (ids differing only in case or '-' versus '_'). One check for the config file, the
    settings page and the live apply."""
    seen_ids: Dict[str, str] = {}
    seen_env: Dict[str, str] = {}
    for device in configs:
        if device.id in seen_ids:
            raise ValueError(f"devices: id {device.id!r} is used twice; every device needs its own id")
        env = device.credential_env_name
        if env in seen_env:
            raise ValueError(f"devices: ids {seen_env[env]!r} and {device.id!r} share the credential variables "
                             f"PENGUMON_DEVICE_{env}_*; rename one")
        seen_ids[device.id] = seen_env[env] = device.id
