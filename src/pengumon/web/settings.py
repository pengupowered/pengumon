"""The dashboard's config sections: `dashboard:` (the default layout) and `display:` (the Grafana links and the
latency chart's first range). The card ids are the panel folders that pengumon.ui discovers, so a new panel needs no
change here."""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.observability.grafana import check_url
from pengumon.ui import panel_ids


class DashboardConfig(BaseModel):
    """The default layout of the dashboard for every browser: `order` lists cards first (those left out follow in
    their usual order), `hidden` the cards not shown. A browser's own choice in the Layout dialog wins over it."""
    model_config = ConfigDict(extra="forbid")
    order: List[str] = Field(default_factory=list, json_schema_extra={"ui_type": "layout"})
    hidden: List[str] = Field(default_factory=list, json_schema_extra={"ui_type": "layout"})

    @field_validator("order", "hidden")
    @classmethod
    def _known_unique_ids(cls, ids: List[str]) -> List[str]:
        known = panel_ids()
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"unknown dashboard card {unknown[0]!r}; the cards are {', '.join(known)}")
        if len(set(ids)) != len(ids):
            raise ValueError("a dashboard card is listed twice")
        return ids


class DisplayConfig(BaseModel):
    """`grafana_url`: Grafana's base URL for the header's Dashboard / Logs / Traces links (PENGUMON_GRAFANA_URL wins
    when set); None: no links. `default_latency_range`: the range the latency chart opens on (a browser's own choice
    wins)."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    grafana_url: Optional[str] = None
    default_latency_range: Literal["live", "15m", "1h", "24h"] = "live"

    @field_validator("grafana_url")
    @classmethod
    def _grafana(cls, value: Optional[str]) -> Optional[str]:
        return check_url(value or "", "grafana_url") or None
