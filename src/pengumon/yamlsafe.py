"""Reading YAML a browser uploads (the path editor's and the Settings page's Import): size and nesting limits, and no
anchors, aliases or merge keys, checked on the parser's events before anything is constructed."""

from typing import Any

import yaml

IMPORT_MAX_BYTES = 256 * 1024
IMPORT_MAX_DEPTH = 16  # nesting of the YAML document; a path export is three deep, a settings export four


def check_yaml_plain(text: str, what: str = "path file") -> None:
    """Refuses what an export never contains and what can blow up while loading: anchors, aliases and merge keys
    (a few hundred bytes of nested aliases expand to gigabytes) and deep nesting. Works on the parser's events, so
    nothing is constructed. Raises ValueError; a YAML syntax error is left to the loader."""
    depth = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ValueError(f"Anchors and aliases (&name, *name) are not allowed in a {what}")
            if isinstance(event, yaml.ScalarEvent) and event.value == "<<" and event.implicit[0]:
                raise ValueError(f"Merge keys (<<) are not allowed in a {what}")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > IMPORT_MAX_DEPTH:
                    raise ValueError(f"That file is nested too deeply to be a {what.removesuffix(' file')}")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
    except yaml.YAMLError:
        return  # reported by the loader with its position


def yaml_error(e: yaml.YAMLError) -> ValueError:
    """A YAML syntax error by position only (PyYAML's own message quotes the line, which may hold a secret)."""
    mark = getattr(e, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    return ValueError(f"That is not valid YAML{where}: {getattr(e, 'problem', None) or type(e).__name__}")


def load_plain(text: str, what: str = "path file") -> Any:
    """The document of an upload, within the limits; raises ValueError with a plain message."""
    name = what.removesuffix(" file")
    if len(text) > IMPORT_MAX_BYTES:
        raise ValueError(f"That file is too big to be a {name}")
    check_yaml_plain(text, what)
    try:
        return yaml.safe_load(text)
    except RecursionError:
        raise ValueError(f"That file is nested too deeply to be a {name}") from None
    except yaml.YAMLError as e:
        raise yaml_error(e) from None
