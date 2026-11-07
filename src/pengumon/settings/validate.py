"""Validating a settings change (SettingsManager.validate): the candidate config built from the stored document and the
change, checked as a whole (the section models, the lists, the secrets, the devices' dry build), with each error
placed where the page shows it, the warnings, and the change's version line (`describe`).

Nothing here writes: `validate` reads the manager's current document, live config, environment and secret slots (it
is the manager's other half, so it uses the manager's own helpers), and returns a `Validation` that
SettingsManager.apply commits.
"""

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from pydantic import ValidationError

from pengumon.config import AppConfig
from pengumon.devices.validation import validate_devices
from pengumon.notify.settings import SECRET_PREFIX, http_url, webhook_id
from pengumon.secrets import slot_env_var, slot_path
from pengumon.settings import env as settings_env
from pengumon.settings import lenient, registry
from pengumon.settings.document import SettingsDocument
from pengumon.settings.importer import present
from pengumon.settings.plain import plain

if TYPE_CHECKING:
    from pengumon.settings.manager import SettingsManager

SECRET_MAX_CHARS = 4096
RETENTION_WARN_DAYS = 7


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


# --- the version line of a change ---------------------------------------------------------------------------------

def _shown(setting: registry.Setting, value: Any, round_seconds: float) -> str:
    """A value as the page shows it, for the version line."""
    if value is None:
        return setting.empty or "none"
    if setting.choice_labels and isinstance(value, str):
        return setting.choice_labels.get(value, value)
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


def list_item_id(item: dict) -> str:
    """A list item's id ("" when it has none)."""
    return str(item.get("id") or "")


def _describe_list(name: str, old: Optional[List[dict]], new: Optional[List[dict]]) -> Optional[str]:
    label = registry.LISTS[name].label
    if new is None:
        return None if old is None else f"{label} back to the default"
    before = {list_item_id(i): i for i in (old or [])}
    after = {list_item_id(i): i for i in new}
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


# --- dotted paths into plain mappings ------------------------------------------------------------------------------

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


# --- validating a change -------------------------------------------------------------------------------------------

