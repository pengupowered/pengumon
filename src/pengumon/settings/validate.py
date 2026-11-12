"""Validating a settings change (SettingsManager.validate): the candidate config built from the stored document and the
change, checked as a whole (the section models, the lists, the secrets, the devices' dry build), with each error
placed where the page shows it, and the warnings. The change's version line is settings/describe.py.

Nothing here writes: `validate` reads what the manager lends it through a `ValidationContext` (the current document, the
live config, the environment, the secret slots) and returns a `Validation` that SettingsManager.apply commits.
"""

import asyncio
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from pydantic import ValidationError

from pengumon.config import AppConfig
from pengumon.devices.validation import validate_devices
from pengumon.notify.settings import SECRET_PREFIX, http_url, webhook_id
from pengumon.secrets import slot_env_var, slot_path
from pengumon.settings import env as settings_env
from pengumon.settings import lenient, registry
from pengumon.settings.finite import MESSAGE, non_finite
from pengumon.settings.describe import describe, list_item_id
from pengumon.settings.document import SettingsDocument
from pengumon.settings.dotted import get_dotted, present, set_dotted
from pengumon.settings.importer import get_path, jsonable
from pengumon.settings.plain import plain
from pengumon.web.settings import theme_warnings

SECRET_MAX_CHARS = 4096
RETENTION_WARN_DAYS = 7


@dataclass
class ValidationContext:
    """What validation reads of the settings manager (SettingsManager.validation_context builds it), and nothing more.
    The callables are asked when the check needs them, not at the start: the manager's state is read where it always
    was."""
    config: AppConfig                                    # the live config
    document: Callable[[], SettingsDocument]             # the stored document
    env_overrides: Callable[[], Dict[str, str]]          # {setting key: text} the environment pins
    environ: Optional[Mapping[str, str]]                 # the environment (None: os.environ)
    token_set: Callable[[], bool]                        # whether an access token is configured
    hop_ids: Callable[[], Awaitable[Set[str]]]           # the ids of the probe path's hops (a DNS probe may not reuse one)
    candidate: Callable[[SettingsDocument, Callable[[str], Optional[str]], Optional[List[Dict[str, str]]]], dict]
    # candidate(document, secret, problems): the document as a config mapping, a webhook's URL from `secret(slot)`
    secret_value: Callable[[str], Optional[str]]         # a secret slot's value, from its sources
    slots: Callable[[Optional[SettingsDocument]], List[str]]  # the secret slots a document uses
    round_seconds: Callable[[], float]                   # the prober's interval


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
                sent = get_dotted(data, spec.path) or []
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

def _check_secrets(ctx: ValidationContext, secrets: Mapping[str, str], document: SettingsDocument,
                   out: Validation) -> None:
    hook_ids = {list_item_id(i) for i in document.lists.get("webhooks") or []}
    for slot, value in secrets.items():
        where = f"secrets.{slot}"
        path = slot_path(slot)
        if path is None:
            out.errors.append({"where": where, "message": "no such secret"})
            continue
        variable = slot_env_var(slot)
        if variable and settings_env.env_value(variable, ctx.environ) is not None:
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
            set_dotted(out.secret_updates, path, value)
        else:
            out.secret_removals.append(path)
        out.slots.add(slot)

def _check_lists(ctx: ValidationContext, change_lists: Mapping[str, Optional[List[dict]]], document: SettingsDocument,
                 pending: Mapping[str, str], hop_ids: Set[str], out: Validation) -> None:
    live_devices = {d.id: d for d in ctx.config.devices}
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
                    url = item.get("url")  # (a wrong kind is reported by the full check)
                    if (old.id not in kept and old.driver == item.get("driver")
                            and isinstance(url, str) and old.url == url.strip()):
                        out.errors.append({"where": f"{where}.id", "message": "an id can't be renamed: its "
                                           f"history and sign-in are kept under the old id ({old.id})"})
            if name == "services" and item.get("ca_file") and not Path(str(item["ca_file"])).is_file():
                out.errors.append({"where": f"{where}.ca_file", "message": "CA file not found"})
            if name == "dns_probes" and item_id in hop_ids:
                out.errors.append({"where": f"{where}.id", "message": "this id is a hop of the probe path (its "
                                                                      "history is kept under it)"})
            if name == "webhooks":
                headers = item.get("headers")
                for header, text in (headers.items() if isinstance(headers, dict) else ()):  # else: the full check
                    if not (isinstance(text, str) and text.startswith(SECRET_PREFIX)):
                        out.errors.append({"where": f"{where}.headers.{header}",
                                           "message": "a header value is a secret: enter it on the page"})
                        continue
                    slot = f"webhook_header.{text[len(SECRET_PREFIX):]}"
                    if slot in pending:
                        if not pending[slot]:
                            out.errors.append({"where": f"secrets.{slot}", "message": "a header secret "
                                               "this webhook uses can't be cleared"})
                    elif ctx.secret_value(slot) is None:
                        out.errors.append({"where": f"secrets.{slot}", "message": "this header secret is "
                                           "not set"})

