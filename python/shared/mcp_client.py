"""
Lightweight MCP HTTP client for use inside trader agents.
Calls a single tool on a streamable-HTTP MCP server and returns the text result.
"""
import json
import os
import structlog
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

log = structlog.get_logger("shared.mcp_client")

TRADINGVIEW_MCP_URL = os.getenv(
    "TRADINGVIEW_MCP_URL", "http://ot-mcp-tradingview:8000/mcp"
)
MASSIVE_MCP_URL = os.getenv(
    "MASSIVE_MCP_URL", "http://ot-mcp-massive:8000/mcp"
)
YAHOO_MCP_URL = os.getenv(
    "YAHOO_MCP_URL", "http://ot-mcp-yahoo:8000/mcp"
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


async def get_classification(ticker: str) -> dict:
    """
    Returns {"sector": str, "industry": str} using Yahoo Finance as primary
    source and Massive MCP (SIC mapping) as sector-only fallback.
    """
    # Primary: Yahoo Finance MCP
    raw = await call_mcp_tool(YAHOO_MCP_URL, "get_classification", {"ticker": ticker})
    if raw:
        try:
            data = json.loads(raw)
            if data.get("sector") or data.get("industry"):
                return {
                    "sector":   data.get("sector") or "",
                    "industry": data.get("industry") or "",
                }
        except Exception:
            pass

    # Fallback: Massive MCP (sector via SIC mapping, no industry)
    sector = await get_sector(ticker)
    return {"sector": sector or "", "industry": ""}


async def get_sector(ticker: str) -> str | None:
    """
    Fetch the GICS-style sector for a ticker via the Massive MCP.
    Returns sector string (e.g. "Healthcare") or None if unavailable.
    """
    raw = await call_mcp_tool(
        MASSIVE_MCP_URL,
        "get_ticker_details",
        {"ticker": ticker},
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("sector") or None
    except Exception as e:
        log.warning("mcp_client.sector_parse_failed", ticker=ticker, error=str(e))
        return None


async def get_avg_volume(ticker: str) -> float | None:
    """
    Fetch the average daily volume for a ticker via Yahoo Finance MCP.
    Returns volume as a float (e.g. 5_000_000) or None on failure.
    """
    raw = await call_mcp_tool(YAHOO_MCP_URL, "get_avg_volume", {"ticker": ticker})
    if not raw:
        return None
    try:
        data = json.loads(raw)
        vol = data.get("avg_volume") or data.get("avgVolume") or data.get("volume")
        return float(vol) if vol else None
    except Exception as e:
        log.warning("mcp_client.avg_volume_parse_failed", ticker=ticker, error=str(e))
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
