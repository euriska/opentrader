"""
Base Agent
All OpenTrader agents inherit from this.
Provides: heartbeat publishing, Redis connection, circuit breaker check,
structured logging, and graceful shutdown.
"""
import asyncio
import logging
import os
import time
import signal
from typing import Optional

import redis.asyncio as aioredis
import structlog

from .redis_client import get_redis, ensure_streams, STREAMS
from .envelope import Envelope, HeartbeatPayload

log = structlog.get_logger(__name__)


class BaseAgent:

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.redis: Optional[aioredis.Redis] = None
        self.start_time  = time.time()
        self._running    = True
        self._hb_interval = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "30"))

        # Graceful shutdown on SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, *_):
        log.info("agent.shutdown_signal", service=self.service_name)
        self._running = False

    async def setup(self):
        """Connect to Redis and ensure streams exist. Call in subclass start()."""
        self.redis = await get_redis()
        await ensure_streams(self.redis)
        log.info("agent.ready", service=self.service_name)

    async def heartbeat_loop(self):
        """Publish heartbeat to system.hb every interval. Run as a task."""
        while self._running:
            try:
                await self._publish_heartbeat("healthy")
            except Exception as e:
                log.error("agent.heartbeat.failed", service=self.service_name, error=str(e))
            await asyncio.sleep(self._hb_interval)

    async def _publish_heartbeat(self, status: str = "healthy"):
        payload = HeartbeatPayload(
            service  = self.service_name,
            status   = status,
            pid      = os.getpid(),
            uptime_s = round(time.time() - self.start_time, 1),
        )
        env = Envelope(
            sender  = self.service_name,
            stream  = STREAMS["heartbeat"],
            payload = payload.model_dump(),
        )
        await self.redis.xadd(
            STREAMS["heartbeat"],
            env.to_redis(),
            maxlen=1000,
        )

    async def is_circuit_broken(self) -> bool:
        """Check if the circuit breaker has been tripped."""
        val = await self.redis.get("system:circuit_broken")
        return val == "1"

    async def is_halted(self) -> bool:
        val = await self.redis.get("system:halted")
        return val == "1"

    async def publish(self, stream_key: str, payload: dict):
        """Publish a message envelope to a Redis stream."""
        stream = STREAMS.get(stream_key, stream_key)
        env = Envelope(
            sender  = self.service_name,
            stream  = stream,
            payload = payload,
        )
        await self.redis.xadd(stream, env.to_redis(), maxlen=5000)

    def uptime(self) -> float:
        return round(time.time() - self.start_time, 1)
