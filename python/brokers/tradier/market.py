"""
Tradier Market Data
Quotes, options chains, and expirations.
Uses live endpoint for both live and sandbox (market data is the same).
"""
import logging
from .client import TradierClient

log = logging.getLogger(__name__)

# Use sandbox endpoint — works for sandbox tokens; live tokens can use either
import os as _os
_market_mode   = _os.getenv("TRADIER_MARKET_MODE", "sandbox")
_market_client = TradierClient(account_id="market", mode=_market_mode)


async def get_quote(symbol: str) -> dict:
    result = await _market_client.get(
        "/markets/quotes",
        params={"symbols": symbol, "greeks": "false"},
    )
    quotes = result.get("quotes", {}).get("quote", {})
    return quotes if isinstance(quotes, dict) else {}


async def get_quotes(symbols: list[str]) -> list:
    result = await _market_client.get(
        "/markets/quotes",
        params={"symbols": ",".join(symbols), "greeks": "false"},
    )
    quotes = result.get("quotes", {}).get("quote", [])
    return quotes if isinstance(quotes, list) else [quotes]


async def get_option_expirations(symbol: str, include_all_roots: bool = True) -> list:
    result = await _market_client.get(
        "/markets/options/expirations",
        params={"symbol": symbol, "includeAllRoots": str(include_all_roots).lower()},
    )
    expirations = result.get("expirations", {})
    if expirations == "null" or expirations is None:
        return []
    raw = expirations.get("date", [])
    return raw if isinstance(raw, list) else [raw]


async def get_option_chain(symbol: str, expiration: str, greeks: bool = True) -> list:
    result = await _market_client.get(
        "/markets/options/chains",
        params={
            "symbol":     symbol,
            "expiration": expiration,
            "greeks":     str(greeks).lower(),
        },
    )
    options = result.get("options", {})
    if options == "null" or options is None:
        return []
    raw = options.get("option", [])
    return raw if isinstance(raw, list) else [raw]


async def get_option_strikes(symbol: str, expiration: str) -> list:
    result = await _market_client.get(
        "/markets/options/strikes",
        params={"symbol": symbol, "expiration": expiration},
    )
    strikes = result.get("strikes", {})
    if strikes == "null" or strikes is None:
        return []
    raw = strikes.get("strike", [])
    return raw if isinstance(raw, list) else [raw]
