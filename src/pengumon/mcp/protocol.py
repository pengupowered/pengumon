"""The MCP rules of POST /mcp: JSON-RPC 2.0 framing, the handshake, and tools/list and tools/call.

Stateless, one JSON-RPC message per POST, JSON answers only (no SSE, no session id): `handle` takes the request body and
the MCP-Protocol-Version header and returns a `Reply` (status and the JSON object to send, or None for an empty body).
Nothing here imports the web framework: api/mcp.py is the route, tools.py the tools, and this module is tested with
plain dicts.

The protocol revisions spoken are the initialize-handshake ones (SUPPORTED_VERSIONS: what the reference SDK client,
mcp 2.3.0, calls HANDSHAKE_PROTOCOL_VERSIONS). The SDK also offers a newer, per-request-envelope revision (2026-07-28)
and probes for it first with `server/discover`; this server answers that probe "method not found", which makes the
client fall back to `initialize`, as the SDK documents for servers that do not speak it.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pengumon import __version__
from pengumon.mcp.tools import TOOLS, BadArguments, Tool, ToolFailure, plain
from pengumon.observability.telemetry import mcp_calls

_log = logging.getLogger("pengumon.mcp")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Pengumon is a home network monitor: it pings every hop between this network and the internet once a second, "
    "records outages and slow spells as incidents, and polls the cable modem and router. Everything here is read-only. "
    "Start with the `status` tool to answer 'is the internet OK right now?'. Use `incidents` and `incident` to see what "
    "went wrong and why, `report` for uptime over a day, week or month, `devices` for the modem and router, `services` "
    "for the apps on the host, and `ai_pack` for a written troubleshooting summary."
)


@dataclass
class Reply:
    status: int
    body: Optional[Dict[str, Any]] = None  # None: an empty body (202)
    headers: Optional[Dict[str, str]] = None


def _error(status: int, code: int, message: str, request_id: Any = None) -> Reply:
    return Reply(status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _result(request_id: Any, result: Dict[str, Any]) -> Reply:
    return Reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


def _valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def encode(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def version_supported(header: Optional[str]) -> bool:
    """Whether an MCP-Protocol-Version header is one we speak (no header: yes, the spec says to assume an old one)."""
    return header is None or header.strip() in SUPPORTED_VERSIONS


def validate(schema: Dict[str, Any], arguments: Dict[str, Any]) -> Optional[str]:
    """Checks `arguments` against a tool's inputSchema (the subset the tools use: an object of string, integer and
    enum properties, `required`, no extra properties). Returns the first problem as a sentence, else None."""
    properties = schema.get("properties", {})
    for name in arguments:
        if name not in properties:
            return f"Unknown argument '{name[:64]}'."  # never more of the caller's text than that
    for name in schema.get("required", ()):
        if name not in arguments:
            return f"Missing argument '{name}'."
    for name, value in arguments.items():
        spec = properties[name]
        kind = spec.get("type")
        if kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"Argument '{name}' must be an integer."
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                return f"Argument '{name}' must be between {spec.get('minimum')} and {spec.get('maximum')}."
        elif kind == "string":
            if not isinstance(value, str):
                return f"Argument '{name}' must be a string."
        if "enum" in spec and value not in spec["enum"]:
            return f"Argument '{name}' must be one of: {', '.join(map(str, spec['enum']))}."
    return None


def _tool_listing(tool: Tool) -> Dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema,
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _text_result(payload: Any, *, error: bool) -> Dict[str, Any]:
    if error:
        return {"content": [{"type": "text", "text": str(payload)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                                            allow_nan=False)}],
            "structuredContent": payload, "isError": False}


async def _call_tool(state, params: Any, request_id: Any) -> Reply:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _error(200, INVALID_PARAMS, "tools/call needs a tool name.", request_id)
    tool = next((t for t in TOOLS if t.name == params["name"]), None)
    if tool is None:
        mcp_calls.add(1, {"tool": "unknown", "outcome": "refused"})  # never the caller's text as a label
        return _error(200, INVALID_PARAMS, f"Unknown tool '{params['name'][:64]}'.", request_id)
    arguments = params.get("arguments")
    arguments = {} if arguments is None else arguments
    problem = "Arguments must be an object." if not isinstance(arguments, dict) else validate(tool.input_schema, arguments)
    if problem is not None:
        mcp_calls.add(1, {"tool": tool.name, "outcome": "refused"})
        return _error(200, INVALID_PARAMS, problem, request_id)
    try:
        payload = plain(await tool.handler(state, arguments))
        result = _text_result(payload, error=False)
        outcome = "ok"
    except BadArguments as e:
        mcp_calls.add(1, {"tool": tool.name, "outcome": "refused"})
        return _error(200, INVALID_PARAMS, str(e), request_id)
    except ToolFailure as e:
        result, outcome = _text_result(str(e), error=True), "error"
    except Exception:  # a bug in a tool: logged here, the caller is told nothing of the inside
        _log.exception("MCP tool %s failed", tool.name)
        result, outcome = _text_result("The tool failed unexpectedly; see Pengumon's log.", error=True), "error"
    mcp_calls.add(1, {"tool": tool.name, "outcome": outcome})
    return _result(request_id, result)


async def handle(raw: bytes, state, version_header: Optional[str] = None) -> Reply:
    """One POST body to its answer. `state` is the app's AppState (tools read it)."""
    if not version_supported(version_header):
        return _error(400, INVALID_REQUEST, "Unsupported MCP-Protocol-Version; supported: " + ", ".join(SUPPORTED_VERSIONS))
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        return _error(400, PARSE_ERROR, "The body is not valid JSON.")
    if not isinstance(message, dict):
        return _error(400, INVALID_REQUEST, "Send one JSON-RPC request object (batches are not supported).")
    if message.get("jsonrpc") != "2.0":
        return _error(400, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
    method = message.get("method")
    if method is None and ("result" in message or "error" in message):
        return Reply(202)  # a response to a request of ours: we send none, and accept the message
    if not isinstance(method, str):
        return _error(400, INVALID_REQUEST, "The message has no method.", message.get("id") if _valid_id(message.get("id")) else None)
    if "id" not in message:
        return Reply(202)  # a notification (initialized, cancelled, ...): nothing to answer
    request_id = message["id"]
    if not _valid_id(request_id):
        return _error(400, INVALID_REQUEST, "The id must be a string or an integer.")
    params = message.get("params")

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION
        return _result(request_id, {
            "protocolVersion": version, "capabilities": {"tools": {}},
            "serverInfo": {"name": "pengumon", "version": __version__}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [_tool_listing(t) for t in TOOLS]})
    if method == "tools/call":
        return await _call_tool(state, params, request_id)
    return _error(200, METHOD_NOT_FOUND, f"Method not found: {method[:64]}", request_id)
