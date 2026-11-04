"""The line-signal config section (`line_signal:`): the DOCSIS levels lines/docsis.py judges a cable line by.

The defaults are the commonly quoted cable-industry limits (lines/docsis.py has the reasoning). Plain pydantic, no
import of the app, so pengumon.config can name it."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LineSignalConfig(BaseModel):
    """Upstream power warns above us_power_warning_dbmv (critical above us_power_critical_dbmv) and below
    us_power_low_warning_dbmv (critical below us_power_low_critical_dbmv); downstream SNR warns below
    ds_snr_warning_db (critical below ds_snr_critical_db); downstream power warns beyond ±ds_power_warning_abs_dbmv.
    New uncorrectable codewords warn from a ratio of uncorrectable_ratio_warning of all codewords (critical from
    uncorrectable_ratio_critical), or, without codeword counts, from uncorrectable_warning_per_minute a minute
    (critical above uncorrectable_critical_per_minute)."""
    model_config = ConfigDict(extra="forbid")

    us_power_critical_dbmv: float = Field(51.0, ge=20, le=70)
    us_power_warning_dbmv: float = Field(49.0, ge=20, le=70)
    us_power_low_warning_dbmv: float = Field(38.0, ge=20, le=70)
    us_power_low_critical_dbmv: float = Field(35.0, ge=20, le=70)
    ds_snr_critical_db: float = Field(30.0, ge=10, le=50)
    ds_snr_warning_db: float = Field(33.0, ge=10, le=50)
    ds_power_warning_abs_dbmv: float = Field(8.0, ge=1, le=20)
    uncorrectable_ratio_warning: float = Field(1e-6, gt=0, le=0.1)
    uncorrectable_ratio_critical: float = Field(1e-4, gt=0, le=0.1)
    uncorrectable_warning_per_minute: float = Field(60.0, ge=1, le=100000)
    uncorrectable_critical_per_minute: float = Field(500.0, ge=1, le=100000)

    @model_validator(mode="after")
    def _ordered(self):
        if not self.ds_snr_critical_db < self.ds_snr_warning_db:
            raise ValueError("line_signal: ds_snr_critical_db must be below ds_snr_warning_db")
        if not (self.us_power_low_critical_dbmv < self.us_power_low_warning_dbmv < self.us_power_warning_dbmv
                < self.us_power_critical_dbmv):
            raise ValueError("line_signal: upstream power levels must rise: low critical < low warning < warning "
                             "< critical")
        if not self.uncorrectable_ratio_warning < self.uncorrectable_ratio_critical:
            raise ValueError("line_signal: uncorrectable_ratio_warning must be below uncorrectable_ratio_critical")
        if not self.uncorrectable_warning_per_minute < self.uncorrectable_critical_per_minute:
            raise ValueError("line_signal: uncorrectable_warning_per_minute must be below "
                             "uncorrectable_critical_per_minute")
        return self
