"""The line's config section (`line:`): the capacity the load is a share of, and the ISP's name."""

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class LineConfig(BaseModel):
    """Internet line capacity in Mbps; None = learned from router history (99.9th percentile, 7 days)."""
    down_mbps: Optional[float] = Field(default=None, gt=0)
    up_mbps: Optional[float] = Field(default=None, gt=0)
    isp_name: Optional[str] = Field(default=None, max_length=60)  # names the dossier ("Acme Outage & ...")

    @field_validator("isp_name")
    @classmethod
    def _clean_isp_name(cls, v: Optional[str]) -> Optional[str]:
        v = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", v).strip() if v else v
        return v or None
