"""
MCP Server Registry
Discovers tools from all configured MCP servers on startup.

To add a new MCP server, extend the MCP_SERVERS env var:
  MCP_SERVERS=yahoo=http://ot-mcp-yahoo:8000/mcp,myserver=http://ot-mcp-myserver:8000/mcp

All discovered tools are automatically available to the AI agent.
Command shortcuts can be mapped in commands.py.
"""
import os
import structlog
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

log = structlog.get_logger("chat-agent.registry")


def _parse_servers() -> dict[str, str]:
    """Parse MCP_SERVERS=name=url,name=url into {name: url}."""
    raw = os.getenv("MCP_SERVERS", "")
    servers = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" in entry:
            name, url = entry.split("=", 1)
            servers[name.strip()] = url.strip()
    return servers


class MCPRegistry:
    def __init__(self):
        self.servers: dict[str, str] = _parse_servers()
        # tool_name -> {"server": name, "url": url, "tool": Tool}
        self._tools: dict[str, dict] = {}

    async def discover(self):
        """Discover and cache all tools from every registered server."""
        for name, url in self.servers.items():
            try:
                tools = await self._list_tools(url)
                for tool in tools:
                    self._tools[tool.name] = {"server": name, "url": url, "tool": tool}
                log.info("registry.discovered", server=name, tools=len(tools),
                         names=[t.name for t in tools])
            except Exception as e:
                log.warning("registry.discover_failed", server=name, url=url, error=str(e))

    async def _list_tools(self, url: str):
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return result.tools

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Route a tool call to the correct MCP server and return text result."""
        entry = self._tools.get(tool_name)
        if not entry:
            return f"Unknown tool: `{tool_name}`"
        url = entry["url"]
        try:
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    texts = [c.text for c in result.content if hasattr(c, "text")]
                    return "\n".join(texts) if texts else str(result.content)
        except Exception as e:
            log.error("registry.call_failed", tool=tool_name, error=str(e))
            return f"Error calling `{tool_name}`: {e}"

    def get_openai_tools(self) -> list[dict]:
        """Return all tools in OpenAI function-calling format for the AI agent."""
        out = []
        for tool_name, entry in self._tools.items():
            tool = entry["tool"]
            schema = tool.inputSchema or {"type": "object", "properties": {}}
            out.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": (tool.description or "")[:512],
                    "parameters": schema,
                },
            })
        return out

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def server_names(self) -> list[str]:
        return list(self.servers.keys())
