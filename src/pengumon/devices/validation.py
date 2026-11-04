"""Checking a devices list before it is saved (the Settings page's validate and PUT; pengumon.settings).

`validate_devices` takes the list as the page sends it (dicts) and the devices that are live now, and returns the
problems as (where, message) pairs; an empty list means the list can be saved. It checks each item with DeviceConfig,
that ids are unique (also once upper-cased: they name the credential variables), that a device that exists is not
renamed, and that every item can be built: a dry build with the driver's own `from_config`, no credentials and no
network, which catches what used to stop the start (a bad option, a missing CA file).

Messages never echo a URL or any other input value that DeviceConfig could hold a password in.
"""

from typing import Any, Dict, Iterable, List, Sequence, Tuple, Union

from pydantic import ValidationError

from pengumon.devices.registry import DeviceConfigError, monitor_class
from pengumon.devices.settings import DeviceConfig, unique_device_ids
from pengumon.drivers import DRIVERS
from pengumon.secrets import CredentialSources

Problem = Tuple[str, str]  # (where, message): where is "devices.<id>" or "devices.<id>.<field>"

RENAME_MESSAGE = ("An id can't be renamed: its history and sign-in are kept under the old id. "
                  "Remove the device and add it again to give it another id.")


def _label(raw: Any, index: int) -> str:
    ident = raw.get("id") if isinstance(raw, dict) else getattr(raw, "id", None)
    return ident if isinstance(ident, str) and ident.strip() else f"#{index + 1}"


async def dry_build(cfg: DeviceConfig) -> None:
    """Builds the device's driver as `build_device` does, without credentials or a network. Raises
    DeviceConfigError."""
    driver_cls = DRIVERS[cfg.driver]
    monitor_class(cfg.id, driver_cls)
    try:
        driver = driver_cls.from_config(cfg, CredentialSources.none().resolve(cfg), client=None)
    except ValueError as e:
        raise DeviceConfigError(f"device {cfg.id}: {e}") from None
    await driver.aclose()


def renamed(items: Sequence[DeviceConfig], live: Iterable[DeviceConfig]) -> List[Tuple[DeviceConfig, DeviceConfig]]:
    """(live device, new item) pairs where an id that exists now is gone from the list and a new item has the same
    driver and URL: that pattern is a rename."""
    new_ids = {c.id for c in items}
    live_ids = {c.id for c in live}
    out = []
    for gone in live:
        if gone.id in new_ids:
            continue
        for item in items:
            if item.id not in live_ids and item.driver == gone.driver and item.url.rstrip("/") == gone.url.rstrip("/"):
                out.append((gone, item))
                break
    return out


async def validate_devices(raw_items: Sequence[Union[Dict[str, Any], DeviceConfig]],
                           live: Iterable[DeviceConfig] = ()) -> List[Problem]:
    live = list(live)
    problems: List[Problem] = []
    parsed: List[DeviceConfig] = []
    for index, raw in enumerate(raw_items):
        label = _label(raw, index)
        if isinstance(raw, DeviceConfig):
            parsed.append(raw)
            continue
        try:
            parsed.append(DeviceConfig.model_validate(raw))
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(p) for p in err["loc"] if not isinstance(p, int))
                problems.append((f"devices.{label}" + (f".{loc}" if loc else ""), err["msg"].removeprefix("Value error, ")))
    if problems:
        return problems
    try:
        unique_device_ids(parsed)
    except ValueError as e:
        return [("devices", str(e).removeprefix("devices: "))]
    for gone, item in renamed(parsed, live):
        problems.append((f"devices.{item.id}.id", RENAME_MESSAGE))
    for cfg in parsed:
        try:
            await dry_build(cfg)
        except DeviceConfigError as e:
            problems.append((f"devices.{cfg.id}", str(e).removeprefix(f"device {cfg.id}: ")))
    return problems
