"""The speed test's config section (`speedtest:`): whether it runs, against which provider, how often, when, and how
much data it may use. Off by default. Read by the scheduler each time it decides (the settings manager updates the
section in place), so every field applies live."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pengumon.notify.settings import http_url
from pengumon.settings.general import time_of_day

PROVIDERS = ("cloudflare", "mlab", "librespeed")


MAX_INTERVAL_HOURS = 365 * 24


class SpeedtestConfig(BaseModel):
    """`download_mb` and `upload_mb` are hard caps per test and direction (decimal megabytes). The window is read in
    general.timezone and may cross midnight; scheduled tests run only inside it. `monthly_cap_mb` 0: no cap. `slow_alert_pct` 0: no alert; else a notification when a
    test measures under that share of the plan speed (line.down_mbps / up_mbps)."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    provider: Literal["cloudflare", "mlab", "librespeed"] = "cloudflare"
    librespeed_url: Optional[str] = None
    # any whole number of hours, 1 hour to 365 days; Settings shows it in hours, days or weeks
    interval_hours: int = Field(24, ge=1, le=MAX_INTERVAL_HOURS, json_schema_extra={"ui_type": "hours"})
    window_start: Optional[str] = Field(None, json_schema_extra={"ui_type": "time"})
    window_end: Optional[str] = Field(None, json_schema_extra={"ui_type": "time"})
    download_mb: Literal[10, 25, 50, 100, 250, 500, 1000] = 25
    upload_mb: Literal[5, 10, 25, 50] = 10
    monthly_cap_mb: int = Field(0, ge=0, le=100_000_000)
    busy_load_pct: int = Field(20, ge=1, le=100)
    slow_alert_pct: int = Field(0, ge=0, le=100)

    @field_validator("librespeed_url")
    @classmethod
    def _url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return http_url(value, "librespeed_url", allow_query=False).rstrip("/")

    @field_validator("window_start", "window_end")
    @classmethod
    def _hhmm(cls, value: Optional[str], info) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return time_of_day(value, info.field_name)

    @model_validator(mode="after")
    def _consistent(self):
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end go together: set both or neither")
        if self.window_start is not None and self.window_start == self.window_end:
            raise ValueError("the window's start and end must differ")
        if self.provider == "librespeed" and not self.librespeed_url:
            raise ValueError("librespeed_url is required when the provider is librespeed")
        return self

    @property
    def download_bytes(self) -> int:
        return self.download_mb * 1_000_000

    @property
    def upload_bytes(self) -> int:
        return self.upload_mb * 1_000_000

    @property
    def monthly_cap_bytes(self) -> int:
        return self.monthly_cap_mb * 1_000_000
