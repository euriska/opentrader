"""
Lightweight MCP HTTP client for use inside trader agents.
Calls a single tool on a streamable-HTTP MCP server and returns the text result.
"""
import os
import structlog
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

log = structlog.get_logger("shared.mcp_client")

TRADINGVIEW_MCP_URL = os.getenv(
    "TRADINGVIEW_MCP_URL", "http://ot-mcp-tradingview:8000/mcp"
)


async def call_mcp_tool(url: str, tool_name: str, arguments: dict) -> str | None:
    """Call a tool on an MCP HTTP server. Returns text result or None on failure."""
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                texts = [c.text for c in result.content if hasattr(c, "text")]
                return "\n".join(texts) if texts else None
    except Exception as e:
        log.warning("mcp_client.call_failed", tool=tool_name, url=url, error=str(e))
        return None


async def get_tv_indicators(ticker: str, interval: str = "1d") -> dict | None:
    """
    Fetch TradingView summary indicators for a ticker.
    Returns parsed dict with keys: recommendation, buy, sell, neutral.
    """
    raw = await call_mcp_tool(
        TRADINGVIEW_MCP_URL,
        "get_indicators",
        {"symbol": ticker, "timeframe": interval},
    )
    if not raw:
        return None
    try:
        import json
        data = json.loads(raw)
        # Normalise — server returns {summary: {RECOMMENDATION, BUY, SELL, NEUTRAL}}
        summary = data.get("summary") or data
        return {
            "recommendation": summary.get("RECOMMENDATION", "NEUTRAL"),
            "buy":            int(summary.get("BUY", 0)),
            "sell":           int(summary.get("SELL", 0)),
            "neutral":        int(summary.get("NEUTRAL", 0)),
        }
    except Exception as e:
        log.warning("mcp_client.parse_failed", ticker=ticker, error=str(e), raw=raw[:200])
        return None


def tv_confirms_direction(indicators: dict, direction: str) -> bool:
    """
    Returns True if TradingView indicators confirm the trade direction.
    BUY/STRONG_BUY confirms long; SELL/STRONG_SELL confirms short.
    NEUTRAL is treated as non-blocking (returns True).
    """
    if indicators is None:
        return True  # MCP unavailable — don't block trades
    rec = indicators.get("recommendation", "NEUTRAL").upper()
    if direction == "long":
        return rec in ("BUY", "STRONG_BUY", "NEUTRAL")
    else:
        return rec in ("SELL", "STRONG_SELL", "NEUTRAL")
