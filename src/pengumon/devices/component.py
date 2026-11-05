"""The devices component: the credential sources, every configured device (devices/registry.py) and its polling
task, and the devices' summaries in the live message (devices/summary.py). The device routes are api/devices.py.

It runs after the settings component, which loaded the stored devices into the config and created the one
SecretStore (data/secrets.json).

A device that cannot run (a CA file that is missing, say) no longer stops the start: it is left out of the registry,
its reason is in `registry.errors[id]` (the Settings page shows it on the device's card) and it is logged.

The DeviceSupervisor owns one polling task per device id and applies a changed devices list without a restart: a
device whose config is unchanged keeps its Device and its task; a changed or removed one is cancelled and its driver
closed; an added or changed one is built and starts polling. Afterwards the hub event `devices_rebuilt` tells the
parts that hold a device (the line load, the classifier's wording), and the path hops linked to a removed device are
unlinked in one path version.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from pengumon.components import Component, Context, LivePart, log_unexpected_exit
from pengumon.devices.device import Device
from pengumon.devices.registry import DeviceRegistry, build_device
from pengumon.devices.settings import DeviceConfig, unique_device_ids
from pengumon.devices.summary import LiveSummaries
from pengumon.observability import get_logger
from pengumon.observability.telemetry import device_rebuilds
from pengumon.secrets import CredentialSources, SecretStore

__all__ = ["DEVICES_REBUILT", "ApplyResult", "DeviceSupervisor", "live_part", "register", "unlink_hops"]

_log = get_logger("pengumon.devices")

DEVICES_REBUILT = "devices_rebuilt"  # the hub event after the devices were rebuilt (settings.hub.Change.events)


def live_part(devices: DeviceRegistry, summaries: Optional[LiveSummaries] = None) -> LivePart:
    summaries = summaries or LiveSummaries(devices)
    return LivePart("devices", lambda diagnosis: summaries.current())


async def warn_unconfigured_history(repository, devices) -> None:
    """One warning naming the device ids that have stored readings but are not in the config: their history is kept
    until retention removes it, but nothing shows it."""
    try:
        stored = await repository.get_stored_device_ids()
    except Exception as e:
        _log.error(f"Could not list the devices with stored readings: {e}")
        return
    orphans = [device_id for device_id in stored if devices.get(device_id) is None]
    if orphans:
        _log.warning(f"Stored readings of devices not in the config: {', '.join(orphans)}; their history is not "
                     "shown (retention removes it)")


@dataclass
class ApplyResult:
    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)  # devices that could not be built, and why

    @property
    def rebuilt(self) -> bool:
        return bool(self.added or self.changed or self.removed or self.errors)


class DeviceSupervisor:
    """Builds the devices of a config list into `registry` and keeps one polling task per device id."""

    def __init__(self, registry: DeviceRegistry, repository=None, credentials: Optional[CredentialSources] = None, *,
                 build=build_device, spawn=None, config=None):
        """`config`: the live config, whose line and router levels every built device's monitor grades by (read at
        each poll, so a saved level applies with no applier)."""
        self.registry = registry
        self._config = config
        self._repository = repository
        self._credentials = credentials or CredentialSources.none()
        self._build = build
        self._spawn_task = spawn or self._create_task  # ctx.spawn: ended at shutdown
        self._tasks: Dict[str, "asyncio.Task"] = {}
        self._running = False
        self._lock = asyncio.Lock()  # two saves at once apply one after the other

    def _make(self, cfg: DeviceConfig, errors: Dict[str, str]) -> Optional[Device]:
        try:
            device = self._build(cfg, self._repository, self._credentials)
        except Exception as e:  # a CA file gone since the list was validated, a driver that can't be built
            message = str(e) or type(e).__name__
            errors[cfg.id] = message.removeprefix(f"device {cfg.id}: ")
            device_rebuilds.add(1, {"device.id": cfg.id, "reason": "failed"})
            _log.error(f"Device {cfg.id} cannot run and is left out: {message}", device=cfg.id, driver=cfg.driver)
            return None
        if self._config is not None and hasattr(device.monitor, "watch_levels"):
            device.monitor.watch_levels(self._config)
        return device

    def build_all(self, configs: Iterable[DeviceConfig]) -> None:
        """The first build (at start): every device that can be built goes into the registry, in config order, with
        one log line each; the others are in registry.errors."""
        configs = list(configs)
        unique_device_ids(configs)
        errors: Dict[str, str] = {}
        devices = [d for d in (self._make(cfg, errors) for cfg in configs) if d is not None]
        self.registry.replace(devices)
        self.registry.errors.clear()
        self.registry.errors.update(errors)
        for device in devices:
            self._log_built(device)

    @staticmethod
    def _log_built(device: Device) -> None:
        offers = ",".join(sorted(c.value for c in device.driver.offers))
        log = get_logger("pengumon.devices", device=device.id, driver=device.driver.name, host=device.driver.host)
        if not device.enabled:
            log.info(f"Device {device.id} ({device.driver.name} at {device.driver.host}) is disabled in configuration",
                     capabilities=offers)
            return
        source = device.credentials_source
        log.info(f"Device {device.id}: {device.driver.name} at {device.driver.host} every "
                 f"{device.interval_seconds:g} s, offers {offers}; "
                 + (f"password from {source}" if source else "no password configured"),
                 interval_s=device.interval_seconds, capabilities=offers, credentials=source or "none")

    @staticmethod
    def _create_task(coro) -> "asyncio.Task":
        task = asyncio.create_task(coro)
        task.add_done_callback(log_unexpected_exit)
        return task

    def start(self) -> None:
        """Starts a polling task for every device in the registry."""
        self._running = True
        for device in self.registry:
            self._spawn(device)

    def _spawn(self, device: Device) -> None:
        if self._running:
            self._tasks[device.id] = self._spawn_task(device.run())

    async def _cancel(self, device_id: str) -> None:
        task = self._tasks.pop(device_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception(f"Polling of {device_id} had failed", device=device_id)

    async def apply(self, configs: Sequence[DeviceConfig]) -> ApplyResult:
        """Makes the running devices match `configs`. An unchanged device (equal DeviceConfig) keeps its Device and
        task; a changed or removed one is cancelled, awaited and its driver closed; an added or changed one is built
        and started. A build that fails leaves that device out, with its reason in registry.errors."""
        async with self._lock:
            configs = list(configs)
            unique_device_ids(configs)
            result = ApplyResult()
            old = {d.id: d for d in self.registry}
            wanted = {c.id: c for c in configs}
            result.removed = [i for i in old if i not in wanted]
            keep: Dict[str, Device] = {}
            fresh: Dict[str, Device] = {}
            stale: List[Device] = [old[i] for i in result.removed]
            for cfg in configs:
                existing = old.get(cfg.id)
                if existing is not None and existing.config == cfg:
                    keep[cfg.id] = existing
                    continue
                device = self._make(cfg, result.errors)
                if existing is not None:
                    stale.append(existing)
                    if device is not None:
                        result.changed.append(cfg.id)  # (one that no longer builds is still configured: only an error)
                elif device is not None:
                    result.added.append(cfg.id)
                if device is not None:
                    fresh[cfg.id] = device
            ordered = [keep.get(c.id) or fresh.get(c.id) for c in configs]
            self.registry.replace([d for d in ordered if d is not None])
            self.registry.errors.clear()
            self.registry.errors.update(result.errors)
            for device in stale:
                await self._cancel(device.id)
                try:
                    await device.aclose()
                except Exception:
                    _log.warning(f"Closing the driver of {device.id} failed", device=device.id, exc_info=True)
            for device in fresh.values():
                previous = old.get(device.id)
                if (previous is not None and previous.driver.name == device.driver.name
                        and previous.driver.host == device.driver.host):
                    device.inherit_gate(previous)  # (a changed driver or address is another device to the gate)
                self._log_built(device)
                self._spawn(device)
            for reason in ("added", "changed", "removed"):
                for device_id in getattr(result, reason):
                    device_rebuilds.add(1, {"device.id": device_id, "reason": reason})
            if result.rebuilt:
                _log.info("Devices applied", added=",".join(result.added), changed=",".join(result.changed),
                          removed=",".join(result.removed), failed=",".join(result.errors))
            return result

    async def stop(self) -> None:
        self._running = False
        for device_id in list(self._tasks):
            await self._cancel(device_id)
        await self.registry.aclose()


async def unlink_hops(manager, removed: Iterable[str]) -> Optional[int]:
    """Clears `device_id` on the path hops linked to a removed device: one path version. Returns the new revision, or
    None when no hop was linked (no version is made) or the path would not take the edit (logged)."""
    removed = set(removed)
    if manager is None or not removed or not any(t.device_id in removed for t in manager.config.targets):
        return None

    def unlink(hops):
        return [h.model_copy(update={"device_id": None}) if h.device_id in removed else h for h in hops]

    try:
        return await manager.edit(unlink)
    except Exception as e:
        _log.error(f"Could not unlink the path hops of removed devices {', '.join(sorted(removed))}: {e}",
                   exc_info=True)
        return None


async def register(ctx: Context) -> Component:
    from pengumon.engine.path import path_manager
    from pengumon.settings.hub import Change

    secret_store = ctx.state.secret_store or SecretStore()  # the settings component's; one instance per app
    credentials = CredentialSources(store=secret_store)
    registry = DeviceRegistry()
    supervisor = DeviceSupervisor(registry, ctx.state.repository, credentials, spawn=ctx.spawn, config=ctx.config)
    supervisor.build_all(ctx.config.devices)
    ctx.state.secret_store, ctx.state.credentials, ctx.state.devices = secret_store, credentials, registry
    if ctx.state.repository is not None:
        await warn_unconfigured_history(ctx.state.repository, registry)
    ctx.state.device_supervisor = supervisor
    summaries = LiveSummaries(registry)

    hub = getattr(ctx, "settings", None)
    if hub is not None:
        @hub.on(lists=["devices"], name="devices")
        async def _devices_changed(change) -> None:
            result = await supervisor.apply(ctx.config.devices)
            summaries.forget_missing()
            if result.removed:
                await unlink_hops(path_manager(ctx.state), result.removed)
            failed = await hub.publish(Change.of(events=[DEVICES_REBUILT]))
            if failed:
                raise RuntimeError(f"devices rebuilt, but {', '.join(failed)} did not follow")

    async def start() -> None:
        supervisor.start()

    return Component("devices", start=start, stop=supervisor.stop, live_part=live_part(registry, summaries))
