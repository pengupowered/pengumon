"""The dashboard page (/) and the favicon. The page is the template web/templates/index.html, which includes the
panels and dialogs that pengumon.web.ui discovers."""

import json
from functools import lru_cache

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from pengumon.state import get_state
from pengumon.observability import grafana
from pengumon.web import themes
from pengumon.web.ui import ui as discovered_ui
from pengumon.web.assets import ASSET_VERSION, ASSET_VERSIONS, STATIC_DIR, TEMPLATES_DIR
from pengumon.web.settings import DashboardConfig, DisplayConfig

router = APIRouter()
# the page's own templates, and the panel and dialog folders (pengumon.web.ui) whose partials it includes
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(STATIC_DIR)])


@lru_cache(maxsize=1)
def page_modules() -> dict:
    """What the page loads (body data-modules): `dialogs` at start, `panels` after them, and `lazy` dialogs that load the
    first time something opens them. A dialog is lazy when its folder has a dialog.json with a "lazy" entry:
    {"actions": [click actions that open it], "modals": [modal ids that the generic openModal action opens],
    "hash": "<location hash that opens it>", "info": "<a flag of /api/info that opens it>"} (js/panels/index.js uses
    these to load it in time)."""
    modules = discovered_ui().modules
    eager, lazy = [], []
    for part in discovered_ui().dialogs:
        if not part.module:
            continue
        manifest = (STATIC_DIR / part.module).with_name("dialog.json")
        opens = json.loads(manifest.read_text(encoding="utf-8")).get("lazy") if manifest.is_file() else None
        if opens is None:
            eager.append(part.module)
        else:
            lazy.append({"module": part.module, **opens})
    return {"dialogs": eager, "lazy": lazy, "panels": modules["panels"]}


def page_theme(display: DisplayConfig) -> dict:
    """What the page needs to show its theme before the first paint (docs/themes.md, "Applying"): the default from
    Settings (`look`, an unknown id gives `default`), its forced mode when it has only one, the short list of themes
    for <html data-looks> (js/theme-boot.js picks this browser's choice from it and links that theme's stylesheet) and
    the custom themes' token blocks."""
    every = themes.all_themes(display.themes)  # once: each custom theme is checked once per page
    look = themes.resolve_default(display.theme, every)
    modes = next(t.modes for t in every if t.id == look)
    return {"look": look, "forced": modes[0] if len(modes) == 1 else "", "effects": display.effects,
            "looks": themes.page_list(every, ASSET_VERSION), "custom_css": themes.custom_css(every)}


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The app mark for browsers that ask for /favicon.ico (the page itself names favicon.svg)."""
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Serves the main web dashboard."""
    state = get_state(request)
    config = state.config
    layout = (config.dashboard if config is not None else DashboardConfig()).model_dump()
    # the effective settings for the help texts (js/settings-values.js reads <body data-settings>); {} before the
    # settings store is loaded
    settings_values = state.settings.values() if state.settings is not None else {}
    display = config.display if config is not None else DisplayConfig()
    return templates.TemplateResponse(request=request, name="index.html", context={
        "v": ASSET_VERSIONS, "grafana_links": grafana.links(), "layout": layout, "ui": discovered_ui(), "modules": page_modules(),
        "settings_values": settings_values, "theme": page_theme(display)})
