"""The probe path (the ICMP targets), owned by the dashboard: loaded from the database at start, edited through the
path editor (api/path.py) and applied without a restart.

One source of truth while the app runs: `config.targets`, the list the probe loop, the classifier (known_targets),
traceroute, the reports and the inspector read. An applied edit replaces its contents in place (so every holder of
the list sees it), hands the prober the new targets (NetworkProber.set_targets) and updates the repository's hidden
ids (hops removed from the path keep their history, hidden). The database (db/path.py) holds the path and its
revision; an edit commits there first, so a refused or failed write changes nothing that is live. Every apply is
also kept there as a version (with a one-line summary of the change), and a hop whose address changed keeps its id,
its history and gets an "address_changed" event; the prober starts its live statistics over. `previews`
(engine/path_preview.py) probes a draft's new hops before it is applied.

At the first start of this version, when the path was never imported or applied, config.yaml's targets and the
custom targets added from the dashboard before (custom_targets) are imported once with their ids, which key all the
history. A config.yaml without targets imports nothing: the default targets are probed (unstored, revision 0) until
the owner applies a path, and the dashboard offers discovery for it. From then on config.yaml's `targets:` is ignored,
with one line in the startup log.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from pengumon.db.path import PathConflict
from pengumon.engine.device_links import link_devices
from pengumon.engine.path_preview import PreviewStore
from pengumon.models.probes import TargetConfig
from pengumon.observability import get_logger

_log = get_logger("pengumon.path")

IGNORED_TARGETS_LINE = ("targets are managed in the dashboard; the targets: section of config.yaml is ignored "
                        "(Path health > Edit > Export / Import moves a path between installs)")

__all__ = ["MAX_PATH_HOPS", "PathConflict", "PathDraft", "PathError", "PathHop", "PathManager", "PathUpdate",
           "hop_dict"]

# --- the path as the editor and its API see it: hops in path order ---------------------------------------------------
#
# A hop is a TargetConfig plus its `position` (0 = nearest to this host). Discovery drafts add `evidence`,
# `confidence`, `suggested_driver` and `status`; a PUT ignores them (pydantic drops unknown fields), so a draft can be
# sent back as it is.

MAX_PATH_HOPS = 64


class PathHop(TargetConfig):
    position: int = Field(..., ge=0, le=100_000)


class PathDraft(BaseModel):
    """A whole path in path order, validated as a PUT validates it (POST /api/path/preview sends one)."""
    hops: List[PathHop] = Field(..., max_length=MAX_PATH_HOPS)

    @model_validator(mode="after")
    def _unique(self):
        ids = [h.id for h in self.hops]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"hop ids must be unique: {', '.join(dup)} is used twice")
        positions = [h.position for h in self.hops]
        if len(set(positions)) != len(positions):
            raise ValueError("two hops have the same position; every hop needs its own place on the path")
        return self

    def targets(self) -> List[TargetConfig]:
        """The hops as TargetConfigs, in path order (their positions only order them)."""
        return [TargetConfig.model_validate(h.model_dump(exclude={"position"}))
                for h in sorted(self.hops, key=lambda h: h.position)]


class PathUpdate(PathDraft):
    """PUT /api/path: the whole path, and the revision it was edited from (409 when another edit came first)."""
    base_revision: int = Field(..., ge=0)


def hop_dict(target: TargetConfig, position: int) -> Dict[str, Any]:
    """One hop of GET /api/path (and of an import draft): the target's fields and its position."""
    return {**target.model_dump(), "position": position}


# --- the live path ----------------------------------------------------------------------------------------------------


class PathError(ValueError):
    """An edit the path cannot take (an id taken by a DNS probe, an unknown device, too many hops)."""


