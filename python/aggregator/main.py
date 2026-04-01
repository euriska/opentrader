"""
OpenTrader Aggregator Agent
Middleware between scrapers and the predictor.

Two concurrent loops:
  1. _ticks_loop  — consumes market.ticks, caches per-ticker sentiment
  2. _scanner_loop — consumes scanner.signals, enriches each OVTLYR candidate
     with sentiment + yfinance fundamentals, writes aggregator:intel:{ticker}
"""
import asyncio
import json
import os
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from .combiner import build_intelligence, fetch_yfinance
from .models import TickerIntelligence

log = structlog.get_logger("aggregator")

TICKS_STREAM   = STREAMS["ticks"]
SCANNER_STREAM = STREAMS["scanner"]
AGG_GROUP      = GROUPS["aggregator"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "aggregator-0")

INTEL_TTL      = int(os.getenv("INTEL_TTL_SEC",      "7200"))   # 2 hours
SENTIMENT_TTL  = int(os.getenv("SENTIMENT_TTL_SEC",  "7200"))
YF_CACHE_TTL   = int(os.getenv("YF_CACHE_TTL_SEC",   "3600"))   # 1 hour for yf data


class AggregatorAgent(BaseAgent):

    def __init__(self):
        super().__init__("aggregator")

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=15, retry_on_timeout=True,
            health_check_interval=30,
        )
        await self._ensure_groups()
        log.info("aggregator.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._ticks_loop(),
            self._scanner_loop(),
        )

    async def _ensure_groups(self):
        for stream in (TICKS_STREAM, SCANNER_STREAM):
            try:
                await self.redis.xgroup_create(
                    stream, AGG_GROUP, id="$", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    log.warning("aggregator.group_error", stream=stream, error=str(e))

    # ── Loop 1: Consume market.ticks → cache sentiment per ticker ─────────────

    async def _ticks_loop(self):
        """Consume market.ticks and update aggregator:sentiment:{ticker}."""
        log.info("aggregator.ticks_loop_start")
        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=AGG_GROUP,
                    consumername=f"{CONSUMER_NAME}-ticks",
                    streams={TICKS_STREAM: ">"},
                    count=20,
                    block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._cache_sentiment(data)
                        await self.redis.xack(TICKS_STREAM, AGG_GROUP, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("aggregator.ticks_loop_error", error=str(e))
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    import redis.asyncio as aioredis
                    self.redis = await aioredis.from_url(
                        REDIS_URL, encoding="utf-8", decode_responses=True,
                        socket_connect_timeout=10, socket_timeout=15,
                        retry_on_timeout=True,
            health_check_interval=30,
                    )

    async def _cache_sentiment(self, data: dict):
        """Store raw sentiment tick in aggregator:sentiment:{ticker}:{source}."""
        ticker = data.get("ticker", "").upper()
        source = data.get("source", "unknown")
        if not ticker:
            return
        key = f"aggregator:sentiment:{ticker}"
        await self.redis.hset(key, source, json.dumps({
            "mention_count":   data.get("mention_count", "1"),
            "sentiment_score": data.get("sentiment_score", "0.0"),
            "sentiment_label": data.get("sentiment_label", "neutral"),
            "headlines":       data.get("headlines", "[]"),
            "ts_utc":          data.get("ts_utc", "0"),
        }))
        await self.redis.expire(key, SENTIMENT_TTL)

    # ── Loop 2: Consume scanner.signals → enrich and write intel ──────────────

    async def _scanner_loop(self):
        """Consume scanner.signals, build TickerIntelligence, cache it."""
        log.info("aggregator.scanner_loop_start")
        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=AGG_GROUP,
                    consumername=f"{CONSUMER_NAME}-scanner",
                    streams={SCANNER_STREAM: ">"},
                    count=10,
                    block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._enrich_candidate(data)
                        await self.redis.xack(SCANNER_STREAM, AGG_GROUP, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("aggregator.scanner_loop_error", error=str(e))
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    import redis.asyncio as aioredis
                    self.redis = await aioredis.from_url(
                        REDIS_URL, encoding="utf-8", decode_responses=True,
                        socket_connect_timeout=10, socket_timeout=15,
                        retry_on_timeout=True,
            health_check_interval=30,
                    )

    async def _enrich_candidate(self, data: dict):
        """Build and cache TickerIntelligence for an OVTLYR candidate."""
        ticker = data.get("ticker", "").upper()
        if not ticker:
            return

        current_price = float(data.get("price") or 0.0)

        # Load cached sentiment from all scrapers
        sentiment_raw = await self.redis.hgetall(f"aggregator:sentiment:{ticker}")
        sentiment_data = {}
        for source, raw in sentiment_raw.items():
            try:
                d = json.loads(raw)
                sentiment_data[source] = {
                    "mention_count":   int(d.get("mention_count", 1)),
                    "sentiment_score": float(d.get("sentiment_score", 0.0)),
                    "sentiment_label": d.get("sentiment_label", "neutral"),
                    "headlines":       json.loads(d.get("headlines", "[]")),
                }
            except Exception:
                pass

        # Fetch yfinance data (with short Redis cache to avoid hammering)
        yf_data = await self._get_yf_cached(ticker, current_price)

        # Build standardized TickerIntelligence
        intel = build_intelligence(
            ticker=ticker,
            sentiment_data=sentiment_data,
            yf_data=yf_data,
            current_price=current_price,
        )

        # Cache the result
        key = f"aggregator:intel:{ticker}"
        await self.redis.set(key, intel.to_json(), ex=INTEL_TTL)

        log.info("aggregator.intel_cached",
                 ticker=ticker,
                 sources=intel.sources,
                 earnings_too_close=intel.earnings_too_close,
                 delta=intel.confidence_delta,
                 summary=intel.summary)

    async def _get_yf_cached(self, ticker: str, current_price: float) -> dict:
        """Return cached yfinance data, or fetch fresh if expired."""
        cache_key = f"aggregator:yf:{ticker}"
        cached = await self.redis.get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass

        yf_data = await fetch_yfinance(ticker)
        if yf_data:
            await self.redis.set(cache_key, json.dumps(yf_data), ex=YF_CACHE_TTL)
        return yf_data

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = AggregatorAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
