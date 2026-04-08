"""
Unusual Whales MCP Server

Provides options flow, dark pool, and market sentiment data
from the Unusual Whales API (https://unusualwhales.com).

Requires env var: UNUSUAL_WHALES_API_KEY
"""
import json
import os
from typing import Any

import requests
from fastmcp import FastMCP

API_KEY  = os.getenv("UNUSUAL_WHALES_API_KEY", "")
BASE_URL = "https://api.unusualwhales.com"

mcp = FastMCP("unusual-whales")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json, text/plain",
        "Content-Type": "application/json",
    }


def _get(path: str, params: dict | None = None, timeout: int = 15) -> dict | list | None:
    """GET request to the UW API with error handling."""
    if not API_KEY:
        return {"error": "UNUSUAL_WHALES_API_KEY not configured"}
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_ticker_flow(ticker: str, limit: int = 50) -> str:
    """
    Get recent options flow for a ticker from Unusual Whales.
    Returns aggregated flow data showing call/put premium split,
    net premium (positive = bullish), and volume ratios.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
        limit:  Max number of flow records (default 50)
    """
    ticker = ticker.upper().strip()
    data   = _get(f"/api/stock/{ticker}/flow-alerts", params={"limit": limit})
    if not data or "error" in (data if isinstance(data, dict) else {}):
        return _safe_json(data)

    # Normalise — UW wraps results in a 'data' key
    records = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return _safe_json(data)

    # Summarise the flow
    bullish = sum(1 for r in records if str(r.get("sentiment", "")).lower() in ("bullish", "call"))
    bearish = sum(1 for r in records if str(r.get("sentiment", "")).lower() in ("bearish", "put"))
    total   = len(records)

    call_premium = sum(float(r.get("total_premium", 0) or 0)
                       for r in records if str(r.get("put_call", "")).upper() == "CALL")
    put_premium  = sum(float(r.get("total_premium", 0) or 0)
                       for r in records if str(r.get("put_call", "")).upper() == "PUT")
    net_premium  = call_premium - put_premium
    flow_signal  = (
        "bullish" if net_premium > 100_000 else
        "bearish" if net_premium < -100_000 else
        "neutral"
    )

    return _safe_json({
        "ticker":        ticker,
        "total_alerts":  total,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "call_premium":  round(call_premium, 2),
        "put_premium":   round(put_premium,  2),
        "net_premium":   round(net_premium,  2),
        "flow_signal":   flow_signal,
        "records":       records[:limit],
    })


@mcp.tool()
def get_ticker_flow_summary(ticker: str) -> str:
    """
    Get the high-level options flow summary for a ticker:
    30-day average volumes, today's call/put ratio, and net flow direction.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
    """
    ticker = ticker.upper().strip()
    data   = _get(f"/api/stock/{ticker}/flow")
    return _safe_json(data)


@mcp.tool()
def get_darkpool_recent(ticker: str, limit: int = 20) -> str:
    """
    Get recent dark pool (off-exchange) trades for a ticker.
    Dark pool prints show institutional accumulation or distribution.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
        limit:  Max records to return (default 20)
    """
    ticker = ticker.upper().strip()
    data   = _get("/api/darkpool/recent", params={"ticker": ticker, "limit": limit})
    if not data or "error" in (data if isinstance(data, dict) else {}):
        return _safe_json(data)

    records = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return _safe_json(data)

    total_size    = sum(int(r.get("size", 0) or 0) for r in records)
    total_notional= sum(float(r.get("price", 0) or 0) * int(r.get("size", 0) or 0)
                        for r in records)

    return _safe_json({
        "ticker":          ticker,
        "print_count":     len(records),
        "total_shares":    total_size,
        "total_notional":  round(total_notional, 2),
        "records":         records[:limit],
    })


@mcp.tool()
def get_market_tide() -> str:
    """
    Get the current market tide — the aggregate call vs put premium
    flowing into the market. Bullish when calls dominate, bearish when puts dominate.
    """
    data = _get("/api/market/tide")
    return _safe_json(data)


@mcp.tool()
def get_market_sentiment() -> str:
    """
    Get overall market sentiment from Unusual Whales, broken down by sector.
    Includes net flow, put/call ratios, and bullish/bearish signals per sector.
    """
    data = _get("/api/market/sentiment")
    return _safe_json(data)


@mcp.tool()
def get_flow_alerts(limit: int = 50, min_premium: int = 500_000) -> str:
    """
    Get the most recent unusual options flow alerts across all tickers.
    Filtered to alerts above a minimum premium threshold.

    Args:
        limit:       Max alerts to return (default 50)
        min_premium: Minimum total premium in USD (default $500k)
    """
    data = _get("/api/option-contract/flow", params={
        "limit":       limit,
        "min_premium": min_premium,
    })
    return _safe_json(data)


@mcp.tool()
def get_short_interest(ticker: str) -> str:
    """
    Get short interest data for a ticker: short float percentage,
    days to cover, and short interest trend.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
    """
    ticker = ticker.upper().strip()
    data   = _get(f"/api/stock/{ticker}/short-interest")
    return _safe_json(data)


@mcp.tool()
def get_greek_exposure(ticker: str) -> str:
    """
    Get options Greek exposure (GEX, DEX, charm) for a ticker.
    High negative GEX can signal volatile price action.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
    """
    ticker = ticker.upper().strip()
    data   = _get(f"/api/stock/{ticker}/greek-exposure")
    return _safe_json(data)


@mcp.tool()
def get_ticker_oi_change(ticker: str) -> str:
    """
    Get the open interest change for a ticker's options.
    Rising OI with bullish flow = strong directional conviction.

    Args:
        ticker: Stock symbol (e.g. 'AAPL')
    """
    ticker = ticker.upper().strip()
    data   = _get(f"/api/stock/{ticker}/oi-change")
    return _safe_json(data)
