"""The discovery config section (`discovery:`): the background route-change checks (drift.py, docs/discovery.md)."""

from pydantic import BaseModel, ConfigDict


class DiscoverySettings(BaseModel):
    """`background`: rerun discovery quietly once a day (and after an incident ends) and say when the route changed.
    `asn_lookup`: let those background runs ask Team Cymru who operates each public hop (off: nothing about the path
    leaves this computer on a background run). Discovery runs you start yourself are not affected."""
    model_config = ConfigDict(extra="forbid")

    background: bool = True
    asn_lookup: bool = False
