"""Dotted paths ("a.b.c") into nested plain mappings: one copy of get, set, present and pop for the settings code.

(importer.get_path reads attributes of the config models, and lenient.py walks tuple paths through lists as well:
those are different walks.)"""

from typing import Any, Sequence, Union


def get_dotted(data: dict, path: str) -> Any:
    """The value at the path, or None when a parent is missing or is not a mapping."""
    node: Any = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def set_dotted(data: dict, path: Union[str, Sequence[str]], value: Any) -> None:
    """Sets the value at the path (a dotted string, or its parts), making the parents."""
    parts = path.split(".") if isinstance(path, str) else path
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def present(raw: Any, path: str) -> bool:
    """Whether the mapping sets the dotted path (every parent a mapping, the leaf present and not null)."""
    node = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return node is not None


def pop_dotted(raw: dict, path: str) -> Any:
    """Removes and returns the value at the path (None when it is not there)."""
    parts = path.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    return node.pop(parts[-1], None) if isinstance(node, dict) else None
