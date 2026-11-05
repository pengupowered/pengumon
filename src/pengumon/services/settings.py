"""The service checks' config section (`service_checks:`)."""

from typing import List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.notify.settings import http_url


class ServiceCheckConfig(BaseModel):
    """An app on this host whose health endpoint is checked over HTTP (services/checker.py)."""
    model_config = ConfigDict(hide_input_in_errors=True)

    id: str
    name: str
    url: str  # http(s), no credentials; a query is allowed
    enabled: bool = True
    timeout_seconds: float = Field(5.0, ge=0.5, le=60)
    ca_file: Optional[str] = None  # PEM CA to verify an HTTPS endpoint against (e.g. the home CA)
    open_url: Optional[str] = None  # the link the dashboard shows (how people reach the app); `url` is never a link
    description: Optional[str] = None
    notify: bool = True  # False: the dashboard shows it, ntfy never hears about it

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return http_url(value, "url", allow_query=True)

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
    # 1 s, not 5, at the bottom: the UI test seed checks every 2 s so its incidents open within the run
    interval_seconds: float = Field(30.0, ge=1, le=3600)
    failures_for_incident: int = Field(3, ge=1, le=100)  # failed checks in a row before a service_down incident
    # a service must fail this long before ntfy hears of it (restarts are quiet)
    notify_after_seconds: float = Field(300.0, ge=0, le=86400)
    services: List[ServiceCheckConfig] = Field(default_factory=list)
