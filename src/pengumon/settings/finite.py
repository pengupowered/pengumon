"""Infinity and NaN in a settings change. JSON as Python reads it (and YAML's .inf / .nan) allows them; a number
model with only a lower bound accepts +inf, and the value then cannot be stored (JSON without NaN) or sent back.
Every route that takes settings values refuses them with a plain message instead."""

import math
from typing import Any, Optional

MESSAGE = "must be a finite number"


def non_finite(value: Any, where: str = "") -> Optional[str]:
    """The place (a dotted path, "[3]" for a list index) of the first float in `value` that is infinite or NaN, else
    None. `where` is the prefix of the place."""
    if isinstance(value, float):
        return where or "(value)" if not math.isfinite(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            found = non_finite(item, f"{where}.{key}" if where else str(key))
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for number, item in enumerate(value):
            found = non_finite(item, f"{where}[{number}]")
            if found:
                return found
    return None
