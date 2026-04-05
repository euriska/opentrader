"""
Yahoo Finance — Per-Ticker Fear & Greed Scorer
Runs once daily after market close.
Scores open positions + OVTLYR signal tickers on a 0-100 F&G scale.
Stores one row per ticker per day in ticker_sentiment; caches in Redis.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote
from typing import Optional

import asyncpg
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from .scorer import score_ticker, score_label

log = structlog.get_logger("scraper-yahoo-sentiment")

CMD_STREAM     = STREAMS["commands"]
CONSUMER_GROUP = GROUPS["scraper-yahoo-sentiment"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "yahoo-sentiment-0")
DB_URL         = os.getenv("DB_URL", "")
MCP_URL        = os.getenv("YAHOO_MCP_URL", "http://ot-mcp-yahoo:8000/mcp")


class YahooSentimentAgent(BaseAgent):

    def __init__(self):
        super().__init__("scraper-yahoo-sentiment")
        self._db: Optional[asyncpg.Pool] = None

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=None,
        )
        if DB_URL:
            try:
                p = urlparse(DB_URL)
                self._db = await asyncpg.create_pool(
                    host=p.hostname, port=p.port or 5432,
                    user=p.username,
                    password=unquote(p.password) if p.password else None,
                    database=p.path.lstrip("/"),
                    min_size=1, max_size=3,
                    max_inactive_connection_lifetime=300,
                )
                log.info("yahoo-sentiment.db_connected")
            except Exception as e:
                log.error("yahoo-sentiment.db_connect_failed", error=str(e))
        await self._ensure_group()
        log.info("yahoo-sentiment.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(CMD_STREAM, CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("yahoo-sentiment.group_error", error=str(e))

    async def _command_loop(self):
        log.info("yahoo-sentiment.command_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue
                messages = await self.redis.xreadgroup(
                    groupname=CONSUMER_GROUP, consumername=CONSUMER_NAME,
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
                log.error("yahoo-sentiment.command_loop_error", error=str(e))
                await asyncio.sleep(5)
                try:
                    await self.redis.ping()
                except Exception:
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger" and job == "score_ticker_sentiment":
                await self._run_scoring()
        except Exception as e:
            log.error("yahoo-sentiment.handle_error", job=job, error=str(e))
        finally:
            await self.redis.xack(CMD_STREAM, CONSUMER_GROUP, msg_id)

    async def _get_tickers(self) -> list[str]:
        """Collect tickers: benchmarks + open positions + OVTLYR signals."""
        seen: set[str] = set()
        tickers: list[str] = []

        def add(t: str):
            t = t.upper().strip()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)

        for b in ("SPY", "QQQ"):
            add(b)

        try:
            raw = await self.redis.get("broker:position_tickers")
            if raw:
                for t in json.loads(raw):
                    add(t)
        except Exception as e:
            log.warning("yahoo-sentiment.position_tickers_error", error=str(e))

        try:
            scanner = await self.redis.hgetall("scanner:ovtlyr:latest")
            for t in scanner:
                add(t)
        except Exception as e:
            log.warning("yahoo-sentiment.scanner_error", error=str(e))

        for list_type in ("bull", "bear", "market_leaders", "alpha_picks"):
            try:
                raw = await self.redis.get(f"ovtlyr:list:{list_type}")
                if raw:
                    for entry in json.loads(raw):
                        if entry.get("ticker"):
                            add(entry["ticker"])
            except Exception as e:
                log.warning("yahoo-sentiment.list_error", list_type=list_type, error=str(e))

        return tickers

    async def _fetch_closes(self, ticker: str) -> list[float]:
        """Fetch ~65 trading days of closing prices via Yahoo Finance MCP."""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(MCP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "get_historical_stock_prices",
                        {"ticker": ticker, "period": "3mo", "interval": "1d"},
                    )
                    text = result.content[0].text if result.content else "[]"
                    records = json.loads(text)
                    if not isinstance(records, list):
                        return []
                    closes = []
                    for r in records:
                        c = r.get("Close")
                        if c is not None:
                            try:
                                v = float(c)
                                if v > 0:
                                    closes.append(v)
                            except (TypeError, ValueError):
                                pass
                    return closes
        except Exception as e:
            log.warning("yahoo-sentiment.fetch_error", ticker=ticker, error=str(e))
            return []

    async def _save(self, ticker: str, today, scores: dict, close: float,
                    prev_close: float = None):
        """Cache in Redis and persist to DB."""
        payload = {
            **scores,
            "close":      close,
            "prev_close": prev_close,
            "date":  today.isoformat(),
            "label": score_label(scores["score"]),
            "ts":    datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.hset("sentiment:latest", ticker, json.dumps(payload))

        if not self._db:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO ticker_sentiment
                    (date, ticker, score, rsi, ma_score, momentum, vol_score, close, raw)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (ticker, date) DO UPDATE SET
                    score     = EXCLUDED.score,
                    rsi       = EXCLUDED.rsi,
                    ma_score  = EXCLUDED.ma_score,
                    momentum  = EXCLUDED.momentum,
                    vol_score = EXCLUDED.vol_score,
                    close     = EXCLUDED.close,
                    raw       = EXCLUDED.raw,
                    ts        = NOW()
                """,
                today, ticker,
                scores["score"], scores["rsi"], scores["ma_score"],
                scores["momentum"], scores["vol_score"],
                close,
                json.dumps(scores),
            )
        except Exception as e:
            log.warning("yahoo-sentiment.db_error", ticker=ticker, error=str(e))

    async def _cache_trends(self, tickers: list[str]):
        """After scoring all tickers, cache 30-day trend arrays in Redis."""
        if not self._db or not tickers:
            return
        try:
            rows = await self._db.fetch(
                """
                SELECT ticker, date, score
                FROM ticker_sentiment
                WHERE ticker = ANY($1)
                  AND date >= CURRENT_DATE - INTERVAL '30 days'
                ORDER BY ticker, date ASC
                """,
                tickers,
            )
            trend_map: dict[str, list] = {}
            for row in rows:
                t = row["ticker"]
                if t not in trend_map:
                    trend_map[t] = []
                trend_map[t].append({
                    "date":  row["date"].isoformat(),
                    "score": float(row["score"]),
                })
            pipe = self.redis.pipeline()
            for ticker, trend in trend_map.items():
                pipe.set(f"sentiment:trend:{ticker}", json.dumps(trend), ex=86400 * 2)
            await pipe.execute()
            log.info("yahoo-sentiment.trends_cached", tickers=len(trend_map))
        except Exception as e:
            log.warning("yahoo-sentiment.trend_cache_error", error=str(e))

    async def _run_scoring(self):
        tickers = await self._get_tickers()
        log.info("yahoo-sentiment.scoring_start", tickers=len(tickers))
        today   = datetime.now(timezone.utc).date()
        scored  = 0
        scored_tickers = []

        for ticker in tickers:
            try:
                closes = await self._fetch_closes(ticker)
                if len(closes) < 15:
                    log.warning("yahoo-sentiment.insufficient_data",
                                ticker=ticker, bars=len(closes))
                    continue
                scores = score_ticker(closes)
                prev_close = closes[-2] if len(closes) >= 2 else None
                await self._save(ticker, today, scores, closes[-1], prev_close)
                scored_tickers.append(ticker)
                scored += 1
                log.debug("yahoo-sentiment.scored",
                          ticker=ticker, score=scores["score"],
                          label=score_label(scores["score"]))
            except Exception as e:
                log.error("yahoo-sentiment.ticker_error", ticker=ticker, error=str(e))

        await self.redis.expire("sentiment:latest", 86400 * 2)
        await self._cache_trends(scored_tickers)
        log.info("yahoo-sentiment.done", scored=scored, total=len(tickers))

    async def shutdown(self):
        self._running = False
        if self._db:
            await self._db.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = YahooSentimentAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
