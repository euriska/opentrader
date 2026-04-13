"""
Scheduler Jobs
Each job publishes a trigger message to the appropriate Redis stream.
Agents consume triggers and execute their tasks.
"""
import asyncio
import logging
from datetime import datetime

import redis.asyncio as aioredis
import structlog

from shared.redis_client import STREAMS
from .calendar import (
    is_market_open, is_active_session, is_trading_day,
    now_et, minutes_to_open, minutes_to_close,
)

log = structlog.get_logger("scheduler.jobs")


import time as _time
import functools

JOB_ERRORS_KEY = "scheduler:job_errors"


async def record_job_error(redis: aioredis.Redis, job_name: str, error: str):
    """Push an error entry; entries auto-expire after 1 hour via score."""
    now = _time.time()
    await redis.zadd(JOB_ERRORS_KEY, {f"{now}:{job_name}:{error[:80]}": now})
    # Trim entries older than 1 hour
    await redis.zremrangebyscore(JOB_ERRORS_KEY, 0, now - 3600)


def tracked(fn):
    """Decorator: log job errors to Redis on exception."""
    @functools.wraps(fn)
    async def wrapper(redis, *args, **kwargs):
        try:
            return await fn(redis, *args, **kwargs)
        except Exception as e:
            log.error("scheduler.job_error", job=fn.__name__, error=str(e))
            try:
                await record_job_error(redis, fn.__name__, str(e))
            except Exception:
                pass
            raise
    return wrapper


async def trigger(redis: aioredis.Redis, job_name: str, payload: dict = None):
    """Publish a job trigger to system.commands stream."""
    await redis.xadd(
        STREAMS["commands"],
        {
            "command":  "trigger",
            "job":      job_name,
            "ts_et":    now_et().isoformat(),
            "payload":  str(payload or {}),
            "issued_by": "scheduler",
        },
        maxlen=1000,
    )
    log.info("scheduler.trigger", job=job_name)


# ── Market session jobs ───────────────────────────────────────────────────────

@tracked
async def job_scrape_ovtlyr(redis: aioredis.Redis):
    """Trigger OVTLYR market scanner. Only fires during active session."""
    if not is_active_session():
        log.debug("scheduler.skip", job="scrape_ovtlyr", reason="market_closed")
        return
    await trigger(redis, "scrape_ovtlyr", {"source": "ovtlyr"})


@tracked
async def job_scrape_position_intel(redis: aioredis.Redis):
    """Scrape OVTLYR dashboard for each open position. Active session only."""
    if not is_active_session():
        log.debug("scheduler.skip", job="scrape_position_intel", reason="market_closed")
        return
    await trigger(redis, "scrape_position_intel", {"source": "ovtlyr"})


@tracked
async def job_scrape_sentiment(redis: aioredis.Redis):
    """Trigger all sentiment scrapers (WSB, SeekingAlpha, Yahoo). Active session only."""
    if not is_active_session():
        log.debug("scheduler.skip", job="scrape_sentiment", reason="market_closed")
        return
    await trigger(redis, "scrape_wsb",       {"source": "wsb"})
    await trigger(redis, "scrape_seekalpha", {"source": "seekalpha"})
    await trigger(redis, "scrape_yahoo",     {"source": "yahoo"})


@tracked
async def job_score_ticker_sentiment(redis: aioredis.Redis):
    """Score F&G sentiment for open positions + OVTLYR signal tickers. Runs after close."""
    if not is_trading_day():
        log.debug("scheduler.skip", job="score_ticker_sentiment", reason="not_trading_day")
        return
    await trigger(redis, "score_ticker_sentiment", {})


@tracked
async def job_predict(redis: aioredis.Redis):
    """Trigger predictor signal generation. Only during market hours."""
    if not is_market_open():
        log.debug("scheduler.skip", job="predict", reason="market_closed")
        return
    await trigger(redis, "run_predictor", {})


@tracked
async def job_heartbeat_check(redis: aioredis.Redis):
    """Trigger orchestrator to log a watchdog status summary."""
    await trigger(redis, "watchdog_status", {})


# ── Market open / close jobs ──────────────────────────────────────────────────

