"""Device drivers: one module per kind of device, each turning that device's answers into capability readings.

DRIVERS is the built-in registry (no plugin loading: every driver ships with pengumon, so the dependency gate sees
everything that runs). A `devices` entry on the Settings page names its driver here; pengumon.devices.registry builds it
with the driver's `from_config`. `devices_with` picks the configured devices by what their driver offers.
"""

from typing import TYPE_CHECKING, Iterable, List, Optional

from pengumon.drivers.asuswrt import AsusWrtDriver
from pengumon.drivers.openwrt import OpenWrtDriver
from pengumon.drivers.snmp import SnmpDriver
from pengumon.drivers.xfinity_xb import XfinityXbDriver

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig

DRIVERS = {driver.name: driver for driver in (AsusWrtDriver, OpenWrtDriver, SnmpDriver, XfinityXbDriver)}


def devices_with(devices: Iterable["DeviceConfig"], capability=None, technology: Optional[str] = None
                 ) -> List["DeviceConfig"]:
    """The configured devices whose driver offers `capability` (and, when given, has that line technology), enabled
    ones first, then disabled ones, each in config order: the history of a device switched off still counts."""
    found = []
    for cfg in devices:
        driver = DRIVERS.get(cfg.driver)
        if driver is None:
            continue
        if capability is not None and capability not in driver.offers:
            continue
        if technology is not None and getattr(driver, "line_technology", None) != technology:
            continue
        found.append(cfg)
    return [c for c in found if c.enabled] + [c for c in found if not c.enabled]


__all__ = ["DRIVERS", "devices_with", "AsusWrtDriver", "OpenWrtDriver", "SnmpDriver", "XfinityXbDriver"]
