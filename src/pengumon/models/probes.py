"""Probe targets, their samples and their rolling statistics."""

import re
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from pengumon.models.hosts import ID_PATTERN, validate_color, validate_host


CategoryType = Literal["local", "gateway", "isp", "public", "dns", "custom"]
# How a hop is probed: ICMP echo (ping), a TCP handshake, or an HTTP(S) request (engine.probe_tcp, engine.probe_http)
ProbeType = Literal["icmp", "tcp", "http"]

# At most this many hops may be probed by TCP or HTTP (each opens a connection every second)
MAX_SOCKET_HOPS = 8
# The Fetch standard's "bad ports": services where a client that does not speak their protocol causes trouble (mail, login,
# file sharing, IRC). Refused for TCP and HTTP probes; 53 and 22 are among them (probe a resolver on 443 or 853).
BLOCKED_PORTS = frozenset({
    0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110,
    111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061,
    6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
})
_PATH_PATTERN = re.compile(r"/[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


class TargetConfig(BaseModel):
    """Configuration for a single probe target with strict validation."""
    id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=100)
    category: CategoryType = "custom"
    address: str = Field(..., max_length=255)
    enabled: bool = True
    record_type: Optional[str] = "A"
    nameserver: Optional[str] = None
    color: Optional[str] = None  # chart color #rrggbb; None = the category's color
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
    # role's. Only the hop's color: the incident rules still judge the role's average (the owner's decision).
    warn_latency_ms: Optional[float] = Field(None, gt=0, le=10000)
    # How the hop is probed (engine.prober): icmp (ping), tcp (a handshake to `port`) or http (a request to address:port).
    # An http hop has the role custom: its time includes the server's own, so it is charted but never judged as a stage.
    probe: ProbeType = "icmp"
    port: Optional[int] = Field(None, ge=1, le=65535)  # tcp: required; http: None = 443 (https) or 80 (http)
    http_scheme: Literal["http", "https"] = "https"
    http_path: str = Field("/", max_length=255)
    http_verb: Literal["HEAD", "GET"] = "HEAD"

    @field_validator("group", "device_id")
    @classmethod
    def validate_optional_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not ID_PATTERN.fullmatch(v):
            raise ValueError("must be 1-64 letters, digits, hyphens or underscores")
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v in BLOCKED_PORTS:
            raise ValueError(f"port {v} is refused: it belongs to a service (mail, login, file sharing, ...) that "
                             "should not get a connection every second; use the port of a web or other open service")
        return v

    @field_validator("http_path")
    @classmethod
    def validate_http_path(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/") or v.startswith("//"):
            raise ValueError("the path must start with a single /")
        if "?" in v or "#" in v:
            raise ValueError("a query string or fragment is not allowed (that is where tokens and passwords hide)")
        if not _PATH_PATTERN.fullmatch(v):
            raise ValueError("the path may only hold letters, digits and - . _ ~ ! $ & ' ( ) * + , ; = : @ % /")
        if _BAD_PERCENT.search(v):
            raise ValueError("every % in the path must be followed by two hex digits")
        return v

    @model_validator(mode="after")
    def validate_probe(self):
        defaults_http = (self.http_scheme, self.http_path, self.http_verb) == ("https", "/", "HEAD")
        if self.probe == "icmp":
            if self.port is not None:
                raise ValueError("port is only used by TCP and HTTP probes")
            if not defaults_http:
                raise ValueError("http_scheme, http_path and http_verb are only used by HTTP probes")
        elif self.probe == "tcp":
            if self.port is None:
                raise ValueError("a TCP probe needs a port")
            if not defaults_http:
                raise ValueError("http_scheme, http_path and http_verb are only used by HTTP probes")
        elif self.category != "custom":
            raise ValueError("an HTTP probe times the server's answer (connection, TLS and the server's own work), "
                             "not a round trip: its hop must have the role custom (Other)")
        return self

    @field_validator("color")
    @classmethod
    def validate_color_value(cls, v: Optional[str]) -> Optional[str]:
        return validate_color(v)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        v = v.strip()
        if not ID_PATTERN.fullmatch(v):
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


def probe_identity(t: "TargetConfig") -> tuple:
    """What makes two hops at one address the same probe: a change of any of these starts the hop's statistics over."""
    return (t.probe, t.port, t.http_scheme, t.http_path, t.http_verb)


def probe_label(t: "TargetConfig") -> Optional[str]:
    """The method as the page shows it ("TCP 443", "HTTPS HEAD /health"); None for a ping."""
    if t.probe == "tcp":
        return f"TCP {t.port}"
    if t.probe != "http":
        return None
    label = f"{t.http_scheme.upper()} {t.http_verb}"
    if t.port is not None and t.port != (443 if t.http_scheme == "https" else 80):
        label += f" :{t.port}"
    if t.http_path != "/":
        label += " " + (t.http_path if len(t.http_path) <= 40 else t.http_path[:40] + "…")
    return label


def probe_words(t: "TargetConfig") -> str:
    """The method in a sentence for the AI pack and the dossier: never the path (it can name something private)."""
    if t.probe == "tcp":
        return f"TCP port {t.port}"
    if t.probe == "http":
        return f"{t.http_scheme.upper()} request"
    return ""


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
    color: Optional[str] = None  # from the target's config; None = the category's color
    group: Optional[str] = None  # from the target's config: hops of one role with the same group are one node
    short_label: Optional[str] = None  # its role words (engine.path_rules.hop_roles); None while it is switched off
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
    # How the hop is probed (None: a DNS lookup), the method as the page shows it (None for a ping and for DNS) and one
    # sentence on what the figure is and how exact (engine.prober.NetworkProber._probe_note)
    probe: Optional[ProbeType] = None
    probe_label: Optional[str] = None
    probe_note: Optional[str] = None



class PathTraceHop(BaseModel):
    """Single hop measurement in an automated path trace."""
    hop: int
    ip: str
    hostname: Optional[str] = None
    latency_ms: Optional[float] = None
    loss_pct: float = 0.0
