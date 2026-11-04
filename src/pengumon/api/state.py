"""The composition root: everything the running app is made of, built once at start by the components listed in
pengumon.main (docs/architecture.md).

The lifespan stores one AppState on `app.state.pengumon`; request handlers receive it through the `get_state`
dependency, never through a module global, so a test gives the app its own state by setting that attribute (or by
overriding the dependency) and nothing leaks between tests or app instances.

A part that is None is not initialised (the app is starting, or a test left it out); handlers that need it answer
503, as they always have.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from starlette.requests import HTTPConnection

from pengumon.capabilities import Capability
from pengumon.devices.samples import RouterHistory
from pengumon.lines import docsis

if TYPE_CHECKING:
    from pengumon.config import AppConfig
    from pengumon.db.repository import DatabaseRepository
    from pengumon.devices.component import DeviceSupervisor
    from pengumon.devices.registry import DeviceRegistry
    from pengumon.engine.discovery.drift import DriftChecker
    from pengumon.engine.discovery.jobs import DiscoveryJobs
    from pengumon.engine.classifier import OutageClassifier
    from pengumon.engine.path import PathManager
    from pengumon.engine.prober import NetworkProber
    from pengumon.notify import Notifier
    from pengumon.settings.preview import PreviewJobs
    from pengumon.services.checker import ServiceChecker
    from pengumon.secrets import CredentialSources, SecretStore
    from pengumon.settings.manager import SettingsManager

STATE_ATTR = "pengumon"


@dataclass
class AppState:
    config: Optional["AppConfig"] = None
    repository: Optional["DatabaseRepository"] = None
    prober: Optional["NetworkProber"] = None
    path: Optional["PathManager"] = None  # the probe path's store and live reload (engine/path.py)
    classifier: Optional["OutageClassifier"] = None
    devices: Optional["DeviceRegistry"] = None
    service_checker: Optional["ServiceChecker"] = None
    notifier: Optional["Notifier"] = None  # ntfy and the webhooks; None when no channel is configured
    device_supervisor: Optional["DeviceSupervisor"] = None  # devices/component.py: applies a changed devices list live
    secret_store: Optional["SecretStore"] = None  # data/secrets.json, shared by the credential sources and endpoints
    credentials: Optional["CredentialSources"] = None  # where the devices' credentials came from at startup
    discovery: Optional["DiscoveryJobs"] = None  # the network discovery runs (engine/discovery)
    drift: Optional["DriftChecker"] = None  # the background route-change checks (engine/discovery/drift.py)
    settings: Optional["SettingsManager"] = None  # the settings store (pengumon.settings.manager)
    preview: Optional["PreviewJobs"] = None  # the outage-rules preview jobs (settings/preview.py)

    def line_history(self) -> Optional[docsis.DocsisHistory]:
        """The stored readings of the device that reports the access line (DeviceRegistry.primary, so a device
        switched off in config still shows its history), when that line is DOCSIS: the only technology the inspector
        and the dossier read so far. None without such a device."""
        device = self.devices.primary(Capability.ACCESS_LINE) if self.devices is not None else None
        if device is None or self.repository is None:
            return None
        if getattr(device.driver, "line_technology", None) != docsis.TECHNOLOGY:
            return None
        return docsis.DocsisHistory(self.repository, device.id)

    def router_history(self) -> Optional[RouterHistory]:
        """The stored polls of the device the inspector and dossier read (the primary one reporting WAN counters)."""
        device = self.devices.primary(Capability.WAN_COUNTERS) if self.devices is not None else None
        if device is None or self.repository is None:
            return None
        return RouterHistory(self.repository, device.id)


def get_state(connection: HTTPConnection) -> AppState:
    """FastAPI dependency: the app's AppState, or an empty one before the lifespan has built it."""
    state = getattr(connection.app.state, STATE_ATTR, None)
    return state if state is not None else AppState()
