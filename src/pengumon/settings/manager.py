"""The settings manager: the settings store at run time (state.settings).

At start (`load`): the one-time import of config.yaml when the store was never initialised, then the stored document
over the model defaults, the environment overrides on top, copied into the live config (`state.config`) in place.
After that config.yaml is only read for the bootstrap keys (pengumon.config.BOOTSTRAP_KEYS); one log line says how many
of its settings keys differ from the store.

A save (`apply`): validate the change as a whole candidate config, write the secret slots to secrets.json, commit the
document as the next revision (db/settings.py, with its version), then go live: copy the new effective config into
the live objects in one synchronous block (so no probe round sees half a change) and call the hub's appliers for
what running parts keep a copy of. An applier that fails leaves its parts "applies after a restart": the store is the
truth and the next start reads it.

The document, its versions, the export and every API answer hold no secret: the slots (ntfy URL and token, webhook
URLs, header secrets) live in secrets.json and only their state (set or not, where from, a URL's host) goes out.
"""

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ValidationError

from pengumon.devices.validation import validate_devices
from pengumon import yamlsafe
from pengumon.config import BOOTSTRAP_KEYS, AppConfig, read_config_file
from pengumon.db.settings import SettingsConflict
from pengumon.notify_settings import SECRET_PREFIX, http_url, webhook_id
from pengumon.observability import get_logger
from pengumon.observability.telemetry import settings_imports, settings_saves
from pengumon.secrets import SECRETS_DIR, SecretStore, resolve_slot, slot_env_var, slot_path
from pengumon.settings import env as settings_env
from pengumon.settings import lenient, registry
from pengumon.settings.document import SettingsDocument
from pengumon.settings.hub import Change, SettingsHub
from pengumon.settings.importer import get_path, jsonable, present, read_settings, secret_names
from pengumon.settings.plain import plain

_log = get_logger("pengumon.settings")

SECRET_MAX_CHARS = 4096
RETENTION_WARN_DAYS = 7
EXPORT_HEADER = ("# Pengumon settings (Settings > Export). Import them on the Settings page of any install. Secrets\n"
                 "# (ntfy, webhook URLs and header values, device passwords) are not in this file.\n")
SOURCES = ("save", "restore", "yaml_import")


class SettingsInvalid(Exception):
    """A change the validation refused. `errors`: [{"where", "message"}]; `status` 422, or 400 for a write to a
    part an environment variable pins."""

    def __init__(self, errors: List[Dict[str, str]], status: int = 422,
                 detail: str = "The settings were not saved: fix the marked values"):
        super().__init__(detail)
        self.errors, self.status, self.detail = errors, status, detail


class SecretsWriteFailed(Exception):
    """secrets.json could not be written; nothing was committed."""


@dataclass
class Validation:
    errors: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    summary: str = ""
    document: Optional[SettingsDocument] = None  # the document after the change
    secret_updates: Dict[str, Any] = field(default_factory=dict)
    secret_removals: List[Tuple[str, ...]] = field(default_factory=list)
    slots: Set[str] = field(default_factory=set)  # slots written or cleared
    status: int = 422

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Applied:
    revision: int
    summary: str
    applied: List[str]
    restart_needed: List[str]
    live_failed: List[str]


# --- dotted paths into plain mappings ----------------------------------------------------------------------------

def set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _host(url: Optional[str]) -> Optional[str]:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


def copy_into(live: AppConfig, new: AppConfig) -> None:
    """Copies `new` into the live config objects in place: nested models field by field (so a holder of a section,
    such as the prober's hop_status, sees the change), lists by slice. `targets` (the path store's) is left alone."""
    for name in type(new).model_fields:
        if name == "targets":
            continue
        _copy_value(live, name, getattr(live, name), getattr(new, name))


def _copy_value(owner: Any, name: str, old: Any, new: Any) -> None:
    if isinstance(new, BaseModel) and type(old) is type(new):
        for field_name in type(new).model_fields:
            _copy_value(old, field_name, getattr(old, field_name), getattr(new, field_name))
    elif isinstance(new, list) and isinstance(old, list):
        old[:] = new
    else:
        setattr(owner, name, new)


def _shown(setting: registry.Setting, value: Any, round_seconds: float) -> str:
    """A value as the page shows it, for the version line."""
    if value is None:
        return "default" if setting.default() is None else "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        number = value * round_seconds if setting.rounds else value * setting.scale
        text = f"{round(number, 4):g}"
        return f"{text} {setting.unit}" if setting.unit else text
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    text = str(value)
    return text if len(text) <= 40 else text[:39] + "…"


