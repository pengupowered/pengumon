"""The `mcp:` config section."""

from pydantic import BaseModel


class McpConfig(BaseModel):
    """`enabled`: serve the read-only MCP endpoint (`/mcp`), so an AI assistant can ask Pengumon about the network.
    Off by default; read on every request, so the setting applies at once. The same sign-in as the API guards it."""
    enabled: bool = False
