"""
OVTLYR Market Scanner Agent
Scrapes OVTLYR for momentum signals and publishes to scanner.signals stream.
This is the PRIMARY signal source for the predictor — not a sentiment scraper.
"""
import asyncio
import json
import os
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from .scraper import OvtlyrScraper

log = structlog.get_logger("scraper-ovtlyr")

CMD_STREAM     = STREAMS["commands"]
SCANNER_STREAM = STREAMS["scanner"]
CONSUMER_GROUP = GROUPS["scraper-ovtlyr"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "scraper-ovtlyr-0")
MAX_TICKERS    = int(os.getenv("MAX_OVTLYR_TICKERS", "30"))


class OvtlyrScraperAgent(BaseAgent):

    def __init__(self):
        super().__init__("scraper-ovtlyr")
        self.ovtlyr = OvtlyrScraper()
        self._ready = False

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=None,
        )
        await self._ensure_group()
        asyncio.create_task(self._init_ovtlyr())
        log.info("scraper-ovtlyr.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _init_ovtlyr(self):
        try:
            await self.ovtlyr.start()
            self._ready = True
            log.info("scraper-ovtlyr.ready")
        except Exception as e:
            log.error("scraper-ovtlyr.init_failed", error=str(e))

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(
                CMD_STREAM, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("scraper-ovtlyr.group_error", error=str(e))

    async def _command_loop(self):
        log.info("scraper-ovtlyr.command_loop_start")
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
                log.error("scraper-ovtlyr.command_loop_error", error=str(e))
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger":
                if job == "scrape_ovtlyr":
                    await self._run_scrape()
                elif job == "pre_market_prep":
                    await self._warmup()
        except Exception as e:
            log.error("scraper-ovtlyr.handle_error", job=job, error=str(e))
        finally:
            await self.redis.xack(CMD_STREAM, CONSUMER_GROUP, msg_id)

    async def _run_scrape(self):
        if not self._ready:
            log.warning("scraper-ovtlyr.not_ready")
            return

        log.info("scraper-ovtlyr.scrape_start")
        try:
            tickers = await self.ovtlyr.scrape()
        except Exception as e:
            log.error("scraper-ovtlyr.scrape_failed", error=str(e))
            return

        if not tickers:
            log.warning("scraper-ovtlyr.no_tickers")
            return

        pipe = self.redis.pipeline()
        published = 0
        for t in tickers[:MAX_TICKERS]:
            entry = {
                "ticker":    t.ticker,
                "direction": t.direction,
                "score":     str(round(t.score, 2)),
                "price":     str(t.price or ""),
                "sector":    t.sector or "",
                "ts_utc":    str(t.ts_utc),
            }
            await self.redis.xadd(SCANNER_STREAM, entry, maxlen=10_000)
            pipe.hset(
                "scanner:ovtlyr:latest",
                t.ticker,
                json.dumps({
                    "direction": t.direction,
                    "score":     t.score,
                    "price":     t.price,
                    "sector":    t.sector,
                    "ts_utc":    t.ts_utc,
                }),
            )
            published += 1

        pipe.expire("scanner:ovtlyr:latest", 3600)
        await pipe.execute()
        log.info("scraper-ovtlyr.published", count=published)

    async def _warmup(self):
        if self._ready:
            await self.ovtlyr.warmup()
        else:
            asyncio.create_task(self._init_ovtlyr())

    async def shutdown(self):
        self._running = False
        await self.ovtlyr.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = OvtlyrScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
