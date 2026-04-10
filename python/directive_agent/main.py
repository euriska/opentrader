"""
OpenTrader Directive Agent

Executes pre-validated trade directives stored at trade:directives in Redis.
Directives are evaluated every 5 minutes. Each directive carries a parsed_action
(direction, quantity, order_type, etc.) and a condition. The agent resolves the
target account(s) from the directive text, checks any timing condition, and
routes orders through the Broker Gateway — no LLM re-evaluation.

Directives are GTC (good-till-cancelled) by default — they remain active until
executed, cancelled, or explicitly deleted by the user.
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, REDIS_URL
from notifier.agentmail import Notifier
from scheduler.calendar import is_market_open, is_trading_day

log = structlog.get_logger("directive-agent")

DIRECTIVES_KEY     = "trade:directives"
TRADE_MODE_DEFAULT = os.getenv("TRADE_MODE", "sandbox")
GATEWAY_TIMEOUT    = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "15"))
CHECK_INTERVAL_SEC = int(os.getenv("DIRECTIVE_CHECK_INTERVAL_SEC", "300"))  # 5 min
SANDBOX_IGNORE_HOURS = os.getenv("SANDBOX_IGNORE_HOURS", "true").lower() == "true"
ASSIGNMENTS_PATH   = os.getenv("ASSIGNMENTS_PATH", "/app/config/assignments.json")

# ── Account resolution ────────────────────────────────────────────────────────

def _load_all_accounts() -> list[dict]:
    """Return all active accounts from assignments.json."""
    try:
        with open(ASSIGNMENTS_PATH) as f:
            records = json.load(f)
        return [r for r in records if r.get("status") == "active"]
    except Exception as e:
        log.warning("directive-agent.assignments_load_failed", error=str(e))
        return []


def _resolve_accounts(directive_text: str) -> list[dict]:
    """
    Resolve target broker accounts from natural-language directive text.

    Rules (case-insensitive, checked in order):
      - "all" + ("paper" or "sandbox")  → every active account
      - broker keyword ("alpaca", "webull", "tradier") + optional mode keyword
        ("paper", "sandbox", "live") → filter matching accounts
      - no recognisable account reference → every active account
    """
    text  = directive_text.lower()
    all_accounts = _load_all_accounts()

    # "all paper/sandbox accounts" or "all broker accounts"
    if re.search(r"\ball\b", text) and re.search(r"\b(paper|sandbox|broker account)\b", text):
        return all_accounts

    # Broker-specific matching
    broker_map = {
        "alpaca":   "alpaca",
        "webull":   "webull",
        "tradier":  "tradier",
    }
    mode_map = {
        "paper":    "paper",
        "sandbox":  "sandbox",
        "live":     "live",
    }

    matched_broker = next((v for k, v in broker_map.items() if k in text), None)
    matched_mode   = next((v for k, v in mode_map.items() if k in text), None)

    if matched_broker or matched_mode:
        results = []
        for acct in all_accounts:
            broker_ok = (matched_broker is None) or (acct.get("broker") == matched_broker)
            mode_ok   = (matched_mode   is None) or (acct.get("mode")   == matched_mode)
            if broker_ok and mode_ok:
                results.append(acct)
        if results:
            return results

    # No account reference found — use all active accounts
    return all_accounts


# ── Timing condition ──────────────────────────────────────────────────────────

def _condition_is_ready(condition: str) -> bool:
    """
    Return True if this directive's condition is satisfied right now.

    Handles:
      - "immediate execution" / empty / None → always ready
      - "Execute on MM/DD/YYYY at HH:MM [AM|PM]" → ready once that UTC-naive
        wall-clock time has passed (treated as US/Eastern via a +5 h offset)
    """
    if not condition or re.search(r"\bimmediate\b", condition, re.I):
        return True

    # Try to parse a date+time from the condition string
    patterns = [
        # "Execute on 4/8/2026 at 10:00 AM"
        r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)",
        # "4/8/2026 10:00 AM"
        r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)",
    ]
    for pat in patterns:
        m = re.search(pat, condition, re.I)
        if m:
            try:
                dt_str = f"{m.group(1)} {m.group(2).strip()}"
                fmt = "%m/%d/%Y %I:%M %p" if re.search(r"AM|PM", dt_str, re.I) else "%m/%d/%Y %H:%M"
                naive = datetime.strptime(dt_str, fmt)
                # Treat directive times as US Eastern (UTC-5 standard, UTC-4 DST)
                # We use a simple fixed offset of UTC-4 (EDT) during spring/summer
                from datetime import timedelta
                eastern_offset = timedelta(hours=4)
                target_utc = naive.replace(tzinfo=timezone.utc) + eastern_offset
                return datetime.now(timezone.utc) >= target_utc
            except Exception:
                pass

    # Unrecognised condition format — execute (directive was pre-validated)
    log.warning("directive-agent.condition_unparseable", condition=condition)
    return True


# ── Main agent ────────────────────────────────────────────────────────────────

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
                executed, result = await self._execute_directive(directive)
                if executed:
                    directive["status"]      = "executed"
                    directive["executed_at"] = datetime.now(timezone.utc).isoformat()
                    directive["result"]      = result
                    changed = True
                    await self.notifier.directive_executed(directive["text"], result)
                    log.info("directive-agent.executed",
                             id=directive["id"], result=result[:200])
                else:
                    log.debug("directive-agent.skipped",
                              id=directive["id"], reason=result)
            except Exception as e:
                log.error("directive-agent.eval_error",
                          id=directive.get("id"), error=str(e))

        if changed:
            try:
                await self.redis.set(DIRECTIVES_KEY, json.dumps(directives))
            except Exception as e:
                log.error("directive-agent.save_failed", error=str(e))

    async def _execute_directive(self, directive: dict) -> tuple[bool, str]:
        """
        Execute a directive directly from its parsed_action.
        Returns (executed, result_summary).
        """
        text        = directive.get("text", "")
        parsed      = directive.get("parsed_action") or {}
        tickers     = directive.get("tickers") or []

        if not tickers:
            return False, "No tickers in directive"

        condition = parsed.get("condition", "")
        if not _condition_is_ready(condition):
            return False, f"Condition not yet met: {condition}"

        direction   = parsed.get("direction", "long")
        quantity    = parsed.get("quantity")       # may be None (sell-all)
        dollars     = parsed.get("dollars")
        order_type  = parsed.get("order_type", "market")
        limit_price = parsed.get("limit_price")

        side = "buy" if direction == "long" else (
               "sell_short" if direction == "short" else "sell")

        accounts = _resolve_accounts(text)
        if not accounts:
            return False, "No active trading accounts matched directive"

        trade_mode = await self._trade_mode()

        executed_pairs: list[str] = []
        failed_pairs:   list[str] = []

        for account in accounts:
            account_label = account["account_label"]

            for ticker in tickers:
                # Determine quantity
                ticker_qty = quantity
                if ticker_qty is None and dollars:
                    price = await self._get_price(ticker, account_label, trade_mode)
                    if price and price > 0:
                        ticker_qty = max(1, int(dollars / price))
                    else:
                        ticker_qty = 1

                if ticker_qty is None and side == "sell":
                    # Sell-all: fetch position size
                    ticker_qty = await self._get_position_qty(
                        ticker, account_label, trade_mode)
                    if ticker_qty is None or ticker_qty <= 0:
                        log.info("directive-agent.no_position",
                                 ticker=ticker, account=account_label)
                        continue

                ticker_qty = max(1, int(ticker_qty or 1))

                request_id = str(uuid.uuid4())
                cmd: dict = {
                    "command":       "place_order",
                    "request_id":    request_id,
                    "asset_class":   "equity",
                    "account_label": account_label,
                    "symbol":        ticker,
                    "side":          side,
                    "quantity":      str(ticker_qty),
                    "order_type":    order_type,
                    "duration":      parsed.get("duration", "day"),
                    "strategy_tag":  "directive",
                    "tag":           f"dir-{ticker}-{request_id[:8]}",
                    "issued_by":     "directive-agent",
                    "mode":          trade_mode,
                }
                if order_type == "limit" and limit_price:
                    cmd["price"] = str(limit_price)

                log.info("directive-agent.placing_order",
                         ticker=ticker, side=side, qty=ticker_qty,
                         account=account_label, order_type=order_type)

                await self.redis.xadd(STREAMS["broker_commands"], cmd, maxlen=10_000)
                reply_raw = await self.redis.blpop(
                    f"broker:reply:{request_id}", timeout=GATEWAY_TIMEOUT)

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
                        log.info("directive-agent.order_placed",
                                 ticker=ticker, account=account_label,
                                 data=str(r.get("data", {}))[:200])
                    else:
                        log.error("directive-agent.order_failed",
                                  ticker=ticker, account=account_label,
                                  error=r.get("error", ""))
                        failed_pairs.append(f"{ticker}@{account_label}")
                except Exception as e:
                    log.error("directive-agent.reply_parse_error", error=str(e))
                    failed_pairs.append(f"{ticker}@{account_label}")

        if not executed_pairs and not failed_pairs:
            return False, "No positions found to sell"

        if not executed_pairs:
            return False, f"Order placement failed: {', '.join(failed_pairs[:5])}"

        executed_tickers  = sorted({p.split("@")[0] for p in executed_pairs})
        executed_accounts = sorted({p.split("@")[1] for p in executed_pairs})
        summary = (
            f"Executed: {', '.join(executed_tickers)} {direction.upper()} "
            f"via {', '.join(executed_accounts)}"
        )
        if failed_pairs:
            summary += f" (failed: {', '.join(failed_pairs[:5])})"
        return True, summary

    async def _get_price(self, ticker: str, account_label: str,
                         trade_mode: str) -> Optional[float]:
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":       "get_quote",
                    "request_id":    request_id,
                    "symbol":        ticker,
                    "account_label": account_label,
                    "issued_by":     "directive-agent",
                },
                maxlen=10_000,
            )
            reply_raw = await self.redis.blpop(
                f"broker:reply:{request_id}", timeout=10)
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

    async def _get_position_qty(self, ticker: str, account_label: str,
                                trade_mode: str) -> Optional[int]:
        """Fetch the current held quantity for ticker in a specific account."""
        try:
            request_id = str(uuid.uuid4())
            await self.redis.xadd(
                STREAMS["broker_commands"],
                {
                    "command":       "get_positions",
                    "request_id":    request_id,
                    "account_label": account_label,
                    "issued_by":     "directive-agent",
                },
                maxlen=10_000,
            )
            reply_raw = await self.redis.blpop(
                f"broker:reply:{request_id}", timeout=GATEWAY_TIMEOUT)
            if reply_raw is None:
                return None
            _, reply_json = reply_raw
            r = json.loads(reply_json)
            if isinstance(r, list):
                r = r[0]
            positions = r.get("data", {}).get("items", [])
            for pos in positions:
                sym = pos.get("symbol", "").upper()
                if sym == ticker.upper():
                    qty = pos.get("quantity") or pos.get("qty") or pos.get("shares")
                    return int(float(qty)) if qty else None
            return 0  # not held
        except Exception as e:
            log.warning("directive-agent.positions_fetch_failed",
                        ticker=ticker, account=account_label, error=str(e))
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
