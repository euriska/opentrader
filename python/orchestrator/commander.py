"""
Commander — Operator Command Listener
Listens on system.commands stream for directives from:
- Watchdog (restart, circuit_break)
- Operator (manual reset, halt, status)
- Review agent (apply config patches)
"""
import asyncio
import http.client
import logging
import os
import socket
import time

import redis.asyncio as aioredis
import structlog

from shared.redis_client import STREAMS, GROUPS, get_redis

log = structlog.get_logger("commander")

SERVICE_NAME = os.getenv("SERVICE_NAME", "orchestrator")
PODMAN_SOCK  = os.getenv("PODMAN_SOCK", "/var/run/podman.sock")

# Prevent restarting the same container more than once per cooldown window
RESTART_COOLDOWN_SEC = 60

CONTAINER_MAP = {
    "orchestrator":     "ot-orchestrator",
    "scheduler":        "ot-scheduler",
    "predictor":        "ot-predictor",
    "trader-equity":    "ot-trader-equity",
    "trader-options":   "ot-trader-options",
    "scraper-ovtlyr":   "ot-scraper-ovtlyr",
    "scraper-wsb":      "ot-scraper-wsb",
    "scraper-seekalpha":"ot-scraper-seekalpha",
    "scraper-yahoo":    "ot-scraper-yahoo",
    "aggregator":       "ot-aggregator",
    "review-agent":     "ot-review-agent",
    "broker-gateway":   "ot-broker-gateway",
    "mcp-yahoo":        "ot-mcp-yahoo",
    "mcp-alpaca":       "ot-mcp-alpaca",
    "mcp-tradingview":  "ot-mcp-tradingview",
    "chat-agent":       "ot-chat-agent",
}


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects via a Unix domain socket."""
    def __init__(self, sock_path: str):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._sock_path)
        self.sock = s


def _podman_restart(container_name: str, timeout: int = 10) -> bool:
    """Call podman restart via the Unix socket. Returns True on success."""
    try:
        conn = _UnixSocketHTTPConnection(PODMAN_SOCK)
        conn.timeout = timeout
        conn.request("POST", f"/v4.0.0/libpod/containers/{container_name}/restart")
        resp = conn.getresponse()
        resp.read()
        return resp.status in (200, 204)
    except Exception as e:
        log.error("commander.podman_restart.failed", container=container_name, error=str(e))
        return False


class Commander:

    def __init__(self):
        self.redis  = None
        self.stream = STREAMS["commands"]
        self.group  = GROUPS["orchestrator"]
        self._last_restart: dict[str, float] = {}

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

        if not target:
            log.warning("commander.restart.no_target")
            return

        # Cooldown guard — drop duplicate restart commands within the window
        now = time.monotonic()
        last = self._last_restart.get(target, 0.0)
        if now - last < RESTART_COOLDOWN_SEC:
            log.warning(
                "commander.restart.cooldown",
                target=target,
                seconds_since_last=round(now - last),
            )
            return

        self._last_restart[target] = now
        cname = CONTAINER_MAP.get(target, f"ot-{target}")
        log.info("commander.restart", target=target, container=cname, attempt=attempt)

        ok = await asyncio.get_event_loop().run_in_executor(
            None, _podman_restart, cname
        )
        if ok:
            log.info("commander.restart.ok", target=target, container=cname)
        else:
            log.error("commander.restart.failed", target=target, container=cname)

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
        await self.redis.set("system:status_requested", "1", ex=30)
