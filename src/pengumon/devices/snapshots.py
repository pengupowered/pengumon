"""The router's metrics snapshot."""

from datetime import datetime
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field


class RouterMetricsSnapshot(BaseModel):
    """Summary snapshot of the router's metrics (WAN rates, CPU, RAM, clients)."""
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    wan_rx_bytes: Optional[int] = None
    wan_tx_bytes: Optional[int] = None
    wan_rx_rate_mbps: Optional[float] = None  # None: no rate this poll (first poll, counters unreadable or reset)
    wan_tx_rate_mbps: Optional[float] = None
    cpu_usage_pct: Optional[float] = None
    mem_usage_pct: Optional[float] = None
    client_count: Optional[int] = None
    wan_rx_load_pct: Optional[float] = None  # download / the line's capacity; None while the capacity is unknown
    wan_tx_load_pct: Optional[float] = None
    # cpu, mem, wan_rx, wan_tx -> good/warn/critical, decided in pengumon.devices.monitors
    grades: Dict[str, str] = Field(default_factory=dict)
    model_name: Optional[str] = None  # the device's `model` in config.yaml, else the model its driver reports (if any)
    status: Literal["healthy", "warning", "critical", "unconfigured", "error"] = "unconfigured"
    error_message: Optional[str] = None
    error_kind: Optional[str] = Field(default=None, exclude=True)  # as on DocsisSignalSnapshot
