"""REST API routes, one module per feature. `ROUTERS` lists the feature routers that `router.py` mounts."""

from pengumon.api import history, incidents, notifications, ops, reports, targets

ROUTERS = [ops.router, targets.router, incidents.router, history.router, reports.router, notifications.router]
