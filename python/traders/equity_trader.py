"""
OpenTrader Equity Trader
Consumes predictor.signals, sizes positions, routes orders through
the Broker Gateway agent via Redis stream (broker.commands), and
publishes OrderEventPayload to orders.events.

Sandbox-first: only routes to sandbox/paper accounts by default.
Flip TRADE_MODE=live to enable live accounts.
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
from shared.mcp_client import get_tv_indicators, tv_confirms_direction
from scheduler.calendar import is_market_open, is_trading_day

log = structlog.get_logger("trader-equity")

SIG_STREAM     = STREAMS["signals"]
ORD_STREAM     = STREAMS["orders"]
CONSUMER_GROUP = GROUPS["equity"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "trader-equity-0")

# "sandbox" | "live" | "all" — default from env, overridden at runtime via Redis
TRADE_MODE_DEFAULT = os.getenv("TRADE_MODE", "sandbox")
MAX_POS_USD     = float(os.getenv("MAX_POSITION_USD", "500"))
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE_TRADE", "0.70"))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "1.5"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
# Allow trading outside market hours in sandbox (for testing)
SANDBOX_IGNORE_HOURS = os.getenv("SANDBOX_IGNORE_HOURS", "true").lower() == "true"


GATEWAY_TIMEOUT = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "15"))


class EquityTrader(BaseAgent):

    def __init__(self):
        super().__init__("trader-equity")
        # Track tickers we've already entered today to avoid duplicates
        self._positions_today: set[str] = set()

    async def _trade_mode(self) -> str:
        """Return current trade mode — checks Redis first so UI changes take effect immediately."""
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
                    # Stream was deleted/recreated — rebuild consumer group
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
            # Only trade equity/etf here (options trader handles options)
            if asset_cls not in ("equity", "etf"):
                return

            if confidence < MIN_CONFIDENCE:
                log.debug("trader-equity.below_threshold",
                          ticker=ticker, conf=confidence)
                return

            if ticker in self._positions_today:
                log.debug("trader-equity.already_traded", ticker=ticker)
                return

            # Market hours check (skip for sandbox if configured)
            trade_mode = await self._trade_mode()
            in_sandbox = trade_mode == "sandbox"
            if not (in_sandbox and SANDBOX_IGNORE_HOURS):
                if not is_trading_day() or not is_market_open():
                    log.debug("trader-equity.market_closed", ticker=ticker)
                    return

            # TradingView confirmation — skip trade if indicators contradict signal
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

            await self._place_order(ticker, direction, confidence, asset_cls, data)

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
    ):
        # ── 1. Get quote for position sizing ─────────────────────────────────
        price = await self._get_price(ticker)
        if price is None:
            log.warning("trader-equity.no_quote", ticker=ticker)
            # Still proceed — market order will use current price
            price = 0.0

        # ── 2. Size position ──────────────────────────────────────────────────
        qty = self._size_position(price, confidence)
        if qty < 1:
            log.info("trader-equity.qty_too_small",
                     ticker=ticker, price=price, max_pos=MAX_POS_USD)
            return

        side = "buy" if direction == "long" else "sell_short"
        trade_mode = await self._trade_mode()

        log.info("trader-equity.placing",
                 ticker=ticker, side=side, qty=qty,
                 price=price, confidence=confidence,
                 mode=trade_mode)

        # ── 3. Send to broker gateway via Redis stream ────────────────────────
        request_id = str(uuid.uuid4())
        cmd = {
            "command":      "place_order",
            "request_id":   request_id,
            "asset_class":  "equity",
            "symbol":       ticker,
            "side":         side,
            "quantity":     str(qty),
            "order_type":   "market",
            "duration":     "day",
            "strategy_tag": "equity",
            "mode":         trade_mode if trade_mode != "all" else "",
            "tag":          f"ot-{ticker}-{direction[:1]}",
            "issued_by":    "trader-equity",
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
            log.warning("trader-equity.no_accounts_matched", ticker=ticker)
            return

        self._positions_today.add(ticker)

        # ── 5. Publish order events ───────────────────────────────────────────
        for r in results:
            if r.get("status") == "error":
                log.warning("trader-equity.order_rejected",
                            ticker=ticker, error=r.get("error"))
                continue

            acct     = r.get("account_label", "unknown")
            broker   = r.get("broker", "unknown")
            mode     = r.get("mode", await self._trade_mode())
            data     = r.get("data", {})
            order_id = str(data.get("id", data.get("orderId", "")))
            status   = data.get("status", "ok")
            event_type = "fill" if status in ("ok", "filled", "open", "accepted") else "reject"

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
                strategy    = "momentum_equity",
            )

            await self.redis.xadd(
                ORD_STREAM,
                {
                    "event_type":  payload.event_type,
                    "account_id":  payload.account_id,
                    "broker":      payload.broker,
                    "mode":        payload.mode,
                    "ticker":      payload.ticker,
                    "asset_class": payload.asset_class,
                    "direction":   payload.direction,
                    "qty":         str(payload.qty),
                    "price":       str(payload.price or ""),
                    "order_id":    payload.order_id,
                    "strategy":    payload.strategy,
                },
                maxlen=10_000,
            )

            log.info("trader-equity.order_event",
                     ticker=ticker, account=acct, broker=broker,
                     evt=event_type, order_id=order_id)

    async def _get_price(self, ticker: str) -> Optional[float]:
        """Fetch latest quote via broker gateway. Returns None on failure."""
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":      "get_quote",
                    "request_id":   request_id,
                    "symbol":       ticker,
                    "strategy_tag": "equity",
                    "mode":         (await self._trade_mode()) if (await self._trade_mode()) != "all" else "",
                    "issued_by":    "trader-equity",
                },
                maxlen=10_000,
            )
            reply_raw = await self.redis.blpop(
                f"broker:reply:{request_id}", timeout=10
            )
            if reply_raw is None:
                return None
            _, reply_json = reply_raw
            r = json.loads(reply_json)
            if isinstance(r, list):
                r = r[0]
            data = r.get("data", {})
            price = data.get("last") or data.get("ask") or data.get("bid")
            return float(price) if price else None
        except Exception as e:
            log.warning("trader-equity.quote_failed", ticker=ticker, error=str(e))
            return None

    def _size_position(self, price: float, confidence: float) -> int:
        """
        Size position based on max_position_usd and confidence.
        Higher confidence → larger position (up to max).
        """
        if price <= 0:
            # No price — default to 1 share
            return 1
        # Scale dollars by confidence (0.70 → 70% of max, 1.0 → 100%)
        dollars = MAX_POS_USD * confidence
        qty = math.floor(dollars / price)
        return max(qty, 1)

    async def _midnight_reset(self):
        """Reset today's position tracker at midnight ET."""
        from scheduler.calendar import now_et
        while self._running:
            now    = now_et()
            # Wait until next midnight ET
            secs   = (24 * 3600) - (now.hour * 3600 + now.minute * 60 + now.second)
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
