"""Lenient validation: a mapping validated against a pydantic model, with the parts the model refuses left out (so
they take their defaults) instead of the whole mapping refused.

Used where one bad value must cost that value only: the start-up read of config.yaml (pengumon.config.load_config
with `lenient=True`), the one-time import of config.yaml into the settings store (settings/importer.py) and a stored
document that a newer build's range refuses (settings/manager.py).

What is left out for an error:
- an error inside a list item: the whole item (a device with a bad URL is left out, the other devices stay);
- an error on a single value: that value;
- an error of a rule across several values (a model validator: "degraded below down", "rollups at least raw"): the
  values whose removal alone clears that error; when several do, only the last one in the file (the least is lost);
  when none does, everything under that section.

Plain pydantic: imports nothing of the app (pengumon.config uses it).
"""

import copy
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)
Path = Tuple[Any, ...]  # keys (str) and list indexes (int) into the mapping

MAX_ROUNDS = 500
MAX_TRIALS = 150  # candidates tried against a rule across values: each costs a whole validation
REASON_MAX = 200


def path_text(path: Path) -> str:
    """("devices", 1) -> "devices[1]"; ("classifier", "down_loss_pct") -> "classifier.down_loss_pct"."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def reason(error: Dict[str, Any]) -> str:
    """The message of one pydantic error, without the input (pydantic keeps it apart) and without the "Value error,"
    prefix of a validator's own message."""
    text = str(error.get("msg") or error.get("type") or "invalid")
    text = text.removeprefix("Value error, ").removeprefix("Assertion failed, ")
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 1] + "…"


def _get(data: Any, path: Path) -> Any:
    for part in path:
        data = data[part]
    return data


def _remove(data: Any, path: Path) -> None:
    parent = _get(data, path[:-1])
    del parent[path[-1]]


def _walk(data: Any, loc: Path) -> Tuple[Path, bool]:
    """(the part of `loc` that exists in `data`, whether that path went through a list item)."""
    walked: List[Any] = []
    through_list = False
    current = data
    for part in loc:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            current = current[part]
            through_list = True
        else:
            break
        walked.append(part)
    return tuple(walked), through_list


def _first_list_item(data: Any, path: Path) -> Optional[Path]:
    """The path of the outermost list item on `path`, if it goes through one."""
    current = data
    for n, part in enumerate(path):
        if isinstance(current, list):
            return path[:n + 1]
        current = current[part]
    return None


def leaves(data: Any, prefix: Path = ()) -> Iterator[Path]:
    """Every removable unit under `prefix`, in document order: the values of mappings (descending into nested
    mappings) and whole list items."""
    node = _get(data, prefix)
    if isinstance(node, dict):
        if not node and prefix:
            yield prefix
        for key in list(node):
            child = node[key]
            if isinstance(child, dict) and child:
                yield from leaves(data, prefix + (key,))
            elif isinstance(child, list) and child:
                for i in range(len(child)):
                    yield prefix + (key, i)
            else:
                yield prefix + (key,)
    elif isinstance(node, list):
        for i in range(len(node)):
            yield prefix + (i,)
    else:
        yield prefix


def _errors(model: Type[BaseModel], data: Any) -> List[Dict[str, Any]]:
    try:
        model.model_validate(data)
    except ValidationError as e:
        return e.errors(include_url=False, include_input=False, include_context=False)
    return []


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return tuple(a.get("loc", ())) == tuple(b.get("loc", ())) and a.get("msg") == b.get("msg")


def culprits(model: Type[BaseModel], data: Any, error: Dict[str, Any], prefix: Path,
             candidates: Optional[List[Path]] = None) -> List[Path]:
    """The candidates (default: every leaf under `prefix`) whose removal alone clears `error`, in document order.
    Only the last MAX_TRIALS candidates are tried (each is a whole validation, and the last one in the file is the one
    given up anyway); with more, the earlier ones are not named."""
    found = []
    tries = candidates if candidates is not None else list(leaves(data, prefix))
    tries = tries[-MAX_TRIALS:]
    for path in tries:
        trial = copy.deepcopy(data)
        try:
            _remove(trial, path)
        except (KeyError, IndexError, TypeError):
            continue
        if not any(_same(error, e) for e in _errors(model, trial)):
            found.append(path)
    return found


def _to_remove(model: Type[BaseModel], data: Any, error: Dict[str, Any]) -> Path:
    loc = tuple(error.get("loc", ()))
    walked, through_list = _walk(data, loc)
    if through_list:
        item = _first_list_item(data, walked)
        if item is not None:
            return item
    target = _get(data, walked)
    if walked and not isinstance(target, dict) and not (isinstance(target, list) and len(walked) < len(loc)):
        return walked  # a single value (or a value of the wrong kind, such as a list given as a number)
    # a rule across values (a model validator), or a part missing under a mapping
    found = culprits(model, data, error, walked)
    if found:
        return found[-1]
    return walked


def validate_leniently(model: Type[M], data: Dict[str, Any]) -> Tuple[M, List[Tuple[str, str]]]:
    """(the model built from `data` without the parts it refuses, [(path, reason)] of what was left out). `data` is
    not changed. Raises ValueError only when nothing is left to leave out and the model still refuses (a model whose
    defaults themselves are invalid)."""
    data = copy.deepcopy(data) if isinstance(data, dict) else {}
    dropped: List[Tuple[str, str]] = []
    origins: Dict[Path, List[int]] = {}  # list path -> the file's index of each item still there
    for _ in range(MAX_ROUNDS):
        errors = _errors(model, data)
        if not errors:
            return model.model_validate(data), dropped
        error = errors[0]
        path = _to_remove(model, data, error)
        if not path:  # the whole mapping: nothing smaller to leave out
            raise ValueError(f"the settings cannot be used: {reason(error)}")
        shown = path
        if isinstance(path[-1], int):  # a list item: named by its place in the file, not after earlier removals
            parent = path[:-1]
            index = origins.setdefault(parent, list(range(len(_get(data, parent)))))
            shown = parent + (index.pop(path[-1]),)
        dropped.append((path_text(shown), reason(error)))
        _remove(data, path)
    raise ValueError("the settings cannot be used: too many problems")


def removed_paths(dropped: List[Tuple[str, str]]) -> List[str]:
    return [path for path, _ in dropped]
