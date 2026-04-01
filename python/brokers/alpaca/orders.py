"""
Alpaca Orders
Place, cancel, and query orders for a single Alpaca account.
Alpaca uses fractional shares and supports day/gtc/ioc/fok time-in-force.
"""
import logging
from typing import Optional

from .client import AlpacaClient

log = logging.getLogger(__name__)

# Alpaca side mapping from platform convention
_SIDE_MAP = {
    "buy":           "buy",
    "sell":          "sell",
    "sell_short":    "sell",   # Alpaca sells short automatically if no position
    "buy_to_cover":  "buy",
    # Options
    "buy_to_open":   "buy",
    "sell_to_open":  "sell",
    "buy_to_close":  "buy",
    "sell_to_close": "sell",
}

# Alpaca type mapping
_TYPE_MAP = {
    "market":     "market",
    "limit":      "limit",
    "stop":       "stop",
    "stop_limit": "stop_limit",
}

# Alpaca time-in-force mapping from platform convention
_DURATION_MAP = {
    "day": "day",
    "gtc": "gtc",
    "pre": "opg",
    "post": "cls",
    "ioc": "ioc",
    "fok": "fok",
}


class AlpacaOrders:
    """Order management for a single Alpaca account."""

    def __init__(self, client: AlpacaClient, account_label: str):
        self.client        = client
        self.account_label = account_label

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
        body: dict = {
            "symbol":        symbol.upper(),
            "qty":           str(quantity),
            "side":          _SIDE_MAP.get(side, side),
            "type":          _TYPE_MAP.get(order_type, order_type),
            "time_in_force": _DURATION_MAP.get(duration, "day"),
        }
        if price is not None:
            body["limit_price"] = str(round(price, 2))
        if stop is not None:
            body["stop_price"] = str(round(stop, 2))
        if tag:
            body["client_order_id"] = tag[:48]  # Alpaca max 48 chars

        log.info(
            f"[alpaca:{self.account_label}] Order: {side} {quantity} {symbol} "
            f"@ {order_type} {price or ''}"
        )
        return await self.client.post("/orders", json=body)

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
        """
        Place an options order via Alpaca.
        option_symbol must be the OCC format: e.g. AAPL240315C00180000
        """
        body: dict = {
            "symbol":        option_symbol.upper(),
            "qty":           str(quantity),
            "side":          _SIDE_MAP.get(side, side),
            "type":          _TYPE_MAP.get(order_type, order_type),
            "time_in_force": _DURATION_MAP.get(duration, "day"),
            "order_class":   "simple",
        }
        if price is not None:
            body["limit_price"] = str(round(price, 2))
        if tag:
            body["client_order_id"] = tag[:48]

        log.info(
            f"[alpaca:{self.account_label}] Option: {side} {quantity}x {option_symbol}"
        )
        return await self.client.post("/orders", json=body)

    async def cancel_order(self, order_id: str) -> dict:
        log.info(f"[alpaca:{self.account_label}] Cancelling {order_id}")
        result = await self.client.delete(f"/orders/{order_id}")
        return result or {"cancelled": order_id}

    async def cancel_all_orders(self) -> list[dict]:
        """DELETE /orders cancels all open orders; returns list of cancelled order IDs."""
        log.info(f"[alpaca:{self.account_label}] Cancelling all open orders")
        result = await self.client.delete("/orders")
        return result if isinstance(result, list) else []

    async def get_orders(self, status: str = "all") -> list[dict]:
        """
        status: open | closed | all
        Alpaca uses: open | closed | all
        """
        alpaca_status = status if status in ("open", "closed", "all") else "all"
        result = await self.client.get("/orders", params={"status": alpaca_status, "limit": 500})
        return result if isinstance(result, list) else []

    async def get_order(self, order_id: str) -> dict:
        return await self.client.get(f"/orders/{order_id}")
