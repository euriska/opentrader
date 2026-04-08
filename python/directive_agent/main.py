"""
OpenTrader Directive Agent

Evaluates natural-language trade directives (stored at trade:directives in Redis)
every 5 minutes during market hours. Uses an LLM to determine whether each active
directive should be executed given current market conditions, then routes orders
through the Broker Gateway and sends notifications upon execution.

Directives are GTC (good-till-cancelled) by default — they remain active until
executed, cancelled, or explicitly deleted by the user.
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, REDIS_URL
from shared.mcp_client import get_tv_indicators
from shared.assignments import load_active_assignments
from notifier.agentmail import Notifier
from scheduler.calendar import is_market_open, is_trading_day

log = structlog.get_logger("directive-agent")

DIRECTIVES_KEY   = "trade:directives"
TRADE_MODE_DEFAULT = os.getenv("TRADE_MODE", "sandbox")
GATEWAY_TIMEOUT    = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "15"))
CHECK_INTERVAL_SEC = int(os.getenv("DIRECTIVE_CHECK_INTERVAL_SEC", "300"))  # 5 min
SANDBOX_IGNORE_HOURS = os.getenv("SANDBOX_IGNORE_HOURS", "true").lower() == "true"

_llm_key = os.getenv("OPENROUTER_API_KEY", "")
USE_LLM  = bool(_llm_key) and not _llm_key.startswith("your_")


class DirectiveAgent(BaseAgent):

    def __init__(self):
        super().__init__("directive-agent")
        self.notifier = Notifier("alerts")

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

        log.info("directive-agent.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._directive_loop(),
        )

    async def _directive_loop(self):
        log.info("directive-agent.loop_start")
        while self._running:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)

                if await self.is_halted():
                    continue

                if not is_trading_day():
                    continue

                trade_mode = await self._trade_mode()
                in_sandbox = trade_mode == "sandbox"
                if not (in_sandbox and SANDBOX_IGNORE_HOURS):
                    if not is_market_open():
                        continue

                await self._evaluate_directives()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("directive-agent.loop_error", error=str(e))
                await asyncio.sleep(30)

    async def _evaluate_directives(self):
        try:
            raw = await self.redis.get(DIRECTIVES_KEY)
            if not raw:
                return
            directives = json.loads(raw)
        except Exception as e:
            log.warning("directive-agent.load_failed", error=str(e))
            return

        active = [d for d in directives if d.get("status") == "active"]
        if not active:
            return

        log.info("directive-agent.evaluating", count=len(active))
        changed = False

        for directive in active:
            try:
                executed, result = await self._evaluate_one(directive)
                if executed:
                    directive["status"]      = "executed"
                    directive["executed_at"] = datetime.now(timezone.utc).isoformat()
                    directive["result"]      = result
                    changed = True
                    await self.notifier.directive_executed(directive["text"], result)
                    log.info("directive-agent.executed",
                             id=directive["id"], result=result[:200])
            except Exception as e:
                log.error("directive-agent.eval_error",
                          id=directive.get("id"), error=str(e))

        if changed:
            try:
                await self.redis.set(DIRECTIVES_KEY, json.dumps(directives))
            except Exception as e:
                log.error("directive-agent.save_failed", error=str(e))

    async def _evaluate_one(self, directive: dict) -> tuple[bool, str]:
        """
        Ask the LLM whether this directive should execute now.
        Returns (should_execute, result_summary).

        Supports multi-ticker directives: directive["tickers"] is the authoritative
        list of symbols. The LLM provides an action template (direction, quantity,
        order_type) applied to every ticker; a per-ticker override is used only when
        the directive contains a single ticker and no explicit list.
        """
        if not USE_LLM:
            return False, "LLM not configured — directive not evaluated"

        text = directive.get("text", "")
        # Use parsed tickers from directive creation (authoritative), cap at 10
        directive_tickers: list[str] = directive.get("tickers") or []

        # Gather live market context for each directive ticker
        market_context = ""
        for ticker in directive_tickers[:10]:
            try:
                tv = await get_tv_indicators(ticker)
                if tv:
                    price = await self._get_price(ticker)
                    market_context += (
                        f"\n{ticker}: price=${price or '?'}, "
                        f"TV={tv['recommendation']}, buy={tv['buy']}, sell={tv['sell']}"
                    )
            except Exception:
                pass

        # Get active assignments to know which accounts/modes to trade on
        assignments = load_active_assignments("equity") or []
        account_info = ""
        if assignments:
            account_info = f"Active trading accounts: {', '.join(a['account_label'] for a in assignments[:3])}"

        tickers_note = (
            f"Tickers involved: {', '.join(directive_tickers)}"
            if directive_tickers else ""
        )

        trade_mode = await self._trade_mode()

        prompt = f"""
You are evaluating a trade directive to determine if it should be executed right now.

Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Trade mode: {trade_mode}
{account_info}
{tickers_note}

Live market data:{market_context if market_context else ' (not available)'}

Trade directive:
"{text}"

Analyze whether conditions are met to execute this directive NOW.

The "action" object is a template applied to ALL tickers in the directive.
Only set "ticker" if the directive refers to exactly one symbol and none are
listed in "Tickers involved" above.