def _where(loc: Tuple[Any, ...], data: dict, error: dict, touched_values: List[str],
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
                hook_id = list_item_id(sent[index]) if index < len(sent) else ""
                if rest == "url":
                    return [f"secrets.webhook.{hook_id}.url"]
                ids = [list_item_id(i) for i in document.lists.get("webhooks") or []]
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

def _check_secrets(manager: "SettingsManager", secrets: Mapping[str, str], document: SettingsDocument,
                   out: Validation) -> None:
    hook_ids = {list_item_id(i) for i in document.lists.get("webhooks") or []}
    for slot, value in secrets.items():
        where = f"secrets.{slot}"
        path = slot_path(slot)
        if path is None:
            out.errors.append({"where": where, "message": "no such secret"})
            continue
        variable = slot_env_var(slot)
        if variable and settings_env.env_value(variable, manager._environ) is not None:
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

def _check_lists(manager: "SettingsManager", change_lists: Mapping[str, Optional[List[dict]]], document: SettingsDocument,
                 pending: Mapping[str, str], hop_ids: Set[str], out: Validation) -> None:
    live_devices = {d.id: d for d in manager.config.devices}
    for name, items in change_lists.items():
        if items is None:
            continue
        ids: Set[str] = set()
        for i, item in enumerate(items):
            where = f"lists.{name}[{i}]"
            item_id = list_item_id(item)
            if item_id and item_id in ids:
                out.errors.append({"where": f"{where}.id", "message": "this id is used twice"})
            ids.add(item_id)
            if name == "devices" and item_id and item_id not in live_devices:
                kept = {list_item_id(x) for x in items}
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
                    elif manager.secret_value(slot) is None:
                        out.errors.append({"where": f"secrets.{slot}", "message": "this header secret is "
                                           "not set"})

def _warnings(manager: "SettingsManager", document: SettingsDocument, out: Validation) -> None:
    def value(key):
        return document.values.get(key, registry.get(key).default())

    days = value("database.retention_days")
    before = manager.config.database.retention_days
    if isinstance(days, int) and days < RETENTION_WARN_DAYS:
        out.warnings.append({"where": "values.database.retention_days",
                             "message": f"Under {RETENTION_WARN_DAYS} days, Pengumon cannot learn your line's speed from "
                                        f"its traffic: that needs {RETENTION_WARN_DAYS} days of samples."})
    if isinstance(days, int) and days < before:
        out.warnings.append({"where": "values.database.retention_days",
                             "message": f"Going from {before} to {days} days deletes the oldest {before - days} days of samples "
                                        f"at the next hourly cleanup."})
    if value("privacy.client_details") == "full" and not manager._token_set():
        out.warnings.append({"where": "values.privacy.client_details",
                             "message": "With Always and no password set, anyone who can reach the page sees "
                                        "the clients' names and addresses."})

def _normalise_webhooks(items: List[dict], current: List[dict]) -> List[dict]:
    """New webhooks get an id from their name (an item saved before keeps its id)."""
    taken = {list_item_id(i) for i in items if list_item_id(i)}
    out = []
    for number, item in enumerate(items, start=1):
        item = dict(item)
        if not list_item_id(item):
            item["id"] = webhook_id(item.get("name") or f"webhook-{number}", taken)
            taken.add(item["id"])
        out.append(item)
    return out

async def validate(manager: "SettingsManager", values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                   secrets: Mapping[str, str] = None) -> Validation:
    """Checks a change against the manager's current state as a whole candidate config; nothing is applied."""
    secrets = secrets or {}
    out = Validation()
    overrides = manager.env_overrides()
    new_values = dict(manager.document().values)
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
    new_lists = dict(manager.document().lists)
    for name, items in lists.items():
        if name not in registry.LISTS:
            out.errors.append({"where": f"lists.{name}", "message": "no such list"})
            continue
        if items is not None and not (isinstance(items, list) and all(isinstance(i, dict) for i in items)):
            out.errors.append({"where": f"lists.{name}", "message": "must be a list of items"})
            continue
        if name == "webhooks" and items is not None:
            items = _normalise_webhooks(items, manager.document().lists.get("webhooks") or [])
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
    _check_secrets(manager, secrets, document, out)
    pending = {slot: (v or "").strip() for slot, v in secrets.items() if isinstance(v, str)}

    def secret(slot: str) -> Optional[str]:
        if slot in pending:
            return pending[slot] or None
        return manager.secret_value(slot)

    _check_lists(manager, {n: document.lists.get(n) for n in lists}, document, pending,
                      await manager._hop_ids() if "dns_probes" in lists else set(), out)
    problems: List[Dict[str, str]] = []
    data = manager._data(document, secret, problems)
    if "webhooks" in lists or any(slot.startswith("webhook.") for slot in secrets):
        out.errors += problems
    try:
        AppConfig.model_validate(data)
    except ValidationError as e:
        for error in e.errors(include_url=False, include_input=False, include_context=False):
            for where in _where(tuple(error.get("loc", ())), data, error, list(values),
                                     list(lists), document):
                message = plain(lenient.reason(error), ".".join(str(p) for p in error.get("loc", ()) if isinstance(p, str)))
                if {"where": where, "message": message} not in out.errors:
                    out.errors.append({"where": where, "message": message})
    if lists.get("devices") is not None and not any(e["where"].startswith("lists.devices") for e in out.errors):
        # the dry build of every device (what used to stop the start: a bad option, a missing CA file)
        items = document.lists.get("devices") or []
        index = {list_item_id(item): n for n, item in enumerate(items)}
        for where, message in await validate_devices(items, manager.config.devices):
            _, _, rest = where.partition(".")
            item_id, _, field = rest.partition(".")
            at = f"lists.devices[{index[item_id]}]" if item_id in index else "lists.devices"
            out.errors.append({"where": at + (f".{field}" if field else ""), "message": message})
    _warnings(manager, document, out)
    # removing a webhook removes its URL and the header secrets no remaining webhook names
    if "webhooks" in lists:
        kept = {list_item_id(i) for i in document.lists.get("webhooks") or []}
        named = set(manager.slots(document))
        for item in manager.document().lists.get("webhooks") or []:
            if list_item_id(item) in kept:
                continue
            url_slot = f"webhook.{list_item_id(item)}.url"
            out.secret_removals.append(slot_path(url_slot))
            out.slots.add(url_slot)
            for slot in manager.slots(SettingsDocument(lists={"webhooks": [item]}))[2:]:
                if slot.startswith("webhook_header.") and slot not in named:
                    out.secret_removals.append(slot_path(slot))
                    out.slots.add(slot)
    out.document = document
    out.summary = describe(manager.document(), document, out.slots, manager.round_seconds)
    return out
