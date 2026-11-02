"""Mounts the feature routers (api/__init__.py ROUTERS) under one `router`. No route handlers live here: a new
endpoint goes in its feature's module (tests/test_api_routers.py guards this)."""

from fastapi import APIRouter

from pengumon.api import ROUTERS
from pengumon.api.ops import metrics_router

router = APIRouter()
for _router in ROUTERS:
    router.include_router(_router)

__all__ = ["router", "metrics_router"]
