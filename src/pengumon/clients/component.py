"""The clients component: the client list of a router that reports one (api/clients.py), and with services the
pattern for an optional feature. It also puts the clients' row-key salt in force at start (clients/keys.py).

Its routes (GET /api/devices/{id}/clients and .../history) are always mounted; for a device that offers no client
inventory it answers 400. The readings themselves are the router monitor's (devices/monitors.py), kept in memory
only. So `register` has nothing to build: it returns None, unless a configured device offers the client inventory,
and then a component with no start, stop or live part (the client rows never ride the live stream).
"""

from typing import Optional

from pengumon.capabilities import Capability
from pengumon.clients.keys import load_key_salt
from pengumon.components import Component, Context

__all__ = ["OPTIONAL", "register"]

OPTIONAL = True


async def register(ctx: Context) -> Optional[Component]:
    # the clients' row keys (clients/keys.py), the same across restarts: loaded whatever is configured now, since a
    # router added later in Settings (no restart) and the traffic routes key their rows the same way
    await load_key_salt(ctx.state.repository)
    if not ctx.state.devices.offering(Capability.CLIENT_INVENTORY):
        return None
    return Component("clients")
