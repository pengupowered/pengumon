"""The dashboard's config sections: `dashboard:` (the default layout and card colors) and `display:` (the Grafana links,
the latency chart's first range, the theme and the custom themes). The card ids are the panel folders that
pengumon.web.ui discovers and the theme ids the folders of pengumon.web.themes, so a new panel or theme needs no change
here."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pengumon.observability.grafana import check_url
from pengumon.web import theme_checks
from pengumon.web import themes as theme_rules
from pengumon.web.ui import panel_ids

# The tokens a card's own colors set (Layout > a card's Colors): the accent and the eight series colors.
CARD_TOKENS = ("--accent", *(f"--s{i}" for i in range(1, 9)))


class CardColors(BaseModel):
    """A card's colors: another theme's accent and series colors (`theme`), or its own (`custom`, token -> #rrggbb,
    of CARD_TOKENS). Neither: the page theme's."""
    model_config = ConfigDict(extra="forbid")
    theme: Optional[str] = None
    custom: Dict[str, str] = Field(default_factory=dict)

    @field_validator("theme")
    @classmethod
    def _theme_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not theme_rules.ID_RE.fullmatch(value):
            raise ValueError("a theme id is lower-case letters, digits and dashes")
        return value

    @field_validator("custom")
    @classmethod
    def _colors(cls, value: Dict[str, str]) -> Dict[str, str]:
        for name, color in value.items():
            if name not in CARD_TOKENS:
                raise ValueError(f"{name!r} is not a card color; they are {', '.join(CARD_TOKENS)}")
            if not isinstance(color, str) or not theme_rules.HEX6.fullmatch(color):
                raise ValueError(f"{name}: a color like #1a2b3c")
        return value


class DashboardConfig(BaseModel):
    """The default layout of the dashboard for every browser: `order` lists cards first (those left out follow in
    their usual order), `hidden` the cards not shown. A browser's own choice in the Layout dialog wins over it."""
    model_config = ConfigDict(extra="forbid")
    order: List[str] = Field(default_factory=list, json_schema_extra={"ui_type": "layout"})
    hidden: List[str] = Field(default_factory=list, json_schema_extra={"ui_type": "layout"})
    colors: Dict[str, CardColors] = Field(default_factory=dict, json_schema_extra={"ui_type": "layout"})

    @field_validator("colors")
    @classmethod
    def _known_cards(cls, value: Dict[str, CardColors]) -> Dict[str, CardColors]:
        known = panel_ids()
        unknown = [i for i in value if i not in known]
        if unknown:
            raise ValueError(f"unknown dashboard card {unknown[0]!r}; the cards are {', '.join(known)}")
        return value

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


class ThemeItem(BaseModel):
    """A custom theme (Settings > Display): a theme.json without skin or effect, checked by pengumon.web.themes'
    rules for custom themes (an id starting with "custom-", colors for every color token of each mode, text sizes
    and corner radii in their ranges, nothing else)."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    name: str
    description: str = ""
    modes: List[str]
    tokens: Dict[str, Dict[str, str]]
    tags: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_theme(self) -> "ThemeItem":
        _, problems = theme_rules.check_theme(self.model_dump(), custom=True)
        if problems:
            raise ValueError("; ".join(problems[:3]) + (f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""))
        return self


class DisplayConfig(BaseModel):
    """`grafana_url`: Grafana's base URL for the header's Dashboard / Logs / Traces links (PENGUMON_GRAFANA_URL wins
    when set); None: no links. `default_latency_range`: the range the latency chart opens on (a browser's own choice
    wins). `theme`: the theme every browser starts with (a browser's own choice in the header wins; an id that names
    no theme shows the default). `effects`: whether a theme's animated effect runs (a browser can turn it off).
    `themes`: the custom themes."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    grafana_url: Optional[str] = None
    default_latency_range: Literal["live", "15m", "1h", "24h"] = "live"
    theme: str = Field(theme_rules.DEFAULT_ID, json_schema_extra={"ui_type": "theme"})
    effects: bool = True
    themes: List[ThemeItem] = Field(default_factory=list, max_length=theme_rules.MAX_CUSTOM)

    @field_validator("theme")
    @classmethod
    def _theme_id(cls, value: str) -> str:
        if not theme_rules.ID_RE.fullmatch(value):
            raise ValueError("a theme id is lower-case letters, digits and dashes")
        return value

    @field_validator("themes")
    @classmethod
    def _unique_theme_ids(cls, items: List[ThemeItem]) -> List[ThemeItem]:
        ids = [t.id for t in items]
        for i in ids:
            if ids.count(i) > 1:
                raise ValueError(f"two custom themes have the id {i!r}; each id is used once")
        return items

    @field_validator("grafana_url")
    @classmethod
    def _grafana(cls, value: Optional[str]) -> Optional[str]:
        return check_url(value or "", "grafana_url") or None


def theme_warnings(items: List[ThemeItem]) -> List[Dict[str, str]]:
    """What the readability rules of the built-in themes (web/theme_checks.py: text contrast, status colors and series
    colors apart) find wrong in the custom themes, as warnings: {"where": "values.display.themes", "message": ...}.
    Not an error: the colors are the owner's call (docs/themes.md), but a theme whose text matches its background
    hides the page for every browser that uses it."""
    out: List[Dict[str, str]] = []
    for theme in theme_rules.custom_themes(list(items)):
        for problem in theme_checks.problems(theme):
            out.append({"where": "values.display.themes", "message": f"Custom theme {problem}"})
    return out
