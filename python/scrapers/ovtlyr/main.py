"""
OVTLYR Market Scanner Agent
Scrapes OVTLYR for momentum signals and publishes to scanner.signals stream.
This is the PRIMARY signal source for the predictor — not a sentiment scraper.
"""
import asyncio
import json
import os
import structlog
from datetime import datetime
from typing import Optional

import asyncpg

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from .scraper import OvtlyrScraper

log = structlog.get_logger("scraper-ovtlyr")

CMD_STREAM     = STREAMS["commands"]
SCANNER_STREAM = STREAMS["scanner"]
CONSUMER_GROUP = GROUPS["scraper-ovtlyr"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "scraper-ovtlyr-0")
MAX_TICKERS    = int(os.getenv("MAX_OVTLYR_TICKERS", "30"))
DB_URL         = os.getenv("DB_URL", "")


class OvtlyrScraperAgent(BaseAgent):

    def __init__(self):
        super().__init__("scraper-ovtlyr")
        self.ovtlyr = OvtlyrScraper()
        self._ready = False
        self._db: Optional[asyncpg.Pool] = None

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=None,
        )
        if DB_URL:
            from urllib.parse import urlparse, unquote
            try:
                # asyncpg's own URL parser breaks on unencoded '@' in passwords;
                # parse with stdlib and pass kwargs instead.
                p = urlparse(DB_URL)
                db_kwargs = dict(
                    host=p.hostname, port=p.port or 5432,
                    user=p.username,
                    password=unquote(p.password) if p.password else None,
                    database=p.path.lstrip("/"),
                )
                self._db = await asyncpg.create_pool(
                    min_size=1, max_size=3,
                    max_inactive_connection_lifetime=300,
                    **db_kwargs,
                )
                log.info("scraper-ovtlyr.db_connected")
            except Exception as e:
                log.error("scraper-ovtlyr.db_connect_failed", error=str(e))
        await self._ensure_group()
        asyncio.create_task(self._init_ovtlyr())
        log.info("scraper-ovtlyr.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _init_ovtlyr(self):
        try:
            await self.ovtlyr.start()
            self._ready = True
            log.info("scraper-ovtlyr.ready")
        except Exception as e:
            log.error("scraper-ovtlyr.init_failed", error=str(e))

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(
                CMD_STREAM, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("scraper-ovtlyr.group_error", error=str(e))

    async def _command_loop(self):
        log.info("scraper-ovtlyr.command_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue
                messages = await self.redis.xreadgroup(
                    groupname=CONSUMER_GROUP, consumername=CONSUMER_NAME,
                    streams={CMD_STREAM: ">"}, count=5, block=5000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_command(msg_id, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("scraper-ovtlyr.command_loop_error", error=str(e))
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger":
                if job == "scrape_ovtlyr":
                    await self._run_scrape()
                elif job == "scrape_position_intel":
                    await self._run_position_intel()
                elif job == "pre_market_prep":
                    await self._warmup()
        except Exception as e:
            log.error("scraper-ovtlyr.handle_error", job=job, error=str(e))
        finally:
            await self.redis.xack(CMD_STREAM, CONSUMER_GROUP, msg_id)

    async def _run_scrape(self):
        if not self._ready:
            log.warning("scraper-ovtlyr.not_ready")
            return

        log.info("scraper-ovtlyr.scrape_start")
        try:
            tickers = await self.ovtlyr.scrape()
        except Exception as e:
            log.error("scraper-ovtlyr.scrape_failed", error=str(e))
            return

        if not tickers:
            log.warning("scraper-ovtlyr.no_tickers")
            return

        pipe = self.redis.pipeline()
        published = 0
        for t in tickers[:MAX_TICKERS]:
            entry = {
                "ticker":    t.ticker,
                "direction": t.direction,
                "score":     str(round(t.score, 2)),
                "price":     str(t.price or ""),
                "sector":    t.sector or "",
                "ts_utc":    str(t.ts_utc),
            }
            await self.redis.xadd(SCANNER_STREAM, entry, maxlen=10_000)
            pipe.hset(
                "scanner:ovtlyr:latest",
                t.ticker,
                json.dumps({
                    "direction": t.direction,
                    "score":     t.score,
                    "price":     t.price,
                    "sector":    t.sector,
                    "ts_utc":    t.ts_utc,
                }),
            )
            published += 1

        pipe.expire("scanner:ovtlyr:latest", 86400)  # 24h — outlasts any schedule interval
        await pipe.execute()
        log.info("scraper-ovtlyr.published", count=published)

        # Enrich each candidate with per-ticker dashboard data (nine_score, fear_greed, oscillator)
        await self._enrich_candidates([t.ticker for t in tickers[:MAX_TICKERS]])

        # Scrape all list types and save to DB
        await self._run_list_scrape()

    async def _enrich_candidates(self, ticker_list: list[str]):
        """
        Call scrape_ticker() for each watchlist candidate and write nine_score,
        oscillator, fear_greed, and the actual dashboard signal back into
        scanner:ovtlyr:latest so the predictor can use them.
        """
        log.info("scraper-ovtlyr.enrich_start", tickers=len(ticker_list))
        enriched = 0
        pipe = self.redis.pipeline()

        for ticker in ticker_list:
            try:
                data = await self.ovtlyr.scrape_ticker(ticker)
                if not data:
                    continue

                # Load the existing entry so we can merge into it
                existing_raw = await self.redis.hget("scanner:ovtlyr:latest", ticker)
                existing = json.loads(existing_raw) if existing_raw else {}

                # Apply dashboard signal direction if available (overrides bull-list default)
                raw_signal = data.get("signal", "")
                if raw_signal:
                    if raw_signal.lower() in ("sell", "short", "bear"):
                        existing["direction"] = "short"
                    elif raw_signal.lower() in ("buy", "long", "bull"):
                        existing["direction"] = "long"

                # Merge enrichment fields
                if data.get("nine_score") is not None:
                    existing["nine_score"] = data["nine_score"]
                if data.get("oscillator"):
                    existing["oscillator"] = data["oscillator"]
                if data.get("fear_greed") is not None:
                    existing["fear_greed"] = data["fear_greed"]
                if data.get("signal_active") is not None:
                    existing["signal_active"] = data["signal_active"]
                if data.get("signal_date_str"):
                    existing["signal_date"] = data["signal_date_str"]

                pipe.hset("scanner:ovtlyr:latest", ticker, json.dumps(existing))
                enriched += 1

            except Exception as e:
                log.warning("scraper-ovtlyr.enrich_ticker_error", ticker=ticker, error=str(e))

        await pipe.execute()
        log.info("scraper-ovtlyr.enrich_done", enriched=enriched, total=len(ticker_list))

    async def _get_position_tickers(self) -> list[str]:
        """
        Collect open position tickers.
        Primary: broker:position_tickers Redis key written by the webui on each positions fetch.
        Fallback: DB trades table (status='open').
        """
        import json as _json

        # Primary: webui writes this key after every /api/broker/positions call
        try:
            raw = await self.redis.get("broker:position_tickers")
            if raw:
                tickers = _json.loads(raw)
                if isinstance(tickers, list) and tickers:
                    return tickers
        except Exception as e:
            log.warning("scraper-ovtlyr.position_tickers_redis_error", error=str(e))

        # Fallback: DB trades table
        if self._db:
            try:
                rows = await self._db.fetch(
                    "SELECT DISTINCT ticker FROM trades WHERE status = 'open' ORDER BY ticker"
                )
                return [r["ticker"] for r in rows]
            except Exception as e:
                log.warning("scraper-ovtlyr.position_tickers_db_error", error=str(e))

        return []

    async def _run_position_intel(self):
        """Scrape OVTLYR dashboard for each open position ticker and store in Redis/DB."""
        if not self._ready:
            log.warning("scraper-ovtlyr.position_intel_not_ready")
            return

        # Always include market benchmarks; add open position tickers
        base_tickers = ["SPY", "QQQ"]
        position_tickers = await self._get_position_tickers()

        # Deduplicate while preserving order: benchmarks first, then positions
        seen = set(base_tickers)
        tickers = list(base_tickers)
        for t in position_tickers:
            if t not in seen:
                seen.add(t)
                tickers.append(t)

        if not position_tickers:
            log.info("scraper-ovtlyr.position_intel_no_open_positions_scraping_benchmarks")

        log.info("scraper-ovtlyr.position_intel_start", tickers=tickers)
        scraped = 0

        for ticker in tickers:
            try:
                data = await self.ovtlyr.scrape_ticker(ticker)
                if not data:
                    continue

                # Parse signal_date from string if present
                signal_date = None
                date_str = data.get("signal_date_str", "")
                if date_str:
                    try:
                        signal_date = datetime.strptime(date_str, "%b %d, %Y").date()
                    except ValueError:
                        pass

                # Always cache in Redis (primary path — works without DB)
                await self.redis.hset(
                    "ovtlyr:position_intel",
                    ticker,
                    json.dumps({**data, "ts": datetime.utcnow().isoformat()}),
                )
                scraped += 1

                # Persist to DB if available
                if self._db:
                    try:
                        await self._db.execute(
                            """
                            INSERT INTO ovtlyr_intel
                                (ticker, signal, signal_active, signal_date, nine_score,
                                 oscillator, fear_greed, last_close, avg_vol_30d, raw)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                            """,
                            ticker,
                            data.get("signal"),
                            data.get("signal_active"),
                            signal_date,
                            data.get("nine_score"),
                            data.get("oscillator"),
                            data.get("fear_greed"),
                            data.get("last_close"),
                            data.get("avg_vol_30d"),
                            json.dumps({k: v for k, v in data.items() if k != "signal_date_str"}),
                        )
                    except Exception as db_err:
                        log.warning("scraper-ovtlyr.position_intel_db_insert_error",
                                    ticker=ticker, error=str(db_err))

            except Exception as e:
                log.error("scraper-ovtlyr.position_intel_ticker_error",
                          ticker=ticker, error=str(e))

        await self.redis.expire("ovtlyr:position_intel", 7200)
        log.info("scraper-ovtlyr.position_intel_done", scraped=scraped, total=len(tickers))

    async def _run_list_scrape(self):
        """Scrape Bull/Bear/Market Leaders/Alpha Picks lists and store in DB."""
        if not self._db:
            return
        try:
            lists = await self.ovtlyr.scrape_lists()
        except Exception as e:
            log.error("scraper-ovtlyr.list_scrape_failed", error=str(e))
            return

        inserted = 0
        for list_type, entries in lists.items():
            if not entries:
                continue
            for entry in entries:
                if not entry.get("ticker"):
                    continue
                try:
                    sig_date = None
                    if entry.get("signal_date"):
                        from datetime import date as _date
                        sig_date = _date.fromisoformat(entry["signal_date"])
                    await self._db.execute(
                        """
                        INSERT INTO ovtlyr_lists
                            (list_type, ticker, name, sector, signal, signal_date, last_price, avg_vol_30d)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        """,
                        list_type,
                        entry["ticker"],
                        entry.get("name"),
                        entry.get("sector"),
                        entry.get("signal"),
                        sig_date,
                        float(entry["last_price"]) if entry.get("last_price") else None,
                        entry.get("avg_vol_30d"),
                    )
                    inserted += 1
                except Exception as e:
                    log.error("scraper-ovtlyr.list_insert_error",
                              ticker=entry.get("ticker"), error=str(e))

        log.info("scraper-ovtlyr.lists_saved", inserted=inserted)

        # Cache latest snapshot per list in Redis for fast API reads
        import json as _json
        pipe = self.redis.pipeline()
        for list_type, entries in lists.items():
            if entries:
                pipe.set(
                    f"ovtlyr:list:{list_type}",
                    _json.dumps(entries),
                    ex=7200,
                )
        await pipe.execute()

        # Compute and store market breadth
        await self._store_breadth(lists)

    async def _store_breadth(self, lists: dict):
        """
        Compute market breadth = bull / (bull + bear) * 100.
        Detect crossovers vs previous reading, cache in Redis, persist to DB.
        """
        import json as _json
        bull_count  = len(lists.get("bull", []))
        bear_count  = len(lists.get("bear", []))
        total_count = bull_count + bear_count
        if total_count == 0:
            return

        breadth_pct = round(bull_count / total_count * 100, 2)

        # Detect crossover vs previous reading
        signal = "bullish" if breadth_pct >= 50 else "bearish"
        try:
            prev_raw = await self.redis.get("ovtlyr:market_breadth")
            if prev_raw:
                prev = _json.loads(prev_raw)
                prev_pct = float(prev.get("breadth_pct", breadth_pct))
                if prev_pct < 50 and breadth_pct >= 50:
                    signal = "bullish_cross"
                elif prev_pct >= 50 and breadth_pct < 50:
                    signal = "bearish_cross"
        except Exception:
            pass

        ts_iso = datetime.utcnow().isoformat()
        snapshot = {
            "bull_count":  bull_count,
            "bear_count":  bear_count,
            "total_count": total_count,
            "breadth_pct": breadth_pct,
            "signal":      signal,
            "ts":          ts_iso,
        }

        pipe = self.redis.pipeline()
        # Current snapshot
        pipe.set("ovtlyr:market_breadth", _json.dumps(snapshot), ex=86400)
        # Rolling history (last 200 readings ≈ ~10 hours of 3-min data)
        pipe.lpush("ovtlyr:market_breadth:history", _json.dumps({
            "breadth_pct": breadth_pct,
            "signal":      signal,
            "ts":          ts_iso,
        }))
        pipe.ltrim("ovtlyr:market_breadth:history", 0, 199)
        await pipe.execute()

        # Persist to DB
        if self._db:
            try:
                await self._db.execute(
                    """
                    INSERT INTO ovtlyr_breadth (bull_count, bear_count, total_count, breadth_pct, signal, raw)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    bull_count, bear_count, total_count, breadth_pct, signal,
                    _json.dumps(snapshot),
                )
            except Exception as e:
                log.warning("scraper-ovtlyr.breadth_db_insert_error", error=str(e))

        log.info("scraper-ovtlyr.breadth_stored",
                 breadth_pct=breadth_pct, bull=bull_count, bear=bear_count, signal=signal)

    async def _warmup(self):
        if self._ready:
            await self.ovtlyr.warmup()
        else:
            asyncio.create_task(self._init_ovtlyr())

    async def shutdown(self):
        self._running = False
        await self.ovtlyr.close()
        if self._db:
            await self._db.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = OvtlyrScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
