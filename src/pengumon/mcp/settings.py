"""The `mcp:` config section."""

from pydantic import BaseModel


class McpConfig(BaseModel):
    """`enabled`: serve the read-only MCP endpoint (`/mcp`), so an AI assistant can ask Pengumon about the network.
    Off by default; read on every request, so the setting applies at once. The same sign-in as the API guards it.
    `real_names`: the tools answer with the names, host names and home addresses the dashboard shows the same caller
    (on, the default); off, the hops and devices are role words and home addresses are hidden. Secrets never are."""
    enabled: bool = False
    real_names: bool = True
