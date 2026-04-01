"""
OpenTrader Orchestrator
- Publishes its own heartbeat to system.hb
- Monitors heartbeats from all other agents
- Detects missed heartbeats and triggers self-healing
- Listens on system.commands for operator directives
- Sends alerts via notifier when agents go unhealthy
"""
import asyncio
import logging
import os
import time

import redis.asyncio as aioredis
import structlog

from shared.redis_client import get_redis, ensure_streams, STREAMS, GROUPS
from shared.envelope import Envelope, HeartbeatPayload
from .watchdog import Watchdog
from .commander import Commander

log = structlog.get_logger("orchestrator")

SERVICE_NAME       = os.getenv("SERVICE_NAME", "orchestrator")
HB_INTERVAL        = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "30"))
HB_TTL             = int(os.getenv("HEARTBEAT_TTL_SEC", "90"))


class Orchestrator:

    def __init__(self):
        self.redis:     aioredis.Redis = None
        self.watchdog:  Watchdog       = None
        self.commander: Commander      = None
        self.start_time = time.time()

    async def start(self):
        log.info("orchestrator.starting")
        self.redis = await get_redis()
        await ensure_streams(self.redis)

        self.watchdog  = Watchdog(self.redis, ttl_sec=HB_TTL)
        self.commander = Commander()          # gets its own connection in run()
        await self.watchdog.notifier.ensure_inbox()

        await asyncio.gather(
            self._heartbeat_loop(),
            self.watchdog.run(),
            self.commander.run(),
            self._heartbeat_consumer(),
        )

    # ── Publish own heartbeat ────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Publish orchestrator heartbeat every HB_INTERVAL seconds."""
        while True:
            try:
                payload = HeartbeatPayload(
                    service  = SERVICE_NAME,
                    status   = "healthy",
                    pid      = os.getpid(),
                    uptime_s = round(time.time() - self.start_time, 1),
                )
                env = Envelope(
                    sender  = SERVICE_NAME,
                    stream  = STREAMS["heartbeat"],
                    payload = payload.model_dump(),
                )
                await self.redis.xadd(
                    STREAMS["heartbeat"],
                    env.to_redis(),
                    maxlen=1000,    # keep last 1000 heartbeats
                )
                log.debug("orchestrator.heartbeat.sent")
            except Exception as e:
                log.error("orchestrator.heartbeat.failed", error=str(e))

            await asyncio.sleep(HB_INTERVAL)

    # ── Consume heartbeats from all agents ───────────────────────────────────

    async def _heartbeat_consumer(self):
        """Read heartbeats from all agents and update watchdog."""
        group  = GROUPS["orchestrator"]
        stream = STREAMS["heartbeat"]
        # Own dedicated connection — avoids contention with heartbeat publish loop
        redis  = await get_redis()

        while True:
            try:
                messages = await redis.xreadgroup(
                    groupname    = group,
                    consumername = SERVICE_NAME,
                    streams      = {stream: ">"},
                    count        = 50,
                    block        = 5000,
                )
                for _, entries in (messages or []):
                    for entry_id, data in entries:
                        env = Envelope.from_redis(data)
                        if env.sender != SERVICE_NAME:
                            await self.watchdog.record(
                                service = env.payload.get("service", env.sender),
                                status  = env.payload.get("status", "healthy"),
                            )
                        await redis.xack(stream, group, entry_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("orchestrator.hb_consumer.error", error=str(e))
                await asyncio.sleep(5)
                try:
                    await redis.ping()
                except Exception:
                    try:
                        await redis.aclose()
                    except Exception:
                        pass
                    redis = await get_redis()


async def main():
    logging.basicConfig(level=logging.INFO)
    orch = Orchestrator()
    await orch.start()


if __name__ == "__main__":
    asyncio.run(main())
