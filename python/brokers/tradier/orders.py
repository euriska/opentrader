"""
Tradier Orders
Place, modify, cancel, and query orders across all accounts.
Supports: equity, ETF, options (single-leg and spreads).
"""
import logging
from typing import Optional
from .accounts import TradierAccount

log = logging.getLogger(__name__)


class TradierOrders:
    """Order management for a single Tradier account."""

    def __init__(self, account: TradierAccount):
        self.account = account
        self.client  = account.client
        self.acct_id = account.id

    async def place_equity_order(
        self,
        symbol:     str,
        side:       str,          # buy | sell | buy_to_cover | sell_short
        quantity:   int,
        order_type: str = "market",  # market | limit | stop | stop_limit
        price:      Optional[float] = None,
        stop:       Optional[float] = None,
        duration:   str = "day",     # day | gtc | pre | post
        tag:        Optional[str] = None,
    ) -> dict:
        """Place an equity or ETF order."""
        data = {
            "class":    "equity",
            "symbol":   symbol,
            "side":     side,
            "quantity": str(quantity),
            "type":     order_type,
            "duration": duration,
        }
        if price:
            data["price"] = str(round(price, 2))
        if stop:
            data["stop"] = str(round(stop, 2))
        if tag:
            data["tag"] = tag

        log.info(
            f"[tradier:{self.account.label}] Order: {side} {quantity} {symbol} "
            f"@ {order_type} {price or ''}"
        )
        result = await self.client.post(
            f"/accounts/{self.acct_id}/orders", data=data
        )
        return result.get("order", result)

    async def place_option_order(
        self,
        symbol:         str,        # underlying (e.g. SPY)
        option_symbol:  str,        # OCC symbol (e.g. SPY240315C00520000)
        side:           str,        # buy_to_open | sell_to_open | buy_to_close | sell_to_close
        quantity:       int,
        order_type:     str = "market",
        price:          Optional[float] = None,
        duration:       str = "day",
        tag:            Optional[str] = None,
    ) -> dict:
        """Place a single-leg options order."""
        data = {
            "class":         "option",
            "symbol":        symbol,
            "option_symbol": option_symbol,
            "side":          side,
            "quantity":      str(quantity),
            "type":          order_type,
            "duration":      duration,
        }
        if price:
            data["price"] = str(round(price, 2))
        if tag:
            data["tag"] = tag

        log.info(
            f"[tradier:{self.account.label}] Option order: {side} {quantity}x {option_symbol}"
        )
        result = await self.client.post(
            f"/accounts/{self.acct_id}/orders", data=data
        )
        return result.get("order", result)

    async def cancel_order(self, order_id: str) -> dict:
        log.info(f"[tradier:{self.account.label}] Cancelling order {order_id}")
        return await self.client.delete(
            f"/accounts/{self.acct_id}/orders/{order_id}"
        )

    async def modify_order(
        self,
        order_id:   str,
        order_type: Optional[str] = None,
        price:      Optional[float] = None,
        stop:       Optional[float] = None,
        quantity:   Optional[int] = None,
        duration:   Optional[str] = None,
    ) -> dict:
        data = {}
        if order_type: data["type"]     = order_type
        if price:      data["price"]    = str(round(price, 2))
        if stop:       data["stop"]     = str(round(stop, 2))
        if quantity:   data["quantity"] = str(quantity)
        if duration:   data["duration"] = duration

        log.info(f"[tradier:{self.account.label}] Modifying order {order_id}: {data}")
        return await self.client.put(
            f"/accounts/{self.acct_id}/orders/{order_id}", data=data
        )

    async def get_orders(self, include_tags: bool = True) -> list:
        result = await self.client.get(
            f"/accounts/{self.acct_id}/orders",
            params={"includeTags": str(include_tags).lower()},
        )
        orders = result.get("orders", {})
        if orders == "null" or orders is None:
            return []
        raw = orders.get("order", [])
        return raw if isinstance(raw, list) else [raw]

    async def get_order(self, order_id: str) -> dict:
        result = await self.client.get(
            f"/accounts/{self.acct_id}/orders/{order_id}"
        )
        return result.get("order", result)

    async def cancel_all_open_orders(self) -> list:
        """Cancel all open orders — used by circuit breaker."""
        orders = await self.get_orders()
        open_orders = [o for o in orders if o.get("status") in ("open", "partially_filled")]
        results = []
        for order in open_orders:
            try:
                result = await self.cancel_order(str(order["id"]))
                results.append(result)
            except Exception as e:
                log.error(f"[tradier:{self.account.label}] Cancel failed for {order['id']}: {e}")
        log.info(
            f"[tradier:{self.account.label}] Cancelled {len(results)}/{len(open_orders)} open orders"
        )
        return results
