"""
Webull Orders (Official Developer API)
Place, cancel, and query orders for a single Webull account.
"""
import logging
import uuid
from typing import Optional

from .client import WebullClient

log = logging.getLogger(__name__)

_SIDE_MAP = {
    "buy":          "BUY",
    "sell":         "SELL",
    "sell_short":   "SELL_SHORT",
    "buy_to_cover": "BUY_TO_COVER",
}

_TYPE_MAP = {
    "market":     "MKT",
    "limit":      "LMT",
    "stop":       "STP",
    "stop_limit": "STP_LMT",
}

_DURATION_MAP = {
    "day": "DAY",
    "gtc": "GTC",
    "pre": "DAY",
    "post": "DAY",
}

_OPTION_SIDE_MAP = {
    "buy_to_open":   "BUY_TO_OPEN",
    "sell_to_open":  "SELL_TO_OPEN",
    "buy_to_close":  "BUY_TO_CLOSE",
    "sell_to_close": "SELL_TO_CLOSE",
}


class WebullOrders:

    def __init__(self, client: WebullClient, account_id: str, account_label: str, mode: str):
        self.client        = client
        self.account_id    = account_id
        self.account_label = account_label
        self.mode          = mode

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
        log.info(
            f"[webull:{self.account_label}] Order: {side} {quantity} {symbol} "
            f"@ {order_type} {price or ''}"
        )
        body: dict = {
            "account_id":   self.account_id,
            "symbol":       symbol.upper(),
            "side":         _SIDE_MAP.get(side, side.upper()),
            "tif":          _DURATION_MAP.get(duration, "DAY"),
            "order_type":   _TYPE_MAP.get(order_type, "MKT"),
            "qty":          str(quantity),
            "client_order_id": str(uuid.uuid4()),
        }
        if price is not None:
            body["limit_price"] = str(price)
        if stop is not None:
            body["stop_price"] = str(stop)
        if tag:
            body["remark"] = tag
        return await self.client.post("/trade/order/place", body=body)

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
        body: dict = {
            "account_id":      self.account_id,
            "symbol":          option_symbol.upper(),
            "side":            _OPTION_SIDE_MAP.get(side, side.upper()),
            "tif":             _DURATION_MAP.get(duration, "DAY"),
            "order_type":      _TYPE_MAP.get(order_type, "MKT"),
            "qty":             str(quantity),
            "client_order_id": str(uuid.uuid4()),
        }
        if price is not None:
            body["limit_price"] = str(price)
        if tag:
            body["remark"] = tag
        log.info(f"[webull:{self.account_label}] Option: {side} {quantity}x {option_symbol}")
        return await self.client.post("/trade/order/place", body=body)

    async def cancel_order(self, order_id: str) -> dict:
        log.info(f"[webull:{self.account_label}] Cancelling {order_id}")
        return await self.client.post("/trade/order/cancel", body={
            "account_id": self.account_id,
            "order_id":   order_id,
        })

    async def cancel_all_orders(self) -> list[dict]:
        orders = await self.get_orders(status="open")
        results = []
        for o in orders:
            oid = str(o.get("order_id", o.get("id", "")))
            if oid:
                try:
                    results.append(await self.cancel_order(oid))
                except Exception as e:
                    log.error(f"[webull:{self.account_label}] Cancel {oid} failed: {e}")
        log.info(f"[webull:{self.account_label}] Cancelled {len(results)} orders")
        return results

    async def get_orders(self, status: str = "all") -> list[dict]:
        params: dict = {"account_id": self.account_id, "page_size": 100}
        result = await self.client.get("/trade/order/list", params=params)
        items = result.get("items", result.get("data", []))
        if isinstance(result, list):
            items = result
        if status == "open":
            items = [o for o in items if o.get("status") in ("WORKING", "PARTIAL_FILLED")]
        elif status == "filled":
            items = [o for o in items if o.get("status") == "FILLED"]
        return items
