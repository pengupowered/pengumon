"""The cause category of an incident (incidents.taxonomy.incident_cause), as the API sends it."""

import typing

import pytest

from pengumon.services import checker  # noqa: F401  (registers the service_down kind)
from pengumon.incidents.taxonomy import (
    CAUSE_CATEGORIES, CAUSE_CODES, KINDS, CauseCode, IncidentKind, incident_cause, register_kind, Kind)
from pengumon.incidents.model import Incident


def test_every_kind_has_a_cause_and_every_cause_code_a_category():
    assert set(typing.get_args(IncidentKind)) <= set(KINDS) and "service_down" in KINDS
    assert set(typing.get_args(CauseCode)) == set(CAUSE_CODES)
    assert set(CAUSE_CODES.values()) <= set(CAUSE_CATEGORIES)
    for name, kind in KINDS.items():
        assert kind.name == name
        # legacy rows, and isp_outage (line or the provider's network): the cause code decides
        assert kind.cause in CAUSE_CATEGORIES or name.startswith("legacy_") or name == "isp_outage", name
        for code in CAUSE_CODES:
            assert incident_cause(name, code) in CAUSE_CATEGORIES
    assert incident_cause(None, "unknown_outage") == "unknown"


@pytest.mark.parametrize("kind,code,expected", [
    ("local_loss", "local_wifi_mesh_drop", "router"),
    ("gateway_loss", "double_nat_congestion", "gateway"),   # an older build's gateway incident
    ("gateway_loss", "gateway_unreachable", "gateway"),
    ("gateway_down", "local_switch_cable_fault", "gateway"),   # the kind decides, not the code
    ("isp_outage", "access_line_drop", "line"),            # the provider's first hop timed out: line or that router
    ("isp_outage", "internet_backbone_transit_loss", "isp"),  # it answered, the internet did not
    ("isp_loss", "xfinity_last_mile_coax_drop", "line"),   # the legacy cause name reads as the current one
    ("backbone_loss", "internet_backbone_transit_loss", "isp"),
    ("backbone_loss", "access_line_drop", "isp"),          # an older build's code: the kind decides
    ("upstream_latency", "access_line_drop", "isp"),
    ("internet_outage", "target_specific_outage", "internet"),
    ("dns_slow", "dns_resolver_failure", "dns"),
    ("busy_latency", "load_induced_latency", "load"),
    ("service_down", "service_unavailable", "service"),
    (None, "access_line_drop", "line"),                      # no kind (an unclassified row): the cause code decides
    ("legacy_other", "double_nat_congestion", "gateway"),
    ("legacy_latency", "local_wifi_mesh_drop", "router"),
    ("something_newer", "dns_resolver_failure", "dns"),      # an unknown kind falls back to the code
])
def test_category_by_kind_then_cause_code(kind, code, expected):
    assert incident_cause(kind, code) == expected


def test_an_older_backbone_outage_recorded_as_access_line_drop_is_read_as_the_providers_network():
    """Older builds recorded an isp_outage whose ISP hop still answered as access_line_drop; its description says so."""
    old = "The local modem and Hub answer, but public internet traffic is timing out (60.0% loss). Possible causes: ..."
    assert incident_cause("isp_outage", "access_line_drop", old) == "isp"
    assert incident_cause("isp_outage", "access_line_drop", "Packets to Hub (198.51.100.1) are timing out") == "line"
    assert Incident(start_time=1, cause_code="access_line_drop", title="t", description=old, severity="critical",
                    kind="isp_outage").cause == "isp"


async def test_the_incident_api_sends_the_category(app_state):
    from httpx import ASGITransport, AsyncClient
    from pengumon.main import app
    from pengumon.db.repository import DatabaseRepository
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        repo = DatabaseRepository(db_path=os.path.join(tmp, "c.db"))
        await repo.initialize()
        app_state.repository = repo
        for kind, code in (("gateway_loss", "double_nat_congestion"), ("isp_loss", "access_line_drop"), ("dns_outage", "dns_resolver_failure"), (None, "unknown_outage")):
            await repo.create_incident(Incident(start_time=1.0, cause_code=code, title="t", description="d",
                                                severity="warning", affected_targets=[], kind=kind))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            body = (await ac.get("/api/incidents")).json()
        await repo.close()
    assert sorted(i["cause"] for i in body) == ["dns", "gateway", "line", "unknown"]
    assert Incident(start_time=1, cause_code="nominal", title="t", description="d", severity="info").model_dump()["cause"] == "unknown"


def test_each_classifier_kind_sits_where_the_path_health_names_it():
    """The lanes use the hero's words: local_* is the Router, gateway_* the Gateway, isp_loss the Line (loss from the
    provider's first hop on), the hops beyond the ISP; isp_outage is either, by its cause code."""
    for name, kind in KINDS.items():
        if name.startswith("local_"):
            assert kind.cause == "router", name
        elif name.startswith("gateway_"):
            assert kind.cause == "gateway", name
        elif name == "isp_loss":
            assert kind.cause == "line", name
        elif name in ("backbone_loss", "upstream_latency", "hop_deprioritised"):
            assert kind.cause == "isp", name
    assert KINDS["isp_outage"].cause is None


def test_a_kind_registered_twice_differently_is_refused():
    register_kind(KINDS["service_down"])  # the same row again is harmless
    with pytest.raises(ValueError):
        register_kind(Kind("service_down", "router"))
