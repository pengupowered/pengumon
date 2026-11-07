"""GET /api/themes: every theme (built in and custom) with what the header menu, a card's colours and Settings'
Export and Duplicate need, the default from Settings and whether animated effects are on (docs/themes.md). The page
starts with a short list of its own (<html data-looks>); this is read when a menu or dialog needs more."""

from fastapi import APIRouter, Response

from pengumon.api.common import StateDep, json_response
from pengumon.web import themes
from pengumon.web.assets import ASSET_VERSION
from pengumon.web.settings import DisplayConfig

router = APIRouter(prefix="/api")


@router.get("/themes")
async def get_themes(state: StateDep) -> Response:
    """{default, effects, themes: [{id, name, description, modes, tags, custom, skin, effect (module URL or null),
    effect_on, css, views, chart, theme (an importable theme.json: colours per mode)}], looks: the page's short list (as
    <html data-looks>, the same function builds both: js/themes.js replaces its list with it)}, built-in themes first."""
    display = state.config.display if state.config is not None else DisplayConfig()
    every = themes.all_themes(display.themes)  # once: each custom theme is checked once per request
    return json_response({"default": themes.resolve_default(display.theme, every), "effects": display.effects,
                          "themes": [themes.api_entry(t, ASSET_VERSION) for t in every],
                          "looks": themes.page_list(every, ASSET_VERSION)})
