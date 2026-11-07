"""Validation of the values that name things: hosts, ids and chart colours."""

import ipaddress
import re
from typing import Optional



# Hostname regex: each label starts and ends with an alphanumeric and may contain hyphens and underscores in
# between (local names such as "my_nas"). The first character of the whole host is therefore never "-", so a host
# can never be read as a command option.
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*$"
)

# Characters that have no place in a host and would matter to a shell
_FORBIDDEN = (";", "&", "|", "$", "`", ">", "<", "\n", "\r", " ")


def validate_host(v: str) -> str:
    """Accepts an IPv4 or IPv6 address or a hostname, and rejects anything that could be read as an option.

    The value is passed to ping and traceroute as an argument, so a leading "-" would make it an option.
    """
    v = v.strip()
    if not v:
        raise ValueError("Host is empty.")
    if any(c in v for c in _FORBIDDEN):
        raise ValueError("Host contains invalid characters.")
    try:
        if ":" in v:
            ipaddress.IPv6Address(v)
        elif re.fullmatch(r"[0-9.]+", v):
            ipaddress.IPv4Address(v)  # rejects 999.1.1.1, 1.2.3 and 192.0.02.4
        elif not HOSTNAME_PATTERN.fullmatch(v):
            raise ValueError(v)
    except ValueError:
        raise ValueError("Host must be a valid IPv4, IPv6, or hostname.") from None
    return v

ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_color(v: Optional[str]) -> Optional[str]:
    """An optional chart colour: #rrggbb (lower-cased), or None for the category's colour."""
    if v is None:
        return None
    if not COLOR_PATTERN.fullmatch(v):
        raise ValueError("color must be #rrggbb")
    return v.lower()
