"""Access-line technologies: how a line's readings are judged, and how its faults are described, shared by every
driver for that technology."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LineWording:
    """How diagnoses phrase the possible causes of a fault on the access line (between the gateway and the ISP's
    first hops), per line technology."""

    outage: str  # the line or the ISP's first hop stopped passing traffic ("Possible causes: {outage}.")
    loss: str    # it loses part of the traffic ("Possible causes: {loss}, or the hop deprioritising ping replies.")


GENERIC = LineWording(
    outage="the access line to the ISP going down or an outage at the ISP's first hop",
    loss="access-line impairment, congestion at the ISP's first hop",
)
WORDING = {
    "docsis": LineWording(
        outage="a coaxial line sync drop or a CMTS neighborhood outage",
        loss="coaxial RF plant impairment, CMTS node congestion",
    ),
}


def wording(technology: Optional[str]) -> LineWording:
    """The wording for a line technology; the generic one when it is unknown (no access-line device)."""
    return WORDING.get(technology or "", GENERIC)
