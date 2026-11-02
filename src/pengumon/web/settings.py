"""The dashboard's config section (`dashboard:`): the default layout. The card ids are the panel folders that
pengumon.ui discovers, so a new panel needs no change here."""

from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.ui import panel_ids


class DashboardConfig(BaseModel):
    """The default layout of the dashboard for every browser: `order` lists cards first (those left out follow in
    their usual order), `hidden` the cards not shown. A browser's own choice in the Layout dialog wins over it."""
    model_config = ConfigDict(extra="forbid")
    order: List[str] = Field(default_factory=list)
    hidden: List[str] = Field(default_factory=list)

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
