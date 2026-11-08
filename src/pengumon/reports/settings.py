"""The report config sections: `uptime:` (what the colored uptime strips call an outage, reports/uptime.py) and
`reports:` (the failing-hop rule of the report and the dossier, reports/common.py). Plain pydantic, no import of the
app, so pengumon.config can name them."""

from pydantic import BaseModel, ConfigDict, Field


class UptimeConfig(BaseModel):
    """A block with `outage_dead_minutes` minutes of every probe lost is an outage; one with `degraded_loss_pct` %
    loss, or one such minute, is degraded. Separate from the incident rules (classifier:)."""
    model_config = ConfigDict(extra="forbid")

    outage_dead_minutes: int = Field(2, ge=1, le=60)
    degraded_loss_pct: float = Field(1.0, ge=0.1, le=50)


class ReportsConfig(BaseModel):
    """A hop counts as lossy, and as failing, from `material_loss_points` percentage points above the hops before
    it."""
    model_config = ConfigDict(extra="forbid")

    material_loss_points: float = Field(0.5, ge=0.1, le=20)
