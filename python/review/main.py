"""
OpenTrader Review Agent
Two responsibilities:
  1. Trade Recorder  — consumes orders.events, writes to trades table
  2. EOD Reporter    — on trigger, pulls fills, runs LLM analysis, notifies
  3. Strategy Review — every 50 trades, deep analysis + param recommendations
"""
import asyncio
import json
import os
from datetime import datetime, date, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse, quote

import asyncpg
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from notifier.agentmail import Notifier
from brokers.tradier.gateway import TradierGateway
from scheduler.calendar import now_et

log = structlog.get_logger("review-agent")

ORD_STREAM    = STREAMS["orders"]
CMD_STREAM    = STREAMS["commands"]
REV_STREAM    = STREAMS["review"]
ORD_GROUP     = "review-orders-group"
CMD_GROUP     = GROUPS["review"]
CONSUMER_NAME = os.getenv("HOSTNAME", "review-0")

DB_URL           = os.getenv("DB_URL", "")
STRATEGY_REVIEW_THRESHOLD = int(os.getenv("STRATEGY_REVIEW_THRESHOLD", "50"))

_llm_key = os.getenv("OPENROUTER_API_KEY", "")
USE_LLM  = bool(_llm_key) and not _llm_key.startswith("your_")


def _safe_db_url(url: str) -> str:
    try:
        import re
        m = re.match(
            r'^(postgresql(?:\+\w+)?://)'
            r'([^:]+)'
            r':'
            r'(.+)'
            r'@([^@/]+)'
            r'(/.*)?$',
            url,
        )
        if not m:
            return url
        scheme, user, _, host_port, dbpath = m.groups()
        pw = url[len(scheme) + len(user) + 1 : url.rfind(f"@{host_port}")]
        return f"{scheme}{user}:{quote(pw, safe='')}@{host_port}{dbpath or ''}"
    except Exception:
        return url


