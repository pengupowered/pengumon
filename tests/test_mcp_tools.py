"""The MCP tools on the seeded state of mcp_helpers (an AppState, no web framework): what each returns, and that no
tool output holds a secret."""

import json
import time

import pytest

from mcp_helpers import DEVICE_HOSTS, MARKERS
from pengumon.mcp import protocol
from pengumon.mcp.tools import TOOLS, ToolFailure

BY_NAME = {t.name: t for t in TOOLS}


async def call(state, name, **arguments):
    return await BY_NAME[name].handler(state, arguments)


async def test_status_reports_the_diagnosis_and_each_stage(mcp_state):
    out = await call(mcp_state, "status")
    assert (out["status"], out["title"], out["load_pct"]) == ("degraded", "Example title", 12.5)
    assert out["active_incident"] is None and out["as_of"].endswith(("-05:00", "-04:00"))  # America/New_York
    assert list(out["stages"]) == ["local", "gateway", "isp", "public"]
    assert out["stages"]["isp"] == {"hops": 1, "status": "degraded", "latency_ms": 9.0, "loss_pct": 1.0, "jitter_ms": 0.5}
    assert out["stages"]["local"]["status"] == "up"
    # no hop's name or address: stages only
    assert "192.168" not in json.dumps(out) and "example-isp" not in json.dumps(out)


async def test_status_before_the_classifier_exists_is_a_tool_failure(app_state):
    with pytest.raises(ToolFailure):
        await call(app_state, "status")


async def test_incidents_lists_newest_first_in_the_configured_zone(mcp_state):
    out = await call(mcp_state, "incidents")
    assert (out["period"], out["total_in_period"], out["returned"]) == ("7d", 1, 1)
    (row,) = out["incidents"]
    assert row["id"] == mcp_state.example_incident_id and row["severity"] == "warning" and row["kind"] == "gateway_loss"
    assert row["start"][-6:] in ("-05:00", "-04:00") and set(row) >= {"end", "duration_seconds", "cause", "title", "load_pct"}
    assert (await call(mcp_state, "incidents", period="24h"))["total_in_period"] == 1
    assert (await call(mcp_state, "incidents", limit=1))["incidents"] == [row]


async def test_incidents_outside_the_period_are_left_out(mcp_state):
    from pengumon.incidents.model import Incident
    import time
    await mcp_state.repository.create_incident(Incident(
        start_time=time.time() - 20 * 86400, end_time=time.time() - 20 * 86400 + 60, cause_code="unknown_outage",
        title="Old", description="d", severity="warning", resolved=True))
    assert (await call(mcp_state, "incidents", period="7d"))["total_in_period"] == 1
    out = await call(mcp_state, "incidents", period="30d")
    assert out["total_in_period"] == 2 and [i["title"] for i in out["incidents"]][-1] == "Old"


async def test_incident_gives_the_evidence_and_not_the_raw_samples(mcp_state):
    out = await call(mcp_state, "incident", id=mcp_state.example_incident_id)
    assert out["incident"]["id"] == mcp_state.example_incident_id and out["incident"]["description"]
    evidence = out["evidence"]
    assert {h["target_id"] for h in evidence["hop_impact"]} >= {"gw"}
    assert evidence["probe_samples_omitted"] > 0 and "samples" not in evidence and "router_snapshots" not in evidence
    assert evidence["window_start"][-6:] in ("-05:00", "-04:00")
    gw = next(h for h in evidence["hop_impact"] if h["target_id"] == "gw")
    assert gw["loss_pct"] > 50


async def test_incident_path_trace_hides_lan_hops_and_keeps_public_ones(mcp_state):
    from pengumon.models.probes import PathTraceHop
    await mcp_state.repository.update_incident_path_trace(mcp_state.example_incident_id, [
        PathTraceHop(hop=1, ip="192.168.88.1", hostname="router.lan", latency_ms=1.0),
        PathTraceHop(hop=2, ip="fe80::1", hostname=None, latency_ms=2.0),
        PathTraceHop(hop=3, ip="*", latency_ms=None, loss_pct=100.0),
        PathTraceHop(hop=4, ip="198.51.100.9", hostname="core1.example-isp.net", latency_ms=9.0),
    ])
    trace = (await call(mcp_state, "incident", id=mcp_state.example_incident_id))["incident"]["path_trace"]
    assert [(h["hop"], h["ip"], h["hostname"]) for h in trace] == [
        (1, "private", None), (2, "private", None), (3, "private", None), (4, "198.51.100.9", "core1.example-isp.net")]
    assert "router.lan" not in json.dumps(trace) and "192.168.88.1" not in json.dumps(trace)


