"""The sign-in's config section (`access:`). The password itself is no setting: it lives in secrets.json (pengumon.access)."""

from pydantic import BaseModel


class AccessConfig(BaseModel):
    """`allow_viewers`: with a password set, people without it may look at the dashboard but change nothing
    (homeguard's viewer policy, pengumon.security.VIEWER_POLICY). PENGUMON_VIEW_WITHOUT_TOKEN wins while it is set."""
    allow_viewers: bool = False
