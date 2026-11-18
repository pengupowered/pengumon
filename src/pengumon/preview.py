"""Previewing the dashboard without a device: what a household without the router or the modem Pengumon reads would see.

A browser turns it on in Settings > Display, which sets the cookie `pengumon_preview`: `router:absent`, `modem:absent`,
`router:basic`, comma-separated (device id, colon, mode). Every request and the live stream's handshake carry it, so
each answer is shaped for that browser alone; nothing stored changes, the devices are still read and their history
kept, and ending the preview brings everything back. Modes:

* `absent`: the device was never set up: it is in no answer, its routes are 404, nothing it measured is shown.
* `basic`: the device as a basic UPnP router, which only counts the bytes through its internet port: of what it
  offers only `wan_counters` (a device that does not offer it is unchanged).

`PreviewRegistry` is the device registry as such a browser sees it, with the registry's read interface; the routes and
the views they call take it instead of `state.devices` (`viewer_registry`). Without a preview they get the real
registry, unwrapped. The settings routes are not previewed: the configuration stays visible and editable (and
GET /api/devices?configured=1 gives the editors the devices as configured).

What a basic router keeps is said once, here: its WAN figures (BASIC_SNAPSHOT_KEEPS, BASIC_GRADES, BASIC_BUCKET_FIELDS);
`basic_snapshot` and `basic_summary` cut a snapshot and a live summary to them, and `is_basic` tells a device seen as
one. The routes call those, never a field list of their own.
"""

import re
from urllib.parse import unquote
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping, Optional, Tuple

from pengumon.capabilities import Capability

COOKIE = "pengumon_preview"
ABSENT = "absent"
BASIC = "basic"
MODES = (ABSENT, BASIC)
MAX_ENTRIES = 10
BASIC_OFFERS: FrozenSet[Capability] = frozenset({Capability.WAN_COUNTERS})
BASIC_DRIVER = "upnp_igd"  # what a device previewed as basic is taken for (its name and label; the configuration's driver)
_ENTRY = re.compile(r"^([A-Za-z0-9_-]{1,64}):(absent|basic)$")


class Preview:
    """Which devices a browser previews without, and how: device id -> ABSENT or BASIC."""

    def __init__(self, modes: Optional[Mapping[str, str]] = None):
        self.modes: Dict[str, str] = dict(modes or {})

    @classmethod
    def parse(cls, value: Optional[str]) -> "Preview":
        """From the cookie's value (as the page writes it, URL-encoded: `router%3Aabsent%2Cmodem%3Abasic`, or plain); an
        entry that is not `id:mode` is left out, and so is any past MAX_ENTRIES."""
        modes: Dict[str, str] = {}
        for part in unquote((value or "")[:2048]).split(",")[: MAX_ENTRIES * 2]:
            m = _ENTRY.match(part.strip())
            if m and len(modes) < MAX_ENTRIES:
                modes[m.group(1)] = m.group(2)
        return cls(modes)

    @property
    def active(self) -> bool:
        return bool(self.modes)

    def mode(self, device_id: str) -> Optional[str]:
        return self.modes.get(device_id)

    def hides(self, device_id: str) -> bool:
        return self.modes.get(device_id) == ABSENT

    def offers(self, device: Any) -> FrozenSet[Capability]:
        """What `device` offers as this browser sees it."""
        full = frozenset(device.driver.offers)
        mode = self.modes.get(device.id)
        if mode == ABSENT:
            return frozenset()
        if mode == BASIC and Capability.WAN_COUNTERS in full:
            return full & BASIC_OFFERS
        return full



def from_request(request: Any) -> Preview:
    """The preview of an HTTP request or a WebSocket (both have `cookies`); an empty one without the cookie."""
    cookies = getattr(request, "cookies", None) or {}
    return Preview.parse(cookies.get(COOKIE))


