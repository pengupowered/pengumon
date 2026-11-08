"""POST /mcp: the JSON-RPC and MCP rules (pengumon.mcp.protocol on plain dicts), the route (off by default, 405, the
telemetry) and the guards it shares with the API (Host, Origin, the token, the rate limit)."""

import ast
import dataclasses
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import obs_helpers
from mcp_helpers import TOKEN
from pengumon import __version__, security
from pengumon.config import AppConfig
from pengumon.main import app, security_config
from pengumon.mcp import protocol
from pengumon.mcp.tools import TOOLS
from pengumon.state import AppState

obs_helpers.install()
ROOT = Path(__file__).resolve().parent.parent


def rpc(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the protocol on dicts -----------------------------------------------------------------------------------------

async def test_initialize_answers_the_asked_version_when_supported_else_the_latest():
    state = AppState()
    for asked in protocol.SUPPORTED_VERSIONS:
        reply = await protocol.handle(rpc("initialize", {"protocolVersion": asked}), state)
        assert reply.status == 200 and reply.body["result"]["protocolVersion"] == asked
    for odd in ("1999-01-01", None, 5):
        reply = await protocol.handle(rpc("initialize", {"protocolVersion": odd}), state)
        assert reply.body["result"]["protocolVersion"] == protocol.LATEST_VERSION
    result = (await protocol.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}), state)).body["result"]
    assert result["capabilities"] == {"tools": {}} and result["serverInfo"] == {"name": "pengumon", "version": __version__}
    assert "status" in result["instructions"]


async def test_notifications_and_responses_are_202_with_no_body():
    for body in (rpc("notifications/initialized", id=None), rpc("anything/at/all", id=None),
                 json.dumps({"jsonrpc": "2.0", "id": 3, "result": {}}).encode()):
        reply = await protocol.handle(body, AppState())
        assert (reply.status, reply.body) == (202, None)


async def test_ping_and_tools_list():
    assert (await protocol.handle(rpc("ping", id="a"), AppState())).body == {"jsonrpc": "2.0", "id": "a", "result": {}}
    tools = (await protocol.handle(rpc("tools/list"), AppState())).body["result"]["tools"]
    assert [t["name"] for t in tools] == ["status", "incidents", "incident", "report", "devices", "services", "speed_tests",
                                       "ai_pack"]
    for tool in tools:
        assert tool["description"].strip() and tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"] == {"readOnlyHint": True, "openWorldHint": False}


async def test_each_error_code():
    state = AppState()
    cases = [
        (b"{not json", 400, -32700),
        (b"[]", 400, -32600),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', 400, -32600),   # a batch
        (b'"ping"', 400, -32600),
        (b'{"id":1,"method":"ping"}', 400, -32600),                      # no jsonrpc version
        (b'{"jsonrpc":"2.0","id":1}', 400, -32600),                      # no method
        (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', 400, -32600),   # a bool is no id
        (b'{"jsonrpc":"2.0","id":null,"method":"ping"}', 400, -32600),
        (rpc("no/such/method"), 200, -32601),
        (rpc("server/discover"), 200, -32601),                           # the SDK's probe: it falls back to initialize
        (rpc("tools/call", {"name": "nope"}), 200, -32602),
        (rpc("tools/call", {}), 200, -32602),
        (rpc("tools/call", {"name": "status", "arguments": {"x": 1}}), 200, -32602),
        (rpc("tools/call", {"name": "status", "arguments": []}), 200, -32602),
        (rpc("tools/call", {"name": "incidents", "arguments": {"period": "1y"}}), 200, -32602),
        (rpc("tools/call", {"name": "incidents", "arguments": {"limit": 0}}), 200, -32602),
        (rpc("tools/call", {"name": "incidents", "arguments": {"limit": 201}}), 200, -32602),
        (rpc("tools/call", {"name": "incidents", "arguments": {"limit": "5"}}), 200, -32602),
        (rpc("tools/call", {"name": "incidents", "arguments": {"limit": True}}), 200, -32602),
        (rpc("tools/call", {"name": "incident", "arguments": {}}), 200, -32602),
    ]
    for body, status, code in cases:
        reply = await protocol.handle(body, state)
        assert (reply.status, reply.body["error"]["code"]) == (status, code), body
        assert reply.body["jsonrpc"] == "2.0"
    echoed = await protocol.handle(rpc("no/such/method", id="req-9"), state)
    assert echoed.body["id"] == "req-9"


async def test_the_protocol_version_header():
    for ok in (None, *protocol.SUPPORTED_VERSIONS):
        assert (await protocol.handle(rpc("ping"), AppState(), ok)).status == 200
    assert (await protocol.handle(rpc("ping"), AppState(), "2030-01-01")).status == 400
    assert (await protocol.handle(rpc("notifications/initialized", id=None), AppState(), "2030-01-01")).status == 400


async def test_a_tool_that_breaks_tells_the_caller_nothing_of_the_inside(monkeypatch):
    async def boom(state, args):
        raise RuntimeError("secret detail /srv/x")

    broken = dataclasses.replace(TOOLS[0], handler=boom)
    monkeypatch.setattr(protocol, "TOOLS", [broken])
    reply = await protocol.handle(rpc("tools/call", {"name": "status"}), AppState())
    result = reply.body["result"]
    assert result["isError"] is True and "secret detail" not in json.dumps(result)


def test_the_protocol_and_the_tools_import_no_web_framework():
    for name in ("protocol.py", "tools.py"):
        tree = ast.parse((ROOT / "src" / "pengumon" / "mcp" / name).read_text(encoding="utf-8"))
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module] + [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.split(".")[0] in ("fastapi", "starlette")], name


# --- the route -----------------------------------------------------------------------------------------------------

