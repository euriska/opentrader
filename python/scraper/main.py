"""
OpenTrader Scraper Agent
Listens for scheduler triggers on system.commands, runs OVTLYR scraper,
and publishes results to the market.ticks stream for the predictor.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from notifier.agentmail import Notifier

from .ovtlyr import OvtlyrScraper

log = structlog.get_logger("scraper")

TICKS_STREAM   = STREAMS["ticks"]
CMD_STREAM     = STREAMS["commands"]
CONSUMER_GROUP = GROUPS["scraper"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "scraper-0")

# Max tickers to publish per scrape run (avoid stream flooding)
MAX_OVTLYR = int(os.getenv("MAX_OVTLYR_TICKERS", "30"))


class ScraperAgent(BaseAgent):

    def __init__(self):
        super().__init__("scraper")
        self.ovtlyr = OvtlyrScraper()
        self._ovtlyr_ready = False

    async def run(self):
        await self.setup()
        # Override Redis connection with longer socket timeout for XREADGROUP block calls
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=15,   # must exceed XREADGROUP block ms (5000) + margin
            retry_on_timeout=True,
            health_check_interval=30,
        )
        await self._ensure_consumer_group()

        log.info("scraper.starting")

        # Launch browser in background — don't block startup
        asyncio.create_task(self._init_ovtlyr())

        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _init_ovtlyr(self):
        try:
            await self.ovtlyr.start()
            self._ovtlyr_ready = True
            log.info("scraper.ovtlyr_ready")
        except Exception as e:
            log.error("scraper.ovtlyr_init_failed", error=str(e))

    async def _ensure_consumer_group(self):
        try:
            await self.redis.xgroup_create(
                CMD_STREAM, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("scraper.group_create", error=str(e))

    async def _command_loop(self):
        """Consume system.commands, handle scrape triggers."""
        log.info("scraper.command_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue

                messages = await self.redis.xreadgroup(
                    groupname    = CONSUMER_GROUP,
                    consumername = CONSUMER_NAME,
                    streams      = {CMD_STREAM: ">"},
                    count        = 10,
                    block        = 5000,
                )

                if not messages:
                    continue

                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_command(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("scraper.command_loop_error", error=str(e))
                await asyncio.sleep(3)
                # Reconnect Redis on timeout/connection errors
                try:
                    await self.redis.ping()
                except Exception:
                    log.warning("scraper.redis_reconnect")
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        command = data.get("command", "")
        job     = data.get("job", "")

        try:
            if command == "trigger":
                if job == "scrape_ovtlyr":
                    await self._run_ovtlyr()
                elif job == "pre_market_prep":
                    await self._warmup()

        except Exception as e:
            log.error("scraper.handle_command_error", job=job, error=str(e))
        finally:
            # ACK so we don't reprocess
            await self.redis.xack(CMD_STREAM, CONSUMER_GROUP, msg_id)

    async def _run_ovtlyr(self):
        if not self._ovtlyr_ready:
            log.warning("scraper.ovtlyr_not_ready")
            return

        log.info("scraper.ovtlyr.start")
        try:
            tickers = await self.ovtlyr.scrape()
        except Exception as e:
            log.error("scraper.ovtlyr.failed", error=str(e))
            return

        published = 0
        for t in tickers[:MAX_OVTLYR]:
            await self.redis.xadd(
                TICKS_STREAM,
                t.to_stream_dict(),
                maxlen=10_000,
            )
            published += 1

        log.info("scraper.ovtlyr.published", count=published)

        # Notify on completion
        now      = datetime.now(timezone.utc).strftime("%H:%M UTC")
        top5     = ", ".join(t.ticker for t in tickers[:5]) if tickers else "none"
        long_ct  = sum(1 for t in tickers if t.direction == "long")
        short_ct = sum(1 for t in tickers if t.direction == "short")
        msg = (
            f"*Scrape Complete* ✅\n"
            f"Time: {now}\n"
            f"Tickers: {published} captured  ({long_ct} long · {short_ct} short)\n"
            f"Top picks: {top5}"
        )
        notifier = Notifier("alerts")
        await asyncio.gather(
            notifier.telegram(msg),
            notifier.discord(msg),
            return_exceptions=True,
        )

        # Store latest OVTLYR batch in Redis hash for quick predictor lookup
        if tickers:
            pipe = self.redis.pipeline()
            for t in tickers[:MAX_OVTLYR]:
                pipe.hset(
                    "scraper:ovtlyr:latest",
                    t.ticker,
                    json.dumps({
                        "direction":  t.direction,
                        "score":      t.score,
                        "ts_utc":     t.ts_utc,
                    }),
                )
            pipe.expire("scraper:ovtlyr:latest", 3600)  # 1 hour TTL
            await pipe.execute()

    async def _warmup(self):
        log.info("scraper.warmup")
        if self._ovtlyr_ready:
            await self.ovtlyr.warmup()
        else:
            asyncio.create_task(self._init_ovtlyr())

    async def shutdown(self):
        self._running = False
        await self.ovtlyr.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = ScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
