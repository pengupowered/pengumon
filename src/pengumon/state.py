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

if TYPE_CHECKING:
    from pengumon.access.manager import AccessManager
    from pengumon.config import AppConfig
    from pengumon.db.repository import DatabaseRepository
    from pengumon.devices.component import DeviceSupervisor
    from pengumon.devices.registry import DeviceRegistry
    from pengumon.engine.discovery.drift import DriftChecker
    from pengumon.engine.discovery.jobs import DiscoveryJobs
    from pengumon.engine.classifier import OutageClassifier
    from pengumon.engine.path import PathManager
    from pengumon.engine.prober import NetworkProber
    from pengumon.notify.notifier import Notifier
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
    access: Optional["AccessManager"] = None  # the sign-in password and the first-run question (access/manager.py)


def get_state(connection: HTTPConnection) -> AppState:
    """FastAPI dependency: the app's AppState, or an empty one before the lifespan has built it."""
    state = getattr(connection.app.state, STATE_ATTR, None)
    return state if state is not None else AppState()
