"""A LibreSpeed server of the owner's (librespeed-php or the Go backend): `GET {base}/garbage.php?ckSize=<MiB>` and
`POST {base}/empty.php`. Only that server sees anything. `ckSize` is a whole number of MiB, so the cap is divided into
one request per stream of whole MiB (base.plan_requests with a MiB unit; what is left over, less than a MiB per stream,
is not used); the reader cuts the body off at the amount asked for in any case. The server's label is the URL's host.
"""

import time
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from pengumon.speedtest.providers.base import HttpProvider, ProviderError, refused

MIB = 1024 * 1024


class LibreSpeedProvider(HttpProvider):
    name = "librespeed"
    DOWN_PATH = "/garbage.php"
    UP_PATH = "/empty.php"
    UNIT = MIB

    def __init__(self, base_url: str, clock: Callable[[], float] = time.perf_counter,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__(base_url.rstrip("/"), clock, transport)
        self.server = urlsplit(self.base).hostname
        self._who = self.server or "the LibreSpeed server"

    def _download_request(self, size: int) -> Optional[Tuple[Dict[str, int], int]]:
        mib = size // MIB
        if mib < 1:
            return None  # less than one MiB of the cap is left: nothing can be asked for
        return {"ckSize": mib}, min(size, mib * MIB)

    def _refused(self, status: int) -> ProviderError:
        """The reason for an error answer; a 404 most likely means the address is not the folder of the backend."""
        if status == 404:
            return ProviderError(f"{self._who} answered HTTP 404: is the address the folder that holds garbage.php? "
                                 f"For the official Docker image it is http://{urlsplit(self.base).netloc}/backend")
        return refused(status, self._who)