async def test_an_unknown_incident_is_a_tool_failure_with_a_sentence(mcp_state):
    for name, args in (("incident", {"id": 424242}), ("ai_pack", {"scope": "incident", "incident_id": 424242})):
        with pytest.raises(ToolFailure, match="no incident with id 424242"):
            await call(mcp_state, name, **args)
    reply = await protocol.handle(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "incident", "arguments": {"id": 424242}}}).encode(), mcp_state)
    result = reply.body["result"]
    assert result["isError"] is True and "424242" in result["content"][0]["text"] and "structuredContent" not in result


async def test_report_has_uptime_per_hop_and_the_reliability_summary(mcp_state):
    out = await call(mcp_state, "report", period="7d")
    assert out["period"] == "7d" and out["zone"] == "America/New_York"
    names = [h["name"] for h in out["uptime_by_hop"]]
    assert names == [t["name"] for t in __import__("mcp_helpers").TARGETS]
    gateway = next(h for h in out["uptime_by_hop"] if h["name"] == "Example gateway")
    assert 90 < gateway["availability_pct"] < 100 and out["incidents"]["count"] == 1
    assert out["window_start"][-6:] in ("-05:00", "-04:00")
    assert (await call(mcp_state, "report", period="24h"))["period"] == "24h"


async def test_devices_are_summaries_with_counts_only_and_no_addresses(mcp_state):
    out = await call(mcp_state, "devices")
    assert [d["id"] for d in out["devices"]] == ["modem", "router"]
    modem, router = out["devices"]
    assert modem["kind"] and modem["model"] == "Example-Cable 3.1" and router["model"] == "Example-Mesh X1"
    assert modem["status"] == "error" and modem["error_kind"]  # polled against a refused connection
    # fixed words, not the driver's
    assert modem["latest"]["error_message"].startswith("The device could not be reached.")
    text = json.dumps(out)
    for host in DEVICE_HOSTS:
        assert host not in text


async def test_device_client_counts_hold_no_identity(mcp_state):
    from types import SimpleNamespace

    from pengumon.capabilities import Client, ClientsReading
    router = mcp_state.devices.get("router")
    reading = ClientsReading((Client(key="02:00:00:aa:bb:01", label_parts=("Canary-Client-Phone",),
                                     mac="02:00:00:aa:bb:01", connection="wired"),))
    router.monitor = SimpleNamespace(latest_clients=reading, clients_at=1.0, latest_mesh=None,
                                     latest_snapshot=getattr(router.monitor, "latest_snapshot", None))
    out = await call(mcp_state, "devices")
    clients = out["devices"][1]["clients"]
    assert (clients["online"], clients["wired"]) == (1, 1)
    assert "Canary-Client-Phone" not in json.dumps(out) and "02:00:00" not in json.dumps(out)


async def test_services_come_from_the_checker(mcp_state):
    out = await call(mcp_state, "services")
    assert [s["name"] for s in out["services"]] == ["Example App"] and out["services"][0]["status"] == "unknown"
    mcp_state.service_checker = None
    assert (await call(mcp_state, "services"))["services"] == []


async def test_ai_pack_returns_the_markdown_and_its_modules(mcp_state):
    out = await call(mcp_state, "ai_pack", hours=24, level="anonymized")
    assert out["level"] == "anonymized" and out["markdown"].startswith("# ") and out["modules"] is not None
    assert "roles" not in out
    incident = await call(mcp_state, "ai_pack", scope="incident", incident_id=mcp_state.example_incident_id)
    assert incident["scope"] == "incident" and incident["hours"] is None


