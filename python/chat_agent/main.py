"""
OpenTrader Chat Agent
─────────────────────
Multi-platform bot (Discord + Telegram) with:
  • Command mode  — !quote AAPL, !news TSLA, !trending, etc.
  • AI agent mode — natural language via OpenRouter with MCP tool use

MCP servers are auto-discovered at startup from MCP_SERVERS env var.
Adding a new MCP server:
  1. Add it to MCP_SERVERS: "yahoo=http://ot-mcp-yahoo:8000/mcp,new=http://ot-mcp-new:8000/mcp"
  2. Its tools are automatically available in AI mode
  3. Optionally add command shortcuts in chat_agent/commands.py
"""
import asyncio
import os
import structlog

from .mcp_registry import MCPRegistry
from .discord_bot import start_discord
from .telegram_bot import start_telegram

log = structlog.get_logger("chat-agent")


async def main():
    registry = MCPRegistry()

    if not registry.servers:
        log.error("chat-agent.no_mcp_servers",
                  hint="Set MCP_SERVERS=name=http://host:port/mcp in environment")
        return

    log.info("chat-agent.starting", servers=list(registry.servers.keys()))
    await registry.discover()

    total_tools = len(registry.tool_names())
    if total_tools == 0:
        log.warning("chat-agent.no_tools_discovered",
                    servers=list(registry.servers.keys()))
    else:
        log.info("chat-agent.tools_ready",
                 count=total_tools, tools=registry.tool_names())

    tasks = []

    discord_token = os.getenv("DISCORD_BOT_TOKEN", "")
    if discord_token:
        tasks.append(asyncio.create_task(start_discord(discord_token, registry)))
        log.info("chat-agent.discord_enabled")
    else:
        log.warning("chat-agent.discord_disabled", reason="DISCORD_BOT_TOKEN not set")

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        tasks.append(asyncio.create_task(start_telegram(telegram_token, registry)))
        log.info("chat-agent.telegram_enabled")
    else:
        log.warning("chat-agent.telegram_disabled", reason="TELEGRAM_BOT_TOKEN not set")

    if not tasks:
        log.error("chat-agent.no_platforms_configured",
                  hint="Set DISCORD_BOT_TOKEN and/or TELEGRAM_BOT_TOKEN")
        return

    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
