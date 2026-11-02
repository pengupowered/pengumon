"""The dashboard page (/) and the favicon. The page is the template web/templates/index.html, which includes the
panels and dialogs that pengumon.ui discovers."""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from pengumon.api.state import get_state
from pengumon.observability import grafana
from pengumon.ui import ui as discovered_ui
from pengumon.web.assets import ASSET_VERSIONS, STATIC_DIR, TEMPLATES_DIR
from pengumon.web.settings import DashboardConfig

router = APIRouter()
# the page's own templates, and the panel and dialog folders (pengumon.ui) whose partials it includes
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(STATIC_DIR)])


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The app mark for browsers that ask for /favicon.ico (the page itself names favicon.svg)."""
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Serves the main web dashboard."""
    config = get_state(request).config
    layout = (config.dashboard if config is not None else DashboardConfig()).model_dump()
    return templates.TemplateResponse(request=request, name="index.html", context={
        "v": ASSET_VERSIONS, "grafana_links": grafana.links(), "layout": layout, "ui": discovered_ui()})
