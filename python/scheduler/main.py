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
from apscheduler.schedulers.asyncio  import AsyncIOScheduler
from apscheduler.triggers.cron       import CronTrigger
from apscheduler.triggers.interval   import IntervalTrigger
from apscheduler.triggers.combining  import OrTrigger

from shared.base_agent   import BaseAgent
from shared.redis_client import get_redis
from .jobs import (
    job_scrape_ovtlyr,
    job_scrape_position_intel,
    job_scrape_sentiment,
    job_score_ticker_sentiment,
    job_predict,
    job_heartbeat_check,
    job_market_open,
    job_market_close,
    job_eod_report,
    job_pre_market_prep,
    job_daily_summary,
    job_options_report,
)
from .calendar import TZ

log = structlog.get_logger("scheduler")

SCRAPE_INTERVAL  = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "3"))


def _build_intraday_trigger(start_h: int, start_m: int, end_h: int, end_m: int,
                             interval_min: int, days: str = "mon-fri") -> OrTrigger:
    """
    Build an OrTrigger that fires every `interval_min` minutes between
    start_h:start_m and end_h:end_m on the given days (APScheduler dow string).
    """
    slots = []
    t     = start_h * 60 + start_m
    end_t = end_h   * 60 + end_m
    while t <= end_t:
        h, m = divmod(t, 60)
        slots.append((h, m))
        t += interval_min
    triggers = [
        CronTrigger(day_of_week=days, hour=h, minute=m, timezone=TZ)
        for h, m in slots
    ]
    return OrTrigger(triggers) if len(triggers) > 1 else triggers[0]
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

        self.apscheduler.add_job(
            job_options_report,
            CronTrigger(hour=13, minute=0, day_of_week="mon-fri", timezone=TZ),
            args=[r], id="options_report",
            name="Daily options positions report email (1pm ET)",
            replace_existing=True,
        )

        self.apscheduler.add_job(
            job_score_ticker_sentiment,
            CronTrigger(hour=16, minute=20, timezone=TZ),
            args=[r], id="score_ticker_sentiment",
            name="F&G sentiment scoring (positions + signals)",
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
            job_scrape_position_intel,
            IntervalTrigger(minutes=SCRAPE_INTERVAL, timezone=TZ),
            args=[r], id="scrape_position_intel",
            name=f"OVTLYR position intel every {SCRAPE_INTERVAL}m",
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
                cfg              = json.loads(raw)
                minutes          = cfg.get("minutes")
                seconds          = cfg.get("seconds")
                name             = cfg.get("name")
                intraday_start   = cfg.get("intraday_start")
                intraday_end     = cfg.get("intraday_end")
                intraday_iv      = cfg.get("intraday_interval_min")
                intraday_days    = cfg.get("intraday_days", "mon-fri")
                # Fallback: parse intraday/cron+interval fields from schedule string if explicit fields absent
                if not (intraday_start and intraday_end and intraday_iv):
                    import re as _re
                    sched_str = cfg.get("schedule", "")
                    # intraday format
                    m = _re.match(
                        r'intraday start=([\d:]+) interval=(\d+)m end=([\d:]+)(?:\s+days=([\w,]+))?',
                        sched_str
                    )
                    if m:
                        intraday_start = m.group(1)
                        intraday_iv    = m.group(2)
                        intraday_end   = m.group(3)
                        intraday_days  = m.group(4) or "mon-fri"
                    else:
                        # cron+interval format: "cron hour=9 minute=30 ... interval=15m until=16:00"
                        hi = _re.search(r'hour=(\d+)', sched_str)
                        mi = _re.search(r'minute=(\d+)', sched_str)
                        ii = _re.search(r'interval=(\d+)m', sched_str)
                        ui = _re.search(r'until=([\d:]+)', sched_str)
                        di = _re.search(r'days=([\w,]+)', sched_str)
                        if hi and mi and ii and ui:
                            intraday_start = f"{hi.group(1)}:{mi.group(1).zfill(2)}"
                            intraday_iv    = ii.group(1)
                            intraday_end   = ui.group(1)
                            intraday_days  = di.group(1) if di else "mon-fri"
                if intraday_start and intraday_end and intraday_iv:
                    sh, sm = map(int, intraday_start.split(":"))
                    eh, em = map(int, intraday_end.split(":"))
                    trigger = _build_intraday_trigger(sh, sm, eh, em, int(intraday_iv), intraday_days)
                    self.apscheduler.reschedule_job(job_id, trigger=trigger)
                elif minutes is not None:
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
                         minutes=minutes, seconds=seconds, intraday=bool(intraday_start))
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

            # Read existing Redis record FIRST to preserve user overrides
            existing_raw = await self.redis.get(f"{JOB_KEY_PREFIX}{j.id}")
            existing = json.loads(existing_raw) if existing_raw else {}

            if hasattr(trigger, "interval"):
                total = int(trigger.interval.total_seconds())
                if total < 60:
                    interval_sec = total
                    schedule_generated = f"every {total}s"
                else:
                    interval_min = total // 60
                    schedule_generated = f"every {interval_min}m"
            elif t_type == "or":
                schedule_generated = "intraday"
            elif hasattr(trigger, "fields"):
                parts = [f"{f.name}={f}" for f in trigger.fields if not f.is_default]
                schedule_generated = "cron " + " ".join(parts)
            else:
                schedule_generated = t_type
            # Prefer the user-saved schedule string; fall back to generated only when absent
            schedule = existing.get("schedule") or schedule_generated

            record = json.dumps({
                "id":                   j.id,
                "name":                 existing.get("name", j.name),
                "schedule":             schedule,
                "trigger_type":         t_type,
                "next_run":             j.next_run_time.isoformat() if j.next_run_time else None,
                "enabled":              existing.get("enabled", True),
                "notify":               existing.get("notify", True),
                "command":              existing.get("command", "trigger"),
                "minutes":              interval_min,
                "seconds":              interval_sec,
                "payload":              existing.get("payload"),
                "intraday_start":       existing.get("intraday_start"),
                "intraday_end":         existing.get("intraday_end"),
                "intraday_interval_min": existing.get("intraday_interval_min"),
                "intraday_days":        existing.get("intraday_days"),
                # Preserve execution history — written by @tracked on each run
                "last_run":             existing.get("last_run"),
                "last_status":          existing.get("last_status"),
                "last_error":           existing.get("last_error"),
                "run_count":            existing.get("run_count", 0),
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
                        minutes          = record.get("minutes")
                        seconds          = record.get("seconds")
                        intraday_start   = record.get("intraday_start")
                        intraday_end     = record.get("intraday_end")
                        intraday_iv      = record.get("intraday_interval_min")
                        intraday_days    = record.get("intraday_days", "mon-fri")
                        if not (intraday_start and intraday_end and intraday_iv):
                            import re as _re2
                            sched_str = record.get("schedule", "")
                            hi = _re2.search(r'hour=(\d+)', sched_str)
                            mi = _re2.search(r'minute=(\d+)', sched_str)
                            ii = _re2.search(r'interval=(\d+)m', sched_str)
                            ui = _re2.search(r'until=([\d:]+)', sched_str)
                            di = _re2.search(r'days=([\w,]+)', sched_str)
                            if hi and mi and ii and ui:
                                intraday_start = f"{hi.group(1)}:{mi.group(1).zfill(2)}"
                                intraday_iv    = ii.group(1)
                                intraday_end   = ui.group(1)
                                intraday_days  = di.group(1) if di else "mon-fri"
                        if intraday_start and intraday_end and intraday_iv:
                            sh, sm = map(int, intraday_start.split(":"))
                            eh, em = map(int, intraday_end.split(":"))
                            trigger = _build_intraday_trigger(sh, sm, eh, em, int(intraday_iv), intraday_days)
                            self.apscheduler.reschedule_job(job_id, trigger=trigger)
                            log.info("scheduler.job_rescheduled_intraday", job_id=job_id,
                                     start=intraday_start, end=intraday_end, interval=intraday_iv)
                        elif minutes is not None:
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