class _DriverView:
    """A device's driver as a basic UPnP router (BASIC): `offers` and `class_offers` are the preview's, and it names
    itself as the UPnP driver does (`name`, `label`: "UPnP router" where the dashboard says what the device is);
    everything else is the driver's own."""

    def __init__(self, driver: Any, offers: FrozenSet[Capability]):
        from pengumon.drivers import DRIVERS  # (the drivers load only when a preview asks)
        self._driver = driver
        self.offers = offers
        self.class_offers = offers
        self.name = BASIC_DRIVER
        self.label = getattr(DRIVERS.get(BASIC_DRIVER), "label", None) or "UPnP router"
        self.needs_sign_in = False  # UPnP has no password: no Setup button, no sign-in fields
        self.sign_in = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


class DeviceView:
    """A device as a BASIC preview shows it: its driver offers only the WAN counters. Reads go to the device; its
    snapshots are cut to a basic router's with `basic_snapshot` where a route answers one."""

    def __init__(self, device: Any, offers: FrozenSet[Capability]):
        self._device = device
        self.driver = _DriverView(device.driver, offers)
        # as a UPnP router: nothing to sign in with, so nothing missing (Device's own properties read the real driver)
        self.needs_sign_in = False
        self.has_credentials = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device, name)


def is_basic(device: Any) -> bool:
    """Whether `device` is seen as a basic UPnP router (a DeviceView of a previewing registry)."""
    return isinstance(device, DeviceView)


# What a basic router keeps: the snapshot's WAN figures (every other figure is None), the grades of its WAN load, and of
# the bucketed history the WAN columns. A router's "warning" status comes only from its CPU or mesh grade (devices/
# monitors.py), which a basic router has not: it is healthy then.
BASIC_SNAPSHOT_DROPS = ("cpu_usage_pct", "mem_usage_pct", "client_count", "uptime_s", "cpu_temp_c", "wan_link_mbps",
                        "model_name")
BASIC_GRADES = frozenset({"wan_rx", "wan_tx"})
BASIC_BUCKET_FIELDS = frozenset({"ts", "rx_avg", "rx_max", "tx_avg", "tx_max"})
# The live summary's keys beyond the snapshot's that a basic router has not: its client list and mesh.
_BASIC_SUMMARY_DROPS = frozenset(BASIC_SNAPSHOT_DROPS) | {"clients", "mesh"}


def _basic_status(status: Optional[str]) -> Optional[str]:
    return "healthy" if status == "warning" else status


def basic_snapshot(snapshot: Any) -> Any:
    """A router snapshot as a basic router would have taken it (None stays None)."""
    if snapshot is None:
        return None
    update: Dict[str, Any] = {k: None for k in BASIC_SNAPSHOT_DROPS}
    update["grades"] = {k: v for k, v in snapshot.grades.items() if k in BASIC_GRADES}
    update["status"] = _basic_status(snapshot.status)
    return snapshot.model_copy(update=update)


def basic_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """A live device summary (devices/summary.py) as a basic router's."""
    out = {k: v for k, v in summary.items() if k not in _BASIC_SUMMARY_DROPS}
    if isinstance(out.get("grades"), dict):
        out["grades"] = {k: v for k, v in out["grades"].items() if k in BASIC_GRADES}
    if "status" in out:
        out["status"] = _basic_status(out["status"])
    return out


def class_offers(driver: Any) -> FrozenSet[Capability]:
    """What a driver's class can offer (`type(driver).offers`, before its options switch some off), as a preview sees
    it."""
    return frozenset(getattr(driver, "class_offers", None) or type(driver).offers)


