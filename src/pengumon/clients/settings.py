"""The client list's config section (`privacy:`)."""

from typing import Literal

from pydantic import BaseModel


class PrivacyConfig(BaseModel):
    """What the API tells about the devices on the network. `client_details`: "token" (the default) shows the names, IP
    and MAC addresses of connected clients only when PENGUMON_ACCESS_TOKEN is set (so only someone who has it can read
    them), "full" always shows them, "masked" never does (a client is its vendor and the last four hex digits of its
    MAC). Counts are never hidden."""
    client_details: Literal["token", "full", "masked"] = "token"
