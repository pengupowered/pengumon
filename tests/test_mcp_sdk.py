"""POST /mcp against the official MCP SDK client (the mcp package, a test dependency only): the real app on a real
port, the handshake (the SDK's default tries `server/discover` first, then falls back to `initialize`), tools/list and
every tool, once with a bearer token and once open. The proof that the server written here speaks the protocol."""

import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from mcp_helpers import MARKERS, TOKEN
from pengumon.main import app, security_config

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
async def server(mcp_state):
    """The app (with the seeded state; no lifespan, which would replace it) served by uvicorn on a free port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_config=None)
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    for _ in range(200):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    assert uv.started
    yield f"http://127.0.0.1:{port}/mcp"
    uv.should_exit = True
    await task


def _client(url, token=None, mode="auto"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(headers=headers, timeout=60)
    return Client(streamable_http_client(url, http_client=http), mode=mode)


async def _exercise(client, state):
    assert client.server_info.name == "pengumon"
    tools = (await client.list_tools()).tools
    assert sorted(t.name for t in tools) == ["ai_pack", "devices", "incident", "incidents", "report", "services", "speed_tests",
                                          "status"]
    assert all(t.annotations.read_only_hint is True and t.annotations.open_world_hint is False for t in tools)
    calls = [("status", {}), ("incidents", {"period": "30d"}), ("incident", {"id": state.example_incident_id}),
             ("report", {"period": "24h"}), ("devices", {}), ("services", {}), ("speed_tests", {"period": "30d"}),
             ("ai_pack", {"level": "anonymized"})]
    for name, arguments in calls:
        result = await client.call_tool(name, arguments)
        assert not result.is_error, (name, result.content)
        assert result.structured_content, name
        assert json.loads(result.content[0].text) == result.structured_content
        text = json.dumps(result.structured_content)
        assert not [m for m in MARKERS if m in text], name
    status = (await client.call_tool("status", {})).structured_content
    assert status["status"] == "degraded"
    failed = await client.call_tool("incident", {"id": 424242})
    assert failed.is_error and "424242" in failed.content[0].text
    with pytest.raises(MCPError):
        await client.call_tool("incidents", {"period": "1y"})
    with pytest.raises(MCPError):
        await client.call_tool("no_such_tool", {})


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_sdk_client_talks_to_the_open_endpoint(server, mcp_state, mode):
    async with _client(server, mode=mode) as client:
        await _exercise(client, mcp_state)


async def test_the_sdk_client_with_a_bearer_token(server, mcp_state, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client(server, TOKEN) as client:
        await _exercise(client, mcp_state)
    with pytest.raises(Exception):
        async with _client(server):  # no token: the handshake is refused
            pass
    with pytest.raises(Exception):
        async with _client(server, "wrong-token"):
            pass
