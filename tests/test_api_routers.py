"""The API is one router module per feature; api/router.py only mounts them."""

import ast
from pathlib import Path

from pengumon.api import router as router_module
from pengumon.api.router import ROUTERS
from pengumon.main import app


def test_api_router_py_holds_no_route_handlers_only_includes():
    tree = ast.parse(Path(router_module.__file__).read_text())
    handlers = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert handlers == [], f"route handlers belong in their feature's module, not api/router.py: {handlers}"
    decorated = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in {"get", "post", "put", "delete", "patch", "api_route"}]
    assert decorated == []


def test_the_app_serves_every_feature_router_and_metrics_outside_api():
    paths = set(app.openapi()["paths"])
    assert ROUTERS and "/metrics" in paths
    assert {"/api/info", "/api/status", "/api/targets", "/api/incidents", "/api/outages", "/api/incidents/dossier",
            "/api/history", "/api/report", "/api/report/loss-grid", "/api/uptime", "/api/mtr/trace",
            "/api/health"} <= paths
