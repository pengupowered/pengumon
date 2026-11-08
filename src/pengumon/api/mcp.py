"""POST /mcp: the read-only MCP endpoint (pengumon.mcp; docs/mcp.md). The route only reads the request and hands the
body to the protocol; every other method is 405 (no SSE stream, no sessions). While `mcp.enabled` is off every method
on /mcp and /mcp/ answers 404, decided on each request, so the setting applies at once. The security middleware has
already checked Host, Origin, the sign-in and the body size, as for the API."""

from fastapi import APIRouter, Depends, Request, Response

from pengumon.api.common import StateDep
from pengumon.mcp import protocol
from pengumon.security import rate_limit

router = APIRouter()

_RATE = rate_limit("mcp", 120, 60)


def _enabled(state) -> bool:
    return bool(state.config is not None and state.config.mcp.enabled)


# Both spellings, and every method: while MCP is off nothing here may answer anything but 404 (an unmatched method would
# be 405 with "Allow: POST", and "/mcp/" a redirect to "/mcp", either of which tells that the endpoint exists).
_PATHS = ("/mcp", "/mcp/")
_OTHER_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"]


@router.post(_PATHS[0], include_in_schema=False)
@router.post(_PATHS[1], include_in_schema=False)
async def post_mcp(request: Request, state: StateDep) -> Response:
    if not _enabled(state):
        return Response(status_code=404)
    await _RATE(request)
    reply = await protocol.handle(await request.body(), state, request.headers.get("mcp-protocol-version"))
    if reply.body is None:
        return Response(status_code=reply.status)
    return Response(protocol.encode(reply.body), status_code=reply.status, media_type="application/json")


@router.api_route(_PATHS[0], methods=_OTHER_METHODS, include_in_schema=False)
@router.api_route(_PATHS[1], methods=_OTHER_METHODS, include_in_schema=False)
async def other_mcp(state: StateDep) -> Response:
    if not _enabled(state):
        return Response(status_code=404)
    return Response(status_code=405, headers={"Allow": "POST"})