async def test_ai_pack_carries_the_speed_tests_without_the_librespeed_host(mcp_state):
    from mcp_helpers import LIBRESPEED_HOST
    out = await call(mcp_state, "ai_pack", level="full")
    md = out["markdown"]
    assert "## Speed tests" in md and "3 tests (2 ok, 1 failed)" in md and "own LibreSpeed server" in md
    assert "median 188 of 300 Mbps" in md and LIBRESPEED_HOST not in md


async def test_ai_pack_scope_and_incident_id_must_agree(mcp_state):
    from pengumon.mcp.tools import BadArguments
    for args in ({"scope": "incident"}, {"incident_id": 1}):
        with pytest.raises(BadArguments):
            await call(mcp_state, "ai_pack", **args)


async def test_ai_pack_refuses_while_another_build_runs(mcp_state):
    from pengumon.reports.common import pack_building
    async with pack_building:
        with pytest.raises(ToolFailure, match="Another pack"):
            await call(mcp_state, "ai_pack")


async def test_without_a_repository_the_history_tools_say_so(app_state):
    for name in ("incidents", "report", "speed_tests", "ai_pack"):
        with pytest.raises(ToolFailure, match="still starting"):
            await call(app_state, name)
    with pytest.raises(ToolFailure):
        await call(app_state, "devices")


# --- speed tests ---------------------------------------------------------------------------------------------------

async def test_speed_tests_answer_is_the_line_against_the_plan(mcp_state):
    out = await call(mcp_state, "speed_tests")
    assert (out["enabled"], out["provider"], out["period"]) == (True, "your LibreSpeed server", "7d")
    assert (out["plan_download_mbps"], out["plan_upload_mbps"], out["monthly_cap_mb"]) == (300, 20, 500)
    assert out["data_used_this_month_mb"] == 220.0 and out["tests_in_period"] == 3
    # the schedule's next run comes from the runner, which this state does not have
    assert out["next_scheduled_run"] is None and out["running_now"] is False
    s = out["summary"]
    assert (s["ok"], s["failed"], s["download_median_mbps"], s["download_lowest_mbps"]) == (2, 1, 187.5, 95.0)
    assert s["tests_under_half_of_plan"] == 1 and s["bufferbloat_grade"] == "B"
    newest, middle, failed = out["tests"]                                # newest first
    assert newest["time"][-6:] in ("-05:00", "-04:00") and newest["download_mbps"] == 280.0
    assert (newest["confidence"], newest["note"]) == ("ok", None)                # 100 MB at 280 Mbps lasted 2.9 s
    assert (newest["bufferbloat_grade"], newest["downloaded_mb"], newest["uploaded_mb"]) == ("B", 100.0, 10.0)
    assert (middle["status"], failed["status"], failed["download_mbps"]) == ("ok", "failed", None)
    assert failed["reason"] == "your LibreSpeed server answered HTTP 404" and failed["server"] is None
    assert [t["time"] for t in out["tests"]] == sorted((t["time"] for t in out["tests"]), reverse=True)


async def test_speed_tests_give_the_lines_rate_the_router_measured_and_say_so(mcp_state):
    out = await call(mcp_state, "speed_tests")
    assert {t["measured_by"] for t in out["tests"] if t["status"] == "ok"} == {"test"}   # the seed has no router peaks
    assert out["tests"][0]["test_download_mbps"] == 280.0 and out["tests"][0]["traffic_note"] is None
    await mcp_state.repository.insert_speed_test({
        "ts": time.time() - 5, "duration_s": 20.0, "trigger": "manual", "provider": "cloudflare", "server": "IAD",
        "status": "ok", "down_mbps": 300.0, "up_mbps": 18.0, "bytes_down": 300_000_000, "bytes_up": 18_000_000,
        "wan_down_mbps": 910.0, "wan_up_mbps": 19.5})
    newest = (await call(mcp_state, "speed_tests"))["tests"][0]
    assert (newest["download_mbps"], newest["upload_mbps"], newest["measured_by"]) == (910.0, 19.5, "router")
    assert (newest["test_download_mbps"], newest["test_upload_mbps"]) == (300.0, 18.0)
    assert "other traffic on your network" in newest["traffic_note"]


