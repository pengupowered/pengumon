"""The devices component: the credential sources, every configured device (devices/registry.py) and its polling
task, the device API (api/devices.py) and the devices' summaries in the live message (devices/summary.py).

It is the first component: building the devices checks that each one can run (a CA file that is missing, say) and
stops the start with an error naming the device, before the database is opened or migrated.
"""

from pengumon.api.devices import router
from pengumon.components import Component, Context, LivePart
from pengumon.devices.registry import DeviceRegistry, build_devices
from pengumon.devices.summary import LiveSummaries
from pengumon.secrets import CredentialSources, SecretStore

__all__ = ["live_part", "register", "router"]


def live_part(devices: DeviceRegistry) -> LivePart:
    summaries = LiveSummaries(devices)
    return LivePart("devices", lambda diagnosis: summaries.current())


async def register(ctx: Context) -> Component:
    secret_store = SecretStore()  # data/secrets.json, read once and shared by everything that reads or writes it
    credentials = CredentialSources(store=secret_store)
    devices = build_devices(ctx.config.devices, ctx.state.repository, credentials)
    ctx.state.secret_store, ctx.state.credentials, ctx.state.devices = secret_store, credentials, devices

    async def start() -> None:
        for device in devices:
            ctx.spawn(device.run())

    return Component("devices", start=start, stop=devices.aclose, live_part=live_part(devices))
