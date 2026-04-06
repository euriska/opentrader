"""
OpenTrader Equity Trader
Consumes predictor.signals, looks up active strategy assignments,
sizes positions using the assigned strategy's parameters, and routes
orders to the assigned accounts through the Broker Gateway agent via
Redis stream (broker.commands).

Trading strategy parameters (confidence threshold, max position size,
etc.) come exclusively from the Strategy Assignment workflow — there
are no embedded strategy values in this agent.
"""
import asyncio
import json
import math
import os
import uuid
from typing import Optional

import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from shared.envelope import OrderEventPayload
from shared.mcp_client import get_tv_indicators, tv_confirms_direction, get_avg_volume
from shared.exclusions import is_excluded
from shared.assignments import load_active_assignments
from shared.risk_controls import get_risk_controls, check_slippage, check_liquidity
from scheduler.calendar import is_market_open, is_trading_day

log = structlog.get_logger("trader-equity")

SIG_STREAM     = STREAMS["signals"]
ORD_STREAM     = STREAMS["orders"]
CONSUMER_GROUP = GROUPS["equity"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "trader-equity-0")

# Operational defaults — not strategy parameters
TRADE_MODE_DEFAULT   = os.getenv("TRADE_MODE", "sandbox")
SANDBOX_IGNORE_HOURS = os.getenv("SANDBOX_IGNORE_HOURS", "true").lower() == "true"
GATEWAY_TIMEOUT      = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "15"))


