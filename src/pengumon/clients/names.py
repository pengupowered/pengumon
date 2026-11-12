"""The names the owner gives connected devices in Pengumon (schema v20, table client_names): a device whose router name
is unhelpful ("EXAMPLE NETWORK TECHNOLOGY CO" for a game console) can be called what the household calls it.

A name is shown instead of the router's wherever a client's name is shown in full (`privacy.client_details`: the client
list, the router's traffic, the incident inspector); never where clients are masked, and never in the dossier, the AI
pack or MCP, which describe a client by its kind only. The names are kept in memory once read (a handful of rows), so
a client list costs no extra database read.
"""

from typing import Dict, Optional

from pengumon.drivers.common import clean_text

NAME_MAX = 40       # characters of a name
MAX_NAMES = 1000    # names kept at most (a household has tens of devices)


def clean_name(value: object) -> Optional[str]:
    """A name as given, made safe to show and to put in sentences: printable, spaces collapsed, curly brackets made
    round (clean_text), at most NAME_MAX characters; None for an empty one (which takes a name away)."""
    return clean_text(value, NAME_MAX)


class ClientNames:
    """MAC -> name, read from the repository once and kept in step with every change made through `set`."""

    def __init__(self):
        self._names: Optional[Dict[str, str]] = None

    def current(self) -> Dict[str, str]:
        """The names as last read ({} before the first read)."""
        return self._names or {}

    async def load(self, repository) -> Dict[str, str]:
        """The names, read from the repository the first time."""
        if self._names is None:
            self._names = await repository.get_client_names() if repository is not None else {}
        return self._names

    async def set(self, repository, mac: str, name: Optional[str]) -> None:
        """Stores `name` for `mac` (None: removes it). ValueError when MAX_NAMES are kept and this is a new one."""
        names = await self.load(repository)
        if name and mac not in names and len(names) >= MAX_NAMES:
            raise ValueError(f"at most {MAX_NAMES} device names are kept")
        await repository.set_client_name(mac, name)
        if name:
            names[mac] = name
        else:
            names.pop(mac, None)
