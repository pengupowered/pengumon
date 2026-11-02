"""The DOCSIS signal readings: a channel and the summary snapshot of the modem."""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class DocsisChannel(BaseModel):
    """DOCSIS Downstream or Upstream RF Channel metrics."""
    channel_id: int
    direction: Literal["downstream", "upstream"]
    frequency_mhz: float
    power_dbmv: float
    snr_db: Optional[float] = None
    modulation: Optional[str] = None
    correcteds: Optional[int] = 0
    uncorrectables: Optional[int] = 0
    uncorrectables_recent: int = 0  # New uncorrectable errors during recent poll window
    unerrored: Optional[int] = None  # Cumulative error-free codewords, when the modem reports them; read live, not stored
    grades: Dict[str, str] = Field(default_factory=dict)  # power, snr, uncorrectables -> good/warn/critical (pengumon.grades)


class DocsisSignalSnapshot(BaseModel):
    """Summary snapshot of Cable Modem DOCSIS health."""
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    downstream_power_avg: Optional[float] = None
    downstream_snr_avg: Optional[float] = None
    upstream_power_avg: Optional[float] = None
    uncorrectables_total: int = 0  # Cumulative since modem boot
    uncorrectables_recent: int = 0  # Active error count in last poll interval
    uncorrectables_rate_per_sec: float = 0.0  # Live error rate per second
    # Context of the new uncorrectables, set when there are some (see DocsisErrorTracker): the seconds the count covers,
    # uncorrectable / all codewords of that interval (None: the modem does not report error-free codewords, or no
    # baseline), the downstream channels affected with their frequency span, whether the errors recurred on
    # consecutive reads, a sentence saying how serious this is, and whether the probes to the gateway and ISP hops
    # lost packets in that window (None: not checked).
    uncorrectables_interval_seconds: Optional[float] = None
    uncorrectables_ratio: Optional[float] = None
    errors_affected_channels: int = 0
    errors_span_mhz: Optional[List[float]] = None
    errors_recurring: bool = False
    errors_note: Optional[str] = None
    errors_probe_loss: Optional[bool] = None
    channels: List[DocsisChannel] = Field(default_factory=list)
    # ds_power, ds_snr, us_power, uncorrectables -> good/warn/critical, decided in pengumon.lines.docsis
    grades: Dict[str, str] = Field(default_factory=dict)
    status: Literal["healthy", "warning", "critical", "unconfigured", "error"] = "unconfigured"
    error_message: Optional[str] = None
    # For status "error": the DriverError kind (timeout, unreachable, auth, protocol, ...). Stored, not sent to
    # clients. Only timeout/unreachable say the device could not be reached; None on rows from before schema v7.
    error_kind: Optional[str] = Field(default=None, exclude=True)
