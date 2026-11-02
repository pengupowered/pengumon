"""The service checks' config section (`service_checks:`)."""

from typing import List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class ServiceCheckConfig(BaseModel):
    """An app on this host whose health endpoint is checked over HTTP (services/checker.py)."""
    id: str
    name: str
    url: str
    enabled: bool = True
    timeout_seconds: float = 5.0
    ca_file: Optional[str] = None  # PEM CA to verify an HTTPS endpoint against (e.g. the home CA)
    open_url: Optional[str] = None  # the link the dashboard shows (how people reach the app); `url` is never a link
    description: Optional[str] = None
    notify: bool = True  # False: the dashboard shows it, ntfy never hears about it

    @field_validator("open_url")
    @classmethod
    def _open_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("open_url must be an http or https URL with a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("open_url must not contain credentials")
        if parts.query or "?" in value:
            raise ValueError("open_url must not have a query string")
        return value


class ServiceChecksConfig(BaseModel):
    interval_seconds: float = 30.0
    failures_for_incident: int = 3  # failed checks in a row before a service_down incident
    notify_after_seconds: float = 300.0  # a service must fail this long before ntfy hears of it (restarts are quiet)
    services: List[ServiceCheckConfig] = Field(default_factory=list)