class PathManager:
    def __init__(self, config, repository, prober=None, resolve=None):
        self.config = config
        self.repository = repository
        self.prober = prober
        self.lock = asyncio.Lock()
        self.previews = PreviewStore()  # draft hops probed before an apply (POST /api/path/preview)
        self._resolve = resolve  # host name -> addresses for the device links (engine.device_links); None: the system's

    async def link(self, targets: List[TargetConfig], only=None) -> List[TargetConfig]:
        """The targets with their home hops linked to the configured device at their address (engine.device_links)."""
        if not self.config.devices:
            return list(targets)
        extra = {"resolve": self._resolve} if self._resolve else {}
        return await link_devices(targets, self.config.devices, only=only, **extra)

    async def load(self) -> None:
        """At start, before the prober is built: the one-time import, then the stored path into config.targets."""
        config, repository = self.config, self.repository
        if not await repository.path_initialized():
            custom = await repository.list_custom_targets()
            if config.targets_from_file or custom:
                # what was probed until now: config.yaml's targets (or the defaults), then the custom ones
                taken = {t.id for t in config.targets}
                imported = await self.link(list(config.targets) + [t for t in custom if t.id not in taken])
                if await repository.import_path(imported[:MAX_PATH_HOPS], imported[MAX_PATH_HOPS:]):
                    _log.info("Imported the probe path into the database", hops=min(len(imported), MAX_PATH_HOPS),
                              from_config=len(config.targets), custom=len(imported) - len(config.targets))
                    if len(imported) > MAX_PATH_HOPS:
                        _log.warning(f"A path holds at most {MAX_PATH_HOPS} hops: the first {MAX_PATH_HOPS} targets "
                                     f"are on the path and the other {len(imported) - MAX_PATH_HOPS} were imported "
                                     "as removed hops (history kept; restore them in the path editor)")
            else:
                return  # nothing to import: the defaults are probed, unstored, until a path is applied
        elif config.targets_from_file:
            _log.info(IGNORED_TARGETS_LINE)
        if await repository.drop_stored_ignores_ping():
            _log.info("Dropped the stored \"ignores ping\" settings of the path: whether a hop ignores ping is observed")
        revision, hops = await repository.load_path()
        if not await repository.path_devices_linked():  # a path stored by an older version: link its home hops once
            linked = await self.link(hops)
            if linked != hops:
                await repository.save_path(revision, linked)
                _log.info("Linked the path's home hops to their devices by address",
                          hops=sum(a != b for a, b in zip(linked, hops)))
            else:
                await repository.mark_path_devices_linked()
            hops = linked
        config.targets[:] = hops

    async def current(self) -> Tuple[int, List[TargetConfig]]:
        """The revision (0: never applied) and the live hops in path order."""
        return await self.repository.path_revision(), list(self.config.targets)

    def check(self, targets: List[TargetConfig]) -> None:
        """What a hop list must also satisfy beyond its own validation (raises PathError)."""
        if len(targets) > MAX_PATH_HOPS:
            raise PathError(f"At most {MAX_PATH_HOPS} hops are allowed")
        ids = [t.id for t in targets]
        if len(set(ids)) != len(ids):
            raise PathError("Target ID already exists")
        dns = {d.id for d in self.config.dns_probes}
        clash = sorted(dns.intersection(ids))
        if clash:
            raise PathError(f"{', '.join(clash)} is the id of a DNS lookup; give the hop another id")
        devices = {d.id for d in self.config.devices}
        missing = [t for t in targets if t.device_id and t.device_id not in devices]
        if missing:
            names = ", ".join(f"\"{t.name}\" (device {t.device_id})" for t in missing)
            raise PathError(f"{names} {'is' if len(missing) == 1 else 'are'} linked to a device that is not set up here. "
                            "On that hop choose Change, then No device, or add the device in Settings, Devices.")

    async def apply(self, base_revision: int, targets: List[TargetConfig]) -> int:
        """Stores the path if it is still at base_revision (else PathConflict), then makes it live. A home hop new
        in this path is linked to the device at its address when it has none. The new revision."""
        self.check(targets)
        live = {t.id for t in self.config.targets}
        targets[:] = await self.link(targets, only={t.id for t in targets if t.id not in live})
        async with self.lock:
            revision = await self.repository.save_path(base_revision, targets)
            await self._live_after_save(targets)
        return revision

    async def edit(self, change: Callable[[List[TargetConfig]], List[TargetConfig]]) -> int:
        """Applies change(current hops) to the current revision, with no chance of a conflict (POST and DELETE
        /api/targets). `change` may raise PathError or LookupError to refuse."""
        async with self.lock:
            revision = await self.repository.path_revision()
            targets = change(list(self.config.targets))
            self.check(targets)
            revision = await self.repository.save_path(revision, targets)
            await self._live_after_save(targets)
        return revision

    async def _live_after_save(self, targets: List[TargetConfig]) -> None:
        """Makes a committed path live. When that fails, the live state is reloaded from the database so what is
        probed and what is stored agree (the error is raised after)."""
        try:
            self._go_live(targets)
        except Exception:
            _log.error("Applying the saved path failed; reloading the live path from the database", exc_info=True)
            try:
                _, stored = await self.repository.load_path()
                self._go_live(stored)
            except Exception:
                _log.error("Reloading the stored path failed too", exc_info=True)
            raise

    def _go_live(self, targets: List[TargetConfig]) -> None:
        self.config.targets[:] = targets  # in place: the classifier, the loop and the reports hold this list
        if self.prober is not None:
            self.prober.set_targets(targets, dns_ids=[d.id for d in self.config.dns_probes])
        _log.info("Probe path applied", hops=len(targets))


def path_manager(state) -> Optional[PathManager]:
    """The app's PathManager (built by the probing component), or one built from the state's parts when a test set
    them by hand; None while the engine is not ready."""
    if state.path is not None:
        return state.path
    if not state.config or not state.repository or not state.prober:
        return None
    state.path = PathManager(state.config, state.repository, state.prober)
    return state.path
