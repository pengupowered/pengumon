"""The settings manager: the settings store at run time (state.settings).

At start (`load`): the one-time import of config.yaml when the store was never initialized, then the stored document
over the model defaults, the environment overrides on top, copied into the live config (`state.config`) in place.
After that config.yaml is only read for the bootstrap keys (pengumon.config.BOOTSTRAP_KEYS); one log line says how many
of its settings keys differ from the store.

A save (`apply`): validate the change as a whole candidate config (settings/validate.py), write the secret slots to secrets.json, commit the
document as the next revision (db/settings.py, with its version), then go live: copy the new effective config into
the live objects in one synchronous block (so no probe round sees half a change) and call the hub's appliers for
what running parts keep a copy of. An applier that fails leaves its parts "applies after a restart": the store is the
truth and the next start reads it.

The document, its versions, the export and every API answer hold no secret: the slots (ntfy URL and token, webhook
URLs, header secrets) live in secrets.json and only their state (set or not, where from, a URL's host) goes out.
"""

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

from pengumon import yamlsafe
from pengumon.config import AppConfig, read_config_file
from pengumon.db.settings import SettingsConflict
from pengumon.observability import get_logger
from pengumon.observability.telemetry import settings_imports, settings_saves
from pengumon.secrets import SECRETS_DIR, SECRETS_FILE_DAMAGED, SecretsFileUnusable, SecretStore, resolve_slot
from pengumon.settings import env as settings_env
from pengumon.settings import lenient, registry
from pengumon.settings.candidate import candidate_data, copy_into, document_slots
from pengumon.settings.describe import describe
from pengumon.settings.document import SettingsDocument
from pengumon.settings.dotted import set_dotted
from pengumon.settings.hub import Change, SettingsHub
from pengumon.settings.importer import get_path, jsonable, read_settings, secret_names
from pengumon.settings.transfer import draft_from_import, export_yaml
from pengumon.settings.validate import Validation, ValidationContext
from pengumon.settings.validate import validate as validate_change

_log = get_logger("pengumon.settings")

SOURCES = ("save", "restore", "yaml_import")


class SettingsInvalid(Exception):
    """A change the validation refused. `errors`: [{"where", "message"}]; `status` 422, or 400 for a write to a
    part an environment variable pins."""

    def __init__(self, errors: List[Dict[str, str]], status: int = 422,
                 detail: str = "The settings were not saved: fix the marked values"):
        super().__init__(detail)
        self.errors, self.status, self.detail = errors, status, detail


class SecretsWriteFailed(Exception):
    """secrets.json could not be written; nothing was committed. `detail` says why, in words for the page."""

    def __init__(self, detail: str = "The secrets file could not be written; nothing was saved"):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Applied:
    revision: int
    summary: str
    applied: List[str]
    restart_needed: List[str]
    live_failed: List[str]


def _host(url: Optional[str]) -> Optional[str]:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


# --- the manager ---------------------------------------------------------------------------------------------------

