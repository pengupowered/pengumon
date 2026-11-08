"""The prober's config sections: `prober:` (with its `hop_status:` colors) and `dns_probes:`. Plain pydantic models
that import nothing of the app, so pengumon.config can name them without an import cycle.

`prober.interval_seconds` (the round) is a bootstrap key: it stays in config.yaml and is never on the Settings page;
tests run it at 0.05 s."""

import ipaddress
from typing import Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from pengumon.models.hosts import ID_PATTERN, validate_color, validate_host

_MS = dict(ge=0, le=10000)  # a hop color level, ms
DNS_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT")
HISTORY_PROBES = 60  # the probes the prober keeps per target (TargetStats.recent_latencies): about the last minute
DEFAULT_WINDOW_PROBES = 10  # the default of classifier.window_probes: the last probes loss and latency are judged over


class HopStatusConfig(BaseModel):
    """Per-target status colors (up / degraded / down) on the dashboard. Stricter than the classifier's spike
    thresholds on purpose: one hop turns amber before the line as a whole counts as degraded. Loss is judged over
    the classifier's window (classifier.window_probes), latency and jitter over the prober's whole history."""
    down_loss_fraction: float = Field(default=0.4, ge=0, le=1)       # share of the window lost, strictly above: down
    degraded_loss_fraction: float = Field(default=0.1, ge=0, le=1)   # strictly above: degraded
    local_latency_ms: float = Field(default=20.0, **_MS)
    local_jitter_ms: float = Field(default=10.0, **_MS)
    gateway_latency_ms: float = Field(default=30.0, **_MS)
    gateway_jitter_ms: float = Field(default=15.0, **_MS)
    public_latency_ms: float = Field(default=60.0, **_MS)
    public_jitter_ms: float = Field(default=25.0, **_MS)
    dns_latency_ms: float = Field(default=100.0, **_MS)
    dns_jitter_ms: float = Field(default=40.0, **_MS)

    @model_validator(mode="after")
    def _degraded_below_down(self):
        if not self.degraded_loss_fraction < self.down_loss_fraction:
            raise ValueError("degraded_loss_fraction must be below down_loss_fraction")
        return self

    def latency_limits(self, category: str) -> Optional[Tuple[float, float]]:
        """(latency, jitter) limits for the category; None for isp and custom, which are judged on loss only."""
        return {
            "local": (self.local_latency_ms, self.local_jitter_ms),
            "gateway": (self.gateway_latency_ms, self.gateway_jitter_ms),
            "public": (self.public_latency_ms, self.public_jitter_ms),
            "dns": (self.dns_latency_ms, self.dns_jitter_ms),
        }.get(category)


class ProberConfig(BaseModel):
    interval_seconds: float = 1.0  # bootstrap: the probe round (config.yaml only)
    timeout_seconds: float = Field(1.0, ge=0.2, le=1.0)  # a ping (and, unless dns_timeout_seconds, a lookup) is lost after
    dns_timeout_seconds: Optional[float] = Field(None, ge=0.2, le=5)  # None: timeout_seconds
    ping_count_per_probe: int = 1  # read nowhere; the settings import drops it
    hop_status: HopStatusConfig = Field(default_factory=HopStatusConfig)

    @model_validator(mode="after")
    def _timeout_within_the_round(self):
        # Only a timeout someone set is held to the round: tests shorten the round to 0.05 s and keep the default.
        if "timeout_seconds" in self.model_fields_set and self.timeout_seconds > self.interval_seconds:
            raise ValueError("timeout_seconds must not be longer than the probe round (interval_seconds)")
        return self


class DnsProbeConfig(BaseModel):
    id: str
    name: str
    domain: str = "google.com"
    nameserver: Optional[str] = None  # an IP address; None (or empty): the system resolver, which ignores record_type
    record_type: str = "A"  # one of DNS_RECORD_TYPES
    enabled: bool = True
    color: Optional[str] = None  # chart color #rrggbb; None = the DNS category's color

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        v = v.strip()
        if not ID_PATTERN.fullmatch(v):
            raise ValueError("id must be 1-64 letters, digits, hyphens or underscores")
        return v

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, v: str) -> str:
        return validate_host(v)

    @field_validator("nameserver")
    @classmethod
    def _valid_nameserver(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        try:
            ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError("nameserver must be an IP address (or empty for the system resolver)") from None
        return v.strip()

    @field_validator("record_type")
    @classmethod
    def _valid_record_type(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in DNS_RECORD_TYPES:
            raise ValueError(f"record_type must be one of {', '.join(DNS_RECORD_TYPES)}")
        return v

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: Optional[str]) -> Optional[str]:
        return validate_color(v)