def _warnings(ctx: ValidationContext, document: SettingsDocument, out: Validation) -> None:
    def value(key):
        return document.values.get(key, registry.get(key).default())

    days = value("database.retention_days")
    before = ctx.config.database.retention_days
    if isinstance(days, int) and days < RETENTION_WARN_DAYS:
        out.warnings.append({"where": "values.database.retention_days",
                             "message": f"Under {RETENTION_WARN_DAYS} days, Pengumon cannot learn your line's speed from "
                                        f"its traffic: that needs {RETENTION_WARN_DAYS} days of samples."})
    if isinstance(days, int) and days < before:
        out.warnings.append({"where": "values.database.retention_days",
                             "message": f"Going from {before} to {days} days deletes the oldest {before - days} days of samples "
                                        f"at the next hourly cleanup."})
    if value("privacy.client_details") == "full" and not ctx.token_set():
        out.warnings.append({"where": "values.privacy.client_details",
                             "message": "With Always and no password set, anyone who can reach the page sees "
                                        "the clients' names and addresses."})
    out.warnings += theme_warnings(document.lists.get("themes") or [])  # (the custom themes' readability: not an error)

def _normalize_webhooks(items: List[dict], current: List[dict]) -> List[dict]:
    """New webhooks get an id from their name (an item saved before keeps its id)."""
    taken = {list_item_id(i) for i in items if list_item_id(i)}
    out = []
    for number, item in enumerate(items, start=1):
        item = dict(item)
        if not list_item_id(item):
            name = item.get("name")
            item["id"] = webhook_id(name if isinstance(name, str) and name else f"webhook-{number}", taken)
            taken.add(item["id"])
        out.append(item)
    return out

def _merge_values(ctx: ValidationContext, values: Mapping[str, Any], out: Validation) -> Dict[str, Any]:
    """The stored values with the change on top (a null removes one); the changes the page may not make are errors."""
    overrides = ctx.env_overrides()
    new_values = dict(ctx.document().values)
    for key, value in values.items():
        if key not in registry.BY_KEY:
            out.errors.append({"where": f"values.{key}", "message": "no such setting"})
            continue
        if key in overrides:
            out.errors.append({"where": f"values.{key}", "message": f"set by the environment variable "
                               f"{settings_env.SETTING_ENV[key]}; change it there"})
            out.status = 400
            continue
        if non_finite(value):
            out.errors.append({"where": f"values.{key}", "message": MESSAGE})
            continue
        if value is None:
            new_values.pop(key, None)
        else:
            new_values[key] = value
    return new_values


def _merge_lists(ctx: ValidationContext, lists: Mapping[str, Optional[List[dict]]],
                 out: Validation) -> Dict[str, Optional[List[dict]]]:
    """The stored lists with the change on top (None: back to the default); a list of the wrong shape or size is an
    error."""
    new_lists = dict(ctx.document().lists)
    for name, items in lists.items():
        if name not in registry.LISTS:
            out.errors.append({"where": f"lists.{name}", "message": "no such list"})
            continue
        if items is not None and not (isinstance(items, list) and all(isinstance(i, dict) for i in items)):
            out.errors.append({"where": f"lists.{name}", "message": "must be a list of items"})
            continue
        if items is not None and non_finite(items):
            out.errors.append({"where": f"lists.{name}", "message": f"a number {MESSAGE}"})
            continue
        if items is not None and len(items) > registry.LIST_MAX[name]:
            out.errors.append({"where": f"lists.{name}", "message": f"too many: at most "
                               f"{registry.LIST_MAX[name]} {registry.LISTS[name].label.lower()} can be kept"})
            continue
        if name == "webhooks" and items is not None:
            items = _normalize_webhooks(items, ctx.document().lists.get("webhooks") or [])
        new_lists[name] = copy.deepcopy(items) if items is not None else None
    return new_lists


def _as_models_store_them(new_lists: Dict[str, Optional[List[dict]]], changed: Iterable[str]) -> None:
    """The changed lists' items as their models store them (every field, normalized); a bad item is reported by the
    full check."""
    for name, items in list(new_lists.items()):
        if items is None or name not in changed:
            continue
        model = registry.LISTS[name].item_model
        normalized = []
        for item in items:
            try:
                normalized.append(model.model_validate(item).model_dump(mode="json"))
            except ValidationError:
                normalized.append(item)
        new_lists[name] = normalized


