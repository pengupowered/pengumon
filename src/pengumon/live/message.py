"""The message pushed to the dashboard over the WebSocket."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from pengumon.incidents.model import SystemDiagnosis
from pengumon.models.probes import TargetStats


class LiveStreamMessage(BaseModel):
    """Payload pushed to WebSocket clients every probe tick."""
    timestamp: float
    diagnosis: SystemDiagnosis
    targets: List[TargetStats]
    # Every enabled device by id: a summary (status, grades, headline figures, no channels; see
    # pengumon.devices.summary).
    devices: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # The line capacity and the load on it: {load_pct, down_mbps, up_mbps}; each None while unknown.
    line: Optional[Dict[str, Optional[float]]] = None
    # Changes whenever an incident is created, resolved or annotated (the dashboard fetches the incident list when it does).
    incidents_version: int = 0
