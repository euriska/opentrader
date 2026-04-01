"""
Alpaca BrokerConnector
Implements the BrokerConnector interface for a single Alpaca account.
"""
import logging
from typing import Optional

from brokers.base       import BrokerConnector
from .client            import AlpacaClient
from .orders            import AlpacaOrders
from .positions         import AlpacaPositions
from . import market    as _market

log = logging.getLogger(__name__)


class AlpacaConnector(BrokerConnector):
    """
    One instance per Alpaca account entry in accounts.toml.
    Alpaca uses a single API key pair; mode selects paper vs live base URL.
    """

    broker_name = "alpaca"

    def __init__(
        self,
        account_label: str,
        account_id:    str,
        mode:          str,  # "live" | "paper"
    ):
        self.account_label = account_label
        self.account_id    = account_id
        self.mode          = mode

        self._client    = AlpacaClient(mode=mode)
        self._orders    = AlpacaOrders(self._client, account_label)
        self._positions = AlpacaPositions(self._client, account_label)

        log.info(f"AlpacaConnector ready: {account_label} [{mode}]")

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
        return await _market.get_quote(symbol)

    async def get_quotes(self, symbols: list[str]) -> list[dict]:
        return await _market.get_quotes(symbols)
