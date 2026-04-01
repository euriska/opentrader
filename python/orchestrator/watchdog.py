"""
Self-Healing Watchdog
Tracks last-seen timestamps for every agent.
Classifies faults and dispatches recovery actions.
"""
import asyncio
import logging
import time
import os
from typing import Optional

import redis.asyncio as aioredis
import structlog

from shared.redis_client import STREAMS
from notifier.agentmail import Notifier

log = structlog.get_logger("watchdog")

# Known agents the orchestrator expects to hear from
MONITORED_AGENTS = [
    "scheduler",
    "predictor",
    "trader-equity",
    "trader-options",
    "scraper",
    "review-agent",
]

MAX_RESTARTS    = int(os.getenv("WATCHDOG_MAX_RESTARTS", "3"))
BACKOFF_BASE    = int(os.getenv("WATCHDOG_BACKOFF_SEC", "10"))
ESCALATE_AFTER  = int(os.getenv("WATCHDOG_ESCALATE_AFTER", "3"))


class AgentState:
    def __init__(self, name: str):
        self.name          = name
        self.last_seen:    float = time.time()  # assume alive at start
        self.status:       str   = "healthy"
        self.restart_count: int  = 0
        self.faulted:      bool  = False


class Watchdog:

    def __init__(self, redis: aioredis.Redis, ttl_sec: int = 90):
        self.redis    = redis
        self.ttl      = ttl_sec
        self.notifier = Notifier("alerts")
        self.agents   = {name: AgentState(name) for name in MONITORED_AGENTS}
        self._circuit_broken = False

    async def record(self, service: str, status: str = "healthy"):
        """Called by orchestrator when a heartbeat arrives."""
        if service not in self.agents:
            self.agents[service] = AgentState(service)

        agent = self.agents[service]
        agent.last_seen = time.time()
        agent.status    = status

        # Clear fault state if agent recovered
        if agent.faulted and status == "healthy":
            agent.faulted      = False
            agent.restart_count = 0
            log.info("watchdog.agent.recovered", agent=service)
            await self.notifier.alert(
                subject=f"[RECOVERED] {service}",
                body=f"Agent {service} has recovered and is sending healthy heartbeats.",
            )

    async def run(self):
        """Main watchdog loop — checks all agents every 15 seconds."""
        log.info("watchdog.started", ttl_sec=self.ttl)
        await asyncio.sleep(60)  # grace period on startup

        while True:
            await self._check_all()
            await asyncio.sleep(15)

    async def _check_all(self):
        now = time.time()
        for name, agent in self.agents.items():
            age = now - agent.last_seen
            if age > self.ttl and not agent.faulted:
                await self._handle_fault(agent, age)

    async def _handle_fault(self, agent: AgentState, age_sec: float):
        agent.faulted = True
        agent.restart_count += 1

        fault_type = self._classify(agent)
        log.warning(
            "watchdog.fault_detected",
            agent       = agent.name,
            fault_type  = fault_type,
            missed_sec  = round(age_sec),
            restart_count = agent.restart_count,
        )

        if fault_type == "transient":
            await self._restart_agent(agent)

        elif fault_type == "degraded":
            await self.notifier.alert(
                subject=f"[DEGRADED] {agent.name}",
                body=(
                    f"Agent *{agent.name}* has missed heartbeats for "
                    f"{round(age_sec)}s ({agent.restart_count} restart attempts).\n"
                    f"System is throttling — monitoring closely."
                ),
            )

        elif fault_type == "fatal":
            await self._trip_circuit_breaker(agent)

    def _classify(self, agent: AgentState) -> str:
        if agent.restart_count <= MAX_RESTARTS:
            return "transient"
        elif agent.restart_count <= ESCALATE_AFTER:
            return "degraded"
        else:
            return "fatal"

    async def _restart_agent(self, agent: AgentState):
        """
        Publish a restart command to system.commands stream.
        The host systemd/podman layer picks this up and restarts the container.
        """
        backoff = BACKOFF_BASE * (2 ** (agent.restart_count - 1))
        log.info(
            "watchdog.restart",
            agent   = agent.name,
            attempt = agent.restart_count,
            backoff = backoff,
        )
        await self.redis.xadd(
            STREAMS["commands"],
            {
                "command":    "restart",
                "target":     agent.name,
                "attempt":    str(agent.restart_count),
                "backoff_sec": str(backoff),
                "issued_by":  "watchdog",
            },
            maxlen=500,
        )
        await self.notifier.alert(
            subject=f"[RESTART] {agent.name} — attempt {agent.restart_count}",
            body=(
                f"Watchdog is restarting *{agent.name}* "
                f"(attempt {agent.restart_count}/{MAX_RESTARTS}).\n"
                f"Backoff: {backoff}s"
            ),
        )
        await asyncio.sleep(backoff)

    async def _trip_circuit_breaker(self, agent: AgentState):
        """Halt all trading and escalate to human operator."""
        if self._circuit_broken:
            return  # already tripped

        self._circuit_broken = True
        log.critical(
            "watchdog.circuit_breaker.tripped",
            agent         = agent.name,
            restart_count = agent.restart_count,
        )

        # Publish circuit breaker event to commands stream
        await self.redis.xadd(
            STREAMS["commands"],
            {
                "command":   "circuit_break",
                "reason":    f"{agent.name} unresponsive after {agent.restart_count} restarts",
                "issued_by": "watchdog",
            },
            maxlen=500,
        )

        # Persist circuit break state in Redis
        await self.redis.set("system:circuit_broken", "1")
        await self.redis.set("system:circuit_reason", agent.name)

        await self.notifier.alert(
            subject="[CIRCUIT BREAKER] Trading halted",
            body=(
                f"*CIRCUIT BREAKER TRIPPED*\n\n"
                f"Agent *{agent.name}* failed to recover after "
                f"{agent.restart_count} restart attempts.\n\n"
                f"All trading has been halted. "
                f"Manual intervention required.\n\n"
                f"To resume: `redis-cli SET system:circuit_broken 0`\n"
                f"Then restart the affected container."
            ),
        )

    def status(self) -> dict:
        now = time.time()
        return {
            "circuit_broken": self._circuit_broken,
            "agents": {
                name: {
                    "last_seen_sec": round(now - a.last_seen),
                    "status":        a.status,
                    "faulted":       a.faulted,
                    "restarts":      a.restart_count,
                }
                for name, a in self.agents.items()
            }
        }
