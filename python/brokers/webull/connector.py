"""
Webull BrokerConnector
Implements the BrokerConnector interface for a single Webull account.
"""
import logging
from typing import Optional

from brokers.base       import BrokerConnector
from .client            import WebullClient
from .orders            import WebullOrders
from .positions         import WebullPositions

log = logging.getLogger(__name__)


class WebullConnector(BrokerConnector):
    """
    One instance per Webull account entry in accounts.toml.
    Supports paper (act.webull.com) and live (tradeapi.webull.com) modes.
    """

    broker_name = "webull"

    def __init__(
        self,
        account_label: str,
        account_id:    str,
        mode:          str,   # "live" | "paper"
    ):
        self.account_label = account_label
        self.account_id    = account_id
        self.mode          = mode

        self._client    = WebullClient(mode=mode)
        self._orders    = WebullOrders(self._client, account_id, account_label, mode)
        self._positions = WebullPositions(self._client, account_id, account_label, mode)

        log.info(f"WebullConnector ready: {account_label} [{mode}]")

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
        return await self._orders.cancel_all_orders()

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict]:
        return await self._positions.get_positions()

    async def get_balances(self) -> dict:
        return await self._positions.get_balances()

    async def get_orders(self, status: str = "all") -> list[dict]:
        return await self._orders.get_orders(status=status)

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> dict:
        result = await self._client.get(
            "/quotes/ticker/getTickerRealTime",
            params={"symbol": symbol.upper()},
        )
        return {
            "symbol": symbol.upper(),
            "last":   float(result.get("close", result.get("pPrice", 0)) or 0),
            "bid":    float(result.get("bidPrice", 0) or 0),
            "ask":    float(result.get("askPrice", 0) or 0),
            "raw":    result,
        }

    async def get_quotes(self, symbols: list[str]) -> list[dict]:
        return [await self.get_quote(s) for s in symbols]
