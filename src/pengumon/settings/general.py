"""The general config section (`general:`): the time zone that quiet hours and the daily route check are read in.

The zone is an IANA name, checked with zoneinfo against the system's zone data (python:3.12-slim ships
/usr/share/zoneinfo; tzdata is not a dependency). Where that data is missing only "UTC" and fixed offsets ("+01:00")
are accepted. Plain pydantic, no import of the app, so pengumon.config can name it."""

import os
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_OFFSET = re.compile(r"[+-](0[0-9]|1[0-4]):[0-5][0-9]")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def time_of_day(value: str, what: str) -> str:
    """`value` stripped, when it is a 24-hour "HH:MM"."""
    value = value.strip()
    if not _HHMM.fullmatch(value):
        raise ValueError(f"{what} must be a time of day as HH:MM (00:00 to 23:59)")
    return value


def known_zone(name: str) -> bool:
    """Whether zoneinfo can load `name` (an IANA zone such as "Europe/Berlin")."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not name or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def check_timezone(value: str) -> str:
    """`value` stripped, when it is an IANA zone zoneinfo knows, "UTC" or a fixed offset "+HH:MM" / "-HH:MM"."""
    value = (value or "").strip()
    if value == "UTC" or _OFFSET.fullmatch(value) or known_zone(value):
        return value
    raise ValueError("timezone must be an IANA time zone such as Europe/Berlin, UTC, or an offset such as +01:00")


def server_timezone(environ=None, localtime: Path = Path("/etc/localtime")) -> str:
    """The server's own zone: TZ when it names one, else the zone /etc/localtime links to, else "UTC"."""
    environ = os.environ if environ is None else environ
    tz = (environ.get("TZ") or "").strip().lstrip(":")
    if tz and known_zone(tz):
        return tz
    try:
        target = os.readlink(localtime)
    except OSError:
        target = ""
    name: Optional[str] = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
    return name if name and known_zone(name) else "UTC"


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(default_factory=server_timezone, json_schema_extra={"ui_type": "timezone"})

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return check_timezone(value)
