"""The providers by the name the setting uses. `make_provider` builds the one the config selects."""

from typing import Dict, Type

from pengumon.speedtest.providers.base import SpeedProvider
from pengumon.speedtest.providers.cloudflare import CloudflareProvider
from pengumon.speedtest.providers.librespeed import LibreSpeedProvider
from pengumon.speedtest.providers.mlab import MLabProvider
from pengumon.speedtest.settings import PROVIDERS, SpeedtestConfig

REGISTRY: Dict[str, Type[SpeedProvider]] = {
    CloudflareProvider.name: CloudflareProvider,
    MLabProvider.name: MLabProvider,
    LibreSpeedProvider.name: LibreSpeedProvider,
}
assert set(REGISTRY) == set(PROVIDERS)


def make_provider(config: SpeedtestConfig, **kwargs) -> SpeedProvider:
    """A fresh provider for one test. `kwargs` (clock, transport, ...) are for tests."""
    if config.provider == "librespeed":
        return LibreSpeedProvider(config.librespeed_url or "", **kwargs)
    return REGISTRY[config.provider](**kwargs)
