"""The settings document: what the settings store holds, what a version keeps and what an export writes.

`values` holds the keys the owner set ({registry key: value}); a key that is missing means its default. `lists`
holds the four lists by name (pengumon.settings.registry.LISTS); a list that is missing, or None, means its default
(dns_probes: the two default lookups; the others: empty). A document never holds a secret.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SettingsDocument:
    values: Dict[str, Any] = field(default_factory=dict)
    lists: Dict[str, Optional[List[dict]]] = field(default_factory=dict)

    def value(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def list_items(self, name: str) -> Optional[List[dict]]:
        """The stored items of list `name`; None when the list is at its default."""
        return self.lists.get(name)

    def to_json(self) -> dict:
        """{"values": {...}, "lists": {...}}: the shape of a version's `document` column and of a version draft."""
        return {"values": dict(self.values), "lists": {k: v for k, v in self.lists.items() if v is not None}}

    @classmethod
    def from_json(cls, data: Optional[dict]) -> "SettingsDocument":
        data = data or {}
        return cls(values=dict(data.get("values") or {}), lists=dict(data.get("lists") or {}))
