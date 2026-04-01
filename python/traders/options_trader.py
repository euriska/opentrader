"""
OpenTrader Options Trader
Consumes predictor.signals for options-class assets, confirms direction
with TradingView indicators, sizes contracts, and routes orders through
the Broker Gateway via broker.commands stream.
"""
import asyncio
import json
import os
import uuid
from typing import Optional

import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from shared.envelope import OrderEventPayload
from shared.mcp_client import get_tv_indicators, tv_confirms_direction
from scheduler.calendar import is_market_open, is_trading_day

log = structlog.get_logger("trader-options")

SIG_STREAM     = STREAMS["signals"]
ORD_STREAM     = STREAMS["orders"]
CONSUMER_GROUP = GROUPS["options"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "trader-options-0")

TRADE_MODE_DEFAULT   = os.getenv("TRADE_MODE", "sandbox")
MAX_CONTRACTS        = int(os.getenv("MAX_CONTRACTS", "1"))
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE_TRADE", "0.75"))
SANDBOX_IGNORE_HOURS = os.getenv("SANDBOX_IGNORE_HOURS", "true").lower() == "true"
GATEWAY_TIMEOUT      = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "15"))

# Options strategy defaults
DEFAULT_EXPIRY_DAYS = int(os.getenv("OPTIONS_EXPIRY_DAYS", "7"))   # DTE target
OTM_OFFSET_PCT      = float(os.getenv("OPTIONS_OTM_PCT", "2.0"))   # % OTM for strike


class OptionsTrader(BaseAgent):

    def __init__(self):
        super().__init__("trader-options")
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
        log.info("trader-options.starting", mode=TRADE_MODE_DEFAULT)

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
                log.warning("trader-options.group_create", error=str(e))

    async def _signal_loop(self):
        log.info("trader-options.signal_loop_start")
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
                log.error("trader-options.signal_loop_error", error=err)
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
            # Only options signals
            if asset_cls != "options":
                return

            if confidence < MIN_CONFIDENCE:
                log.debug("trader-options.below_threshold",
                          ticker=ticker, conf=confidence)
                return

            if ticker in self._positions_today:
                log.debug("trader-options.already_traded", ticker=ticker)
                return

            trade_mode = await self._trade_mode()
            in_sandbox = trade_mode == "sandbox"
            if not (in_sandbox and SANDBOX_IGNORE_HOURS):
                if not is_trading_day() or not is_market_open():
                    log.debug("trader-options.market_closed", ticker=ticker)
                    return

            # TradingView confirmation — veto if indicators contradict signal
            tv = await get_tv_indicators(ticker)
            if not tv_confirms_direction(tv, direction):
                log.info("trader-options.tv_veto",
                         ticker=ticker, direction=direction,
                         tv_rec=tv.get("recommendation") if tv else "unavailable")
                return
            if tv:
                log.info("trader-options.tv_confirmed",
                         ticker=ticker, direction=direction,
                         tv_rec=tv["recommendation"],
                         buy=tv["buy"], sell=tv["sell"])

            await self._place_option_order(ticker, direction, confidence, data)

        except Exception as e:
            log.error("trader-options.handle_signal_error",
                      ticker=ticker, error=str(e))
        finally:
            await self.redis.xack(SIG_STREAM, CONSUMER_GROUP, msg_id)

    async def _place_option_order(
        self,
        ticker:     str,
        direction:  str,
        confidence: float,
        data:       dict,
    ):
        trade_mode = await self._trade_mode()

        price = await self._get_price(ticker)
        if not price:
            log.warning("trader-options.no_price", ticker=ticker)
            return

        opt_type      = "call" if direction == "long" else "put"
        offset        = price * (OTM_OFFSET_PCT / 100)
        target_strike = round(price + offset if opt_type == "call" else price - offset, 2)

        contract_symbol = await self._resolve_contract(ticker, opt_type, target_strike)
        if not contract_symbol:
            log.warning("trader-options.no_contract",
                        ticker=ticker, opt_type=opt_type, strike=target_strike)
            return

        contracts = MAX_CONTRACTS
        order_id  = str(uuid.uuid4())
        acct, broker = await self._route_account(trade_mode)

        log.info("trader-options.placing_order",
                 ticker=ticker, contract=contract_symbol,
                 opt_type=opt_type, contracts=contracts,
                 account=acct, broker=broker, mode=trade_mode)

        await self.redis.xadd(
            STREAMS["broker_commands"],
            {
                "command":      "place_option_order",
                "request_id":   order_id,
                "symbol":       contract_symbol,
                "underlying":   ticker,
                "option_type":  opt_type,
                "contracts":    str(contracts),
                "order_type":   "market",
                "strategy_tag": "options_momentum",
                "mode":         trade_mode,
                "issued_by":    "trader-options",
            },
            maxlen=10_000,
        )

        self._positions_today.add(ticker)

        payload = OrderEventPayload(
            event_type  = "submitted",
            account_id  = acct,
            broker      = broker,
            mode        = trade_mode,
            ticker      = contract_symbol,
            asset_class = "options",
            direction   = direction,
            qty         = float(contracts),
            price       = None,
            order_id    = order_id,
            strategy    = "momentum_options",
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
                "price":       "",
                "order_id":    payload.order_id,
                "strategy":    payload.strategy,
            },
            maxlen=10_000,
        )
        log.info("trader-options.order_submitted",
                 ticker=ticker, contract=contract_symbol, order_id=order_id)

    async def _get_price(self, ticker: str) -> Optional[float]:
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":      "get_quote",
                    "request_id":   request_id,
                    "symbol":       ticker,
                    "strategy_tag": "options",
                    "mode":         await self._trade_mode(),
                    "issued_by":    "trader-options",
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
            log.warning("trader-options.quote_failed", ticker=ticker, error=str(e))
            return None

    async def _resolve_contract(
        self,
        ticker:        str,
        opt_type:      str,
        target_strike: float,
    ) -> Optional[str]:
        """Ask broker gateway for nearest options contract."""
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":       "get_option_contract",
                    "request_id":    request_id,
                    "symbol":        ticker,
                    "option_type":   opt_type,
                    "target_strike": str(target_strike),
                    "expiry_days":   str(DEFAULT_EXPIRY_DAYS),
                    "mode":          await self._trade_mode(),
                    "issued_by":     "trader-options",
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
            return r.get("data", {}).get("symbol")
        except Exception as e:
            log.warning("trader-options.contract_resolve_failed",
                        ticker=ticker, error=str(e))
            return None

    async def _route_account(self, trade_mode: str) -> tuple[str, str]:
        if trade_mode == "sandbox":
            return os.getenv("TRADIER_SANDBOX_ACCOUNT_NUMBER", "sandbox"), "tradier"
        acct1 = os.getenv("TRADIER_PROD_ACCOUNT_1", "")
        if acct1:
            return acct1, "tradier"
        return "unknown", "tradier"

    async def _midnight_reset(self):
        from scheduler.calendar import now_et
        while self._running:
            now  = now_et()
            secs = (24 * 3600) - (now.hour * 3600 + now.minute * 60 + now.second)
            await asyncio.sleep(secs + 1)
            self._positions_today.clear()
            log.info("trader-options.daily_reset")

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = OptionsTrader()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
