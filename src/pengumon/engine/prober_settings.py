"""The prober's config sections: `prober:` (with its `hop_status:` colours) and `dns_probes:`. Plain pydantic models
that import nothing of the app, so pengumon.config can name them without an import cycle."""

from typing import Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from pengumon.models.hosts import validate_color


class HopStatusConfig(BaseModel):
    """Per-target status colours (up / degraded / down) on the dashboard. Stricter than the classifier's spike
    thresholds on purpose: one hop turns amber before the line as a whole counts as degraded."""
    down_loss_fraction: float = Field(default=0.4, ge=0, le=1)       # share of the last 10 probes lost, strictly above: down
    degraded_loss_fraction: float = Field(default=0.1, ge=0, le=1)   # strictly above: degraded
    local_latency_ms: float = Field(default=20.0, ge=0)
    local_jitter_ms: float = Field(default=10.0, ge=0)
    gateway_latency_ms: float = Field(default=30.0, ge=0)
    gateway_jitter_ms: float = Field(default=15.0, ge=0)
    public_latency_ms: float = Field(default=60.0, ge=0)
    public_jitter_ms: float = Field(default=25.0, ge=0)
    dns_latency_ms: float = Field(default=100.0, ge=0)
    dns_jitter_ms: float = Field(default=40.0, ge=0)

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
    interval_seconds: float = 1.0
    timeout_seconds: float = 1.0
    ping_count_per_probe: int = 1
    hop_status: HopStatusConfig = Field(default_factory=HopStatusConfig)


class DnsProbeConfig(BaseModel):
    id: str
    name: str
    domain: str = "google.com"
    nameserver: Optional[str] = None
    record_type: str = "A"
    enabled: bool = True
    color: Optional[str] = None  # chart colour #rrggbb; None = the DNS category's colour

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: Optional[str]) -> Optional[str]:
        return validate_color(v)
