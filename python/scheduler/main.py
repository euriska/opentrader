"""
OpenTrader Scheduler
APScheduler-based job runner.
All jobs are market-hours aware — they check the calendar before firing.
"""
import asyncio
import json
import logging
import os

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron     import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from shared.base_agent   import BaseAgent
from shared.redis_client import get_redis
from .jobs import (
    job_scrape_ovtlyr,
    job_scrape_sentiment,
    job_predict,
    job_heartbeat_check,
    job_market_open,
    job_market_close,
    job_eod_report,
    job_pre_market_prep,
    job_daily_summary,
)
from .calendar import TZ

log = structlog.get_logger("scheduler")

SCRAPE_INTERVAL  = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "3"))
JOB_KEY_PREFIX   = "scheduler:job:"
JOB_INDEX_KEY    = "scheduler:jobs"


class Scheduler(BaseAgent):

    def __init__(self):
        super().__init__("scheduler")
        self.apscheduler = AsyncIOScheduler(timezone=TZ)

    async def start(self):
        await self.setup()
        self._register_jobs()
        await self._apply_config_overrides()
        self.apscheduler.start()
        log.info("scheduler.started", jobs=len(self.apscheduler.get_jobs()))
        await self._publish_jobs()

        # Run heartbeat + main loop concurrently
        await asyncio.gather(
            self.heartbeat_loop(),
            self._idle_loop(),
            self._reload_loop(),
        )

    def _register_jobs(self):
        r = self.redis  # convenience reference

        # ── Daily ─────────────────────────────────────────────────────────
        self.apscheduler.add_job(
            job_daily_summary,
            CronTrigger(hour=8, minute=0, timezone=TZ),
            args=[r], id="daily_summary",
            name="Daily summary + morning alert",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_pre_market_prep,
            CronTrigger(hour=9, minute=0, timezone=TZ),
            args=[r], id="pre_market_prep",
            name="Pre-market scraper warmup",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_market_open,
            CronTrigger(hour=9, minute=30, timezone=TZ),
            args=[r], id="market_open",
            name="Market open signal",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_market_close,
            CronTrigger(hour=16, minute=0, timezone=TZ),
            args=[r], id="market_close",
            name="Market close signal",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_eod_report,
            CronTrigger(hour=16, minute=5, timezone=TZ),
            args=[r], id="eod_report",
            name="EOD report trigger",
            replace_existing=True,
        )

        # ── Interval — active session ──────────────────────────────────────
        self.apscheduler.add_job(
            job_scrape_ovtlyr,
            IntervalTrigger(minutes=SCRAPE_INTERVAL, timezone=TZ),
            args=[r], id="scrape_ovtlyr",
            name=f"OVTLYR market scanner every {SCRAPE_INTERVAL}m",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_scrape_sentiment,
            IntervalTrigger(minutes=SCRAPE_INTERVAL, timezone=TZ),
            args=[r], id="scrape_sentiment",
            name=f"r/wallstreetbets/SeekAlpha/Yahoo sentiment every {SCRAPE_INTERVAL}m",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_predict,
            IntervalTrigger(minutes=5, timezone=TZ),
            args=[r], id="predict",
            name="Predictor signal run every 5m",
            replace_existing=True,
        )

        # ── Interval — always on ──────────────────────────────────────────
        self.apscheduler.add_job(
            job_heartbeat_check,
            IntervalTrigger(seconds=30, timezone=TZ),
            args=[r], id="hb_check",
            name="Watchdog status check",
            replace_existing=True,
        )

        log.info(
            "scheduler.jobs_registered",
            count=len(self.apscheduler.get_jobs()),
            scrape_interval_min=SCRAPE_INTERVAL,
        )

    async def _apply_config_overrides(self):
        """Apply persisted user schedule overrides from Redis to APScheduler."""
        try:
            job_ids = await self.redis.smembers(JOB_INDEX_KEY)
        except Exception:
            return
        for job_id in job_ids:
            raw = await self.redis.get(f"{JOB_KEY_PREFIX}{job_id}")
            if not raw:
                continue
            try:
                cfg     = json.loads(raw)
                minutes = cfg.get("minutes")
                seconds = cfg.get("seconds")
                name    = cfg.get("name")
                if minutes is not None:
                    self.apscheduler.reschedule_job(
                        job_id, trigger=IntervalTrigger(minutes=minutes, timezone=TZ)
                    )
                elif seconds is not None:
                    self.apscheduler.reschedule_job(
                        job_id, trigger=IntervalTrigger(seconds=seconds, timezone=TZ)
                    )
                job = self.apscheduler.get_job(job_id)
                if job and name:
                    job.modify(name=name)
                log.info("scheduler.override_applied", job_id=job_id,
                         minutes=minutes, seconds=seconds)
            except Exception as e:
                log.warning("scheduler.override_failed", job_id=job_id, error=str(e))

    async def _publish_jobs(self):
        """Write all APScheduler jobs to Redis for the WebUI to read.
        Merges with existing Redis records so user overrides are preserved."""
        jobs = self.apscheduler.get_jobs()
        pipe = self.redis.pipeline()
        ids = []
        for j in jobs:
            trigger = j.trigger
            t_type = type(trigger).__name__.replace("Trigger", "").lower()
            interval_min = interval_sec = None
            if hasattr(trigger, "interval"):
                total = int(trigger.interval.total_seconds())
                if total < 60:
                    interval_sec = total
                    schedule = f"every {total}s"
                else:
                    interval_min = total // 60
                    schedule = f"every {interval_min}m"
            elif hasattr(trigger, "fields"):
                parts = [f"{f.name}={f}" for f in trigger.fields if not f.is_default]
                schedule = "cron " + " ".join(parts)
            else:
                schedule = t_type

            # Read existing Redis record to preserve user overrides (name, enabled, etc.)
            existing_raw = await self.redis.get(f"{JOB_KEY_PREFIX}{j.id}")
            existing = json.loads(existing_raw) if existing_raw else {}

            record = json.dumps({
                "id":           j.id,
                "name":         existing.get("name", j.name),
                "schedule":     schedule,
                "trigger_type": t_type,
                "next_run":     j.next_run_time.isoformat() if j.next_run_time else None,
                "enabled":      existing.get("enabled", True),
                "command":      existing.get("command", "trigger"),
                "minutes":      interval_min,
                "seconds":      interval_sec,
                "payload":      existing.get("payload"),
            })
            pipe.set(f"{JOB_KEY_PREFIX}{j.id}", record, ex=3600)
            ids.append(j.id)
        pipe.delete(JOB_INDEX_KEY)
        if ids:
            pipe.sadd(JOB_INDEX_KEY, *ids)
        await pipe.execute()
        log.info("scheduler.jobs_published", count=len(ids))

    async def _reload_loop(self):
        """Subscribe to scheduler:reload pub/sub and apply interval changes to APScheduler."""
        import json as _json
        log.info("scheduler.reload_loop_start")
        while self._running:
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("scheduler:reload")
                async for message in pubsub.listen():
                    if not self._running:
                        return
                    if message.get("type") != "message":
                        continue
                    payload = message.get("data", "")
                    if payload.startswith("delete:"):
                        job_id = payload[7:]
                        try:
                            self.apscheduler.remove_job(job_id)
                            log.info("scheduler.job_removed", job_id=job_id)
                        except Exception:
                            pass
                        continue
                    job_id = payload
                    raw = await self.redis.get(f"scheduler:job:{job_id}")
                    if not raw:
                        continue
                    try:
                        record = _json.loads(raw)
                        minutes = record.get("minutes")
                        seconds = record.get("seconds")
                        if minutes is not None:
                            self.apscheduler.reschedule_job(
                                job_id, trigger=IntervalTrigger(minutes=minutes, timezone=TZ)
                            )
                            log.info("scheduler.job_rescheduled", job_id=job_id, minutes=minutes)
                        elif seconds is not None:
                            self.apscheduler.reschedule_job(
                                job_id, trigger=IntervalTrigger(seconds=seconds, timezone=TZ)
                            )
                            log.info("scheduler.job_rescheduled", job_id=job_id, seconds=seconds)
                        if record.get("name"):
                            job = self.apscheduler.get_job(job_id)
                            if job:
                                job.modify(name=record["name"])
                        await self._publish_jobs()
                    except Exception as e:
                        log.error("scheduler.reload_error", job_id=job_id, error=str(e))
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("scheduler.reload_loop_reconnect", error=str(e))
                await asyncio.sleep(5)
                try:
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()
                except Exception:
                    pass

    async def _idle_loop(self):
        """Keep the process alive, refresh job next_run times in Redis."""
        while self._running:
            await self._publish_jobs()
            log.debug("scheduler.status", active_jobs=len(self.apscheduler.get_jobs()))
            await asyncio.sleep(60)  # refresh every minute


async def main():
    logging.basicConfig(level=logging.INFO)
    sched = Scheduler()
    await sched.start()


if __name__ == "__main__":
    asyncio.run(main())
