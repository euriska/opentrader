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

    async def get_option_chain(self, symbol: str) -> dict:
        """
        Fetch options chain via Alpaca's v1beta1 options snapshots API.
        Returns up to 8 nearest expirations with full Greeks.
        """
        import asyncio as _asyncio
        from datetime import date as _date, timedelta as _td
        sym = symbol.upper()

        # ── Current quote ─────────────────────────────────────────────────────
        price = 0.0
        try:
            q = await self._client.get_quote(sym) if hasattr(self._client, "get_quote") else {}
            price = float(q.get("last") or q.get("ask") or 0)
        except Exception:
            pass
        if not price:
            try:
                q = await _market.get_quote(sym)
                price = float(q.get("last") or q.get("ask") or 0)
            except Exception:
                pass

        # ── Snapshots (all contracts, paginated) ──────────────────────────────
        DATA_V1B1 = "https://data.alpaca.markets/v1beta1"
        today_str = _date.today().isoformat()

        snapshots: dict = {}
        page_token: str | None = None
        for _ in range(20):   # max 20 pages
            params: dict = {
                "underlying_symbols": sym,
                "expiration_date_gte": today_str,
                "limit": 250,
                "feed": "indicative",
            }
            if page_token:
                params["page_token"] = page_token
            try:
                resp = await self._client._request(
                    "GET", f"/options/snapshots/{sym}",
                    params=params, base=DATA_V1B1,
                )
            except Exception:
                break
            snaps = resp.get("snapshots", {})
            snapshots.update(snaps)
            page_token = resp.get("next_page_token")
            if not page_token:
                break

        if not snapshots:
            return {"ticker": sym, "price": round(price, 2),
                    "expirations": [], "calls": [], "puts": []}

        all_calls, all_puts = [], []
        exp_set: set[str] = set()

        for contract_sym, snap in snapshots.items():
            details = snap.get("details") or snap.get("detail") or {}
            otype   = str(details.get("type") or details.get("optionType") or "").lower()
            if otype not in ("call", "put"):
                continue

            strike   = float(details.get("strikePrice") or details.get("strike_price") or 0)
            exp_date = str(details.get("expirationDate") or details.get("expiration_date") or "")[:10]
            exp_set.add(exp_date)

            q_snap  = snap.get("latestQuote") or {}
            t_snap  = snap.get("latestTrade") or {}
            greeks  = snap.get("greeks") or {}
            iv      = snap.get("impliedVolatility")

            bid   = float(q_snap.get("bp") or 0)
            ask   = float(q_snap.get("ap") or 0)
            last  = float(t_snap.get("p") or 0)
            mid   = round((bid + ask) / 2, 2) if bid and ask else last
            intrinsic = round(max(0.0, price - strike) if otype == "call"
                              else max(0.0, strike - price), 2)

            def _g(k): return round(float(greeks[k]), 6) if greeks.get(k) is not None else None

            rec = {
                "contract":   contract_sym,
                "strike":     strike,
                "expiration": exp_date,
                "bid": bid, "ask": ask, "mid": mid, "last": last,
                "intrinsic":  intrinsic,
                "extrinsic":  round(max(0.0, mid - intrinsic), 2),
                "iv":    round(float(iv), 4) if iv is not None else None,
                "delta": _g("delta"), "gamma": _g("gamma"),
                "theta": _g("theta"), "vega":  _g("vega"),
                "volume": int(snap.get("dailyBar", {}).get("v", 0)),
                "oi":     0,
                "itm":    (otype == "call" and price > strike) or
                          (otype == "put"  and price < strike),
            }
            (all_calls if otype == "call" else all_puts).append(rec)

        expirations = sorted(exp_set)[:8]
        return {
            "ticker": sym, "price": round(price, 2),
            "expirations": expirations,
            "calls": all_calls, "puts": all_puts,
        }