class EquityTrader(BaseAgent):

    def __init__(self):
        super().__init__("trader-equity")
        self._positions_today: set[str] = set()

    async def _trade_mode(self) -> str:
        try:
            stored = await self.redis.get("config:trade_mode")
            return stored if stored else TRADE_MODE_DEFAULT
        except Exception:
            return TRADE_MODE_DEFAULT

    async def run(self):
        await self.setup()

        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=15,
            retry_on_timeout=True,
            health_check_interval=30,
        )

        await self._ensure_consumer_group()
        log.info("trader-equity.starting", mode=TRADE_MODE_DEFAULT)

        await asyncio.gather(
            self.heartbeat_loop(),
            self._signal_loop(),
            self._midnight_reset(),
        )

    async def _ensure_consumer_group(self):
        try:
            await self.redis.xgroup_create(
                SIG_STREAM, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("trader-equity.group_create", error=str(e))

    async def _signal_loop(self):
        log.info("trader-equity.signal_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue

                messages = await self.redis.xreadgroup(
                    groupname    = CONSUMER_GROUP,
                    consumername = CONSUMER_NAME,
                    streams      = {SIG_STREAM: ">"},
                    count        = 5,
                    block        = 5000,
                )
                if not messages:
                    continue

                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_signal(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                err = str(e)
                log.error("trader-equity.signal_loop_error", error=err)
                if "NOGROUP" in err:
                    await self._ensure_consumer_group()
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_signal(self, msg_id: str, data: dict):
        ticker     = data.get("ticker", "")
        direction  = data.get("direction", "long")
        confidence = float(data.get("confidence", 0.0))
        asset_cls  = data.get("asset_class", "equity")

        try:
            if asset_cls not in ("equity", "etf"):
                return

            if ticker in self._positions_today:
                log.debug("trader-equity.already_traded", ticker=ticker)
                return

            # Resolve active assignments whose strategy covers this asset class.
            # Strategy parameters (min confidence, max position) come from here.
            assignments = load_active_assignments(asset_cls)
            if not assignments:
                log.debug("trader-equity.no_assignments",
                          ticker=ticker, asset_class=asset_cls)
                return

            trade_mode = await self._trade_mode()
            in_sandbox = trade_mode == "sandbox"
            # Always enforce trading day (Alpaca paper rejects on weekends/holidays)
            if not is_trading_day():
                log.debug("trader-equity.not_trading_day", ticker=ticker)
                return
            # Sandbox mode can optionally bypass intraday hours check
            if not (in_sandbox and SANDBOX_IGNORE_HOURS):
                if not is_market_open():
                    log.debug("trader-equity.market_closed", ticker=ticker)
                    return

            # Sector / ticker / industry exclusion check
            if await is_excluded(self.redis, ticker):
                return

            # TradingView confirmation — veto if indicators contradict signal
            tv = await get_tv_indicators(ticker)
            if not tv_confirms_direction(tv, direction):
                log.info("trader-equity.tv_veto",
                         ticker=ticker, direction=direction,
                         tv_rec=tv.get("recommendation") if tv else "unavailable")
                return
            if tv:
                log.info("trader-equity.tv_confirmed",
                         ticker=ticker, direction=direction,
                         tv_rec=tv["recommendation"],
                         buy=tv["buy"], sell=tv["sell"])

            # Place an order for each assigned account, using that
            # assignment's strategy parameters
            for assignment in assignments:
                if confidence < assignment["min_confidence"]:
                    log.debug("trader-equity.below_threshold",
                              ticker=ticker, conf=confidence,
                              required=assignment["min_confidence"],
                              strategy=assignment["strategy_name"])
                    continue
                await self._place_order(
                    ticker, direction, confidence, asset_cls, data, assignment
                )

        except Exception as e:
            log.error("trader-equity.handle_signal_error",
                      ticker=ticker, error=str(e))
        finally:
            await self.redis.xack(SIG_STREAM, CONSUMER_GROUP, msg_id)

    async def _place_order(
        self,
        ticker:     str,
        direction:  str,
        confidence: float,
        asset_cls:  str,
        raw_data:   dict,
        assignment: dict,
    ):
        account_label = assignment["account_label"]
        strategy_name = assignment["strategy_name"]
        max_pos_usd   = assignment["max_pos_usd"]

        # ── 1. Get quote for position sizing + risk controls ─────────────────
        trade_mode = await self._trade_mode()
        quote = await self._get_quote(ticker, trade_mode)
        price = quote.get("last") or quote.get("ask") or quote.get("bid")
        if price is None:
            log.warning("trader-equity.no_quote", ticker=ticker)
            price = 0.0
        else:
            price = float(price)

        # ── 1a. Risk controls — slippage + liquidity ──────────────────────────
        controls = await get_risk_controls(self.redis)
        ok, spread = check_slippage(quote.get("bid"), quote.get("ask"), controls["max_slippage_pct"])
        if not ok:
            log.info("trader-equity.slippage_blocked",
                     ticker=ticker, spread_pct=spread,
                     max_pct=controls["max_slippage_pct"])
            return
        if controls["min_volume_k"] > 0:
            avg_vol = await get_avg_volume(ticker)
            vol_ok, vol_k = check_liquidity(avg_vol, controls["min_volume_k"])
            if not vol_ok:
                log.info("trader-equity.liquidity_blocked",
                         ticker=ticker, vol_k=vol_k,
                         min_k=controls["min_volume_k"])
                return

        # ── 2. Size position using the assigned strategy's max position ───────
        qty = self._size_position(price, confidence, max_pos_usd)
        if qty < 1:
            log.info("trader-equity.qty_too_small",
                     ticker=ticker, price=price, max_pos=max_pos_usd)
            return

        side = "buy" if direction == "long" else "sell_short"

        log.info("trader-equity.placing",
                 ticker=ticker, side=side, qty=qty,
                 price=price, confidence=confidence,
                 account=account_label, strategy=strategy_name,
                 mode=trade_mode)

        # ── 3. Send to broker gateway — route to the assigned account ─────────
        request_id = str(uuid.uuid4())
        cmd = {
            "command":       "place_order",
            "request_id":    request_id,
            "asset_class":   "equity",
            "account_label": account_label,
            "symbol":        ticker,
            "side":          side,
            "quantity":      str(qty),
            "order_type":    "market",
            "duration":      "day",
            "strategy_tag":  strategy_name,
            "tag":           f"ot-{ticker}-{direction[:1]}",
            "issued_by":     "trader-equity",
        }
        await self.redis.xadd(STREAMS["broker_commands"], cmd, maxlen=10_000)

        # ── 4. Wait for gateway reply ─────────────────────────────────────────
        reply_raw = await self.redis.blpop(
            f"broker:reply:{request_id}", timeout=GATEWAY_TIMEOUT
        )
        if reply_raw is None:
            log.warning("trader-equity.gateway_timeout",
                        ticker=ticker, request_id=request_id)
            return

        _, reply_json = reply_raw
        try:
            results = json.loads(reply_json)
        except Exception:
            log.error("trader-equity.reply_parse_error", raw=reply_json[:200])
            return

        if not isinstance(results, list):
            results = [results]

        if not results:
            log.warning("trader-equity.no_accounts_matched",
                        ticker=ticker, account=account_label)
            return

        self._positions_today.add(ticker)

        # ── 5. Publish order events ───────────────────────────���───────────────
        for r in results:
            acct   = r.get("account_label", account_label)
            broker = r.get("broker", assignment["broker"])
            mode   = r.get("mode", trade_mode)

            if r.get("status") == "error":
                reject_reason = r.get("error", "gateway error")
                log.warning("trader-equity.order_rejected",
                            ticker=ticker, error=reject_reason)
                await self.redis.xadd(
                    ORD_STREAM,
                    {
                        "event_type":    "reject",
                        "account_id":    acct,
                        "broker":        broker,
                        "mode":          mode,
                        "ticker":        ticker,
                        "asset_class":   asset_cls,
                        "direction":     direction,
                        "qty":           str(qty),
                        "price":         str(price or ""),
                        "order_id":      "",
                        "strategy":      strategy_name,
                        "reject_reason": reject_reason,
                    },
                    maxlen=10_000,
                )
                continue

            data     = r.get("data", {})
            order_id = str(data.get("id", data.get("orderId", "")))
            status   = data.get("status", "ok")
            event_type = "fill" if status in ("ok", "filled", "open", "accepted", "pending_new", "new") else "reject"
            reject_reason = "" if event_type == "fill" else (
                data.get("reject_reason") or data.get("reason") or status
            )

            payload = OrderEventPayload(
                event_type  = event_type,
                account_id  = acct,
                broker      = broker,
                mode        = mode,
                ticker      = ticker,
                asset_class = asset_cls,
                direction   = direction,
                qty         = float(qty),
                price       = price or None,
                order_id    = order_id,
                strategy    = strategy_name,
            )

            await self.redis.xadd(
                ORD_STREAM,
                {
                    "event_type":    payload.event_type,
                    "account_id":    payload.account_id,
                    "broker":        payload.broker,
                    "mode":          payload.mode,
                    "ticker":        payload.ticker,
                    "asset_class":   payload.asset_class,
                    "direction":     payload.direction,
                    "qty":           str(payload.qty),
                    "price":         str(payload.price or ""),
                    "order_id":      payload.order_id,
                    "strategy":      payload.strategy,
                    "reject_reason": reject_reason,
                },
                maxlen=10_000,
            )

            log.info("trader-equity.order_event",
                     ticker=ticker, account=acct, broker=broker,
                     strategy=strategy_name, evt=event_type, order_id=order_id)

    async def _get_quote(self, ticker: str, trade_mode: str) -> dict:
        """Fetch full quote (last, bid, ask) via broker gateway. Returns {} on failure."""
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":    "get_quote",
                    "request_id": request_id,
                    "symbol":     ticker,
                    "mode":       trade_mode if trade_mode != "all" else "",
                    "issued_by":  "trader-equity",
                },
                maxlen=10_000,
            )
            reply_raw = await self.redis.blpop(
                f"broker:reply:{request_id}", timeout=10
            )
            if reply_raw is None:
                return {}
            _, reply_json = reply_raw
            r = json.loads(reply_json)
            if isinstance(r, list):
                r = r[0]
            data = r.get("data", {})
            def _f(v):
                try:
                    return float(v) if v else None
                except Exception:
                    return None
            return {
                "last": _f(data.get("last")),
                "bid":  _f(data.get("bid")),
                "ask":  _f(data.get("ask")),
            }
        except Exception as e:
            log.warning("trader-equity.quote_failed", ticker=ticker, error=str(e))
            return {}

    async def _get_price(self, ticker: str, trade_mode: str) -> Optional[float]:
        """Fetch latest price via broker gateway. Returns None on failure."""
        q = await self._get_quote(ticker, trade_mode)
        p = q.get("last") or q.get("ask") or q.get("bid")
        return float(p) if p else None

    def _size_position(self, price: float, confidence: float, max_pos_usd: float) -> int:
        """
        Size position using the assigned strategy's max_pos_usd and signal confidence.
        Higher confidence → larger position (up to max).
        """
        if price <= 0:
            return 1
        dollars = max_pos_usd * confidence
        qty = math.floor(dollars / price)
        return max(qty, 1)

    async def _midnight_reset(self):
        """Reset today's position tracker at midnight ET."""
        from scheduler.calendar import now_et
        while self._running:
            now  = now_et()
            secs = (24 * 3600) - (now.hour * 3600 + now.minute * 60 + now.second)
            await asyncio.sleep(secs + 1)
            self._positions_today.clear()
            log.info("trader-equity.daily_reset")

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = EquityTrader()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
