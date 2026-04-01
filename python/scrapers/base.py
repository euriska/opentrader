"""
Base class for all sentiment scraper agents.
Each scraper subscribes to system.commands, handles its trigger job,
and publishes raw ticks to market.ticks.
"""
import asyncio
import os
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL


class BaseScraper(BaseAgent):
    SOURCE      = "unknown"        # override: "wsb" | "seekalpha" | "yahoo"
    TRIGGER_JOB = "scrape_unknown" # override: "scrape_wsb" etc.
    GROUP_KEY   = "scraper"        # override: key into GROUPS dict

    TICKS_STREAM = STREAMS["ticks"]
    CMD_STREAM   = STREAMS["commands"]

    def __init__(self, service_name: str):
        super().__init__(service_name)
        self.log = structlog.get_logger(service_name)

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=30,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        await self._ensure_group()
        await self._on_start()
        self.log.info(f"{self.SOURCE}.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _on_start(self):
        """Override for agent-specific startup."""
        pass

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(
                self.CMD_STREAM, GROUPS[self.GROUP_KEY], id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                self.log.warning(f"{self.SOURCE}.group_create_error", error=str(e))

    async def _command_loop(self):
        consumer = os.getenv("HOSTNAME", f"{self.SOURCE}-0")
        self.log.info(f"{self.SOURCE}.command_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue
                messages = await self.redis.xreadgroup(
                    groupname=GROUPS[self.GROUP_KEY],
                    consumername=consumer,
                    streams={self.CMD_STREAM: ">"},
                    count=5,
                    block=5000,
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
                self.log.warning(f"{self.SOURCE}.command_loop_reconnect", error=err)
                # Back off longer when Redis is restarting (ECONNREFUSED / DNS / loading)
                backoff = 10 if any(x in err for x in ("111", "name resolution", "loading")) else 5
                await asyncio.sleep(backoff)
                try:
                    await self.redis.ping()
                except Exception:
                    import redis.asyncio as aioredis
                    try:
                        self.redis = await aioredis.from_url(
                            REDIS_URL, encoding="utf-8", decode_responses=True,
                            socket_connect_timeout=10, socket_timeout=30,
                            retry_on_timeout=True,
            health_check_interval=30,
                        )
                        await self._ensure_group()
                    except Exception as re:
                        self.log.warning(f"{self.SOURCE}.reconnect_failed", error=str(re))

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger" and job == self.TRIGGER_JOB:
                await self.scrape()
        except Exception as e:
            self.log.error(f"{self.SOURCE}.scrape_error", error=str(e))
        finally:
            await self.redis.xack(self.CMD_STREAM, GROUPS[self.GROUP_KEY], msg_id)

    async def scrape(self):
        """Override in each scraper — fetch data and call publish() for each ticker."""
        raise NotImplementedError

    async def publish(self, ticker: str, payload: dict):
        """Publish a raw tick to market.ticks."""
        await self.redis.xadd(
            self.TICKS_STREAM,
            {"source": self.SOURCE, "ticker": ticker, **payload},
            maxlen=50_000,
        )

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()
