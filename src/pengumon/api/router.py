"""Lists the feature routers (`ROUTERS`) and mounts them under one `router`. No route handlers live here: a new
endpoint goes in its feature's module (tests/test_api_routers.py guards this)."""

from fastapi import APIRouter

from pengumon.api import (access, backup, clients, devices, discovery, history, incidents, live, mcp, notifications, ops,
                          outages, path, reports, services, settings, settings_preview, speedtests, targets, themes,
                          traffic)
from pengumon.api.ops import metrics_router

# Every route of the app but /metrics and the page (main.py), mounted whether or not its feature is configured: the
# routes and the OpenAPI schema never depend on the settings.
ROUTERS = [ops.router, targets.router, path.router, incidents.router, outages.router, history.router, reports.router, notifications.router,
           settings.router, settings_preview.router, devices.router, services.router, clients.router, live.router,
           discovery.router, access.router, themes.router, mcp.router, speedtests.router, traffic.router, backup.router]

router = APIRouter()
for _router in ROUTERS:
    router.include_router(_router)

__all__ = ["ROUTERS", "router", "metrics_router"]
