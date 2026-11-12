"""Reading settings out of a config.yaml mapping: the one-time import at the first start of the settings store, and
Settings > Import (a YAML export, or a whole config.yaml, read into a draft).

The file is read leniently (settings/lenient.py): every value is validated with the other values of its section, and
one the models refuse is left out and reported by its key with the reason, never with the value. A key the file does
not set stays unset (its default). A value the file sets is stored even when it equals the default: the owner wrote
it. A list the file has, even an empty one, is stored as set, in file order; a list it leaves out stays at its default.

Left alone: the bootstrap keys (server.host, server.port, database.db_path, prober.interval_seconds; they stay in
config.yaml) and `targets:` (the probe path has its own store). `prober.ping_count_per_probe` does nothing and is
dropped with a note.

Webhooks: each gets an id from its name. At the one-time import its `url` moves to secrets.json (slot
`webhook.<id>.url`) and an inline header value moves to the header secret `<id>_<header>`, the header becoming
`secret:<id>_<header>`, so the store holds no credential. Settings > Import never brings a secret in: those URLs and
values are left out with a note, and the page asks for them.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import ValidationError

from pengumon.config import BOOTSTRAP_KEYS, AppConfig
from pengumon.notify.settings import SECRET_PREFIX, WebhookConfig, WebhookItem, webhook_id
from pengumon.settings import lenient, registry
from pengumon.settings.dotted import pop_dotted, present
from pengumon.settings.document import SettingsDocument

DROPPED_KEYS = {"prober.ping_count_per_probe": "does nothing; dropped"}
LIST_WORDS = {"devices": "devices", "services": "services", "webhooks": "webhooks", "dns_probes": "DNS lookups",
              "themes": "custom themes"}
_SECRET_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class ReadResult:
    document: SettingsDocument
    report: List[Dict[str, str]] = field(default_factory=list)  # [{key, reason}]: left out; never a value
    notes: List[str] = field(default_factory=list)  # informational: what was dropped or left in config.yaml on purpose
    secret_updates: Dict[str, Dict[str, str]] = field(default_factory=dict)  # merged into secrets.json (import only)

    def summary(self) -> str:
        """The version line: "Imported from config.yaml: 27 values; devices 2, services 3, DNS lookups 2"."""
        doc = self.document
        lists = [f"{LIST_WORDS[name]} {len(items)}" for name, items in doc.lists.items() if items is not None]
        if not doc.values and not lists:
            return "Started with the defaults"
        values = f"{len(doc.values)} value{'s' if len(doc.values) != 1 else ''}"
        return "Imported from config.yaml: " + values + (f"; {', '.join(lists)}" if lists else "")


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _dropped(paths: Iterable[str], key: str) -> bool:
    return any(p == key or key.startswith(p + ".") for p in paths)


def _secret_name(hook_id: str, header: str, taken: Set[str]) -> str:
    base = _SECRET_NAME_CHARS.sub("-", f"{hook_id}_{header}")[:64].strip("-") or "webhook"
    name, number = base, 1
    while name in taken:
        number += 1
        suffix = f"-{number}"
        name = base[:64 - len(suffix)] + suffix
    taken.add(name)
    return name


def _webhooks(raw_hooks: Any, move_secrets: bool, taken_secrets: Set[str],
              result: ReadResult) -> Optional[List[dict]]:
    """The webhook items (no url, header values as secret names), the secrets moved out (one-time import) or the
    notes for those left out (Settings > Import)."""
    if raw_hooks is None:
        return None
    if not isinstance(raw_hooks, list):
        result.report.append({"key": "notifications.webhooks", "reason": "must be a list"})
        return None
    model = WebhookConfig if move_secrets else WebhookItem
    hooks: List[Any] = []
    for i, row in enumerate(raw_hooks):
        where = f"notifications.webhooks[{i}]"
        if not isinstance(row, dict):
            result.report.append({"key": where, "reason": "must be a mapping"})
            continue
        row = dict(row)
        had_url = bool(row.get("url"))
        if not move_secrets:
            row.pop("url", None)
        try:
            hooks.append((where, model.model_validate(row), had_url))
        except ValidationError as e:
            result.report.append({"key": where, "reason": lenient.reason(e.errors()[0])})
    names: Set[str] = set()
    ids = [h.id for _, h, _ in hooks if h.id]
    taken_ids: Set[str] = set()
    items: List[dict] = []
    urls_left_out = values_left_out = 0
    for number, (where, hook, had_url) in enumerate(hooks, start=1):
        hook.name = hook.name or f"webhook-{number}"
        if hook.name in names:
            result.report.append({"key": where, "reason": "the name is used twice"})
            continue
        if hook.id and hook.id in taken_ids:
            result.report.append({"key": where, "reason": "the id is used twice"})
            continue
        names.add(hook.name)
        hook.id = hook.id or webhook_id(hook.name, taken_ids | set(ids))
        taken_ids.add(hook.id)
        headers: Dict[str, str] = {}
        for header, text in hook.headers.items():
            if text.startswith(SECRET_PREFIX):
                headers[header] = text
                continue
            name = _secret_name(hook.id, header, taken_secrets)
            headers[header] = SECRET_PREFIX + name
            if move_secrets:
                result.secret_updates.setdefault("webhooks", {})[name] = text
            else:
                values_left_out += 1
        if move_secrets:
            result.secret_updates.setdefault("webhook_urls", {})[hook.id] = hook.url
        elif had_url:
            urls_left_out += 1
        item = WebhookItem.model_validate({**hook.model_dump(exclude={"url"}), "headers": headers})
        items.append(item.model_dump(mode="json"))
    if urls_left_out:
        result.notes.append(f"{urls_left_out} webhook URL{'s' if urls_left_out != 1 else ''} in the file "
                            f"{'were' if urls_left_out != 1 else 'was'} not imported: secrets are entered on the page")
    if values_left_out:
        result.notes.append(f"{values_left_out} header value{'s' if values_left_out != 1 else ''} in the file "
                            f"{'were' if values_left_out != 1 else 'was'} not imported: secrets are entered on the "
                            "page")
    return items


SECRETISH = re.compile(r"ntfy|token|password|secret", re.I)
MAX_UNKNOWN_NOTES = 8


def _unknown_notes(data: dict) -> List[str]:
    """A note for every key of the file that is no setting of this build (it is left out): a person who exported from
    another version, or typed a secret into the file, otherwise sees nothing happen."""
    known = set(registry.stored_leaf_paths())
    unknown: List[str] = []

    def walk(node: Any, path: str) -> None:
        if path in known:
            return
        if isinstance(node, dict) and node:
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif path:  # (an empty file has no keys at all, so nothing is unknown; the root path "" is not a key)
            unknown.append(path)

    walk(data, "")
    notes = [f"{p}: secrets are entered on the page, not read from a file" if SECRETISH.search(p)
             else f"{p}: not a setting of this version" for p in unknown[:MAX_UNKNOWN_NOTES]]
    if len(unknown) > MAX_UNKNOWN_NOTES:
        notes.append(f"and {len(unknown) - MAX_UNKNOWN_NOTES} more keys that are not settings of this version")
    return notes


def read_settings(raw: Any, *, hop_ids: Iterable[str] = (), move_secrets: bool = False,
                  taken_secrets: Iterable[str] = ()) -> ReadResult:
    """The settings a config.yaml mapping (or an export) sets. `hop_ids`: the probe path's ids, live and removed (a
    DNS lookup may not take one); `move_secrets`: the one-time import (webhook URLs and inline header values go to
    `secret_updates`); `taken_secrets`: header secret names already in use."""
    result = ReadResult(SettingsDocument())
    if not isinstance(raw, dict):
        result.report.append({"key": "(file)", "reason": "must be a YAML mapping"})
        return result
    data = copy.deepcopy(raw)
    data.pop("targets", None)
    raw_hooks = pop_dotted(data, "notifications.webhooks")
    for key, why in DROPPED_KEYS.items():
        if present(raw, key):
            result.notes.append(f"{key}: {why}")
    left = [key for key in BOOTSTRAP_KEYS if present(raw, key)] + (["targets"] if raw.get("targets") is not None
                                                                   else [])
    if left:
        result.notes.append(f"left in config.yaml: {', '.join(left)}")

    result.notes += _unknown_notes(data)

    config, dropped = lenient.validate_leniently(AppConfig, data)
    gone = lenient.removed_paths(dropped)
    result.report += [{"key": path, "reason": why} for path, why in dropped
                      if path not in BOOTSTRAP_KEYS and path not in DROPPED_KEYS]

    values: Dict[str, Any] = {}
    for setting in registry.SETTINGS:
        if present(data, setting.key) and not _dropped(gone, setting.key):
            values[setting.key] = jsonable(get_path(config, setting.key))

    _recovery_from_onset(data, values, result)

    lists: Dict[str, Optional[List[dict]]] = {}
    hop_ids = set(hop_ids)
    for name, spec in registry.LISTS.items():
        if name == "webhooks":
            items = _webhooks(raw_hooks, move_secrets, set(taken_secrets), result)
            if items is not None:
                lists[name] = items
            continue
        if not present(data, spec.path) or spec.path in gone:
            continue
        items = jsonable(list(get_path(config, spec.path)))
        if name == "dns_probes":
            kept = []
            for i, item in enumerate(items):
                if item.get("id") in hop_ids:
                    result.report.append({"key": f"dns_probes[{i}]", "reason": "the id is taken by a hop of the probe "
                                                                              "path"})
                else:
                    kept.append(item)
            items = kept
        lists[name] = items
    result.document = SettingsDocument(values=values, lists=lists)
    return result


ONSET_KEY = "classifier.consecutive_failures_for_incident"
RECOVERY_KEY = "classifier.consecutive_successes_for_recovery"


def _recovery_from_onset(data: dict, values: Dict[str, Any], result: ReadResult) -> None:
    """Before the settings store, recovery was confirmed after as many good rounds as a loss outage needs bad ones
    (the onset count). A file that sets the onset and not the recovery keeps that behavior: the recovery count is
    stored as the onset's (when that differs from the recovery's default)."""
    if ONSET_KEY not in values or present(data, RECOVERY_KEY):
        return
    onset = values[ONSET_KEY]
    if onset == registry.get(RECOVERY_KEY).default():
        return
    values[RECOVERY_KEY] = onset
    result.notes.append(f"{RECOVERY_KEY}: set to {ONSET_KEY} ({onset}), as earlier versions confirmed recovery "
                        "after the onset count")


def secret_names(updates: Dict[str, Dict[str, str]]) -> List[Tuple[str, ...]]:
    """The secrets.json paths an import writes (for logs: names only)."""
    return [(section, name) for section, names in updates.items() for name in names]
