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

    async def get_option_chain(self, symbol: str) -> dict:
        import asyncio as _asyncio
        sym = symbol.upper()

        # ── Expiration dates ──────────────────────────────────────────────────
        expirations: list[str] = []
        try:
            raw = await self._client.get(
                "/quotes/option/queryExpireDates",
                params={"symbol": sym},
            )
            dates = raw if isinstance(raw, list) else raw.get("expireDateList", raw.get("data", []))
            expirations = [str(d) for d in dates if d][:8]
        except Exception as e:
            log.warning(f"[webull] option expirations for {sym}: {e}")

        # ── Current quote ─────────────────────────────────────────────────────
        price = 0.0
        try:
            q = await self._client.get(
                "/quotes/ticker/getTickerRealTime",
                params={"symbol": sym},
            )
            price = float(q.get("close") or q.get("pPrice") or 0)
        except Exception:
            pass

        if not expirations:
            return {"ticker": sym, "price": round(price, 2),
                    "expirations": [], "calls": [], "puts": []}

        # ── Chain per expiry ──────────────────────────────────────────────────
        async def _fetch_exp(exp: str):
            try:
                raw = await self._client.get(
                    "/quotes/option/queryOptionByExpireDate",
                    params={"symbol": sym, "expireDate": exp},
                )
                contracts = (raw if isinstance(raw, list)
                             else raw.get("data", raw.get("optionList", [])))
                return contracts if isinstance(contracts, list) else []
            except Exception:
                return []

        all_chains = await _asyncio.gather(*[_fetch_exp(e) for e in expirations])

        all_calls, all_puts = [], []
        for contracts in all_chains:
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                raw_type = str(
                    c.get("direction") or c.get("right") or
                    c.get("option_type") or c.get("optionType") or ""
                ).upper()
                otype = "call" if raw_type in ("CALL", "C") else ("put" if raw_type in ("PUT", "P") else "")
                if not otype:
                    continue

                strike = float(c.get("strikePrice") or c.get("strike_price") or c.get("strike") or 0)
                bid    = float(c.get("bidPrice") or c.get("bid") or 0)
                ask    = float(c.get("askPrice") or c.get("ask") or 0)
                last   = float(c.get("lastPrice") or c.get("close") or c.get("last") or 0)
                mid    = round((bid + ask) / 2, 2) if bid and ask else last
                intrinsic = round(max(0.0, price - strike) if otype == "call"
                                  else max(0.0, strike - price), 2)
                exp_date = (c.get("expireDate") or c.get("expiration_date") or c.get("expiryDate") or "")

                def _fg(k): return round(float(c[k]), 6) if c.get(k) is not None else None
                iv = c.get("impVol") or c.get("impliedVolatility") or c.get("iv")

                rec = {
                    "contract":   c.get("symbol") or c.get("tickerId") or "",
                    "strike":     strike,
                    "expiration": str(exp_date)[:10],
                    "bid": bid, "ask": ask, "mid": mid, "last": last,
                    "intrinsic":  intrinsic,
                    "extrinsic":  round(max(0.0, mid - intrinsic), 2),
                    "iv":    round(float(iv), 4) if iv is not None else None,
                    "delta": _fg("delta"), "gamma": _fg("gamma"),
                    "theta": _fg("theta"), "vega":  _fg("vega"),
                    "volume": int(c.get("volume") or 0),
                    "oi":     int(c.get("openInterest") or c.get("open_interest") or 0),
                    "itm":    (otype == "call" and price > strike) or
                              (otype == "put"  and price < strike),
                }
                (all_calls if otype == "call" else all_puts).append(rec)

        return {
            "ticker": sym, "price": round(price, 2),
            "expirations": expirations,
            "calls": all_calls, "puts": all_puts,
        }

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
