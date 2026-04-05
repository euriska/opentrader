"""
Entry point for running the Massive MCP server with streamable-HTTP transport.
Used by the ot-mcp-massive container so other services can reach it over the network.
"""
import os
from mcp.server.transport_security import TransportSecuritySettings
from server import massive_server

host = os.getenv("FASTMCP_HOST", "0.0.0.0")
port = int(os.getenv("FASTMCP_PORT", "8000"))

massive_server.settings.host = host
massive_server.settings.port = port

massive_server.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

massive_server.run(transport="streamable-http")
