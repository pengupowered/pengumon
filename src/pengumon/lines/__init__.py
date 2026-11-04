"""Access-line technologies: how a line's readings are judged, and how its faults are described, shared by every
driver for that technology."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LineWording:
    """How diagnoses phrase where a fault sits, per line technology.

    The gateway hop is the gateway (modem) answering on its home-side address: those probes never use the access line,
    so loss there is the gateway itself or the link from the router to it (`gateway` names the box). The access line
    runs from the gateway to the provider's first router: loss from that hop on, while the gateway answers, is the
    line or that router (`outage`, `loss`)."""

    outage: str  # the provider's first hop stopped answering ("Possible causes: {outage}.")
    loss: str    # it loses part of the traffic ("Possible causes: {loss}, or the hop deprioritising ping replies.")
    gateway: str = "the gateway (modem)"  # the box at the home end of the line, as the gateway diagnoses name it


GENERIC = LineWording(
    outage="the line to the provider going down (lost signal or sync) or an outage at the provider's first router",
    loss="an impaired line to the provider or congestion at the provider's first router",
)
WORDING = {
    "docsis": LineWording(
        outage="the cable line losing signal or sync (the coax into the home or the provider's cable plant) or an "
               "outage at the provider's first router (the CMTS, a neighbourhood outage)",
        loss="cable line (RF) impairment or congestion at the provider's first router (the CMTS node)",
        gateway="the cable modem (gateway)",
    ),
}


def wording(technology: Optional[str]) -> LineWording:
    """The wording for a line technology; the generic one when it is unknown (no access-line device)."""
    return WORDING.get(technology or "", GENERIC)


def __getattr__(name: str):
    """`CONTRIBUTIONS`, technology -> its AI-pack contribution class, loaded on first use: the contribution module needs
    pengumon.capabilities, which itself imports this package, so a plain import here would be circular."""
    if name == "CONTRIBUTIONS":
        from pengumon.lines.docsis_contrib import DocsisContribution
        return {"docsis": DocsisContribution}
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
