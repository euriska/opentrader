"""HTTP entry point for the Unusual Whales MCP server."""
import os
from server import mcp

host = os.getenv("FASTMCP_HOST", "0.0.0.0")
port = int(os.getenv("FASTMCP_PORT", "8000"))

mcp.run(transport="streamable-http", host=host, port=port)