class SettingsManager:
    def __init__(self, config: AppConfig, repository, secret_store: SecretStore, hub: Optional[SettingsHub] = None,
                 *, config_path: Optional[Path] = None, file_raw: Optional[dict] = None,
                 environ: Optional[Mapping[str, str]] = None, secrets_dir: Optional[Path] = SECRETS_DIR,
                 token_set: Callable[[], bool] = lambda: False):
        """`config` is the live config (state.config): every apply copies into it. `config_path` / `file_raw`: the
        config.yaml read for the one-time import and the start-up comparison. `environ`: the variables that override
        settings (default os.environ). `token_set` tells whether an access token is configured (a warning)."""
        self.config = config
        self.repository = repository
        self.secrets = secret_store
        self.hub = hub or SettingsHub()
        self._config_path = config_path
        self._raw = file_raw
        self._environ = environ
        self._secrets_dir = secrets_dir
        self._token_set = token_set
        self.revision = 0
        self._document = SettingsDocument()
        self._live_failed: Set[str] = set()
        self._start_values: Dict[str, Any] = {}
        self._import_info: Dict[str, Any] = {"report": [], "imported_at": None}
        self._differing: List[str] = []
        self._lock = asyncio.Lock()

    # --- reading ------------------------------------------------------------------------------------------------

    @property
    def round_seconds(self) -> float:
        return self.config.prober.interval_seconds

    def file_raw(self) -> dict:
        if self._raw is None:
            try:
                self._raw = read_config_file(self._config_path) if self._config_path is not None else {}
            except ValueError as e:
                _log.warning(f"config.yaml could not be read for the settings: {e}")
                self._raw = {}
        return self._raw

    def document(self) -> SettingsDocument:
        return self._document

    def values(self) -> Dict[str, Any]:
        """{registry key: effective value} (env > stored > default)."""
        return {s.key: jsonable(get_path(self.config, s.key)) for s in registry.SETTINGS}

    def list_items(self, name: str) -> List[dict]:
        """The items of a list as the store has them (webhooks without a URL), or its default items."""
        items = self._document.lists.get(name)
        return copy.deepcopy(items) if items is not None else jsonable(registry.LISTS[name].default())

    def env_overrides(self) -> Dict[str, str]:
        return settings_env.setting_overrides(self._environ)

    def secret_value(self, slot: str) -> Optional[str]:
        """The value of a secret slot (env, a secret file, then secrets.json), for the parts that send with it.
        Never log or return it from the API."""
        return resolve_slot(slot, self.secrets, env=self._environ, secrets_dir=self._secrets_dir)[0]

    def secret_state(self, slot: str) -> Dict[str, Any]:
        value, source = resolve_slot(slot, self.secrets, env=self._environ, secrets_dir=self._secrets_dir)
        state: Dict[str, Any] = {"set": value is not None, "source": source}
        if slot.endswith(".url") and value is not None:
            state["host"] = _host(value)
        return state

    def slots(self, document: Optional[SettingsDocument] = None) -> List[str]:
        """The slots the document uses: ntfy's, each webhook's URL and every header secret it names."""
        return document_slots(document or self._document)

    def secret_states(self) -> Dict[str, Dict[str, Any]]:
        return {slot: self.secret_state(slot) for slot in self.slots()}

    def value_states(self) -> Dict[str, Dict[str, Any]]:
        overrides = self.env_overrides()
        values = self.values()
        out = {}
        for setting in registry.SETTINGS:
            stored = self._document.values.get(setting.key)
            if setting.key in overrides:
                source = "env"
            else:
                source = "stored" if setting.key in self._document.values else "default"
            failed = setting.key in self._live_failed
            pending = failed or (not setting.live and values[setting.key] != self._start_values.get(setting.key))
            out[setting.key] = {"value": values[setting.key], "stored": stored, "source": source,
                                "pending_restart": pending, "live_failed": failed}
        return out

    def list_states(self) -> Dict[str, Dict[str, Any]]:
        return {name: {"set": self._document.lists.get(name) is not None, "items": self.list_items(name),
                       "errors": {}} for name in registry.LISTS}

    def import_report(self) -> List[Dict[str, str]]:
        return list(self._import_info.get("report") or [])

    def config_yaml_state(self) -> Dict[str, Any]:
        return {"ignored_since": self._import_info.get("imported_at"), "differing_keys": len(self._differing)}

    @property
    def differing_keys(self) -> List[str]:
        return list(self._differing)

    # --- the effective config -------------------------------------------------------------------------------------

    def _data(self, document: SettingsDocument, secret: Callable[[str], Optional[str]],
              problems: Optional[List[Dict[str, str]]] = None) -> dict:
        """The candidate config as a mapping (settings/candidate.py)."""
        return candidate_data(self.config, document, secret, problems)

    def _with_defaults(self, config: AppConfig, document: SettingsDocument) -> AppConfig:
        if document.lists.get("dns_probes") is None:
            config.dns_probes = registry.LISTS["dns_probes"].default()
        return config

    def effective(self, document: Optional[SettingsDocument] = None,
                  secret: Optional[Callable[[str], Optional[str]]] = None) -> Tuple[AppConfig, List[Tuple[str, str]]]:
        """(the effective config of `document`, what had to be left out of it): the document over the defaults and
        the environment overrides on top, read leniently, so a stored value this build refuses (or a bad variable)
        costs that value only."""
        document = document if document is not None else self._document
        data = self._data(document, secret or self.secret_value)
        for key, text in self.env_overrides().items():
            set_dotted(data, key, text)
        config, dropped = lenient.validate_leniently(AppConfig, data)
        return self._with_defaults(config, document), dropped

    def _go_live_copy(self, config: AppConfig) -> None:
        copy_into(self.config, config)

    # --- start -------------------------------------------------------------------------------------------------------

    async def _hop_ids(self) -> Set[str]:
        ids = {t.id for t in self.config.targets}
        try:
            _, hops = await self.repository.load_path()
            ids |= {t.id for t in hops} | set(await self.repository.removed_path_ids())
        except Exception as e:  # the path store is optional for this check
            _log.warning(f"Could not read the probe path's ids for the settings import: {type(e).__name__}")
        return ids

    async def _import_once(self) -> None:
        raw = self.file_raw()
        taken = self.secrets.data().get("webhooks")
        result = read_settings(raw, hop_ids=await self._hop_ids(), move_secrets=True,
                               taken_secrets=taken.keys() if isinstance(taken, dict) else ())
        if result.secret_updates:
            # secrets.json first: a crash before the commit re-runs the import, which writes the same entries
            self.secrets.change(result.secret_updates)
            _log.info(f"Moved {len(secret_names(result.secret_updates))} webhook secret(s) from config.yaml to "
                      f"{self.secrets.path}")
        if await self.repository.initialize_settings(result.document, result.summary(), result.report):
            line = result.summary().replace("Imported from config.yaml", "Imported settings from config.yaml")
            if result.report:
                line += (f"; {len(result.report)} left out (defaults kept): "
                         + ", ".join(r["key"] for r in result.report))
            _log.info(line)
            for note in result.notes:
                _log.info(f"Settings import: {note}")

    async def load(self) -> None:
        """The one-time import (first start of the store), then the stored document into the live config."""
        if not await self.repository.settings_initialized():
            await self._import_once()
        revision, stored = await self.repository.load_settings()
        unknown_keys = sorted(k for k in stored.values if k not in registry.BY_KEY)
        unknown_lists = sorted(n for n in stored.lists if n not in registry.LISTS)
        if unknown_keys or unknown_lists:
            _log.info(f"Stored settings this build does not know are kept and ignored: "
                      f"{', '.join(unknown_keys + unknown_lists)}")
        self.revision = revision
        self._document = SettingsDocument(
            values={k: v for k, v in stored.values.items() if k in registry.BY_KEY},
            lists={n: v for n, v in stored.lists.items() if n in registry.LISTS})
        config, dropped = self.effective()
        if dropped:
            _log.warning(f"Stored settings refused by this build (their defaults are used): "
                         f"{', '.join(lenient.removed_paths(dropped))}")
        self._go_live_copy(config)
        self._start_values = self.values()
        self._import_info = await self.repository.settings_import_info()
        self._compare_file()

    def _compare_file(self) -> None:
        """One line when config.yaml's settings keys differ from the store: key names only, never values."""
        raw = self.file_raw()
        file_doc = read_settings(raw).document
        differing = []
        for key, value in file_doc.values.items():
            stored = self._document.values.get(key, jsonable(registry.get(key).default()))
            if value != stored:
                differing.append(key)
        for name, items in file_doc.lists.items():
            if items is not None and items != self.list_items(name):
                differing.append(registry.LISTS[name].path)
        self._differing = differing = sorted(differing)
        if differing:
            since = self._import_info.get("imported_at")
            date = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d") if since else "the import"
            _log.info(f"config.yaml is ignored for settings (managed in the dashboard since {date}); "
                      f"{len(differing)} key{'s' if len(differing) != 1 else ''} differ: {', '.join(differing)}")

    # --- validating a change (settings/validate.py) --------------------------------------------------------------

    async def validate(self, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                       secrets: Mapping[str, str] = None) -> Validation:
        """Checks a change against the current state as a whole candidate config; nothing is applied."""
        return await validate_change(self.validation_context(), values, lists, secrets)

    def validation_context(self) -> ValidationContext:
        """What validation may read of this manager (settings/validate.py)."""
        return ValidationContext(
            config=self.config, document=self.document, env_overrides=self.env_overrides, environ=self._environ,
            token_set=self._token_set, hop_ids=self._hop_ids, candidate=self._data, secret_value=self.secret_value,
            slots=self.slots, round_seconds=lambda: self.round_seconds)

    # --- saving ------------------------------------------------------------------------------------------------------

    async def apply(self, base_revision: int, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                    secrets: Optional[Mapping[str, str]] = None, source: str = "save") -> Applied:
        """Validates, writes the secrets, commits and goes live. Raises SettingsInvalid, SettingsConflict or
        SecretsWriteFailed (nothing committed for any of them). Each save is counted by its source and outcome."""
        outcome = "saved"
        try:
            return await self._apply(base_revision, values, lists, secrets, source)
        except SettingsInvalid:
            outcome = "invalid"
            raise
        except SettingsConflict:
            outcome = "conflict"
            raise
        except SecretsWriteFailed:
            outcome = "secrets_failed"
            raise
        finally:
            settings_saves.add(1, {"source": source if source in SOURCES else "unknown", "outcome": outcome})

    async def _apply(self, base_revision: int, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                     secrets: Optional[Mapping[str, str]], source: str) -> Applied:
        if source not in SOURCES:
            raise SettingsInvalid([{"where": "source", "message": f"must be one of {', '.join(SOURCES)}"}], 400)
        async with self._lock:
            checked = await self.validate(values, lists, secrets)
            if not checked.ok:
                raise SettingsInvalid(checked.errors, checked.status)
            touched_values = [k for k in values]
            touched_lists = [n for n in lists]
            current, conflicts = await self.repository.settings_conflicts(base_revision, touched_values,
                                                                          touched_lists)
            if conflicts:
                raise SettingsConflict(current, conflicts)
            written: Optional[Dict[Tuple[str, ...], Optional[str]]] = None  # path -> the value it had (None: none)
            if checked.secret_updates:  # written before the commit (a crash between the two loses no secret) ...
                paths = _leaf_paths(checked.secret_updates)
                written = {path: self.secrets.get(path) for path in paths}
                try:
                    self.secrets.change(checked.secret_updates, [])
                except SecretsFileUnusable:  # logged by the store, with what to do
                    raise SecretsWriteFailed(SECRETS_FILE_DAMAGED) from None
                except OSError as e:
                    _log.error(f"secrets.json could not be written: {type(e).__name__}; the settings were not saved")
                    raise SecretsWriteFailed() from None
            document = checked.document
            change_values = {k: document.values.get(k) for k in touched_values}
            change_lists = {n: document.lists.get(n) for n in touched_lists}
            try:
                revision, old, new, summary = await self.repository.save_settings(
                    base_revision, change_values, change_lists, source,
                    lambda before, after: describe(before, after, checked.slots, self.round_seconds))
            except BaseException:  # ... and a failed commit puts back what it replaced (a failed save changes nothing)
                if written:
                    self._restore_secrets(written)
                raise
            if checked.secret_removals:
                try:
                    self.secrets.change({}, checked.secret_removals)
                except (SecretsFileUnusable, OSError) as e:  # committed: the secrets of a removed webhook stay unused
                    _log.error(f"Secrets of removed items could not be deleted from secrets.json: {type(e).__name__}")
            return await self._go_live(revision, summary, checked.slots)

    def _restore_secrets(self, before: Mapping[Tuple[str, ...], Optional[str]]) -> None:
        """Puts the secrets of `before` (path -> value it had, None for none) back, after a commit that failed."""
        updates: dict = {}
        for path, value in before.items():
            if value is not None:
                node = updates
                for part in path[:-1]:
                    node = node.setdefault(part, {})
                node[path[-1]] = value
        try:
            self.secrets.change(updates, [path for path, value in before.items() if value is None])
        except (SecretsFileUnusable, OSError) as e:
            _log.error(f"Secrets could not be put back after a failed save: {type(e).__name__}")

    async def _go_live(self, revision: int, summary: str, slots: Set[str]) -> Applied:
        old_document = self._document
        old_values = self.values()
        old_lists = {name: jsonable(list(get_path(self.config, spec.path))) for name, spec in registry.LISTS.items()}
        _, stored = await self.repository.load_settings()
        self._document = SettingsDocument(
            values={k: v for k, v in stored.values.items() if k in registry.BY_KEY},
            lists={n: v for n, v in stored.lists.items() if n in registry.LISTS})
        self.revision = revision
        config, dropped = self.effective()
        if dropped:
            _log.warning(f"Settings left at their defaults: {', '.join(lenient.removed_paths(dropped))}")
        self._go_live_copy(config)  # one synchronous block: no probe round sees half of it
        new_values = self.values()
        changed_keys = [k for k in new_values if new_values[k] != old_values.get(k)]
        changed_lists = [name for name, spec in registry.LISTS.items()
                         if jsonable(list(get_path(self.config, spec.path))) != old_lists[name]
                         or old_document.lists.get(name) != self._document.lists.get(name)]
        change = Change.of(keys=changed_keys, lists=changed_lists, slots=slots, old=old_document,
                           new=self._document)
        failed: List[str] = []
        if not change.empty:
            ran = self.hub.subscribers(change)
            broke = await self.hub.publish(change)
            for name in broke:
                failed += self.hub.touched_by(name, change)
            for name in ran:  # what applied this time is no longer waiting for a restart
                if name not in broke:
                    self._live_failed -= set(self.hub.touched_by(name, change))
        self._live_failed |= set(failed)
        restart = sorted(k for k in changed_keys if not registry.get(k).live)
        _log.info(f"Settings saved as revision {revision}: {summary}", revision=revision)
        return Applied(revision=revision, summary=summary, applied=sorted(changed_keys) + sorted(changed_lists),
                       restart_needed=restart, live_failed=sorted(set(failed)))

    # --- versions, export, import --------------------------------------------------------------------------------

    async def versions(self) -> List[Dict[str, Any]]:
        return await self.repository.settings_versions()

    async def version_draft(self, revision: int) -> Optional[Dict[str, Any]]:
        """A kept version as a draft against the current document: every value it sets, null for a value stored now
        and not then, and each list (null: back to its default). Saving it restores that version."""
        document = await self.repository.settings_version(revision)
        if document is None:
            return None
        values: Dict[str, Any] = {k: None for k in self._document.values}
        values.update({k: v for k, v in document.values.items() if k in registry.BY_KEY})
        for key in self.env_overrides():
            values.pop(key, None)  # pinned: a restore leaves it alone
        lists = {name: document.lists.get(name) for name in registry.LISTS}
        return {"revision": revision, "values": values, "lists": lists}

    def export_yaml(self) -> str:
        """The stored values (those marked for export) and the set lists, in config.yaml syntax (settings/transfer.py)."""
        return export_yaml(self._document)

    async def import_yaml(self, text: str) -> Dict[str, Any]:
        """An export (or a whole config.yaml) read into a draft {values, lists, notes}. Nothing is applied and no
        secret comes in. Raises ValueError with a plain message."""
        try:
            raw = yamlsafe.load_plain(text, "settings file")
            if not isinstance(raw, dict):
                raise ValueError("A settings file is a YAML mapping (as Export writes it)")
        except ValueError:
            settings_imports.add(1, {"outcome": "invalid"})
            raise
        settings_imports.add(1, {"outcome": "ok"})
        result = await asyncio.to_thread(read_settings, raw, hop_ids=await self._hop_ids())
        return draft_from_import(result, self.env_overrides())


def _leaf_paths(tree: Mapping[str, Any], prefix: Tuple[str, ...] = ()) -> List[Tuple[str, ...]]:
    """The paths of the values (leaves) of a nested dict."""
    paths: List[Tuple[str, ...]] = []
    for key, value in tree.items():
        if isinstance(value, Mapping):
            paths += _leaf_paths(value, prefix + (key,))
        else:
            paths.append(prefix + (key,))
    return paths
