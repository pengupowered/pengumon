"""The fixed words a failed poll is shown in to someone who may not edit are defined once (pengumon.devices.summary) and
used by the API, the live stream and the MCP tool."""

from pengumon.api import devices as api_devices
from pengumon.devices import summary
from pengumon.mcp import tools


def test_the_fixed_words_have_one_definition():
    assert summary.POLL_FAILURES["auth"] and summary.POLL_FAILED
    assert tools.POLL_FAILURES is summary.POLL_FAILURES and tools.POLL_FAILED is summary.POLL_FAILED
    assert api_devices.viewer_error is summary.viewer_error
    assert not hasattr(tools, "_POLL_FAILURES") and not hasattr(api_devices, "_POLL_FAILURES")


def test_viewer_error_words_by_kind_and_keeps_unconfigured_messages():
    assert summary.viewer_error("error", "timeout", "raw 192.0.2.5/x").startswith(summary.POLL_FAILURES["timeout"])
    assert summary.viewer_error("error", "odd", "raw").startswith(summary.POLL_FAILED)
    assert summary.viewer_error("unconfigured", None, "Device x is not set up") == "Device x is not set up"
    assert summary.viewer_error("healthy", None, None) is None


def test_the_settings_route_and_the_manager_share_one_host_helper():
    """api/settings.py had a copy of the manager's `_host`; the same function now serves both."""
    from pengumon.api import settings as api_settings
    from pengumon.settings import manager

    assert api_settings._host is manager._host
    assert manager._host("https://collector.example.com:4318/v1") == "collector.example.com"
    assert manager._host("http://[bad") is None and manager._host(None) is None
