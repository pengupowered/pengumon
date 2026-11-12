"""A settings document as a config: the candidate mapping a change is validated as, the secret slots a document uses,
and the copy of a built config into the live one.

The SettingsManager (manager.py) holds the state these work on and passes it in."""

import copy
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from pengumon.config import BOOTSTRAP_KEYS, AppConfig
from pengumon.notify.settings import SECRET_PREFIX
from pengumon.settings import registry
from pengumon.settings.describe import list_item_id
from pengumon.settings.document import SettingsDocument
from pengumon.settings.dotted import set_dotted
from pengumon.settings.importer import get_path, jsonable


def candidate_data(config: AppConfig, document: SettingsDocument, secret: Callable[[str], Optional[str]],
                   problems: Optional[List[Dict[str, str]]] = None) -> dict:
    """The candidate config as a mapping: the bootstrap keys of the live `config`, the document's values and
    lists (each webhook with its URL from its slot, `secret(slot)`; one without is left out, or reported in
    `problems`)."""
    data: dict = {}
    for key in BOOTSTRAP_KEYS:
        set_dotted(data, key, jsonable(get_path(config, key)))
    for key, value in document.values.items():
        if key in registry.BY_KEY:
            set_dotted(data, key, copy.deepcopy(value))
    for name, spec in registry.LISTS.items():
        items = document.lists.get(name)
        if items is None:
            continue
        if name == "webhooks":
            hooks = []
            for i, item in enumerate(items):
                url = secret(f"webhook.{list_item_id(item)}.url")
                if url is None:
                    if problems is not None:
                        problems.append({"where": f"secrets.webhook.{list_item_id(item)}.url",
                                         "message": "a webhook needs its URL"})
                    continue
                hooks.append({**copy.deepcopy(item), "url": url})
            items = hooks
        set_dotted(data, spec.path, copy.deepcopy(items))
    return data


def document_slots(document: SettingsDocument) -> List[str]:
    """The slots the document uses: ntfy's, each webhook's URL and every header secret it names."""
    out = ["ntfy.url", "ntfy.token"]
    for item in document.lists.get("webhooks") or []:
        out.append(f"webhook.{list_item_id(item)}.url")
        headers = item.get("headers")
        for text in (headers.values() if isinstance(headers, dict) else ()):
            if isinstance(text, str) and text.startswith(SECRET_PREFIX):
                slot = f"webhook_header.{text[len(SECRET_PREFIX):]}"
                if slot not in out:
                    out.append(slot)
    return out


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