def _whole_check(data: dict, document: SettingsDocument, value_names: List[str],
                 list_names: List[str]) -> Tuple[Optional[AppConfig], List[Dict[str, str]]]:
    """The candidate as an AppConfig, or (None) each error placed where the page shows it. CPU work (a rule across
    values tries candidates one by one): runs in a thread."""
    found: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    try:
        return AppConfig.model_validate(data), found
    except ValidationError as e:
        for error in e.errors(include_url=False, include_input=False, include_context=False):
            for where in _where(tuple(error.get("loc", ())), data, error, value_names, list_names, document):
                message = plain(lenient.reason(error),
                                ".".join(str(p) for p in error.get("loc", ()) if isinstance(p, str)))
                if (where, message) not in seen:
                    seen.add((where, message))
                    found.append({"where": where, "message": message})
    return None, found


async def _check_devices(ctx: ValidationContext, document: SettingsDocument, out: Validation) -> None:
    """The dry build of every device (what used to stop the start: a bad option, a missing CA file)."""
    items = document.lists.get("devices") or []
    index = {list_item_id(item): n for n, item in enumerate(items)}
    for where, message in await validate_devices(items, ctx.config.devices):
        _, _, rest = where.partition(".")
        item_id, _, field = rest.partition(".")
        at = f"lists.devices[{index[item_id]}]" if item_id in index else "lists.devices"
        out.errors.append({"where": at + (f".{field}" if field else ""), "message": message})


def _removed_webhook_secrets(ctx: ValidationContext, document: SettingsDocument, out: Validation) -> None:
    """Removing a webhook removes its URL and the header secrets no remaining webhook names."""
    kept = {list_item_id(i) for i in document.lists.get("webhooks") or []}
    named = set(ctx.slots(document))
    for item in ctx.document().lists.get("webhooks") or []:
        if list_item_id(item) in kept:
            continue
        url_slot = f"webhook.{list_item_id(item)}.url"
        out.secret_removals.append(slot_path(url_slot))
        out.slots.add(url_slot)
        for slot in ctx.slots(SettingsDocument(lists={"webhooks": [item]}))[2:]:
            if slot.startswith("webhook_header.") and slot not in named:
                out.secret_removals.append(slot_path(slot))
                out.slots.add(slot)


async def validate(ctx: ValidationContext, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                   secrets: Mapping[str, str] = None) -> Validation:
    """Checks a change against the manager's current state as a whole candidate config; nothing is applied."""
    secrets = secrets or {}
    out = Validation()
    new_values = _merge_values(ctx, values, out)
    new_lists = _merge_lists(ctx, lists, out)
    if out.errors:
        return out
    _as_models_store_them(new_lists, lists)
    document = SettingsDocument(values=new_values, lists={k: v for k, v in new_lists.items() if v is not None})
    _check_secrets(ctx, secrets, document, out)
    pending = {slot: (v or "").strip() for slot, v in secrets.items() if isinstance(v, str)}

    def secret(slot: str) -> Optional[str]:
        if slot in pending:
            return pending[slot] or None
        return ctx.secret_value(slot)

    _check_lists(ctx, {n: document.lists.get(n) for n in lists}, document, pending,
                 await ctx.hop_ids() if "dns_probes" in lists else set(), out)
    problems: List[Dict[str, str]] = []
    data = ctx.candidate(document, secret, problems)
    if "webhooks" in lists or any(slot.startswith("webhook.") for slot in secrets):
        out.errors += problems
    built, found = await asyncio.to_thread(_whole_check, data, document, list(values), list(lists))
    known = {(e["where"], e["message"]) for e in out.errors}
    for entry in found:
        if (entry["where"], entry["message"]) not in known:
            out.errors.append(entry)
    if built is not None and not out.errors:
        # a value is stored as the model reads it ("40" as 40, " 22:00" as 22:00), not as it was sent
        for key in values:
            if key in document.values:
                document.values[key] = jsonable(get_path(built, key))
    if lists.get("devices") is not None and not any(e["where"].startswith("lists.devices") for e in out.errors):
        await _check_devices(ctx, document, out)
    _warnings(ctx, document, out)
    if "webhooks" in lists:
        _removed_webhook_secrets(ctx, document, out)
    out.document = document
    out.summary = describe(ctx.document(), document, out.slots, ctx.round_seconds())
    return out
