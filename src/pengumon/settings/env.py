"""The environment variables that override a setting, and the one rule for reading them.

A variable wins over the stored setting when it is set *and not empty*: docker-compose.yml passes
`${PENGUMON_NOTIFY_URL:-}`, so a variable nobody set arrives as "". Every reader of these variables goes through
`env_value`, so that rule lives in one place.
"""

import os
from typing import Dict, Mapping, Optional

# Registry key -> the variable that overrides it (the Settings page shows "From environment" and refuses an edit).
SETTING_ENV: Dict[str, str] = {
    "server.log_level": "PENGUMON_LOG_LEVEL",
    "server.log_levels": "PENGUMON_LOG_LEVELS",
    "display.grafana_url": "PENGUMON_GRAFANA_URL",
}

# Secret slot -> the variable that overrides it (a save that writes the slot is refused while it is set).
SECRET_ENV: Dict[str, str] = {
    "ntfy.url": "PENGUMON_NOTIFY_URL",
    "ntfy.token": "PENGUMON_NOTIFY_TOKEN",
}

OVERRIDE_VARIABLES = frozenset(SETTING_ENV.values()) | frozenset(SECRET_ENV.values())


def env_value(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The variable's value, stripped; None when it is unset, empty or only white space."""
    environ = os.environ if environ is None else environ
    value = (environ.get(name) or "").strip()
    return value or None


def setting_override(key: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The raw text of the variable that overrides registry key `key`, when one is set; else None."""
    name = SETTING_ENV.get(key)
    return env_value(name, environ) if name else None


def setting_overrides(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """{registry key: raw variable text} for every setting a variable pins right now."""
    found = {key: setting_override(key, environ) for key in SETTING_ENV}
    return {key: value for key, value in found.items() if value is not None}


def secret_override(slot: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The value of the variable that overrides secret slot `slot`, when one is set; else None. Never log it."""
    name = SECRET_ENV.get(slot)
    return env_value(name, environ) if name else None
