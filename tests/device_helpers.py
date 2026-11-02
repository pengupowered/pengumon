"""Two typical devices as tests build them: `modem` (xfinity_xb) and `router` (asuswrt), with the values of
config.example.yaml, and a password from the environment source when one is given."""

from typing import Optional

import httpx

from pengumon.devices.settings import DeviceConfig
from pengumon.devices.device import Device
from pengumon.devices.registry import build_device
from pengumon.devices.wan import WanRates
from pengumon.secrets import CredentialSources, password_env_var

MODEM = {"id": "modem", "driver": "xfinity_xb", "url": "http://192.168.100.1", "poll_interval_seconds": 900.0}
ROUTER = {"id": "router", "driver": "asuswrt", "url": "http://192.168.1.1", "poll_interval_seconds": 10.0}


def modem_config(**fields) -> DeviceConfig:
    return DeviceConfig(**{**MODEM, **fields})


def router_config(*, clientlist_interval_seconds: Optional[float] = None, **fields) -> DeviceConfig:
    if clientlist_interval_seconds is not None:
        fields["options"] = {"clientlist_interval_seconds": clientlist_interval_seconds}
    return DeviceConfig(**{**ROUTER, **fields})


def password_sources(device_id: str, password: Optional[str]) -> CredentialSources:
    """Only the environment, with `password` for the device (none when it is None)."""
    env = {password_env_var(device_id): password} if password else {}
    return CredentialSources(env=env, secrets_dir=None, store={})


def build_modem(repository=None, *, password: Optional[str] = None, client: Optional[httpx.AsyncClient] = None,
                **fields) -> Device:
    return build_device(modem_config(**fields), repository, password_sources("modem", password), client=client)


def build_router(repository=None, *, password: Optional[str] = None, client: Optional[httpx.AsyncClient] = None,
                 clock=None, wall_clock=None, wan: Optional[WanRates] = None, **fields) -> Device:
    """`clock` (monotonic) paces the driver; `wall_clock` timestamps readings and snapshots."""
    return build_device(router_config(**fields), repository, password_sources("router", password), client=client,
                        clock=clock, wall_clock=wall_clock, wan=wan)