Respond with JSON only:
{{
  "should_execute": true | false,
  "reasoning": "brief explanation",
  "action": {{
    "ticker": "TICKER or null if multiple tickers",
    "direction": "long" | "sell" | "short",
    "quantity": <integer shares per ticker, or null for dollar-based>,
    "dollars": <dollar amount per ticker, or null>,
    "order_type": "market" | "limit",
    "limit_price": <price or null>
  }}
}}

Direction values:
- "long"  = buy (open or add to a long position)
- "sell"  = sell existing long position (close/reduce — NOT a short sale)
- "short" = sell short (open a short position)

If conditions are not met, set should_execute=false and action=null.
If the directive is ambiguous or cannot be safely executed, set should_execute=false.
"""
        system = (
            "You are an algorithmic trading assistant. Be conservative — only execute "
            "when conditions are clearly met. Never execute if the directive is unclear."
        )

        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_llm_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model":       os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-sonnet-4-5"),
                        "messages":    [
                            {"role": "system",  "content": system},
                            {"role": "user",    "content": prompt},
                        ],
                        "max_tokens":  500,
                        "temperature": 0.1,
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()

            reply = data["choices"][0]["message"]["content"]
            # Strip markdown code blocks if present
            reply = reply.strip()
            if reply.startswith("```"):
                reply = reply.split("```")[1]
                if reply.startswith("json"):
                    reply = reply[4:]
            result = json.loads(reply)
        except Exception as e:
            log.warning("directive-agent.llm_error", error=str(e))
            return False, f"LLM evaluation failed: {str(e)[:100]}"

        if not result.get("should_execute"):
            return False, result.get("reasoning", "Conditions not met")

        action = result.get("action") or {}
        direction  = action.get("direction", "long")
        quantity   = action.get("quantity")
        dollars    = action.get("dollars")
        order_type  = action.get("order_type", "market")
        limit_price = action.get("limit_price")

        # Resolve the full list of tickers to trade:
        # directive_tickers is authoritative; fall back to the LLM's single ticker.
        tickers_to_trade = directive_tickers or ([action.get("ticker")] if action.get("ticker") else [])
        if not tickers_to_trade:
            return False, "LLM returned should_execute=true but no ticker could be determined"

        # Place order via broker gateway for each ticker × each active account
        assignments = load_active_assignments("equity") or []
        if not assignments:
            return False, "No active trading accounts"

        executed_pairs: list[str] = []   # "TICKER@account"
        failed_pairs:   list[str] = []

        for ticker in tickers_to_trade:
            # Resolve quantity (may vary per ticker if dollar-based)
            ticker_qty = quantity
            if not ticker_qty and dollars:
                price = await self._get_price(ticker)
                if price and price > 0:
                    ticker_qty = max(1, int(dollars / price))
                else:
                    ticker_qty = 1
            ticker_qty = max(1, int(ticker_qty or 1))

            for assignment in assignments:
                account_label = assignment["account_label"]
                request_id    = str(uuid.uuid4())
                cmd = {
                    "command":       "place_order",
                    "request_id":    request_id,
                    "asset_class":   "equity",
                    "account_label": account_label,
                    "symbol":        ticker,
                    "side":          "buy" if direction == "long" else ("sell_short" if direction == "short" else "sell"),
                    "quantity":      str(ticker_qty),
                    "order_type":    order_type,
                    "duration":      "day",
                    "strategy_tag":  "directive",
                    "tag":           f"directive-{ticker}",
                    "issued_by":     "directive-agent",
                    "mode":          trade_mode,
                }
                if order_type == "limit" and limit_price:
                    cmd["limit_price"] = str(limit_price)

                await self.redis.xadd(STREAMS["broker_commands"], cmd, maxlen=10_000)
                reply_raw = await self.redis.blpop(
                    f"broker:reply:{request_id}", timeout=GATEWAY_TIMEOUT
                )
                if reply_raw is None:
                    log.warning("directive-agent.gateway_timeout",
                                ticker=ticker, account=account_label)
                    failed_pairs.append(f"{ticker}@{account_label}")
                    continue

                _, reply_json = reply_raw
                try:
                    r = json.loads(reply_json)
                    if isinstance(r, list):
                        r = r[0]
                    if r.get("status") != "error":
                        executed_pairs.append(f"{ticker}@{account_label}")
                    else:
                        failed_pairs.append(f"{ticker}@{account_label}")
                except Exception:
                    failed_pairs.append(f"{ticker}@{account_label}")

        if not executed_pairs:
            return False, f"Order placement failed for all tickers/accounts"

        reasoning = result.get("reasoning", "")
        executed_tickers = sorted({p.split("@")[0] for p in executed_pairs})
        executed_accounts = sorted({p.split("@")[1] for p in executed_pairs})
        summary = (
            f"Executed: {', '.join(executed_tickers)} {direction.upper()} "
            f"via {', '.join(executed_accounts)}. {reasoning}"
        )
        if failed_pairs:
            summary += f" (failed: {', '.join(failed_pairs[:5])})"
        return True, summary

    async def _get_price(self, ticker: str) -> Optional[float]:
        """Quick price lookup via broker gateway using current trade mode."""
        try:
            trade_mode = await self._trade_mode()
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":    "get_quote",
                    "request_id": request_id,
                    "symbol":     ticker,
                    "mode":       trade_mode if trade_mode != "all" else "",
                    "issued_by":  "directive-agent",
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
        except Exception:
            return None

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = DirectiveAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
