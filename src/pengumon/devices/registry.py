"""Building the configured devices, and the collection the rest of the app finds them in.

`build_device` turns one `devices:` entry into a Device: the driver from pengumon.drivers.DRIVERS (built by its own
`from_config`), the monitor its capabilities call for, its credentials (pengumon.secrets) and the texts the UI shows
while it is not polled. `build_devices` builds them all into a DeviceRegistry and logs one line per device.

The registry is the only way code reaches a device: by id, or by what it provides (`providing`, `primary`). Nothing
here or elsewhere knows a device id, a vendor or an address.
"""

from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Type

import httpx

from pengumon.capabilities import Capability
from pengumon.devices.settings import DeviceConfig, unique_device_ids  # noqa: F401  (re-exported)
from pengumon.devices.device import Device
from pengumon.devices.monitors import LineMonitor, RouterMonitor, UnconfiguredMessages
from pengumon.devices.wan import WanRates, WanReading
from pengumon.drivers import DRIVERS
from pengumon.observability import get_logger
from pengumon.secrets import (
    CredentialSources, password_env_var, privacy_password_env_var, privacy_secret_file_name, secret_file_name,
)

ROUTER_CAPABILITIES = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM})


class DeviceConfigError(ValueError):
    """A devices: entry pengumon cannot run (raised at startup, naming the device)."""


def monitor_class(device_id: str, driver: type) -> Type:
    """The monitor for a driver's capabilities: LineMonitor for access_line, RouterMonitor for wan_counters /
    router_system. Both at once needs a monitor that keeps both snapshots, which does not exist yet."""
    offers = driver.offers
    line = Capability.ACCESS_LINE in offers
    router = bool(offers & ROUTER_CAPABILITIES)
    if line and router:
        raise DeviceConfigError(f"device {device_id}: driver {driver.name} offers access_line together with "
                                "wan_counters/router_system, which no monitor combines yet")
    if line:
        return LineMonitor
    if router:
        return RouterMonitor
    raise DeviceConfigError(f"device {device_id}: driver {driver.name} offers nothing pengumon can monitor "
                            f"({', '.join(sorted(c.value for c in offers)) or 'no capabilities'})")


def unconfigured_messages(device_id: str, *, privacy: bool = False) -> UnconfiguredMessages:
    """What the UI shows while the device is disabled or has no password (`privacy`: an SNMPv3 authPriv device, which
    also needs its privacy password)."""
    if privacy:
        missing = (f"Password or privacy password for {device_id} not configured. Set {password_env_var(device_id)} and "
                   f"{privacy_password_env_var(device_id)}, add /run/secrets/{secret_file_name(device_id)} and "
                   f"/run/secrets/{privacy_secret_file_name(device_id)}, or enter them in the settings.")
    else:
        missing = (f"Password for {device_id} not configured. Set {password_env_var(device_id)}, add "
                   f"/run/secrets/{secret_file_name(device_id)}, or enter it in the settings.")
    return UnconfiguredMessages(disabled=f"Device {device_id} is disabled in configuration.", missing_credentials=missing)


def build_device(cfg: DeviceConfig, repository=None, secrets: Optional[CredentialSources] = None, *,
                 client: Optional[httpx.AsyncClient] = None, clock: Optional[Callable[[], float]] = None,
                 wall_clock: Optional[Callable[[], float]] = None, wan: Optional[WanRates] = None) -> Device:
    """One device from its config. `secrets` defaults to no sources but the config itself (tests); `client`, the
    clocks and `wan` are for tests: `clock` (monotonic) paces the driver, `wall_clock` timestamps readings and
    snapshots."""
    driver_cls = DRIVERS[cfg.driver]
    monitor_cls = monitor_class(cfg.id, driver_cls)
    credentials = (secrets or CredentialSources.none()).resolve(cfg)
    clocks = {k: v for k, v in (("clock", clock), ("wall_clock", wall_clock)) if v is not None}
    try:
        driver = driver_cls.from_config(cfg, credentials, client=client, **clocks)
    except ValueError as e:
        raise DeviceConfigError(f"device {cfg.id}: {e}") from None
    messages = unconfigured_messages(cfg.id, privacy=getattr(driver, "takes_privacy_password", False))
    if monitor_cls is RouterMonitor:
        if wan is None and wall_clock is not None:
            wan = WanRates(clock=wall_clock)
        monitor = RouterMonitor(repository, messages=messages, model=cfg.model, wan=wan, role=cfg.id, device_id=cfg.id)
    else:
        monitor = LineMonitor(repository, messages=messages, role=cfg.id, device_id=cfg.id)
    minimum = getattr(driver_cls, "min_poll_seconds", 0.0)
    interval = cfg.poll_interval_seconds
    if interval < minimum:
        get_logger("pengumon.devices", device=cfg.id, driver=cfg.driver).warning(
            f"Device {cfg.id}: poll_interval_seconds {interval:g} is below the {minimum:g} s the {cfg.driver} driver "
            f"allows; polling every {minimum:g} s", configured_s=interval, minimum_s=minimum)
        interval = minimum
    return Device(cfg.id, driver, monitor, interval_seconds=interval, enabled=cfg.enabled,
                  config=cfg, credentials_source=credentials.password_source, min_interval_seconds=minimum,
                  **({"clock": wall_clock} if wall_clock is not None else {}))


