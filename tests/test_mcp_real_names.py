"""mcp.real_names ("Answers use real names and addresses", on by default): on, the tools show what the dashboard shows
the same caller; off, the hops and devices are role words and home addresses are hidden; secrets are never shown, and
connected clients follow privacy.client_details either way."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_helpers import CLIENT_MARKERS, SECRET_MARKERS, TOKEN
from pengumon import security
from pengumon.config import AppConfig
from pengumon.main import app, security_config
from pengumon.mcp import protocol
from pengumon.settings import registry
from pengumon.drivers.common import normal_mac
from test_mcp_tools import PLANT_DOTLESS, PLANT_HOST, PLANT_LAN, PLANT_MAC, _plant

NAME, CLIENT_MAC, CLIENT_IP = CLIENT_MARKERS
URL_SECRET = "CANARY-url-secret-0042"
URL_WITH_SECRETS = f"https://user:{URL_SECRET}@speed.example.com/run?token={URL_SECRET}&x=1"


def rpc(name, arguments=None):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments or {}}}).encode()


async def answer(state, name, arguments=None, client_details="masked") -> str:
    """The tool's answer as the client reads it (the text content), through the protocol."""
    reply = await protocol.handle(rpc(name, arguments), state, client_details=client_details)
    assert reply.body["result"]["isError"] is False, reply.body
    return json.dumps(reply.body["result"]["structuredContent"])


async def planted(state, real: bool):
    incident_id = await _plant(state)
    state.config.mcp.real_names = real
    from pengumon.capabilities import Client, ClientsReading
    from pengumon.drivers.common import normal_mac
    mac = normal_mac(CLIENT_MAC)  # the router's list holds MACs in normal form, as its traffic rows do
    state.devices.get("router").monitor.latest_clients = ClientsReading(
        (Client(key=mac, label_parts=(NAME,), ip=CLIENT_IP, mac=mac, connection="wifi", band="5"),))
    # a failed speed test whose message holds a URL with a password and a token in its query
    await state.repository.insert_speed_test({
        "ts": __import__("time").time() - 5, "duration_s": 1.0, "trigger": "manual", "provider": "cloudflare",
        "server": None, "status": "failed", "down_mbps": None, "up_mbps": None, "bytes_down": 0, "bytes_up": 0,
        "reason": f"could not reach {URL_WITH_SECRETS}"})
    await state.repository.insert_speed_test({
        "ts": __import__("time").time() - 6, "duration_s": 1.0, "trigger": "manual", "provider": "cloudflare",
        "server": None, "status": "failed", "down_mbps": None, "up_mbps": None, "bytes_down": 0, "bytes_up": 0,
        "reason": f"Cannot connect to host {PLANT_HOST}:80 [Connect call failed ('{PLANT_LAN}', 80)]"})
    return incident_id


def test_the_setting_is_on_by_default_with_the_approved_words():
    assert AppConfig().mcp.real_names is True
    entry = registry.BY_KEY["mcp.real_names"]
    assert entry.label == "Answers use real names and addresses"
    assert entry.effect == (
        "On: the AI assistant sees your devices and hops by the names, host names and home addresses the dashboard "
        "shows you, so its answers can name them. Off: they are replaced by role words (\"the router\", \"a game "
        "console (Sony)\") and home addresses are hidden. Connected devices' names follow \"Show connected clients' "
        "names and addresses\" either way. Passwords and secrets are never shown. What the assistant sees goes to the "
        "company that runs it.")


async def test_status_names_the_hops_and_addresses_when_on_and_by_role_when_off(mcp_state):
    await planted(mcp_state, True)
    on = await answer(mcp_state, "status")
    assert PLANT_HOST in on and "192.168.88.1" in on and "Example gateway" in on
    mcp_state.config.mcp.real_names = False
    off = await answer(mcp_state, "status")
    assert not any(v in off for v in (PLANT_HOST, "192.168.88.1", "Example gateway", PLANT_LAN, PLANT_DOTLESS))
    assert "Router Disconnect" in off


async def test_incidents_titles_follow_the_setting(mcp_state):
    await planted(mcp_state, True)
    assert PLANT_HOST in await answer(mcp_state, "incidents", {"period": "30d"})
    mcp_state.config.mcp.real_names = False
    off = await answer(mcp_state, "incidents", {"period": "30d"})
    assert PLANT_HOST not in off and "Router Disconnect" in off


async def test_incident_keeps_the_trace_and_description_when_on_and_hides_them_when_off(mcp_state):
    incident_id = await planted(mcp_state, True)
    on = await answer(mcp_state, "incident", {"id": incident_id})
    assert PLANT_HOST in on and "192.168.88.1" in on and PLANT_LAN in on and PLANT_DOTLESS in on
    trace = json.loads(on)["incident"]["path_trace"]
    assert [(h["ip"], h["hostname"]) for h in trace] == [("192.168.88.1", PLANT_HOST),
                                                         ("198.51.100.1", "po-1.edge.example-isp.net")]
    mcp_state.config.mcp.real_names = False
    off = await answer(mcp_state, "incident", {"id": incident_id})
    assert not any(v in off for v in (PLANT_HOST, "192.168.88.1", PLANT_LAN, PLANT_DOTLESS))
    assert [(h["ip"], h["hostname"]) for h in json.loads(off)["incident"]["path_trace"]][0] == ("private", None)


