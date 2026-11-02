"""The classifier's verdict for one round: what it concluded, as a frozen value with a machine-readable kind."""

import dataclasses
from dataclasses import dataclass
from typing import Tuple

from pengumon.incidents.taxonomy import CauseCode, IncidentKind
from pengumon.incidents.model import SeverityType, StatusCode


@dataclass(frozen=True)
class Verdict:
    """What the classifier concluded this round. `kind` drives control flow; the texts are for people."""

    kind: IncidentKind
    status: StatusCode
    cause: CauseCode
    title: str
    description: str
    severity: SeverityType
    affected: Tuple[str, ...] = ()

    @property
    def is_fault(self) -> bool:
        return self.status in ("degraded", "down")

    def with_(self, **changes) -> "Verdict":
        return dataclasses.replace(self, **changes)


NOMINAL = Verdict(
    "nominal",
    "healthy",
    "nominal",
    "All Network Hops Operational",
    "Every hop answers, with no packet loss and normal latency.",
    "info",
)
