"""Which configured device a hop of the probe path is (TargetConfig.device_id), found by address.

A hop links to a device only where that means something: a hop of your network (local) or the line / gateway
(gateway), the devices Pengumon signs in to. A hop is linked automatically when exactly one configured device's URL
host is the hop's address (a host name in the URL is resolved first; a hop given by name matches the same name). That
happens on the one-time import of the path, once for a path stored by an older version, and for a hop that is new in an
applied path; the editor proposes the same link for a hop that has none. A hop the owner linked, one the owner set to
"No device" (device_unlinked), or one that is not a home hop, is left as it is.
"""

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import urlsplit

from pengumon.models.probes import TargetConfig

LINKED_ROLES = ("local", "gateway")
RESOLVE_TIMEOUT_S = 2.0

Resolver = Callable[[str], Awaitable[Iterable[str]]]


def host_of(url: str) -> str:
    """The host of a device URL ("http://192.0.2.1:8080/x" -> "192.0.2.1", "snmp://[2001:db8::1]" -> "2001:db8::1")."""
    return urlsplit(url if "://" in url else f"http://{url}").hostname or url


def _key(address: str) -> str:
    """An address compared by value (2001:DB8::1 is 2001:db8::1), a name without case or the root dot."""
    text = (address or "").strip().strip("[]")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.lower().rstrip(".")


async def system_resolve(host: str) -> List[str]:
    """The addresses of a host name through the system resolver (empty when it does not resolve in time)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_DGRAM), RESOLVE_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        return []
    return [info[4][0] for info in infos]


async def device_addresses(devices: Sequence, resolve: Resolver = system_resolve) -> Dict[str, Set[str]]:
    """Each configured device's id -> the addresses its URL host stands for (the host itself and, for a name, what it
    resolves to), compared by value."""
    hosts = {d.id: host_of(d.url) for d in devices}

    async def one(host: str) -> Set[str]:
        out = {_key(host)}
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            out.update(_key(a) for a in await resolve(host))
        return out

    found = await asyncio.gather(*(one(h) for h in hosts.values()))
    return dict(zip(hosts, found))


def matching_device(target: TargetConfig, addresses: Dict[str, Set[str]]) -> Optional[str]:
    """The one device at the hop's address, or None (none, or several)."""
    key = _key(target.address)
    found = [device_id for device_id, keys in addresses.items() if key in keys]
    return found[0] if len(found) == 1 else None


def linkable(target: TargetConfig) -> bool:
    """A home hop with no device that the owner did not set to "No device"."""
    return target.category in LINKED_ROLES and not target.device_id and not target.device_unlinked


async def device_matches(targets: Sequence[TargetConfig], devices: Sequence,
                         resolve: Resolver = system_resolve) -> Dict[str, str]:
    """Hop id -> the device it would be linked to, for the home hops that have no device yet (what the editor
    proposes)."""
    open_hops = [t for t in targets if linkable(t)]
    if not open_hops or not devices:
        return {}
    addresses = await device_addresses(devices, resolve)
    out = {}
    for t in open_hops:
        match = matching_device(t, addresses)
        if match:
            out[t.id] = match
    return out


async def link_devices(targets: Sequence[TargetConfig], devices: Sequence, only: Optional[Set[str]] = None,
                       resolve: Resolver = system_resolve) -> List[TargetConfig]:
    """The targets with the automatic links made (copies; the others as they are). `only`: the ids that may be
    linked (the hops new in an applied path); None: any."""
    candidates = [t for t in targets if only is None or t.id in only]
    matches = await device_matches(candidates, devices, resolve)
    return [t.model_copy(update={"device_id": matches[t.id]}) if t.id in matches else t for t in targets]
