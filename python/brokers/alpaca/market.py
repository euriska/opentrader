"""
Alpaca Market Data
Latest quotes and bars via data.alpaca.markets.
Uses the free IEX feed (no subscription required for basic quotes).
"""
import logging
import os
from .client import AlpacaClient

log = logging.getLogger(__name__)

# Shared data client (market data doesn't need account context)
_data_client = AlpacaClient(mode="paper")

FEED = os.getenv("ALPACA_DATA_FEED", "iex")   # "iex" (free) or "sip" (paid)


async def get_quote(symbol: str) -> dict:
    """Return latest quote for a single symbol."""
    result = await _data_client.get_market_data(
        f"/stocks/{symbol.upper()}/quotes/latest",
        params={"feed": FEED},
    )
    q = result.get("quote", result)
    return {
        "symbol": symbol.upper(),
        "bid":    float(q.get("bp", 0) or 0),
        "ask":    float(q.get("ap", 0) or 0),
        "last":   float(q.get("ap", 0) or q.get("bp", 0) or 0),
        "raw":    q,
    }


async def get_quotes(symbols: list[str]) -> list[dict]:
    """Return latest quotes for multiple symbols."""
    syms = ",".join(s.upper() for s in symbols)
    result = await _data_client.get_market_data(
        "/stocks/quotes/latest",
        params={"symbols": syms, "feed": FEED},
    )
    quotes_map = result.get("quotes", {})
    out = []
    for sym in symbols:
        q = quotes_map.get(sym.upper(), {})
        out.append({
            "symbol": sym.upper(),
            "bid":    float(q.get("bp", 0) or 0),
            "ask":    float(q.get("ap", 0) or 0),
            "last":   float(q.get("ap", 0) or q.get("bp", 0) or 0),
            "raw":    q,
        })
    return out
