"""
Shared Redis client factory.
All agents import this — single consistent connection config.
"""
import os
import logging
import redis.asyncio as aioredis

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Stream names — single source of truth
STREAMS = {
    "ticks":           "market.ticks",
    "scanner":         "scanner.signals",
    "signals":         "predictor.signals",
    "orders":          "orders.events",
    "heartbeat":       "system.hb",
    "review":          "system.review",
    "commands":        "system.commands",
    # Broker gateway streams
    "broker_commands": "broker.commands",
    "broker_fills":    "broker.fills",
}

# Consumer group names
GROUPS = {
    "orchestrator":       "orchestrator-group",
    "scheduler":          "scheduler-group",
    "predictor":          "predictor-group",
    "equity":             "trader-equity-group",
    "options":            "trader-options-group",
    "scraper":            "scraper-group",
    "scraper-ovtlyr":     "scraper-ovtlyr-group",
    "scraper-wsb":        "scraper-wsb-group",
    "scraper-seekalpha":  "scraper-seekalpha-group",
    "scraper-yahoo":      "scraper-yahoo-group",
    "aggregator":         "aggregator-group",
    "review":             "review-agent-group",
    # Broker gateway
    "broker_gateway":     "broker-gateway-group",
    "broker_fills":       "broker-fills-group",
}


async def get_redis() -> aioredis.Redis:
    client = await aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=30,       # must exceed xreadgroup block= value (5 s)
        retry_on_timeout=True,
        health_check_interval=30,
    )
    return client


async def ensure_streams(redis: aioredis.Redis):
    """Create streams and consumer groups if they don't exist."""
    for name, stream in STREAMS.items():
        for group_name, group in GROUPS.items():
            try:
                await redis.xgroup_create(
                    stream, group, id="0", mkstream=True
                )
                log.debug(f"Created group '{group}' on stream '{stream}'")
            except Exception as e:
                if "BUSYGROUP" in str(e):
                    pass  # already exists — fine
                else:
                    log.warning(f"Stream setup: {stream}/{group}: {e}")