def build_devices(configs: Sequence[DeviceConfig], repository=None,
                  secrets: Optional[CredentialSources] = None) -> "DeviceRegistry":
    """Every configured device, in config order, with one log line each (what it is, how often it is polled, what
    it offers, where its credentials came from)."""
    secrets = secrets or CredentialSources.none()
    devices = [build_device(cfg, repository, secrets) for cfg in configs]
    registry = DeviceRegistry(devices)
    for device in devices:
        log_device(device)
    return registry


def log_device(device: Device) -> None:
    """The one log line of a device once it is built (here and by the devices component's supervisor): what it is, how
    often it is polled, what it offers, where its credentials came from (never what they are). Not its address: the
    line is exported over OTLP with the other logs (observability/export.py)."""
    offers = ",".join(sorted(c.value for c in device.driver.offers))
    log = get_logger("pengumon.devices", device=device.id, driver=device.driver.name)
    if not device.enabled:
        log.info(f"Device {device.id} ({device.driver.name}) is disabled in configuration",
                 capabilities=offers)
        return
    source = device.credentials_source
    log.info(f"Device {device.id}: {device.driver.name} every "
             f"{device.interval_seconds:g} s, offers {offers}; "
             + (f"password from {source}" if source else
                ("no password configured" if device.needs_sign_in else "needs no password")),
             interval_s=device.interval_seconds, capabilities=offers, credentials=source or "none")


class DeviceRegistry:
    """The configured devices, in config order. `replace` swaps the whole set at once (settings applied live); the
    object stays the same, so everyone holding `state.devices` sees the new set. `errors` holds, per device id, why a
    configured device is not running (it could not be built); the settings page shows it on the device's card."""

    def __init__(self, devices: Iterable[Device] = ()):
        self._devices: Tuple[Device, ...] = ()
        self._by_id: Mapping[str, Device] = {}
        self.errors: Dict[str, str] = {}
        # Whether a speed test is running now (the speedtest component sets it): drivers with reads that can wait
        # (`pause_reads`, the router's statistics pages) hold them meanwhile, whichever devices the registry has.
        self.speed_test_running: Callable[[], bool] = lambda: False
        self.replace(devices)

    def replace(self, devices: Iterable[Device]) -> None:
        """Swaps the devices (a tuple and an id map, assigned together after the check)."""
        new = tuple(devices)
        by_id: Dict[str, Device] = {}
        for device in new:
            if device.id in by_id:
                raise DeviceConfigError(f"device id {device.id!r} is used twice")
            by_id[device.id] = device
        self._devices, self._by_id = new, by_id
        for device in new:
            if hasattr(device.driver, "pause_reads"):
                device.driver.pause_reads = self._speed_test_running

    def _speed_test_running(self) -> bool:
        return bool(self.speed_test_running())

    def __iter__(self) -> Iterator[Device]:
        return iter(self._devices)

    def __len__(self) -> int:
        return len(self._devices)

    def get(self, device_id: str) -> Optional[Device]:
        return self._by_id.get(device_id)

    def all(self) -> Tuple[Device, ...]:
        return self._devices

    def providing(self, capability: Capability) -> Tuple[Device, ...]:
        """Enabled devices whose driver offers the capability, in config order."""
        return tuple(d for d in self._devices if d.enabled and capability in d.driver.offers)

    def offering(self, capability: Capability) -> Tuple[Device, ...]:
        """Every device whose driver offers the capability, disabled ones included."""
        return tuple(d for d in self._devices if capability in d.driver.offers)

    def primary(self, capability: Capability) -> Optional[Device]:
        """The device the single-device views show for a capability (the live message, the
        inspector): the first enabled one offering it, else the first disabled one, so a device switched off
        in config still shows "disabled" instead of disappearing."""
        enabled = self.providing(capability)
        if enabled:
            return enabled[0]
        offering = self.offering(capability)
        return offering[0] if offering else None

    def line_technology(self) -> Optional[str]:
        return line_technology(self._devices)

    def wan_rates_device(self) -> Optional[Device]:
        """The device whose live WAN rates the line load reads."""
        return wan_rates_device(self._devices)

    def wan_rates_source(self) -> Callable[[], List]:
        return wan_rates_source(self._devices)

    async def read_wan_counters(self) -> Optional[WanReading]:
        """A light read of the WAN counters of the device whose rates the line load reads (wan_rates_device), for a
        running speed test; None without one, or when it gives none now (Device.read_wan_counters)."""
        device = self.wan_rates_device()
        return await device.read_wan_counters() if device is not None else None

    async def aclose(self) -> None:
        for device in self._devices:
            await device.aclose()


def line_technology(devices: Iterable[Device]) -> Optional[str]:
    """The technology of the access line, from the first enabled device that reports one; None when no device
    reports the line (diagnoses then use the generic wording)."""
    for device in devices:
        if device.enabled and Capability.ACCESS_LINE in device.driver.offers:
            return getattr(device.driver, "line_technology", None)
    return None


def wan_rates_device(devices: Iterable[Device]) -> Optional[Device]:
    """The first enabled device that reports WAN counters: LineLoad reads its live rates (wan_rates_source). None
    without one: the load is unknown and classification works without it. (The stored rates LineLoad learns from are
    those of DeviceRegistry.primary, which falls back to a disabled device.)"""
    for device in devices:
        if device.enabled and Capability.WAN_COUNTERS in device.driver.offers:
            return device
    return None


def wan_rates_source(devices: Iterable[Device]) -> Callable[[], List]:
    """recent_rates() of wan_rates_device() (for LineLoad); without that device, no rates."""
    device = wan_rates_device(devices)
    return device.monitor.recent_rates if device is not None else list
