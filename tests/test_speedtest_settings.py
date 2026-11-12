"""The speed test's settings section (speedtest/settings.py), its place on the Settings page, and the bufferbloat grade."""

import pytest
from pydantic import ValidationError

from pengumon.config import AppConfig
from pengumon.settings import registry
from pengumon.speedtest.grade import bufferbloat_grade
from pengumon.speedtest.settings import SpeedtestConfig


def test_it_is_off_by_default_and_cloudflare_is_the_default_provider():
    cfg = SpeedtestConfig()
    assert (cfg.enabled, cfg.provider, cfg.interval_hours) == (False, "cloudflare", 24)
    assert (cfg.download_mb, cfg.upload_mb, cfg.monthly_cap_mb, cfg.busy_load_pct) == (25, 10, 0, 20)
    assert (cfg.window_start, cfg.window_end, cfg.librespeed_url) == (None, None, None)
    assert AppConfig().speedtest == cfg


def test_the_default_data_use_is_about_a_gigabyte_a_month():
    cfg = SpeedtestConfig()
    assert (cfg.download_bytes + cfg.upload_bytes) * 31 == pytest.approx(1.1e9, rel=0.02)


@pytest.mark.parametrize("field, value", [("interval_hours", 0), ("interval_hours", 8761), ("interval_hours", 1.5), ("download_mb", 30), ("upload_mb", 100),
                                          ("provider", "ookla"), ("busy_load_pct", 0), ("busy_load_pct", 101),
                                          ("monthly_cap_mb", -1), ("enabled", "maybe")])
def test_values_outside_the_choices_and_ranges_are_refused(field, value):
    with pytest.raises(ValidationError):
        SpeedtestConfig(**{field: value})


def test_librespeed_needs_its_address():
    with pytest.raises(ValidationError, match="librespeed_url is required"):
        SpeedtestConfig(provider="librespeed")
    cfg = SpeedtestConfig(provider="librespeed", librespeed_url=" https://speed.example.com/ ")
    assert cfg.librespeed_url == "https://speed.example.com"
    assert SpeedtestConfig(librespeed_url="").librespeed_url is None  # unused with another provider


@pytest.mark.parametrize("url", ["ftp://speed.example.com", "speed.example.com", "http://", "https://user:pw@speed.example.com",
                                 "https://speed.example.com/?token=1"])
def test_the_librespeed_address_must_be_a_plain_http_url(url):
    with pytest.raises(ValidationError):
        SpeedtestConfig(provider="librespeed", librespeed_url=url)


def test_the_error_never_repeats_the_address():
    with pytest.raises(ValidationError) as e:
        SpeedtestConfig(provider="librespeed", librespeed_url="https://user:hunter2@speed.example.com")
    assert "hunter2" not in str(e.value)


def test_the_window_is_both_ends_or_neither_and_may_cross_midnight():
    assert SpeedtestConfig(window_start="22:00", window_end="02:00").window_start == "22:00"
    for only in ({"window_start": "01:00"}, {"window_end": "01:00"}):
        with pytest.raises(ValidationError, match="go together"):
            SpeedtestConfig(**only)
    with pytest.raises(ValidationError, match="must differ"):
        SpeedtestConfig(window_start="03:00", window_end="03:00")
    with pytest.raises(ValidationError):
        SpeedtestConfig(window_start="25:00", window_end="03:00")
    assert SpeedtestConfig(window_start="", window_end="").window_start is None


def test_the_page_has_a_speed_test_section_after_your_line_with_every_field():
    assert [s.id for s in registry.SECTIONS][2:4] == ["line", "speedtest"]
    keys = {s.key for s in registry.SETTINGS if s.section == "speedtest"}
    assert keys == {f"speedtest.{name}" for name in SpeedtestConfig.model_fields}
    assert registry.unplaced_fields() == []
    assert registry.BY_KEY["speedtest.enabled"].effect == ("Measures download and upload speed on a schedule. "
                                                           "Each test uses the data below.")
    assert "publishes your IP" in registry.BY_KEY["speedtest.provider"].choice_labels["mlab"]
    assert registry.BY_KEY["speedtest.window_start"].constraints()["type"] == "time"
    interval = registry.BY_KEY["speedtest.interval_hours"].constraints()
    assert (interval["type"], interval["min"], interval["max"], interval["choices"]) == ("hours", 1, 8760, None)


@pytest.mark.parametrize("hours", [1, 2, 5, 36, 168, 720, 8760])
def test_the_interval_is_any_whole_number_of_hours_up_to_a_year(hours):
    assert SpeedtestConfig(interval_hours=hours).interval_hours == hours


@pytest.mark.parametrize("hours, text", [(1, "1 hour"), (36, "36 hours"), (24, "1 day"), (72, "3 days"),
                                         (168, "1 week"), (336, "2 weeks"), (720, "30 days"), (8760, "365 days")])
def test_the_version_line_says_the_interval_in_its_largest_whole_unit(hours, text):
    from pengumon.settings.describe import _shown, hours_text
    assert hours_text(hours) == text
    assert _shown(registry.BY_KEY["speedtest.interval_hours"], hours, 1.0) == text


@pytest.mark.parametrize("idle, down, up, grade", [
    (20.0, 24.9, None, "A+"), (20.0, 25.0, None, "A"), (20.0, 49.9, 20.0, "A"), (20.0, 50.0, None, "B"),
    (20.0, 79.9, None, "B"), (20.0, 80.0, None, "C"), (20.0, 219.9, None, "C"), (20.0, 220.0, None, "D"),
    (20.0, 419.9, None, "D"), (20.0, 420.0, None, "F"), (20.0, 1000.0, 30.0, "F"),
    (20.0, 10.0, 12.0, "A+"),            # faster than idle: nothing added
    (20.0, 22.0, 90.0, "C"),             # the worse direction counts
])
def test_the_bufferbloat_grade_follows_the_added_latency(idle, down, up, grade):
    assert bufferbloat_grade(idle, down, up) == grade


def test_no_grade_without_the_idle_latency_or_a_loaded_one():
    assert bufferbloat_grade(None, 50.0, 60.0) is None
    assert bufferbloat_grade(20.0, None, None) is None