class PreviewRegistry:
    """The device registry as a previewing browser sees it (the read side of devices.registry.DeviceRegistry). Its
    devices are worked out once, when it is made (one per request)."""

    def __init__(self, registry: Any, preview: Preview):
        self._registry = registry
        self.preview = preview
        self.errors = {k: v for k, v in getattr(registry, "errors", {}).items() if not preview.hides(k)}
        self.speed_test_running = getattr(registry, "speed_test_running", lambda: False)
        views = (self._view(d) for d in registry.all())
        self._devices: Tuple[Any, ...] = tuple(v for v in views if v is not None)
        self._by_id = {d.id: d for d in self._devices}

    def _view(self, device: Any) -> Optional[Any]:
        mode = self.preview.mode(device.id)
        if mode == ABSENT:
            return None
        if mode == BASIC and Capability.WAN_COUNTERS in device.driver.offers:
            return DeviceView(device, self.preview.offers(device))
        return device

    def all(self) -> Tuple[Any, ...]:
        return self._devices

    def __iter__(self) -> Iterator[Any]:
        return iter(self._devices)

    def __len__(self) -> int:
        return len(self._devices)

    def get(self, device_id: str) -> Optional[Any]:
        return self._by_id.get(device_id)

    def providing(self, capability: Capability) -> Tuple[Any, ...]:
        return tuple(d for d in self._devices if d.enabled and capability in d.driver.offers)

    def offering(self, capability: Capability) -> Tuple[Any, ...]:
        return tuple(d for d in self._devices if capability in d.driver.offers)

    def primary(self, capability: Capability) -> Optional[Any]:
        enabled = self.providing(capability)
        if enabled:
            return enabled[0]
        offering = self.offering(capability)
        return offering[0] if offering else None

    def line_technology(self) -> Optional[str]:
        from pengumon.devices.registry import line_technology
        return line_technology(self._devices)

    def wan_rates_device(self) -> Optional[Any]:
        from pengumon.devices.registry import wan_rates_device
        return wan_rates_device(self._devices)

    def wan_rates_source(self) -> Callable[[], List]:
        from pengumon.devices.registry import wan_rates_source
        return wan_rates_source(self._devices)


def event_devices(devices: Any) -> set:
    """The ids of the devices whose stored events (restarts, link changes) a previewing browser is shown: those it
    sees, except a basic router (which tells neither)."""
    return {d.id for d in devices.all() if not is_basic(d)}


def viewer_registry(state: Any, request: Any) -> Any:
    """`state.devices` as the browser that sent `request` sees it: the real registry without a preview (or without
    devices), else a PreviewRegistry."""
    preview = from_request(request)
    if state.devices is None or not preview.active:
        return state.devices
    return PreviewRegistry(state.devices, preview)


def live_devices(devices: Dict[str, Dict[str, Any]], preview: Preview, basic_ids: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    """The live message's `devices` for a previewing connection: absent ones left out, basic ones (`basic_ids`: the
    ids previewed as basic that offer WAN counters) cut to their status and WAN figures. The same dict without a
    preview."""
    if not preview.active:
        return devices
    out: Dict[str, Dict[str, Any]] = {}
    for device_id, summary in devices.items():
        if preview.hides(device_id):
            continue
        if basic_ids and device_id in basic_ids:
            summary = basic_summary(summary)
        out[device_id] = summary
    return out


def preview_config(config: Any, preview: Preview) -> Any:
    """The app configuration as a previewing browser's reports read it (the AI pack, the ISP dossier: they look at the
    configured devices by driver): devices previewed as absent left out, one previewed as basic as a UPnP router.
    The same object without a preview."""
    if config is None or not preview.active:
        return config
    from pengumon.drivers import DRIVERS  # (the drivers load only when a preview asks)
    devices = []
    for device in config.devices:
        mode = preview.mode(device.id)
        if mode == ABSENT:
            continue
        driver = DRIVERS.get(device.driver)
        if mode == BASIC and driver is not None and Capability.WAN_COUNTERS in driver.offers:
            # a UPnP router takes no options (its options model forbids any): the device's own would not validate
            device = device.model_copy(update={"driver": BASIC_DRIVER, "options": {}})
        devices.append(device)
    return config.model_copy(update={"devices": devices})
