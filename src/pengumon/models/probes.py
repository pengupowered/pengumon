"""Probe targets, their samples and their rolling statistics."""

import re
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from pengumon.models.hosts import ID_PATTERN, validate_color, validate_host


CategoryType = Literal["local", "gateway", "isp", "public", "dns", "custom"]


class TargetConfig(BaseModel):
    """Configuration for a single probe target with strict validation."""
    id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=100)
    category: CategoryType = "custom"
    address: str = Field(..., max_length=255)
    enabled: bool = True
    record_type: Optional[str] = "A"
    nameserver: Optional[str] = None
    color: Optional[str] = None  # chart colour #rrggbb; None = the category's colour
    # A host name is probed at its address of this family (4: IPv4, 6: IPv6); an IP literal is probed as it is
    family: Literal[4, 6] = 4
    # The path editor's fields (pengumon.engine.path): hops of one role sharing a group are one node on Path health;
    # device_id = the configured device (config.yaml devices:) this hop is (engine.device_links). Whether a hop ignores
    # ping is observed, never set (engine.path_rules): an "ignores_ping" in an older export or row is ignored.
    group: Optional[str] = Field(None, max_length=64)
    device_id: Optional[str] = Field(None, max_length=64)
    # The owner set "No device" on this hop: it is never linked by its address again (engine.device_links)
    device_unlinked: bool = False
    # This hop turns amber above this average latency instead of its role's level (prober.hop_status); None: the
    # role's. Only the hop's colour: the incident rules still judge the role's average (the owner's decision).
    warn_latency_ms: Optional[float] = Field(None, gt=0, le=10000)

    @field_validator("group", "device_id")
    @classmethod
    def validate_optional_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not ID_PATTERN.match(v):
            raise ValueError("must be 1-64 letters, digits, hyphens or underscores")
        return v

    @field_validator("color")
    @classmethod
    def validate_color_value(cls, v: Optional[str]) -> Optional[str]:
        return validate_color(v)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        v = v.strip()
        if not ID_PATTERN.match(v):
            raise ValueError("Target ID must be 1-64 alphanumeric characters, hyphens, or underscores.")
        return v

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return validate_host(v)

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        # Strip control characters
        return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", v.strip())


class TracerouteRequest(BaseModel):
    """Request model for on-demand traceroute."""
    target_host: str = Field(default="1.1.1.1", max_length=255)
    family: Literal[4, 6] = 4  # for a host name: trace to its IPv4 or IPv6 address (a literal is traced as it is)

    @field_validator("target_host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        return validate_host(v)



class ProbeSample(BaseModel):
    """Single probe measurement result."""
    target_id: str
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    success: bool
    latency_ms: Optional[float] = None
    packet_loss_pct: float = 0.0
    jitter_ms: Optional[float] = None
    error: Optional[str] = None


class TargetStats(BaseModel):
    """Multi-hop rolling statistics for a monitored target."""
    target_id: str
    name: str
    category: CategoryType
    address: str
    enabled: bool
    color: Optional[str] = None  # from the target's config; None = the category's colour
    group: Optional[str] = None  # from the target's config: hops of one role with the same group are one node
    sent: int = 0
    received: int = 0
    loss_pct: float = 0.0  # Cumulative lifetime loss %
    loss_recent_pct: float = 0.0  # Rolling loss % over recent 60-sample window
    min_ms: Optional[float] = None
    avg_ms: Optional[float] = None
    max_ms: Optional[float] = None
    last_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    recent_latencies: List[Optional[float]] = Field(default_factory=list)  # Last N samples
    status: Literal["up", "degraded", "down"] = "up"
    # No reply while a later stage of the path answers: up, only ignoring ping (engine.path_rules). Set every round.
    ignores_ping: bool = False
    # An IPv6 target while this host has no IPv6 (no route, or none in the OS): not probed, no samples, no verdict
    # (engine.prober; re-checked every few minutes). Shown as "IPv6 unavailable on this host", like ignores_ping.
    ipv6_unavailable: bool = False
    # This host cannot send pings at all now (Prober.probing_problem): nothing is measured, so the figures are stale and
    # the hop is neither up nor down. Set every round.
    probing_unavailable: bool = False



class PathTraceHop(BaseModel):
    """Single hop measurement in an automated path trace."""
    hop: int
    ip: str
    hostname: Optional[str] = None
    latency_ms: Optional[float] = None
    loss_pct: float = 0.0
