"""The classifier's config section (`classifier:`). Kept outside the engine/classifier package on purpose: importing
anything inside it runs the package's __init__ (the OutageClassifier and the repository), and pengumon.config only
needs this model."""

from pydantic import BaseModel, field_validator

from pengumon.models.hosts import validate_host


class ClassifierConfig(BaseModel):
    local_latency_spike_threshold_ms: float = 25.0
    local_jitter_spike_threshold_ms: float = 15.0
    gateway_latency_spike_threshold_ms: float = 35.0
    gateway_jitter_spike_threshold_ms: float = 20.0
    public_latency_spike_threshold_ms: float = 80.0
    public_jitter_spike_threshold_ms: float = 45.0
    dns_timeout_threshold_ms: float = 300.0
    consecutive_failures_for_incident: int = 3
    consecutive_failures_for_latency_incident: int = 10
    # WAN load (% of line capacity) at which latency, and loss seen only at ISP hops, counts as expected.
    busy_load_pct: float = 60.0
    # Traced (traceroute) when an incident starts, to record the path at that moment.
    path_trace_target: str = "1.1.1.1"

    @field_validator("path_trace_target")
    @classmethod
    def _valid_host(cls, v: str) -> str:
        return validate_host(v)
