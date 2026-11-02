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



class PathTraceHop(BaseModel):
    """Single hop measurement in an automated path trace."""
    hop: int
    ip: str
    hostname: Optional[str] = None
    latency_ms: Optional[float] = None
    loss_pct: float = 0.0
