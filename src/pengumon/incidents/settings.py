"""The incidents config section (`incidents:`): how drops are joined into outages (incidents.outages) and the hours in
which an outage at the provider is labeled likely maintenance. Read through the config on every call, so both apply
live and regroup the history."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pengumon.settings.general import time_of_day


class IncidentsConfig(BaseModel):
    """`group_gap_minutes` 0: no grouping, every drop is its own outage. The maintenance window is read in
    general.timezone and may cross midnight; both ends empty: nothing is ever labeled."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    group_gap_minutes: int = Field(10, ge=0, le=120)
    maintenance_start: Optional[str] = Field("00:00", json_schema_extra={"ui_type": "time"})
    maintenance_end: Optional[str] = Field("06:00", json_schema_extra={"ui_type": "time"})

    @field_validator("maintenance_start", "maintenance_end")
    @classmethod
    def _hhmm(cls, value: Optional[str], info) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return time_of_day(value, info.field_name)

    @model_validator(mode="after")
    def _consistent(self):
        if (self.maintenance_start is None) != (self.maintenance_end is None):
            raise ValueError("maintenance_start and maintenance_end go together: set both or neither")
        if self.maintenance_start is not None and self.maintenance_start == self.maintenance_end:
            raise ValueError("the maintenance window's start and end must differ")
        return self
