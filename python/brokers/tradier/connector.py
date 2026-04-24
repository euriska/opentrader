"""
Tradier BrokerConnector
Adapts existing TradierClient/TradierOrders/TradierPositions to the
BrokerConnector interface. One instance per account.
"""
import logging
from typing import Optional

from brokers.base import BrokerConnector
from .client    import TradierClient
from .orders    import TradierOrders
from .positions import TradierPositions
from . import market as _market

log = logging.getLogger(__name__)


class TradierConnector(BrokerConnector):
    """
    Wraps a single Tradier account.
    The gateway registry instantiates one per enabled Tradier account.
    """

    broker_name = "tradier"

    def __init__(
        self,
        account_label: str,
        account_id:    str,
        mode:          str,  # "live" | "sandbox"
    ):
        self.account_label = account_label
        self.account_id    = account_id
        self.mode          = mode
        self._client       = TradierClient(account_id=account_id, mode=mode)

        # Lightweight account shim so existing Orders/Positions classes work
        class _Acct:
            id    = account_id
            label = account_label
            client = self._client
        self._acct_shim = _Acct()

        self._orders    = TradierOrders(self._acct_shim)
        self._positions = TradierPositions(self._acct_shim)

        log.info(f"TradierConnector ready: {account_label} [{mode}]")

    # ── Orders ───────────────────────────────────────────────────────────────

    async def place_equity_order(
        self,
        symbol:     str,
        side:       str,
        quantity:   int,
        order_type: str = "market",
        price:      Optional[float] = None,
        stop:       Optional[float] = None,
        duration:   str = "day",
        tag:        Optional[str] = None,
    ) -> dict:
        return await self._orders.place_equity_order(
            symbol=symbol, side=side, quantity=quantity,
            order_type=order_type, price=price, stop=stop,
            duration=duration, tag=tag,
        )

    async def place_option_order(
        self,
        symbol:        str,
        option_symbol: str,
        side:          str,
        quantity:      int,
        order_type:    str = "market",
        price:         Optional[float] = None,
        duration:      str = "day",
        tag:           Optional[str] = None,
    ) -> dict:
        return await self._orders.place_option_order(
            symbol=symbol, option_symbol=option_symbol,
            side=side, quantity=quantity,
            order_type=order_type, price=price,
            duration=duration, tag=tag,
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await self._orders.cancel_order(order_id)

    async def cancel_all_orders(self) -> list[dict]:
        return await self._orders.cancel_all_open_orders()

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict]:
        return await self._positions.get_positions()

    async def get_balances(self) -> dict:
        return await self._positions.get_balances()

    async def get_orders(self, status: str = "all") -> list[dict]:
        orders = await self._orders.get_orders()
        if status == "all":
            return orders
        return [o for o in orders if o.get("status", "").lower() == status]

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> dict:
        return await _market.get_quote(symbol)

    async def get_quotes(self, symbols: list[str]) -> list[dict]:
        return await _market.get_quotes(symbols)

    async def get_option_chain(self, symbol: str) -> dict:
        import asyncio as _asyncio
        sym = symbol.upper()

        # Quote + expirations in parallel
        quote_task = _asyncio.ensure_future(_market.get_quote(sym))
        exp_task   = _asyncio.ensure_future(_market.get_option_expirations(sym))
        quote, expirations = await _asyncio.gather(quote_task, exp_task)

        price = float(quote.get("last") or quote.get("prevclose") or 0)
        if not expirations:
            return {"ticker": sym, "price": round(price, 2),
                    "expirations": [], "calls": [], "puts": []}

        expirations = expirations[:8]

        # Fetch all expirations in parallel
        chains = await _asyncio.gather(
            *[_market.get_option_chain(sym, exp, greeks=True) for exp in expirations],
            return_exceptions=True,
        )

        all_calls, all_puts = [], []
        for contracts in chains:
            if isinstance(contracts, Exception) or not isinstance(contracts, list):
                continue
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                otype  = (c.get("option_type") or "").lower()
                strike = float(c.get("strike") or 0)
                bid    = float(c.get("bid") or 0)
                ask    = float(c.get("ask") or 0)
                last   = float(c.get("last") or 0)
                mid    = round((bid + ask) / 2, 2) if bid and ask else last
                intrinsic = round(max(0.0, price - strike) if otype == "call"
                                  else max(0.0, strike - price), 2)
                greeks = c.get("greeks") or {}
                def _g(k): return round(float(greeks[k]), 6) if greeks.get(k) is not None else None
                iv = greeks.get("mid_iv") or greeks.get("smv_vol")
                rec = {
                    "contract":   c.get("symbol", ""),
                    "strike":     strike,
                    "expiration": c.get("expiration_date", ""),
                    "bid": bid, "ask": ask, "mid": mid, "last": last,
                    "intrinsic":  intrinsic,
                    "extrinsic":  round(max(0.0, mid - intrinsic), 2),
                    "iv":    round(float(iv), 4) if iv is not None else None,
                    "delta": _g("delta"), "gamma": _g("gamma"),
                    "theta": _g("theta"), "vega":  _g("vega"),
                    "volume": int(c.get("volume") or 0),
                    "oi":     int(c.get("open_interest") or 0),
                    "itm":    (otype == "call" and price > strike) or
                              (otype == "put"  and price < strike),
                }
                (all_calls if otype == "call" else all_puts).append(rec)

        return {
            "ticker": sym, "price": round(price, 2),
            "expirations": expirations,
            "calls": all_calls, "puts": all_puts,
        }
