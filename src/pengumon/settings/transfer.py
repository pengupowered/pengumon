"""Settings in and out as YAML: the export of the stored document, and an imported file read into a draft.

The SettingsManager (manager.py) holds the document and the environment and calls these."""

import copy
from typing import Any, Dict

import yaml

from pengumon.settings import env as settings_env
from pengumon.settings import registry
from pengumon.settings.document import SettingsDocument
from pengumon.settings.dotted import set_dotted
from pengumon.settings.finite import MESSAGE, non_finite
from pengumon.settings.importer import ReadResult
from pengumon.settings.plain import plain

EXPORT_HEADER = ("# Pengumon settings (Settings > Export). Import them on the Settings page of any install. Secrets\n"
                 "# (ntfy, webhook URLs and header values, device passwords) are not in this file.\n")


def export_yaml(document: SettingsDocument) -> str:
    """The stored values (those marked for export) and the set lists, in config.yaml syntax. No secret: a
    webhook has no URL and its headers name their secrets."""
    data: dict = {}
    for key, value in document.values.items():
        setting = registry.BY_KEY.get(key)
        if setting is not None and setting.export:
            set_dotted(data, key, copy.deepcopy(value))
    for name, spec in registry.LISTS.items():
        items = document.lists.get(name)
        if items is not None:
            set_dotted(data, spec.path, copy.deepcopy(items))  # a stored webhook has no url
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else "{}\n"
    return EXPORT_HEADER + body


def draft_from_import(result: ReadResult, overrides: Dict[str, str]) -> Dict[str, Any]:
    """An imported file's settings (`result`, read_settings) as a draft {values, lists, notes, kept}. `overrides`: the
    settings the environment pins, which a file may not set."""
    notes = list(result.notes)
    kept = []  # settings whose value in the file the checks refused: the page keeps what it has for them
    for r in result.report:
        spec = registry.BY_KEY.get(r["key"])
        if spec is None:
            notes.append(f"{r['key']}: {plain(r['reason'])} (not imported)")
            continue
        kept.append(spec.key)
        notes.append(f"{spec.label}: {plain(r['reason'])}. Left as it is now.")
    values = {}
    for key, value in result.document.values.items():
        if non_finite(value):  # .inf in the file: it could not be saved or shown
            kept.append(key)
            notes.append(f"{registry.BY_KEY[key].label}: {MESSAGE}. Left as it is now.")
        elif key in overrides:
            notes.append(f"{key}: set by the environment variable {settings_env.SETTING_ENV[key]} (not imported)")
        else:
            values[key] = value
    lists = {}
    for name, items in result.document.lists.items():
        if items is not None and non_finite(items):
            notes.append(f"{registry.LISTS[name].label}: a number that {MESSAGE} (not imported)")
            continue
        lists[name] = items
    return {"values": values, "lists": lists, "notes": notes, "kept": kept}
