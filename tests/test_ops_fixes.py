"""Small hardening fixes in observability, the service installer and the capability contributions."""

import logging
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from pengumon import service
from pengumon.capabilities.contrib import ModuleSection, render_section
from pengumon.observability import export, logs
from pengumon.observability.prometheus import render_metrics
from test_service import FakeRun


# L7: the endpoint in the start-up log line carries no credentials
def test_the_logged_otlp_endpoint_has_no_credentials_path_or_query():
    assert export._shown_endpoint("https://user:secret@collector.example:4318/v1/x?token=abc") == "https://collector.example:4318"
    assert export._shown_endpoint("http://[2001:db8::1]:4318") == "http://[2001:db8::1]:4318"
    assert export._shown_endpoint("http://h:99999") == "(endpoint)"


# L9: the secret-header filter sits on the handlers, so a child logger of the filtered ones is covered
def test_the_secret_header_filter_is_on_the_handlers_not_only_on_three_loggers():
    handler = logs.setup_logging(stream=None)
    logging.getLogger().removeHandler(handler)
    assert any(isinstance(f, logs.SecretHeaderFilter) for f in handler.filters)
    record = logging.LogRecord("uvicorn.error.child", logging.DEBUG, "x", 1, "> Authorization: Bearer abc", (), None)
    assert handler.filter(record) and "abc" not in record.getMessage()
    from pengumon.observability.export import OtelLogHandler
    otel = OtelLogHandler(SimpleNamespace(get_logger=lambda *a: None))
    record = logging.LogRecord("pengumon.child", logging.INFO, "x", 1, "> Cookie: session=abc", (), None)
    assert otel.filter(record) and "abc" not in record.getMessage()


# L10: a bad per-logger spec changes nothing
def test_a_bad_log_spec_leaves_the_root_level_alone():
    root = logging.getLogger()
    before = root.level
    try:
        root.setLevel(logging.ERROR)
        with pytest.raises(ValueError):
            logs.apply_levels("debug", "not-a-spec")
        assert root.level == logging.ERROR
    finally:
        root.setLevel(before)


# L12: one gauge that raises does not fail the scrape
async def test_one_failing_gauge_is_skipped_and_the_rest_of_the_scrape_is_served(monkeypatch):
    from pengumon.observability import prometheus
    good = SimpleNamespace(prom="pengumon_ok", help="h", collect=lambda state: [({}, 1)])

    def boom(state):
        raise TypeError("snapshot")

    bad = SimpleNamespace(prom="pengumon_bad", help="h", collect=boom)
    monkeypatch.setattr(prometheus, "GAUGES", [bad, good])
    text = await render_metrics(None)
    assert "pengumon_ok 1" in text and "pengumon_bad" not in text


# L29: a table cell or finding cannot break the Markdown
def test_a_cell_with_a_pipe_or_a_newline_stays_in_its_cell_and_its_line():
    section = ModuleSection("head\nline", ["a\n- injected"], (["H|1"], [["x|y\nz"]]), {})
    text = render_section(section)
    assert text.splitlines() == ["- head line", "- a - injected", "| H\\|1 |", "|---|", "| x\\|y z |"]


# L17 / L18 / L21 / L22 / L23: the service installer
def test_a_port_outside_1_to_65535_is_refused_by_the_plan():
    for port in (70000, -1):
        with pytest.raises(service.ServiceError, match="between 1 and 65535"):
            service.plan_install("linux", service.USER, python="/v/bin/python", port=port)


@pytest.mark.parametrize("kind", ["linux", "macos"])
@pytest.mark.parametrize("python", ["/home/me/.local/share/uv/tools/pengumon/bin/python", "/root/venv/bin/python",
                                    "/Users/me/venv/bin/python"])
def test_a_system_service_does_not_run_a_program_from_a_home_folder(kind, python):
    with pytest.raises(service.ServiceError, match="home folder"):
        service.plan_install(kind, service.SYSTEM, python=python)
    service.plan_install(kind, service.USER, python=python)  # a user service may


def test_the_mac_group_id_is_free_in_the_group_list_too():
    run = FakeRun({("dscl", ".", "-read"): (1, ""), ("dscl", ".", "-list", "/Users"): (0, "root 0\n_foo 450\n"),
                   ("dscl", ".", "-list", "/Groups"): (0, "wheel 0\n_bar 451\n")})
    service._ensure_mac_user(run)
    assert ["dscl", ".", "-create", "/Groups/_pengumon", "PrimaryGroupID", "452"] in run.calls


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW here")
def test_writing_a_service_file_does_not_follow_a_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "pengumon.service"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        service._write(link, "new", 0o644)
    assert victim.read_text() == "keep"


def test_the_health_check_ignores_proxy_settings(monkeypatch):
    import http.server
    import threading
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in ("http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")  # a proxy nothing listens on
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        assert service.health_body("127.0.0.1", server.server_port) == {"status": "ok"}
        assert seen == ["/api/health"]
    finally:
        server.shutdown()
