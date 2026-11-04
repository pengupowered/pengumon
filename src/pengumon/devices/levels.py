"""The router-levels config section (`router_levels:`): where the router gauges and the snapshot status turn amber
and red (devices/monitors.py). Plain pydantic, no import of the app, so pengumon.config can name it.

`wan_critical_pct` must stay above classifier.busy_load_pct (the WAN gauge warns from there); AppConfig checks
that, since it spans two sections."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouterLevelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_warning_pct: float = Field(90.0, ge=10, le=100)
    mem_warning_pct: float = Field(85.0, ge=10, le=100)
    mem_critical_pct: float = Field(95.0, ge=10, le=100)  # out-of-memory risk
    wan_critical_pct: float = Field(95.0, ge=10, le=100)  # a line this full is saturated

    @model_validator(mode="after")
    def _ordered(self):
        if not self.mem_warning_pct < self.mem_critical_pct:
            raise ValueError("router_levels: mem_warning_pct must be below mem_critical_pct")
        return self
