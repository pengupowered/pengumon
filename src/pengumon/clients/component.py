"""The clients component: the client list of a router that reports one (api/clients.py), and with services the
pattern for an optional feature.

Its routes (GET /api/devices/{id}/clients and .../history) are always mounted; for a device that offers no client
inventory it answers 400. The readings themselves are the router monitor's (devices/monitors.py), kept in memory
only. So `register` has nothing to build: it returns None, unless a configured device offers the client inventory,
and then a component with no start, stop or live part (the client rows never ride the live stream).
"""

from typing import Optional

from pengumon.capabilities import Capability
from pengumon.components import Component, Context

__all__ = ["OPTIONAL", "register"]

OPTIONAL = True


async def register(ctx: Context) -> Optional[Component]:
    if not ctx.state.devices.offering(Capability.CLIENT_INVENTORY):
        return None
    return Component("clients")