async def test_speed_tests_period_and_limit(mcp_state):
    assert (await call(mcp_state, "speed_tests", limit=2))["returned"] == 2
    assert len((await call(mcp_state, "speed_tests", limit=2))["tests"]) == 2
    out = await call(mcp_state, "speed_tests", period="24h")
    assert out["tests_in_period"] == 3 and out["summary"]["ok"] == 2


async def test_speed_tests_when_the_test_is_off_say_so(mcp_state):
    mcp_state.config.speedtest.enabled = False
    import sqlite3
    con = sqlite3.connect(mcp_state.repository.db_path)
    con.execute("DELETE FROM speed_tests")
    con.commit()
    con.close()
    out = await call(mcp_state, "speed_tests")
    assert out["enabled"] is False and out["tests"] == [] and out["summary"] is None
    assert "turn it on" in out["note"]


# --- no secret in any output --------------------------------------------------------------------------------------

def _all_calls(state):
    yield "status", {}
    yield "incidents", {"period": "30d", "limit": 200}
    yield "incident", {"id": state.example_incident_id}
    yield "report", {"period": "30d"}
    yield "devices", {}
    yield "services", {}
    yield "speed_tests", {"period": "30d", "limit": 200}
    for level in ("full", "vague", "anonymized"):
        yield "ai_pack", {"level": level, "detail": "detailed", "hours": 168}
        yield "ai_pack", {"level": level, "scope": "incident", "incident_id": state.example_incident_id}


async def test_no_tool_output_holds_a_secret(mcp_state):
    """Every secret slot, every kind of device credential (a login, an SNMP community), the sign-in token, the devices'
    hosts and a named client (with privacy.client_details "full") carry a marker; none may appear in what a tool
    returns, as text, as structured content, or as a failure sentence. The extra devices' polls failed, so their error
    messages (which name the host, the login tried, the address dialed) are what the devices tool has to show."""
    from mcp_helpers import (CLIENT_MARKERS, DEVICE_MARKERS, LIBRESPEED_HOST, MORE_DEVICE_HOSTS, SECRET_MARKERS, TOKEN,
                             add_every_device_kind)
    from pengumon.main import security_config

    await add_every_device_kind(mcp_state)
    markers = {**SECRET_MARKERS, **DEVICE_MARKERS, **{h: h for h in (*DEVICE_HOSTS, *MORE_DEVICE_HOSTS)},
               **{c: c for c in CLIENT_MARKERS}, "librespeed.host": LIBRESPEED_HOST}
    security_config.access_token = TOKEN
    seen = 0
    try:
        for name, args in _all_calls(mcp_state):
            reply = await protocol.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                      "params": {"name": name, "arguments": args}}).encode(), mcp_state)
            text = json.dumps(reply.body)
            assert reply.body["result"]["isError"] is False, (name, args, text[:300])
            leaked = [k for k, v in markers.items() if v.lower() in text.lower()]
            assert not leaked, (name, args, leaked)
            seen += 1
    finally:
        security_config.access_token = ""
    assert seen == 13 and len(MARKERS) == len(set(MARKERS)) == 9 and len(markers) == 23
    devices = {d["id"]: d for d in (await BY_NAME["devices"].handler(mcp_state, {}))["devices"]}
    assert {"owrt", "sw", "asus2", "igd"} <= set(devices)  # the extra devices are listed, each after a failed poll
    assert (devices["owrt"]["error_kind"], devices["asus2"]["error_kind"], devices["igd"]["status"]) == (
        "auth", "auth", "error")
    assert devices["router"]["clients"]["online"] == 1  # the named client is there, as a count


async def test_ai_pack_is_limited_to_six_builds_a_minute_like_the_api(mcp_state):
    """GET /api/ai-pack allows 6 a minute; MCP allows 120 calls a minute, so without its own limit a client could build
    packs back to back and keep the dashboard's export refused ("another pack is being built")."""
    import time

    from pengumon.mcp import tools
    now = time.monotonic()
    tools._pack_starts.extend([now - 61] * 6)  # six builds over a minute ago: they no longer count
    assert (await call(mcp_state, "ai_pack", level="anonymized"))["markdown"]
    tools._pack_starts.extend([now] * 5)  # with the one just built: six within the minute
    with pytest.raises(ToolFailure, match="At most 6 packs a minute"):
        await call(mcp_state, "ai_pack", level="anonymized")