class ReviewAgent(BaseAgent):

    def __init__(self):
        super().__init__("review-agent")
        self._db:      Optional[asyncpg.Connection] = None
        self.notifier  = Notifier("review")
        self.gateway   = TradierGateway()
        self._trades_since_review = 0

    async def run(self):
        await self.setup()

        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=15, retry_on_timeout=True,
            health_check_interval=30,
        )

        await self._connect_db()
        await self._ensure_groups()
        await self.notifier.ensure_inbox()

        log.info("review-agent.starting", llm=USE_LLM)

        await asyncio.gather(
            self.heartbeat_loop(),
            self._orders_loop(),
            self._commands_loop(),
        )

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _connect_db(self):
        if not DB_URL:
            log.warning("review-agent.no_db_url")
            return
        dsn = _safe_db_url(DB_URL)
        for attempt in range(1, 6):
            try:
                self._db = await asyncpg.connect(dsn)
                log.info("review-agent.db_connected")
                return
            except Exception as e:
                log.warning("review-agent.db_retry", attempt=attempt, error=str(e))
                await asyncio.sleep(5 * attempt)
        log.error("review-agent.db_failed")

    # ── Consumer groups ───────────────────────────────────────────────────────

    async def _ensure_groups(self):
        for stream, group in [
            (ORD_STREAM, ORD_GROUP),
            (CMD_STREAM, CMD_GROUP),
        ]:
            try:
                await self.redis.xgroup_create(stream, group, id="$", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    log.warning("review-agent.group_create", stream=stream, error=str(e))

    # ── Orders loop — records trades to DB ───────────────────────────────────

    async def _orders_loop(self):
        log.info("review-agent.orders_loop_start")
        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=ORD_GROUP, consumername=CONSUMER_NAME,
                    streams={ORD_STREAM: ">"}, count=20, block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._record_trade(msg_id, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err = str(e)
                if "NOGROUP" in err:
                    await self._ensure_groups()
                log.error("review-agent.orders_loop_error", error=err)
                wait = 10 if "loading" in err.lower() else 3
                await asyncio.sleep(wait)
                try:
                    await self.redis.ping()
                except Exception:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _record_trade(self, msg_id: str, data: dict):
        try:
            event_type = data.get("event_type", "fill")

            # Map broker-side values ("buy"/"sell") to DB-accepted values ("long"/"short")
            raw_dir = (data.get("direction") or "long").lower()
            if raw_dir in ("sell", "sell_short", "short"):
                direction = "short"
            else:
                direction = "long"

            # Don't write rejected orders to the trades table
            if event_type == "reject":
                await self.notifier.trade_reject(data)
            else:
                if self._db:
                    await self._db.execute(
                        """
                        INSERT INTO trades
                            (account_id, broker, mode, ticker, asset_class,
                             direction, qty, entry_price, signal_src, strategy, status)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        """,
                        data.get("account_id", ""),
                        data.get("broker", "tradier"),
                        data.get("mode", "sandbox"),
                        data.get("ticker", ""),
                        data.get("asset_class", "equity"),
                        direction,
                        float(data.get("qty", 1)),
                        float(data.get("price") or 0) or None,
                        "predictor",
                        data.get("strategy", "momentum_equity"),
                        event_type,  # status = "fill" | "pending" | "open"
                    )
                    self._trades_since_review += 1

                # Notify on fills and pending orders (limit orders submitted)
                if event_type in ("fill", "pending"):
                    await self.notifier.trade_fill(data)

            # Check strategic review threshold
            if self._trades_since_review >= STRATEGY_REVIEW_THRESHOLD:
                asyncio.create_task(self._run_strategy_review())
                self._trades_since_review = 0

        except Exception as e:
            log.error("review-agent.record_trade_error", error=str(e))
        finally:
            await self.redis.xack(ORD_STREAM, ORD_GROUP, msg_id)

    # ── Commands loop — EOD report trigger ───────────────────────────────────

    async def _commands_loop(self):
        log.info("review-agent.commands_loop_start")
        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=CMD_GROUP, consumername=CONSUMER_NAME,
                    streams={CMD_STREAM: ">"}, count=5, block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_command(msg_id, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err = str(e)
                if "NOGROUP" in err:
                    await self._ensure_groups()
                log.error("review-agent.commands_loop_error", error=err)
                wait = 10 if "loading" in err.lower() else 3
                await asyncio.sleep(wait)
                try:
                    await self.redis.ping()
                except Exception:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger":
                if job == "eod_report":
                    await self._run_eod_report(data)
                elif job == "run_review":
                    await self._run_strategy_review()
        except Exception as e:
            log.error("review-agent.handle_command_error", job=job, error=str(e))
        finally:
            await self.redis.xack(CMD_STREAM, CMD_GROUP, msg_id)

    # ── EOD Report ────────────────────────────────────────────────────────────

    async def _run_eod_report(self, trigger_data: dict):
        date_str = trigger_data.get("date") or now_et().date().isoformat()
        log.info("review-agent.eod_report.start", date=date_str)

        # 1. Pull today's trades from DB
        trades = await self._get_today_trades(date_str)

        # 2. Enrich with actual fills from Tradier
        fills = await self._get_tradier_fills()

        # 3. Build summary stats
        stats = self._compute_stats(trades, fills)

        # 4. Generate report (LLM if available, else template)
        if USE_LLM and (trades or fills):
            report_text = await self._llm_eod_report(date_str, stats, trades, fills)
        else:
            report_text = self._template_eod_report(date_str, stats)

        # 5. Save to DB
        await self._save_review_log(report_text, stats)

        # 6. Notify
        subject = f"OpenTrader EOD Report — {date_str}"
        await self.notifier.eod_report(subject, report_text)

        # 7. Publish to review stream
        await self.redis.xadd(REV_STREAM, {
            "type":    "eod_report",
            "date":    date_str,
            "summary": json.dumps(stats),
        }, maxlen=500)

        log.info("review-agent.eod_report.done",
                 trades=len(trades), fills=len(fills))

    async def _get_today_trades(self, date_str: str) -> list:
        if not self._db:
            return []
        try:
            rows = await self._db.fetch(
                """
                SELECT id, ticker, direction, qty, entry_price,
                       exit_price, pnl, strategy, status, ts
                FROM trades
                WHERE ts::date = $1
                ORDER BY ts DESC
                """,
                date.fromisoformat(date_str),
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error("review-agent.db_trades_error", error=str(e))
            return []

    async def _get_tradier_fills(self) -> list:
        """Pull today's filled orders from Tradier across all accounts."""
        fills = []
        try:
            all_orders = await self.gateway.get_all_orders()
            today = now_et().date().isoformat()
            for acct_data in all_orders:
                for o in (acct_data.get("orders") or []):
                    if not isinstance(o, dict):
                        continue
                    if o.get("status") in ("filled", "partially_filled"):
                        created = str(o.get("create_date", ""))
                        if today in created:
                            fills.append({
                                "account":    acct_data.get("account"),
                                "ticker":     o.get("symbol"),
                                "side":       o.get("side"),
                                "qty":        o.get("quantity"),
                                "avg_fill":   o.get("avg_fill_price"),
                                "order_id":   o.get("id"),
                                "status":     o.get("status"),
                            })
        except Exception as e:
            log.warning("review-agent.tradier_fills_error", error=str(e))
        return fills

    def _compute_stats(self, trades: list, fills: list) -> dict:
        total   = len(trades)
        longs   = sum(1 for t in trades if t.get("direction") == "long")
        shorts  = total - longs
        filled  = len(fills)

        closed  = [t for t in trades if t.get("pnl") is not None]
        wins    = [t for t in closed if (t.get("pnl") or 0) > 0]
        losses  = [t for t in closed if (t.get("pnl") or 0) < 0]
        total_pnl = sum(float(t.get("pnl") or 0) for t in closed)

        return {
            "date":        now_et().date().isoformat(),
            "total_trades": total,
            "longs":        longs,
            "shorts":       shorts,
            "filled":       filled,
            "closed":       len(closed),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_pnl":    round(total_pnl, 2),
            "avg_pnl":      round(total_pnl / len(closed), 2) if closed else 0.0,
        }

    async def _llm_eod_report(
        self, date_str: str, stats: dict, trades: list, fills: list
    ) -> str:
        from llm.connector import LLMConnector

        trade_lines = "\n".join(
            f"  {t.get('ticker')} {t.get('direction')} {t.get('qty')} shares"
            f" entry=${t.get('entry_price') or '?'}"
            f" pnl=${t.get('pnl') or 'open'}"
            for t in trades[:20]
        ) or "  No trades recorded."

        fill_lines = "\n".join(
            f"  {f.get('ticker')} {f.get('side')} {f.get('qty')} @ ${f.get('avg_fill')}"
            f" [{f.get('account')}]"
            for f in fills[:20]
        ) or "  No Tradier fills today."

        prompt = f"""
Date: {date_str}

Trading Summary:
  Total signals acted on: {stats['total_trades']}
  Filled orders (Tradier): {stats['filled']}
  Closed P&L: ${stats['total_pnl']} ({stats['wins']}W / {stats['losses']}L, {stats['win_rate']}% win rate)

Trades entered:
{trade_lines}

Tradier fills:
{fill_lines}

Write a concise EOD trading report (3-5 paragraphs) covering:
1. What happened today — which signals fired and what was acted on
2. P&L performance and notable winners/losers
3. Signal quality assessment — were OVTLYR signals accurate?
4. Risk observations — position sizing, concentration, anything concerning
5. One concrete recommendation for tomorrow

Be direct, analytical, and specific. Use actual tickers from the data.
"""
        system = (
            "You are a systematic trading desk analyst writing a daily performance report. "
            "Be precise and data-driven. Avoid generic statements."
        )
        try:
            llm = LLMConnector("review")
            return await llm.complete(prompt=prompt, system=system, max_tokens=800)
        except Exception as e:
            log.warning("review-agent.llm_failed", error=str(e))
            return self._template_eod_report(date_str, stats)

    def _template_eod_report(self, date_str: str, stats: dict) -> str:
        return f"""OpenTrader EOD Report — {date_str}
{'=' * 40}

TRADING SUMMARY
  Total trades:  {stats['total_trades']}
  Filled:        {stats['filled']}
  Longs:         {stats['longs']}
  Shorts:        {stats['shorts']}

PERFORMANCE
  Closed trades: {stats['closed']}
  Wins / Losses: {stats['wins']} / {stats['losses']}
  Win rate:      {stats['win_rate']}%
  Total P&L:     ${stats['total_pnl']}
  Avg P&L:       ${stats['avg_pnl']}

{'LLM analysis not available (no API key configured).' if not USE_LLM else ''}
"""

    # ── Strategic Review (every 50 trades) ───────────────────────────────────

    async def _run_strategy_review(self):
        log.info("review-agent.strategy_review.start")

        if not self._db:
            return

        try:
            rows = await self._db.fetch(
                """
                SELECT ticker, direction, qty, entry_price, exit_price,
                       pnl, strategy, ts
                FROM trades
                WHERE status = 'closed' AND pnl IS NOT NULL
                ORDER BY ts DESC
                LIMIT 50
                """
            )
        except Exception as e:
            log.error("review-agent.strategy_review_db_error", error=str(e))
            return

        if not rows:
            log.info("review-agent.strategy_review.no_closed_trades")
            return

        trades = [dict(r) for r in rows]
        closed = len(trades)
        wins   = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        total_pnl = sum(float(t.get("pnl") or 0) for t in trades)
        win_rate  = round(wins / closed * 100, 1) if closed else 0.0

        if USE_LLM:
            findings = await self._llm_strategy_review(trades, win_rate, total_pnl)
        else:
            findings = (
                f"Strategy Review ({closed} trades)\n"
                f"Win rate: {win_rate}% | Total P&L: ${total_pnl:.2f}\n"
                f"LLM analysis unavailable."
            )

        # Save to review_log
        recs = {"win_rate": win_rate, "total_pnl": total_pnl, "trades": closed}
        await self._save_review_log(findings, recs, is_strategy_review=True)

        await self.notifier.review_findings(findings)
        log.info("review-agent.strategy_review.done",
                 trades=closed, win_rate=win_rate, pnl=total_pnl)

    async def _llm_strategy_review(
        self, trades: list, win_rate: float, total_pnl: float
    ) -> str:
        from llm.connector import LLMConnector

        # Build a compact summary for the LLM
        by_ticker: dict = {}
        for t in trades:
            sym = t.get("ticker", "?")
            if sym not in by_ticker:
                by_ticker[sym] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_ticker[sym]["trades"] += 1
            by_ticker[sym]["pnl"] += float(t.get("pnl") or 0)
            if (t.get("pnl") or 0) > 0:
                by_ticker[sym]["wins"] += 1

        ticker_lines = "\n".join(
            f"  {sym}: {d['trades']} trades, "
            f"W/L={d['wins']}/{d['trades']-d['wins']}, "
            f"P&L=${d['pnl']:.2f}"
            for sym, d in sorted(by_ticker.items(), key=lambda x: -abs(x[1]["pnl"]))[:15]
        )

        prompt = f"""
Strategic review of last {len(trades)} closed trades.

Overall: Win rate {win_rate}%, Total P&L ${total_pnl:.2f}

Per-ticker breakdown:
{ticker_lines}

Analyze this trading history and provide:
1. Which tickers are performing well vs poorly — should any be blacklisted?
2. Is the win rate acceptable? What should it be for a momentum strategy?
3. Are there patterns in the losses (sector, market condition, direction)?
4. Specific parameter recommendations: stop_loss_pct, take_profit_pct, min_confidence
5. Overall strategy health: continue, adjust, or pause?

Return JSON with keys:
  summary (string), blacklist (list of tickers),
  recommendations (dict with stop_loss_pct, take_profit_pct, min_confidence),
  health (string: "healthy" | "needs_adjustment" | "pause")
"""
        system = (
            "You are a quantitative portfolio risk manager reviewing systematic "
            "trading performance. Be specific and action-oriented."
        )
        try:
            llm    = LLMConnector("review")
            result = await llm.complete_json(prompt=prompt, system=system, max_tokens=1000)
            summary = result.get("summary", "")
            health  = result.get("health", "unknown")
            recs    = result.get("recommendations", {})
            bl      = result.get("blacklist", [])

            return (
                f"STRATEGY REVIEW — {len(trades)} trades\n"
                f"Health: {health.upper()}\n\n"
                f"{summary}\n\n"
                f"Recommendations: stop_loss={recs.get('stop_loss_pct','?')}% "
                f"take_profit={recs.get('take_profit_pct','?')}% "
                f"min_confidence={recs.get('min_confidence','?')}\n"
                f"Blacklist: {', '.join(bl) if bl else 'None'}"
            )
        except Exception as e:
            log.warning("review-agent.llm_strategy_failed", error=str(e))
            return f"Strategy review: {win_rate}% win rate, ${total_pnl:.2f} P&L ({len(trades)} trades)"

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _save_review_log(
        self,
        findings: str,
        stats: dict,
        is_strategy_review: bool = False,
    ):
        if not self._db:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO review_log (trade_count, findings, recommendations, applied)
                VALUES ($1, $2, $3::jsonb, false)
                """,
                stats.get("total_trades", stats.get("trades", 0)),
                findings,
                json.dumps(stats),
            )
        except Exception as e:
            log.error("review-agent.save_review_log_error", error=str(e))

    async def shutdown(self):
        self._running = False
        if self._db:
            await self._db.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = ReviewAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
