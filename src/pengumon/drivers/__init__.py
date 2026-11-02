"""Device drivers: one module per kind of device, each turning that device's answers into capability readings.

DRIVERS is the built-in registry (no plugin loading: every driver ships with pengumon, so the dependency gate sees
everything that runs). A `devices:` entry in config.yaml names its driver here; pengumon.devices.registry builds it
with the driver's `from_config`.
"""

from pengumon.drivers.asuswrt import AsusWrtDriver
from pengumon.drivers.openwrt import OpenWrtDriver
from pengumon.drivers.snmp import SnmpDriver
from pengumon.drivers.xfinity_xb import XfinityXbDriver

DRIVERS = {driver.name: driver for driver in (AsusWrtDriver, OpenWrtDriver, SnmpDriver, XfinityXbDriver)}

__all__ = ["DRIVERS", "AsusWrtDriver", "OpenWrtDriver", "SnmpDriver", "XfinityXbDriver"]
