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