def test_a_fresh_config_has_mcp_off():
    assert AppConfig().mcp.enabled is False


async def test_off_by_default_every_method_is_404(app_state):
    app_state.config = AppConfig()
    async with _client() as ac:
        # every method and both spellings: a 405 ("Allow: POST") or a redirect of /mcp/ would tell the endpoint exists
        for path in ("/mcp", "/mcp/", "/mcp?x=1"):
            for method in ("POST", "GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"):
                res = await ac.request(method, path, content=rpc("ping") if method in ("POST", "PUT", "PATCH") else None)
                assert (res.status_code, res.content, res.headers.get("allow")) == (404, b"", None), (method, path)
    app_state.config = None  # the state before the config exists: off as well
    async with _client() as ac:
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 404


async def test_the_setting_applies_live(app_state):
    app_state.config = AppConfig()
    async with _client() as ac:
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 404
        app_state.config.mcp.enabled = True
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 200
        app_state.config.mcp.enabled = False
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 404


async def test_get_and_delete_are_405_with_allow_post(mcp_state):
    async with _client() as ac:
        for path in ("/mcp", "/mcp/"):
            for method in ("GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"):
                res = await ac.request(method, path)
                assert res.status_code == 405 and res.headers["allow"] == "POST", (method, path)
        assert (await ac.post("/mcp/", content=rpc("ping"))).json()["result"] == {}  # the trailing slash is the same


async def test_post_answers_json_without_a_session_id(mcp_state):
    async with _client() as ac:
        res = await ac.post("/mcp", content=rpc("initialize", {"protocolVersion": "2025-06-18"}),
                            headers={"MCP-Protocol-Version": "2025-06-18"})
        assert res.status_code == 200 and res.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in res.headers and res.json()["result"]["protocolVersion"] == "2025-06-18"
        done = await ac.post("/mcp", content=rpc("notifications/initialized", id=None))
        assert done.status_code == 202 and done.content == b""
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"MCP-Protocol-Version": "2030-01-01"})).status_code == 400
        assert (await ac.post("/mcp", content=b"{oops")).status_code == 400


async def test_calls_are_counted_by_tool_and_outcome(mcp_state):
    def n(tool, outcome):
        return obs_helpers.total("pengumon.mcp.calls", tool=tool, outcome=outcome)

    before = (n("status", "ok"), n("incident", "error"), n("status", "refused"), n("unknown", "refused"))
    async with _client() as ac:
        await ac.post("/mcp", content=rpc("tools/call", {"name": "status"}))
        await ac.post("/mcp", content=rpc("tools/call", {"name": "incident", "arguments": {"id": 99999}}))
        await ac.post("/mcp", content=rpc("tools/call", {"name": "status", "arguments": {"x": 1}}))
        await ac.post("/mcp", content=rpc("tools/call", {"name": "evil-" + "x" * 500}))
    assert (n("status", "ok"), n("incident", "error"), n("status", "refused"), n("unknown", "refused")) == tuple(
        b + 1 for b in before)


# --- the guards it shares with the API -----------------------------------------------------------------------------

@pytest.fixture
def _guards(monkeypatch):
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)
    security.reset_rate_limits()


async def test_a_foreign_host_is_refused(mcp_state, _guards):
    async with _client() as ac:
        res = await ac.post("/mcp", content=rpc("ping"), headers={"Host": "evil.example"})
        assert res.status_code == 421
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"Host": "192.168.88.5:8000"})).status_code == 200


async def test_a_foreign_origin_is_refused_and_no_origin_passes(mcp_state, _guards):
    async with _client() as ac:
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"Origin": "http://evil.example"})).status_code == 403
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 200  # a non-browser client sends none
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"Origin": "http://test"})).status_code == 200


async def test_with_a_token_set_the_bearer_token_is_needed(mcp_state, _guards, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client() as ac:
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 401
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await ac.post("/mcp", content=rpc("ping"), headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 200


async def test_viewers_without_the_password_cannot_use_mcp(mcp_state, _guards, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "view_without_token", True)
    async with _client() as ac:
        assert (await ac.get("/api/status")).status_code != 401  # a viewer may look at the dashboard...
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 401  # ...but not use MCP
        assert (await ac.get("/mcp")).status_code == 401


async def test_without_a_token_the_endpoint_is_open_like_the_api(mcp_state, _guards):
    async with _client() as ac:
        assert (await ac.get("/api/health")).status_code != 401
        assert (await ac.post("/mcp", content=rpc("ping"))).status_code == 200


async def test_the_endpoint_is_rate_limited_to_120_a_minute(mcp_state, _guards):
    async with _client() as ac:
        codes = [(await ac.post("/mcp", content=rpc("ping"))).status_code for _ in range(122)]
    assert codes[:120] == [200] * 120 and codes[120:] == [429, 429]


async def test_the_body_is_size_limited_like_the_api(mcp_state, _guards):
    async with _client() as ac:
        res = await ac.post("/mcp", content=b'{"jsonrpc":"2.0","id":1,"method":"ping","params":"' + b"x" * 3_000_000 + b'"}')
    assert res.status_code == 413


async def test_an_error_echoes_at_most_64_characters_of_the_callers_text():
    """A tool name, a method or an argument name of the caller's is cut to 64 characters in the error message: the
    endpoint does not mirror arbitrary text back (the id, which JSON-RPC requires to be echoed, is the body cap's)."""
    long = "x" * 5000
    for body in (rpc(long), rpc("tools/call", {"name": long}),
                 rpc("tools/call", {"name": "status", "arguments": {long: 1}})):
        message = (await protocol.handle(body, AppState())).body["error"]["message"]
        assert "x" * 64 in message and "x" * 65 not in message, message[:80]
