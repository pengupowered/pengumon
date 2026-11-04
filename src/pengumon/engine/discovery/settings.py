"""The discovery config section (`discovery:`): the background route-change checks (drift.py, docs/discovery.md)."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.settings.general import time_of_day


class DiscoverySettings(BaseModel):
    """`background`: rerun discovery quietly once a day (and after an incident ends) and say when the route changed.
    `asn_lookup`: let those background runs ask Team Cymru who operates each public hop (off: nothing about the path
    leaves this computer on a background run). Discovery runs you start yourself are not affected.
    `daily_check_time`: "HH:MM" in general.timezone for the daily run; None keeps a random minute picked at start."""
    model_config = ConfigDict(extra="forbid")

    background: bool = True
    asn_lookup: bool = False
    daily_check_time: Optional[str] = Field(None, json_schema_extra={"ui_type": "time"})

    @field_validator("daily_check_time")
    @classmethod
    def _hhmm(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return time_of_day(value, "daily_check_time")
