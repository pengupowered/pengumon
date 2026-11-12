"""Request bodies of the API that no other module owns."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CredentialsRequest(BaseModel):
    """Payload to update the modem's or the router's login."""
    model_config = ConfigDict(hide_input_in_errors=True)  # validation errors must not carry the password
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    # SNMPv3 authPriv devices only (refused for any other); left out, the stored one is kept
    privacy_password: Optional[str] = Field(default=None, min_length=1, max_length=256)
