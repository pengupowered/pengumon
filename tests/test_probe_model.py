"""TargetConfig's probe fields (models.probes): every refusal, and the labels."""

import pytest
from pydantic import ValidationError

from pengumon.models.probes import (BLOCKED_PORTS, MAX_SOCKET_HOPS, TargetConfig, TargetStats, probe_identity,
                                    probe_label, probe_words)


def hop(**kw):
    base = {"id": "h", "name": "Hop", "category": "public", "address": "192.0.2.10"}
    base.update(kw)
    return TargetConfig(**base)


def refusal(**kw):
    with pytest.raises(ValidationError) as e:
        hop(**kw)
    return str(e.value)


def test_a_hop_is_probed_by_ping_unless_it_says_otherwise():
    t = hop()
    assert (t.probe, t.port, t.http_scheme, t.http_path, t.http_verb) == ("icmp", None, "https", "/", "HEAD")
    assert probe_label(t) is None and probe_words(t) == ""
    assert MAX_SOCKET_HOPS == 8


def test_a_tcp_hop_needs_a_port_and_ping_and_http_fields_refuse_what_does_not_belong():
    assert "a TCP probe needs a port" in refusal(probe="tcp")
    assert "port is only used by TCP and HTTP probes" in refusal(port=443)
    assert "only used by HTTP probes" in refusal(http_path="/x")
    assert "only used by HTTP probes" in refusal(probe="tcp", port=443, http_verb="GET")
    assert hop(probe="tcp", port=443).port == 443


def test_an_http_hop_must_be_other():
    assert "must have the role custom" in refusal(probe="http", category="isp")
    t = hop(probe="http", category="custom", http_path="/health")
    assert t.probe == "http"


def test_blocked_ports_are_refused_and_the_list_is_the_fetch_standards():
    assert {22, 25, 53, 6667, 10080} <= BLOCKED_PORTS
    assert "port 25 is refused" in refusal(probe="tcp", port=25)
    assert "port 53 is refused" in refusal(probe="http", category="custom", port=53)
    assert hop(probe="tcp", port=853).port == 853
    assert "greater than or equal to 1" in refusal(probe="tcp", port=0)
    assert "less than or equal to 65535" in refusal(probe="tcp", port=65536)


@pytest.mark.parametrize("path, message", [
    ("health", "start with a single /"), ("//evil.example/x", "start with a single /"),
    ("/a?x=1", "query string or fragment is not allowed"), ("/a#frag", "query string or fragment is not allowed"),
    ("/a b", "may only hold"), ("/café", "may only hold"), ("/a\r\nHost: x", "may only hold"),
    ("/a%2", "two hex digits"), ("/a%zz", "two hex digits"),
])
def test_a_bad_http_path_is_refused(path, message):
    assert message in refusal(probe="http", category="custom", http_path=path)


def test_a_good_http_path_is_kept_trimmed():
    t = hop(probe="http", category="custom", http_path="  /a/b-c_d.e~f%2Fg:h@i;j=k,l+m*n(o)p'q!r$s&t  ")
    assert t.http_path == "/a/b-c_d.e~f%2Fg:h@i;j=k,l+m*n(o)p'q!r$s&t"
    assert "at most 255" in refusal(probe="http", category="custom", http_path="/" + "a" * 255).replace("String should have ", "")


def test_probe_label_and_words():
    assert probe_label(hop(probe="tcp", port=443)) == "TCP 443"
    assert probe_label(hop(probe="http", category="custom")) == "HTTPS HEAD"
    assert probe_label(hop(probe="http", category="custom", http_path="/health")) == "HTTPS HEAD /health"
    assert probe_label(hop(probe="http", category="custom", http_scheme="http", http_verb="GET", port=8080,
                           http_path="/status")) == "HTTP GET :8080 /status"
    assert probe_label(hop(probe="http", category="custom", http_scheme="http", port=80)) == "HTTP HEAD"
    assert probe_label(hop(probe="http", category="custom", port=443)) == "HTTPS HEAD"
    long = probe_label(hop(probe="http", category="custom", http_path="/" + "a" * 60))
    assert long == "HTTPS HEAD /" + "a" * 39 + "…"
    assert probe_words(hop(probe="tcp", port=443)) == "TCP port 443"
    assert probe_words(hop(probe="http", category="custom", http_scheme="http", http_path="/secret")) == "HTTP request"
    assert probe_words(hop(probe="http", category="custom")) == "HTTPS request"


def test_probe_identity_changes_with_every_probe_field():
    base = hop(probe="http", category="custom")
    assert probe_identity(hop()) == ("icmp", None, "https", "/", "HEAD")
    for change in ({"port": 8443}, {"http_scheme": "http"}, {"http_path": "/x"}, {"http_verb": "GET"}):
        assert probe_identity(hop(probe="http", category="custom", **change)) != probe_identity(base)
    assert probe_identity(hop(name="Other name", probe="http", category="custom")) == probe_identity(base)


def test_target_stats_carry_the_method_and_default_to_none():
    s = TargetStats(target_id="h", name="Hop", category="public", address="192.0.2.10", enabled=True)
    assert (s.probe, s.probe_label, s.probe_note) == (None, None, None)
