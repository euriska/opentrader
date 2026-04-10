"""HTTP entry point for the Webull OpenAPI MCP Server.

Runs with streamable-HTTP transport so other containers can reach it
over the internal trading-net network.

Required env vars:
  WEBULL_APP_KEY      — Webull OpenAPI App Key
  WEBULL_APP_SECRET   — Webull OpenAPI App Secret
  WEBULL_ENVIRONMENT  — "uat" or "prod" (default: uat)
  WEBULL_REGION_ID    — "us" or "hk" (default: us)
  WEBULL_TOKEN_DIR    — path to persist OAuth token (default: /app/conf)

Run 'webull-openapi-mcp auth' inside the container once to complete
the initial authentication (and 2FA if required) before starting.
"""
import os

from webull_openapi_mcp.config import load_config
from webull_openapi_mcp.server import build_server

host = os.getenv("FASTMCP_HOST", "0.0.0.0")
port = int(os.getenv("FASTMCP_PORT", "8000"))

config = load_config()
server = build_server(config)
server.run(transport="streamable-http", host=host, port=port)
