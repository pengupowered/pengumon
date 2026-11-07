"""The classifier's config section (`classifier:`). Kept outside the engine/classifier package on purpose: importing
anything inside it runs the package's __init__ (the OutageClassifier and the repository), and pengumon.config only
needs this model.

Every number has a range: the Settings page, its import and config.yaml are all checked against these fields (the
registry in pengumon.settings.registry reads them, it never repeats them). The loss levels, the DNS rules and the
recovery count used to be literals in engine/classifier; their defaults here are those literals, so nothing changes
until someone sets them."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from pengumon.engine.prober_settings import DEFAULT_WINDOW_PROBES, HISTORY_PROBES
from pengumon.models.hosts import validate_host

_LEVEL = dict(ge=1, le=1000)  # a latency or jitter spike level, ms


class ClassifierConfig(BaseModel):
    local_latency_spike_threshold_ms: float = Field(25.0, **_LEVEL)
    local_jitter_spike_threshold_ms: float = Field(15.0, **_LEVEL)
    gateway_latency_spike_threshold_ms: float = Field(35.0, **_LEVEL)
    gateway_jitter_spike_threshold_ms: float = Field(20.0, **_LEVEL)
    public_latency_spike_threshold_ms: float = Field(80.0, **_LEVEL)
    public_jitter_spike_threshold_ms: float = Field(45.0, **_LEVEL)
    # The ISP hops' own latency level; None: the same as the internet's (public_latency_spike_threshold_ms)
    isp_latency_spike_threshold_ms: Optional[float] = Field(None, **_LEVEL)
    dns_timeout_threshold_ms: float = Field(300.0, ge=10, le=5000)  # "Slow DNS" above this median lookup time
    # Loss of the window's probes of a role: from degraded_loss_pct the role is degraded, from down_loss_pct down
    degraded_loss_pct: float = Field(15.0, ge=1, le=90)
    down_loss_pct: float = Field(40.0, ge=2, le=100)
    dns_degraded_loss_pct: float = Field(25.0, ge=1, le=100)  # DNS lookups lost from which DNS is degraded
    dns_cooldown_seconds: float = Field(90.0, ge=0, le=3600)  # after upstream trouble, DNS faults wait this long
    consecutive_failures_for_incident: int = Field(3, ge=1, le=600)  # faulty rounds (with loss) before an incident
    consecutive_failures_for_latency_incident: int = Field(10, ge=1, le=3600)  # latency/jitter only
    consecutive_successes_for_recovery: int = Field(3, ge=1, le=600)  # healthy rounds in a row that end one
    # WAN load (% of line capacity) at which latency, and loss seen only at ISP hops, counts as expected.
    busy_load_pct: float = Field(60.0, ge=10, le=100)
    # Traced (traceroute) when an incident starts, to record the path at that moment.
    path_trace_target: str = "1.1.1.1"
    # The window: each target's last N probes (one a round), over which loss, the median latency and the jitter are
    # judged (engine/window.py), here and for the hop colours' loss. At most what the prober keeps.
    window_probes: int = Field(DEFAULT_WINDOW_PROBES, ge=3, le=HISTORY_PROBES)

    @field_validator("path_trace_target")
    @classmethod
    def _valid_host(cls, v: str) -> str:
        return validate_host(v)

    @model_validator(mode="after")
    def _degraded_below_down(self):
        if not self.degraded_loss_pct < self.down_loss_pct:
            raise ValueError("degraded_loss_pct must be below down_loss_pct")
        return self
