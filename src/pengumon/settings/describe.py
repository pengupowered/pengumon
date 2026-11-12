"""The version line of a settings change ("Loss counts as down from 40 % → 35 %; Webhooks: added home"): each changed
value as the page shows it, each changed list by the ids added, removed and changed, and the secrets that changed."""

from typing import Any, Iterable, List, Optional

from pengumon.settings import registry
from pengumon.settings.document import SettingsDocument


def hours_text(hours: int) -> str:
    """A number of hours in the largest unit it is a whole number of, as Settings shows it: "1 week", "3 days", "36
    hours" (model.js hoursText)."""
    for size, one, many in ((168, "week", "weeks"), (24, "day", "days"), (1, "hour", "hours")):
        if hours % size == 0:
            count = hours // size
            return f"1 {one}" if count == 1 else f"{count:,} {many}"
    return str(hours)


def _shown(setting: registry.Setting, value: Any, round_seconds: float) -> str:
    """A value as the page shows it, for the version line."""
    if value is None:
        return setting.empty or "none"
    if setting.choice_labels and isinstance(value, str):
        return setting.choice_labels.get(value, value)
    if isinstance(value, bool):
        return "on" if value else "off"
    if setting.unit == "h" and isinstance(value, int) and value > 0:
        return hours_text(value)
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