async def _job_notify_enabled(redis: aioredis.Redis, job_id: str) -> bool:
    """Return True if notifications are enabled for this job (default: True)."""
    import json as _json
    raw = await redis.get(f"scheduler:job:{job_id}")
    if not raw:
        return True
    try:
        rec = _json.loads(raw)
        return rec.get("notify", True)
    except Exception:
        return True


@tracked
async def job_market_open(redis: aioredis.Redis):
    """Fires at 09:30 ET on trading days."""
    if not is_trading_day():
        return
    log.info("scheduler.market_open")
    await trigger(redis, "market_open", {
        "date": now_et().date().isoformat(),
    })
    if await _job_notify_enabled(redis, "market_open"):
        await redis.xadd(
            STREAMS["commands"],
            {
                "command":   "notify",
                "channel":   "all",
                "message":   f"Market open — {now_et().date().isoformat()}",
                "issued_by": "scheduler",
            },
            maxlen=500,
        )


@tracked
async def job_market_close(redis: aioredis.Redis):
    """Fires at 16:00 ET on trading days."""
    if not is_trading_day():
        return
    log.info("scheduler.market_close")
    await trigger(redis, "market_close", {
        "date": now_et().date().isoformat(),
    })
    if await _job_notify_enabled(redis, "market_close"):
        await redis.xadd(
            STREAMS["commands"],
            {
                "command":   "notify",
                "channel":   "all",
                "message":   f"Market closed — {now_et().date().isoformat()}",
                "issued_by": "scheduler",
            },
            maxlen=500,
        )


@tracked
async def job_eod_report(redis: aioredis.Redis):
    """Fires at 16:05 ET — triggers EOD report agent."""
    if not is_trading_day():
        return
    log.info("scheduler.eod_report")
    await trigger(redis, "eod_report", {
        "date":     now_et().date().isoformat(),
        "channels": ["agentmail", "telegram", "discord"],
    })


@tracked
async def job_pre_market_prep(redis: aioredis.Redis):
    """Fires at 09:00 ET — warm up scrapers before open."""
    if not is_trading_day():
        return
    log.info("scheduler.pre_market_prep")
    await trigger(redis, "pre_market_prep", {
        "minutes_to_open": minutes_to_open(),
    })


# ── Options report ───────────────────────────────────────────────────────────

@tracked
async def job_options_report(redis: aioredis.Redis):
    """Fires at 13:00 ET on trading days — emails the daily options positions report."""
    if not is_trading_day():
        log.debug("scheduler.skip", job="options_report", reason="not_trading_day")
        return
    import json as _json
    raw = await redis.get("scheduler:job:options_report")
    if raw:
        try:
            rec = _json.loads(raw)
            if not rec.get("enabled", True):
                log.info("scheduler.skip", job="options_report", reason="disabled_by_toggle")
                return
        except Exception:
            pass
    import os as _os
    import aiohttp as _aiohttp
    webui_url  = _os.getenv("WEBUI_INTERNAL_URL", "http://ot-webui:8080")
    token      = _os.getenv("WEBUI_TOKEN", "opentrader")
    log.info("scheduler.options_report")
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.post(
                f"{webui_url}/api/options/report/email/auto?token={token}",
                timeout=_aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200:
                    log.info("scheduler.options_report_sent", message=body.get("message"))
                else:
                    log.error("scheduler.options_report_failed", status=resp.status, detail=body.get("detail"))
    except Exception as e:
        log.error("scheduler.options_report_error", error=str(e))


# ── Maintenance jobs ──────────────────────────────────────────────────────────

@tracked
async def job_daily_summary(redis: aioredis.Redis):
    """Fires at 08:00 ET — logs what's scheduled for today."""
    trading = is_trading_day()
    log.info(
        "scheduler.daily_summary",
        trading_day     = trading,
        date            = now_et().date().isoformat(),
        minutes_to_open = minutes_to_open() if trading else -1,
    )
    if trading:
        await redis.xadd(
            STREAMS["commands"],
            {
                "command":   "notify",
                "channel":   "telegram",
                "message":   (
                    f"Good morning — trading day ahead.\n"
                    f"Market opens in {minutes_to_open()} minutes."
                ),
                "issued_by": "scheduler",
            },
            maxlen=500,
        )
