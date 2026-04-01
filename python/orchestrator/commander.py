"""
Commander — Operator Command Listener
Listens on system.commands stream for directives from:
- Watchdog (restart, circuit_break)
- Operator (manual reset, halt, status)
- Review agent (apply config patches)
"""
import asyncio
import logging
import os

import redis.asyncio as aioredis
import structlog

from shared.redis_client import STREAMS, GROUPS, get_redis

log = structlog.get_logger("commander")

SERVICE_NAME = os.getenv("SERVICE_NAME", "orchestrator")


class Commander:

    def __init__(self):
        self.redis  = None
        self.stream = STREAMS["commands"]
        self.group  = GROUPS["orchestrator"]

    async def run(self):
        # Own dedicated connection — avoids contention with other blocking loops
        self.redis = await get_redis()
        log.info("commander.started")
        while True:
            try:
                messages = await self.redis.xreadgroup(
                    groupname    = self.group,
                    consumername = f"{SERVICE_NAME}-commander",
                    streams      = {self.stream: ">"},
                    count        = 10,
                    block        = 5000,
                )
                for _, entries in (messages or []):
                    for entry_id, data in entries:
                        await self._dispatch(data)
                        await self.redis.xack(self.stream, self.group, entry_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("commander.error", error=str(e))
                await asyncio.sleep(5)
                try:
                    await self.redis.ping()
                except Exception:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass
                    self.redis = await get_redis()

    async def _dispatch(self, data: dict):
        command = data.get("command", "")
        target  = data.get("target", "")
        log.info("commander.command", command=command, target=target)

        handlers = {
            "restart":       self._handle_restart,
            "circuit_break": self._handle_circuit_break,
            "reset_circuit": self._handle_reset_circuit,
            "halt":          self._handle_halt,
            "status":        self._handle_status,
        }

        handler = handlers.get(command)
        if handler:
            await handler(data)
        else:
            log.warning("commander.unknown_command", command=command)

    async def _handle_restart(self, data: dict):
        target  = data.get("target", "")
        attempt = data.get("attempt", "1")
        backoff = int(data.get("backoff_sec", "10"))
        log.info("commander.restart", target=target, attempt=attempt)
        # Write restart request — podman-compose watches this key
        await self.redis.set(f"system:restart:{target}", attempt, ex=300)
        await asyncio.sleep(backoff)

    async def _handle_circuit_break(self, data: dict):
        reason = data.get("reason", "unknown")
        log.critical("commander.circuit_break", reason=reason)
        await self.redis.set("system:circuit_broken", "1")

    async def _handle_reset_circuit(self, data: dict):
        log.info("commander.circuit_reset")
        await self.redis.delete("system:circuit_broken")
        await self.redis.delete("system:circuit_reason")

    async def _handle_halt(self, data: dict):
        log.critical("commander.halt — shutting down")
        await self.redis.set("system:halted", "1")

    async def _handle_status(self, data: dict):
        log.info("commander.status_requested")
        # Status is written to Redis for the requestor to read
        await self.redis.set("system:status_requested", "1", ex=30)