def _item_id(item: dict) -> str:
    return str(item.get("id") or "")


def _describe_list(name: str, old: Optional[List[dict]], new: Optional[List[dict]]) -> Optional[str]:
    label = registry.LISTS[name].label
    if new is None:
        return None if old is None else f"{label} back to the default"
    before = {_item_id(i): i for i in (old or [])}
    after = {_item_id(i): i for i in new}
    added = [i for i in after if i not in before]
    removed = [i for i in before if i not in after]
    changed = [i for i in after if i in before and before[i] != after[i]]
    parts = []
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if changed:
        parts.append(f"changed {', '.join(changed)}")
    if not parts and [i for i in after if i in before] != [i for i in before if i in after]:
        parts.append("order changed")
    if not parts and old is None:
        parts.append("set" if new else "set to none")
    return f"{label}: {'; '.join(parts)}" if parts else None


def describe(old: SettingsDocument, new: SettingsDocument, slots: Iterable[str] = (),
             round_seconds: float = 1.0) -> str:
    """The version line of a change: "Loss counts as down from 40 % → 35 %; Webhooks: added home"."""
    parts: List[str] = []
    for setting in registry.SETTINGS:
        before, after = old.values.get(setting.key), new.values.get(setting.key)
        if (setting.key in old.values) == (setting.key in new.values) and before == after:
            continue
        was = _shown(setting, before if setting.key in old.values else setting.default(), round_seconds)
        now = _shown(setting, after if setting.key in new.values else setting.default(), round_seconds)
        parts.append(f"{setting.label} {was} → {now}" if was != now else f"{setting.label} {now}")
    for name in registry.LISTS:
        text = _describe_list(name, old.lists.get(name), new.lists.get(name))
        if text:
            parts.append(text)
    for slot in sorted(slots):
        parts.append(f"secret {slot} changed")
    if not parts:
        return "No changes"
    text = "; ".join(parts)
    return text[0].upper() + text[1:]


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
        document = document or self._document
        out = ["ntfy.url", "ntfy.token"]
        for item in document.lists.get("webhooks") or []:
            out.append(f"webhook.{_item_id(item)}.url")
            for text in (item.get("headers") or {}).values():
                if isinstance(text, str) and text.startswith(SECRET_PREFIX):
                    slot = f"webhook_header.{text[len(SECRET_PREFIX):]}"
                    if slot not in out:
                        out.append(slot)
        return out

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
        """The candidate config as a mapping: the bootstrap keys of the live config, the document's values and
        lists (each webhook with its URL from its slot; one without is left out, or reported in `problems`)."""
        data: dict = {}
        for key in BOOTSTRAP_KEYS:
            set_path(data, key, jsonable(get_path(self.config, key)))
        for key, value in document.values.items():
            if key in registry.BY_KEY:
                set_path(data, key, copy.deepcopy(value))
        for name, spec in registry.LISTS.items():
            items = document.lists.get(name)
            if items is None:
                continue
            if name == "webhooks":
                hooks = []
                for i, item in enumerate(items):
                    url = secret(f"webhook.{_item_id(item)}.url")
                    if url is None:
                        if problems is not None:
                            problems.append({"where": f"secrets.webhook.{_item_id(item)}.url",
                                             "message": "a webhook needs its URL"})
                        continue
                    hooks.append({**copy.deepcopy(item), "url": url})
                items = hooks
            set_path(data, spec.path, copy.deepcopy(items))
        return data

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
            set_path(data, key, text)
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
        if await self.repository.initialise_settings(result.document, result.summary(), result.report):
            line = result.summary().replace("Imported from config.yaml", "Imported settings from config.yaml")
            if result.report:
                line += (f"; {len(result.report)} left out (defaults kept): "
                         + ", ".join(r["key"] for r in result.report))
            _log.info(line)
            for note in result.notes:
                _log.info(f"Settings import: {note}")

    async def load(self) -> None:
        """The one-time import (first start of the store), then the stored document into the live config."""
        if not await self.repository.settings_initialised():
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

    # --- validating a change -------------------------------------------------------------------------------------

    def _where(self, loc: Tuple[Any, ...], data: dict, error: dict, touched_values: List[str],
               touched_lists: List[str], document: SettingsDocument) -> List[str]:
        """Where the page shows a validation error of the candidate config."""
        text = lenient.path_text(tuple(loc))
        for name, spec in registry.LISTS.items():
            parts = tuple(spec.path.split("."))
            if tuple(loc[:len(parts)]) == parts and len(loc) > len(parts) and isinstance(loc[len(parts)], int):
                index = loc[len(parts)]
                rest = ".".join(str(p) for p in loc[len(parts) + 1:])
                if name == "webhooks":  # the candidate holds only the webhooks with a URL: back to the item's place
                    sent = get_path_dict(data, spec.path) or []
                    hook_id = _item_id(sent[index]) if index < len(sent) else ""
                    if rest == "url":
                        return [f"secrets.webhook.{hook_id}.url"]
                    ids = [_item_id(i) for i in document.lists.get("webhooks") or []]
                    index = ids.index(hook_id) if hook_id in ids else index
                return [f"lists.{name}[{index}]" + (f".{rest}" if rest else "")]
            if tuple(loc) == parts:
                return [f"lists.{name}"]
        for key in registry.BY_KEY:
            if text == key or text.startswith(key + "."):
                return [f"values.{key}"]
        # a rule across values: the touched values (or list items) whose removal alone clears it
        candidates = [tuple(k.split(".")) for k in touched_values if present(data, k)]
        for name in touched_lists:
            path = tuple(registry.LISTS[name].path.split("."))
            items = document.lists.get(name) or []
            candidates += [path + (i,) for i in range(len(items))]
        found = lenient.culprits(AppConfig, data, error, tuple(loc), candidates)
        out = []
        for path in found:
            if isinstance(path[-1], int):
                spec = registry.list_spec_for_path(".".join(str(p) for p in path[:-1]))
                out.append(f"lists.{spec.name}[{path[-1]}]" if spec else f"values.{lenient.path_text(path)}")
            else:
                out.append(f"values.{'.'.join(path)}")
        return out or [f"values.{text}" if text else "values"]

    def _check_secrets(self, secrets: Mapping[str, str], document: SettingsDocument,
                       out: Validation) -> None:
        hook_ids = {_item_id(i) for i in document.lists.get("webhooks") or []}
        for slot, value in secrets.items():
            where = f"secrets.{slot}"
            path = slot_path(slot)
            if path is None:
                out.errors.append({"where": where, "message": "no such secret"})
                continue
            variable = slot_env_var(slot)
            if variable and settings_env.env_value(variable, self._environ) is not None:
                out.errors.append({"where": where, "message": f"set by the environment variable {variable}; "
                                                              "change it there"})
                out.status = 400
                continue
            if not isinstance(value, str):
                out.errors.append({"where": where, "message": "must be text"})
                continue
            value = value.strip()
            if slot.startswith("webhook.") and slot[len("webhook."):-len(".url")] not in hook_ids and value:
                out.errors.append({"where": where, "message": "no webhook has this id"})
                continue
            if value:
                if len(value) > SECRET_MAX_CHARS or "\n" in value or "\r" in value:
                    out.errors.append({"where": where, "message": f"must be one line of at most {SECRET_MAX_CHARS} "
                                                                  "characters"})
                    continue
                if slot.endswith(".url"):
                    try:
                        http_url(value, "the address", allow_query=True)
                    except ValueError as e:
                        out.errors.append({"where": where, "message": str(e)})
                        continue
                set_path_tuple(out.secret_updates, path, value)
            else:
                out.secret_removals.append(path)
            out.slots.add(slot)

    def _check_lists(self, change_lists: Mapping[str, Optional[List[dict]]], document: SettingsDocument,
                     pending: Mapping[str, str], hop_ids: Set[str], out: Validation) -> None:
        live_devices = {d.id: d for d in self.config.devices}
        for name, items in change_lists.items():
            if items is None:
                continue
            ids: Set[str] = set()
            for i, item in enumerate(items):
                where = f"lists.{name}[{i}]"
                item_id = _item_id(item)
                if item_id and item_id in ids:
                    out.errors.append({"where": f"{where}.id", "message": "this id is used twice"})
                ids.add(item_id)
                if name == "devices" and item_id and item_id not in live_devices:
                    kept = {_item_id(x) for x in items}
                    for old in live_devices.values():
                        if (old.id not in kept and old.driver == item.get("driver")
                                and old.url == (item.get("url") or "").strip()):
                            out.errors.append({"where": f"{where}.id", "message": "an id can't be renamed: its "
                                               f"history and sign-in are kept under the old id ({old.id})"})
                if name == "services" and item.get("ca_file") and not Path(str(item["ca_file"])).is_file():
                    out.errors.append({"where": f"{where}.ca_file", "message": "CA file not found"})
                if name == "dns_probes" and item_id in hop_ids:
                    out.errors.append({"where": f"{where}.id", "message": "this id is a hop of the probe path (its "
                                                                          "history is kept under it)"})
                if name == "webhooks":
                    for header, text in (item.get("headers") or {}).items():
                        if not (isinstance(text, str) and text.startswith(SECRET_PREFIX)):
                            out.errors.append({"where": f"{where}.headers.{header}",
                                               "message": "a header value is a secret: enter it on the page"})
                            continue
                        slot = f"webhook_header.{text[len(SECRET_PREFIX):]}"
                        if slot in pending:
                            if not pending[slot]:
                                out.errors.append({"where": f"secrets.{slot}", "message": "a header secret "
                                                   "this webhook uses can't be cleared"})
                        elif self.secret_value(slot) is None:
                            out.errors.append({"where": f"secrets.{slot}", "message": "this header secret is "
                                               "not set"})

    def _warnings(self, document: SettingsDocument, out: Validation) -> None:
        def value(key):
            return document.values.get(key, registry.get(key).default())

        days = value("database.retention_days")
        before = self.config.database.retention_days
        if isinstance(days, int) and days < RETENTION_WARN_DAYS:
            out.warnings.append({"where": "values.database.retention_days",
                                 "message": f"retention under {RETENTION_WARN_DAYS} days: line-capacity learning "
                                            f"needs {RETENTION_WARN_DAYS}"})
        if isinstance(days, int) and days < before:
            out.warnings.append({"where": "values.database.retention_days",
                                 "message": f"lowering raw retention from {before} to {days} days deletes "
                                            f"{before - days} days of samples at the next hourly cleanup"})
        if value("privacy.client_details") == "full" and not self._token_set():
            out.warnings.append({"where": "values.privacy.client_details",
                                 "message": "client details full: anyone who can reach the port sees client names"})

    @staticmethod
    def _normalise_webhooks(items: List[dict], current: List[dict]) -> List[dict]:
        """New webhooks get an id from their name (an item saved before keeps its id)."""
        taken = {_item_id(i) for i in items if _item_id(i)}
        out = []
        for number, item in enumerate(items, start=1):
            item = dict(item)
            if not _item_id(item):
                item["id"] = webhook_id(item.get("name") or f"webhook-{number}", taken)
                taken.add(item["id"])
            out.append(item)
        return out

    async def validate(self, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                       secrets: Mapping[str, str] = None) -> Validation:
        """Checks a change against the current state as a whole candidate config; nothing is applied."""
        secrets = secrets or {}
        out = Validation()
        overrides = self.env_overrides()
        new_values = dict(self._document.values)
        for key, value in values.items():
            if key not in registry.BY_KEY:
                out.errors.append({"where": f"values.{key}", "message": "no such setting"})
                continue
            if key in overrides:
                out.errors.append({"where": f"values.{key}", "message": f"set by the environment variable "
                                   f"{settings_env.SETTING_ENV[key]}; change it there"})
                out.status = 400
                continue
            if value is None:
                new_values.pop(key, None)
            else:
                new_values[key] = value
        new_lists = dict(self._document.lists)
        for name, items in lists.items():
            if name not in registry.LISTS:
                out.errors.append({"where": f"lists.{name}", "message": "no such list"})
                continue
            if items is not None and not (isinstance(items, list) and all(isinstance(i, dict) for i in items)):
                out.errors.append({"where": f"lists.{name}", "message": "must be a list of items"})
                continue
            if name == "webhooks" and items is not None:
                items = self._normalise_webhooks(items, self._document.lists.get("webhooks") or [])
            new_lists[name] = copy.deepcopy(items) if items is not None else None
        if out.errors:
            return out
        # items as their models store them (every field, normalised); a bad item is reported by the full check
        for name, items in list(new_lists.items()):
            if items is None or name not in lists:
                continue
            model = registry.LISTS[name].item_model
            normalised = []
            for item in items:
                try:
                    normalised.append(model.model_validate(item).model_dump(mode="json"))
                except ValidationError:
                    normalised.append(item)
            new_lists[name] = normalised
        document = SettingsDocument(values=new_values, lists={k: v for k, v in new_lists.items() if v is not None})
        self._check_secrets(secrets, document, out)
        pending = {slot: (v or "").strip() for slot, v in secrets.items() if isinstance(v, str)}

        def secret(slot: str) -> Optional[str]:
            if slot in pending:
                return pending[slot] or None
            return self.secret_value(slot)

        self._check_lists({n: document.lists.get(n) for n in lists}, document, pending,
                          await self._hop_ids() if "dns_probes" in lists else set(), out)
        problems: List[Dict[str, str]] = []
        data = self._data(document, secret, problems)
        if "webhooks" in lists or any(slot.startswith("webhook.") for slot in secrets):
            out.errors += problems
        try:
            AppConfig.model_validate(data)
        except ValidationError as e:
            for error in e.errors(include_url=False, include_input=False, include_context=False):
                for where in self._where(tuple(error.get("loc", ())), data, error, list(values),
                                         list(lists), document):
                    message = plain(lenient.reason(error), ".".join(str(p) for p in error.get("loc", ()) if isinstance(p, str)))
                    if {"where": where, "message": message} not in out.errors:
                        out.errors.append({"where": where, "message": message})
        if lists.get("devices") is not None and not any(e["where"].startswith("lists.devices") for e in out.errors):
            # the dry build of every device (what used to stop the start: a bad option, a missing CA file)
            items = document.lists.get("devices") or []
            index = {_item_id(item): n for n, item in enumerate(items)}
            for where, message in await validate_devices(items, self.config.devices):
                _, _, rest = where.partition(".")
                item_id, _, field = rest.partition(".")
                at = f"lists.devices[{index[item_id]}]" if item_id in index else "lists.devices"
                out.errors.append({"where": at + (f".{field}" if field else ""), "message": message})
        self._warnings(document, out)
        # removing a webhook removes its URL and the header secrets no remaining webhook names
        if "webhooks" in lists:
            kept = {_item_id(i) for i in document.lists.get("webhooks") or []}
            named = set(self.slots(document))
            for item in self._document.lists.get("webhooks") or []:
                if _item_id(item) in kept:
                    continue
                url_slot = f"webhook.{_item_id(item)}.url"
                out.secret_removals.append(slot_path(url_slot))
                out.slots.add(url_slot)
                for slot in self.slots(SettingsDocument(lists={"webhooks": [item]}))[2:]:
                    if slot.startswith("webhook_header.") and slot not in named:
                        out.secret_removals.append(slot_path(slot))
                        out.slots.add(slot)
        out.document = document
        out.summary = describe(self._document, document, out.slots, self.round_seconds)
        return out

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
            if checked.secret_updates or checked.secret_removals:
                try:
                    self.secrets.change(checked.secret_updates, checked.secret_removals)
                except OSError as e:
                    _log.error(f"secrets.json could not be written: {type(e).__name__}; the settings were not saved")
                    raise SecretsWriteFailed() from None
            document = checked.document
            change_values = {k: document.values.get(k) for k in touched_values}
            change_lists = {n: document.lists.get(n) for n in touched_lists}
            revision, old, new, summary = await self.repository.save_settings(
                base_revision, change_values, change_lists, source,
                lambda before, after: describe(before, after, checked.slots, self.round_seconds))
            return await self._go_live(revision, summary, checked.slots)

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
            for name in await self.hub.publish(change):
                failed += self.hub.touched_by(name, change)
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
        """The stored values (those marked for export) and the set lists, in config.yaml syntax. No secret: a
        webhook has no URL and its headers name their secrets."""
        data: dict = {}
        for key, value in self._document.values.items():
            setting = registry.BY_KEY.get(key)
            if setting is not None and setting.export:
                set_path(data, key, copy.deepcopy(value))
        for name, spec in registry.LISTS.items():
            items = self._document.lists.get(name)
            if items is not None:
                set_path(data, spec.path, copy.deepcopy(items))  # a stored webhook has no url
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else "{}\n"
        return EXPORT_HEADER + body

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
        result = read_settings(raw, hop_ids=await self._hop_ids())
        notes = list(result.notes)
        kept = []  # settings whose value in the file the checks refused: the page keeps what it has for them
        for r in result.report:
            spec = registry.BY_KEY.get(r["key"])
            if spec is None:
                notes.append(f"{r['key']}: {plain(r['reason'])} (not imported)")
                continue
            kept.append(spec.key)
            notes.append(f"{spec.label}: {plain(r['reason'])}. Left as it is now.")
        overrides = self.env_overrides()
        values = {}
        for key, value in result.document.values.items():
            if key in overrides:
                notes.append(f"{key}: set by the environment variable {settings_env.SETTING_ENV[key]} (not imported)")
            else:
                values[key] = value
        return {"values": values, "lists": dict(result.document.lists), "notes": notes, "kept": kept}


def get_path_dict(data: dict, path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def set_path_tuple(data: dict, path: Tuple[str, ...], value: Any) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value