async def test_report_names_the_hops_when_on(mcp_state):
    await planted(mcp_state, True)
    on = await answer(mcp_state, "report", {"period": "24h"})
    assert PLANT_HOST in on
    mcp_state.config.mcp.real_names = False
    assert PLANT_HOST not in await answer(mcp_state, "report", {"period": "24h"})


async def test_speed_tests_keep_a_server_address_when_on_and_hide_it_when_off(mcp_state):
    await planted(mcp_state, True)
    on = await answer(mcp_state, "speed_tests", {"period": "30d", "limit": 200})
    assert PLANT_HOST in on and PLANT_LAN in on
    mcp_state.config.mcp.real_names = False
    off = await answer(mcp_state, "speed_tests", {"period": "30d", "limit": 200})
    assert PLANT_HOST not in off and PLANT_LAN not in off


async def test_devices_never_carry_a_device_address_either_way(mcp_state):
    from mcp_helpers import DEVICE_HOSTS, MORE_DEVICE_HOSTS
    await planted(mcp_state, True)
    for real in (True, False):
        mcp_state.config.mcp.real_names = real
        text = (await answer(mcp_state, "devices")).lower()
        assert not any(h.lower() in text for h in (*DEVICE_HOSTS, *MORE_DEVICE_HOSTS)), real
        assert "example-mesh x1" in text  # the device by what it is, both ways


@pytest.mark.parametrize("real", [True, False])
async def test_secrets_in_a_url_are_scrubbed_in_both_modes(mcp_state, real):
    await planted(mcp_state, real)
    markers = [URL_SECRET, *SECRET_MARKERS.values()]
    for name, args in (("speed_tests", {"period": "30d", "limit": 200}), ("status", {}), ("incidents", {}),
                       ("devices", {}), ("traffic", {"hours": 720})):
        for details in ("full", "masked"):
            text = await answer(mcp_state, name, args, client_details=details)
            assert not [m for m in markers if m in text], (name, real, details)
    assert "speed.example.com" in await answer(mcp_state, "speed_tests", {"period": "30d", "limit": 200})  # the rest stays


async def test_a_mac_shows_only_where_the_caller_may_see_client_details(mcp_state):
    incident_id = await planted(mcp_state, True)
    full = await answer(mcp_state, "incident", {"id": incident_id}, client_details="full")
    masked = await answer(mcp_state, "incident", {"id": incident_id}, client_details="masked")
    assert PLANT_MAC in full and PLANT_MAC not in masked


async def test_traffic_names_clients_as_the_dashboard_would_for_this_caller(mcp_state):
    await planted(mcp_state, True)
    full = json.loads(await answer(mcp_state, "traffic", {"hours": 720}, client_details="full"))["clients"]
    assert [(c["device"], c["address"]) for c in full] == [(NAME, normal_mac(CLIENT_MAC))]
    masked = await answer(mcp_state, "traffic", {"hours": 720}, client_details="masked")
    assert NAME not in masked and normal_mac(CLIENT_MAC) not in masked and '"address"' not in masked and "Device FE01" in masked
    mcp_state.config.mcp.real_names = False   # off: by kind and maker, whatever the caller may see
    for details in ("full", "masked"):
        off = await answer(mcp_state, "traffic", {"hours": 720}, client_details=details)
        assert NAME not in off and normal_mac(CLIENT_MAC) not in off and "Device FE01" not in off and '"address"' not in off


async def test_a_given_name_shows_in_full_mode_only(mcp_state):
    await planted(mcp_state, True)
    await mcp_state.client_names.set(mcp_state.repository, normal_mac(CLIENT_MAC), "Canary-Given-Name")
    assert "Canary-Given-Name" in await answer(mcp_state, "traffic", {"hours": 720}, client_details="full")
    assert "Canary-Given-Name" not in await answer(mcp_state, "traffic", {"hours": 720}, client_details="masked")


async def test_incident_traffic_hours_name_clients_by_the_callers_details(mcp_state):
    await planted(mcp_state, True)
    incident_id = mcp_state.example_incident_id  # a couple of hours old: its traffic hours are stored
    assert NAME in await answer(mcp_state, "incident", {"id": incident_id}, client_details="full")
    assert NAME not in await answer(mcp_state, "incident", {"id": incident_id}, client_details="masked")
    mcp_state.config.mcp.real_names = False
    assert NAME not in await answer(mcp_state, "incident", {"id": incident_id}, client_details="full")


# --- the route works out what the caller may see ----------------------------------------------------------------

async def test_the_route_passes_what_the_callers_sign_in_allows(mcp_state, monkeypatch):
    """privacy.client_details "token": a caller with the password sees the clients' names, one while no password is
    set does not (api.clients.privacy_mode, as for the dashboard)."""
    await planted(mcp_state, True)
    mcp_state.config.privacy.client_details = "token"
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)
    security.reset_rate_limits()
    body = rpc("traffic", {"hours": 720})

    async def ask(headers):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/mcp", content=body, headers=headers)
        assert res.status_code == 200, res.text
        return res.text

    assert NAME not in await ask({})  # no password set: masked
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    assert NAME in await ask({"Authorization": f"Bearer {TOKEN}"})  # with the password: as the dashboard shows
