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


def _sic_to_sector(sic_code: int | str | None) -> str:
    """Map a Polygon SIC code to a GICS-style sector name."""
    if sic_code is None:
        return "Unknown"
    code = int(sic_code)
    if   100  <= code <= 999:   return "Basic Materials"
    if  1000  <= code <= 1499:  return "Basic Materials"
    if  1500  <= code <= 1799:  return "Industrials"
    if  2000  <= code <= 2111:  return "Consumer Defensive"
    if  2200  <= code <= 2399:  return "Consumer Cyclical"
    if  2400  <= code <= 2499:  return "Industrials"
    if  2500  <= code <= 2599:  return "Consumer Cyclical"
    if  2600  <= code <= 2699:  return "Basic Materials"
    if  2700  <= code <= 2799:  return "Consumer Cyclical"
    if  2800  <= code <= 2829:  return "Basic Materials"
    if  2830  <= code <= 2836:  return "Healthcare"
    if  2837  <= code <= 2899:  return "Basic Materials"
    if  2900  <= code <= 2999:  return "Energy"
    if  3000  <= code <= 3299:  return "Basic Materials"
    if  3300  <= code <= 3399:  return "Basic Materials"
    if  3400  <= code <= 3499:  return "Industrials"
    if  3500  <= code <= 3599:  return "Industrials"
    if  3600  <= code <= 3699:  return "Technology"
    if  3700  <= code <= 3799:  return "Consumer Cyclical"
    if  3800  <= code <= 3840:  return "Industrials"
    if  3841  <= code <= 3851:  return "Healthcare"
    if  3852  <= code <= 3999:  return "Industrials"
    if  4000  <= code <= 4599:  return "Industrials"
    if  4600  <= code <= 4699:  return "Energy"
    if  4700  <= code <= 4799:  return "Industrials"
    if  4800  <= code <= 4899:  return "Communication Services"
    if  4900  <= code <= 4999:  return "Utilities"
    if  code == 5047:           return "Healthcare"
    if  code == 5122:           return "Healthcare"
    if  5000  <= code <= 5199:  return "Industrials"
    if  5200  <= code <= 5999:  return "Consumer Cyclical"
    if  6000  <= code <= 6199:  return "Financial Services"
    if  6200  <= code <= 6299:  return "Financial Services"
    if  6300  <= code <= 6499:  return "Financial Services"
    if  6500  <= code <= 6599:  return "Real Estate"
    if  6700  <= code <= 6999:  return "Financial Services"
    if  7370  <= code <= 7379:  return "Technology"
    if  7000  <= code <= 7399:  return "Consumer Cyclical"
    if  7400  <= code <= 7999:  return "Consumer Cyclical"
    if  8000  <= code <= 8099:  return "Healthcare"
    if  8100  <= code <= 8299:  return "Industrials"
    if  8300  <= code <= 8399:  return "Industrials"
    if  8700  <= code <= 8799:  return "Industrials"
    return "Unknown"


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
        "ticker":          d.ticker,
        "name":            d.name,
        "description":     d.description,
        "sic_code":        d.sic_code,
        "sic_description": d.sic_description,
        "sector":          _sic_to_sector(d.sic_code),
        "market_cap":      d.market_cap,
        "employees":       d.total_employees,
        "exchange":        d.primary_exchange,
        "currency":        d.currency_name,
        "homepage":        d.homepage_url,
        "list_date":       d.list_date,
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
