"""REST API routes, one module per feature. `ROUTERS` lists the feature routers that `router.py` mounts."""

from pengumon.api import history, incidents, notifications, ops, path, reports, settings, settings_preview, targets

ROUTERS = [ops.router, targets.router, path.router, incidents.router, history.router, reports.router, notifications.router,
           settings.router, settings_preview.router]
