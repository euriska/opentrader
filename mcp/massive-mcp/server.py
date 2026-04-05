"""
Massive.com (Polygon.io) MCP Server
Provides real-time and historical market data tools via FastMCP.
"""
import os
from datetime import date, timedelta
from mcp.server.fastmcp import FastMCP

massive_server = FastMCP(
    "massive",
    instructions="""
# Massive Market Data MCP Server

Provides real-time and historical market data from Massive.com (formerly Polygon.io).

Available tools:
- get_quote: Get the latest quote (bid/ask/last/volume) for a ticker
- get_daily_bars: Get OHLCV daily bars for a ticker over a date range
- get_intraday_bars: Get intraday OHLCV bars (1m/5m/15m/1h) for a ticker
- get_ticker_details: Get company details, market cap, description for a ticker
- get_market_status: Get current market open/closed status
- get_prev_close: Get the previous trading day's close price and volume
- get_aggregates: Get aggregate bars for a ticker with flexible timespan
""",
)


def _client():
    from polygon import RESTClient
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        raise ValueError("MASSIVE_API_KEY environment variable not set")
    return RESTClient(api_key)


@massive_server.tool(
    name="get_quote",
    description="Get the latest real-time quote for a ticker: last price, bid, ask, volume, VWAP.",
)
def get_quote(ticker: str) -> dict:
    """
    Args:
        ticker: Stock ticker symbol, e.g. 'AAPL'
    """
    c = _client()
    snap = c.get_snapshot_ticker("stocks", ticker.upper())
    if not snap:
        return {"error": f"No snapshot found for {ticker}"}
    d = snap.day or {}
    p = snap.prev_day or {}
    lt = snap.last_trade or {}
    lq = snap.last_quote or {}
    return {
        "ticker":       ticker.upper(),
        "last":         lt.price if lt else None,
        "bid":          lq.bid_price if lq else None,
        "ask":          lq.ask_price if lq else None,
        "volume":       d.volume if d else None,
        "vwap":         d.vwap if d else None,
        "open":         d.open if d else None,
        "high":         d.high if d else None,
        "low":          d.low if d else None,
        "close":        d.close if d else None,
        "prev_close":   p.close if p else None,
        "change_pct":   snap.todays_change_perc,
    }


@massive_server.tool(
    name="get_daily_bars",
    description="Get OHLCV daily bars for a ticker. Returns up to 365 days of history.",
)
def get_daily_bars(ticker: str, from_date: str = "", to_date: str = "") -> list:
    """
    Args:
        ticker:    Stock ticker symbol, e.g. 'AAPL'
        from_date: Start date YYYY-MM-DD (default: 30 days ago)
        to_date:   End date YYYY-MM-DD (default: today)
    """
    c = _client()
    to   = to_date   or date.today().isoformat()
    frm  = from_date or (date.today() - timedelta(days=30)).isoformat()
    bars = c.get_aggs(ticker.upper(), 1, "day", frm, to, limit=365)
    return [
        {"date": date.fromtimestamp(b.timestamp / 1000).isoformat(),
         "open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume, "vwap": b.vwap}
        for b in (bars or [])
    ]


@massive_server.tool(
    name="get_intraday_bars",
    description="Get intraday OHLCV bars for a ticker on a given date.",
)
def get_intraday_bars(ticker: str, interval: str = "5", bar_date: str = "") -> list:
    """
    Args:
        ticker:   Stock ticker symbol, e.g. 'AAPL'
        interval: Bar size in minutes: '1', '5', '15', '30', '60'
        bar_date: Date YYYY-MM-DD (default: today)
    """
    c    = _client()
    day  = bar_date or date.today().isoformat()
    mins = int(interval) if interval.isdigit() else 5
    bars = c.get_aggs(ticker.upper(), mins, "minute", day, day, limit=500)
    return [
        {"time": b.timestamp, "open": b.open, "high": b.high,
         "low": b.low, "close": b.close, "volume": b.volume}
        for b in (bars or [])
    ]


@massive_server.tool(
    name="get_ticker_details",
    description="Get company details for a ticker: name, description, sector, market cap, exchange.",
)
def get_ticker_details(ticker: str) -> dict:
    """
    Args:
        ticker: Stock ticker symbol, e.g. 'AAPL'
    """
    c = _client()
    d = c.get_ticker_details(ticker.upper())
    if not d:
        return {"error": f"No details found for {ticker}"}
    return {
        "ticker":       d.ticker,
        "name":         d.name,
        "description":  d.description,
        "sic_description": d.sic_description,
        "market_cap":   d.market_cap,
        "employees":    d.total_employees,
        "exchange":     d.primary_exchange,
        "currency":     d.currency_name,
        "homepage":     d.homepage_url,
        "list_date":    d.list_date,
    }


@massive_server.tool(
    name="get_market_status",
    description="Get the current US market open/closed status and upcoming holidays.",
)
def get_market_status() -> dict:
    c      = _client()
    status = c.get_market_status()
    return {
        "market":        status.market,
        "server_time":   status.server_time,
        "exchanges":     status.exchanges.__dict__ if status.exchanges else {},
        "currencies":    status.currencies.__dict__ if status.currencies else {},
    }


@massive_server.tool(
    name="get_prev_close",
    description="Get the previous trading day's OHLCV and change% for a ticker.",
)
def get_prev_close(ticker: str) -> dict:
    """
    Args:
        ticker: Stock ticker symbol, e.g. 'AAPL'
    """
    c    = _client()
    bars = c.get_previous_close_agg(ticker.upper())
    if not bars:
        return {"error": f"No previous close data for {ticker}"}
    b = bars[0]
    return {
        "ticker": ticker.upper(),
        "open":   b.open, "high": b.high, "low": b.low,
        "close":  b.close, "volume": b.volume,
        "vwap":   b.vwap,
    }
