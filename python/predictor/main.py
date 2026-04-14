"""
OpenTrader Predictor Agent
Reads scraper data from Redis, scores tickers, optionally enhances
with LLM analysis, and publishes SignalPayload to predictor.signals.
Also persists signals and sentiment to TimescaleDB.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import structlog
from urllib.parse import urlparse, urlunparse, quote

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, GROUPS, REDIS_URL
from shared.envelope import SignalPayload
from notifier.agentmail import Notifier

from .scorer import score_tickers, apply_stops, ScoredTicker

log = structlog.get_logger("predictor")


def _safe_db_url(url: str) -> str:
    """
    Re-encode a postgres URL so that special characters in the password
    (like '@') don't confuse URL parsers.

    Format: scheme://user:password@host[:port]/dbname
    The trick: the LAST '@' before the host is the user@host separator.
    Everything between the first ':' and that last '@' is the password.
    """
    import re
    # Match: scheme://user:PASSWORD@host/db  (password may contain '@')
    m = re.match(
        r'^(postgresql(?:\+\w+)?://)'  # scheme
        r'([^:]+)'                      # username (no colons)
        r':'                            # separator
        r'(.+)'                         # password + rest
        r'@([^@/]+)'                    # LAST @host (no @ in host)
        r'(/.*)?$',                     # /dbname
        url,
    )
    if not m:
        return url
    scheme, user, pw_and_rest, host_port, dbpath = m.groups()
    # pw_and_rest might be "password@extra@host" — strip the host portion
    # We already captured the host, so password is everything before the last @
    # in the original string up to host_port
    # Actually: pw_and_rest = password + potential extra @s
    # We need: find where host_port starts and strip from pw_and_rest
    pw = url[len(scheme) + len(user) + 1 : url.rfind(f"@{host_port}")]
    safe_pw = quote(pw, safe="")
    return f"{scheme}{user}:{safe_pw}@{host_port}{dbpath or ''}"

CMD_STREAM     = STREAMS["commands"]
SIG_STREAM     = STREAMS["signals"]
CONSUMER_GROUP = GROUPS["predictor"]
CONSUMER_NAME  = os.getenv("HOSTNAME", "predictor-0")

DB_URL = os.getenv("DB_URL", "")

# Strategy thresholds (from strategies.toml — kept simple here)
MIN_CONF_EQUITY = float(os.getenv("MIN_CONFIDENCE_EQUITY", "0.70"))
MIN_CONF_ETF    = float(os.getenv("MIN_CONFIDENCE_ETF",    "0.65"))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",         "1.5"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT",       "3.0"))
MAX_SIGNALS     = int(os.getenv("MAX_SIGNALS_PER_RUN",     "10"))

# LLM enhancement — skip if no key or placeholder
_llm_key = os.getenv("OPENROUTER_API_KEY", "")
USE_LLM = bool(_llm_key) and not _llm_key.startswith("your_")


class PredictorAgent(BaseAgent):

    def __init__(self):
        super().__init__("predictor")
        self._db: Optional[asyncpg.Connection] = None

    async def run(self):
        await self.setup()

        # Override Redis with longer timeout for blocking reads
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=15,
            retry_on_timeout=True,
            health_check_interval=30,
        )

        await self._ensure_consumer_group()
        await self._connect_db()

        self._use_llm = await self._check_llm()
        log.info("predictor.starting", llm=self._use_llm)

        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
        )

    async def _check_llm(self) -> bool:
        """Probe the LLM once at startup; disable if 401/unavailable."""
        if not USE_LLM:
            return False
        try:
            import aiohttp
            key = os.getenv("OPENROUTER_API_KEY", "")
            async with aiohttp.ClientSession() as s:
                r = await s.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=8),
                )
                await r.read()  # drain body so connection closes cleanly
                if r.status == 200:
                    return True
                log.warning("predictor.llm_disabled", status=r.status)
                return False
        except Exception as e:
            log.warning("predictor.llm_disabled", error=str(e))
            return False

    async def _connect_db(self):
        if not DB_URL:
            log.warning("predictor.no_db_url")
            return
        # URL-encode password to handle special chars like '@'
        dsn = _safe_db_url(DB_URL)
        for attempt in range(1, 6):
            try:
                self._db = await asyncpg.connect(dsn)
                log.info("predictor.db_connected")
                return
            except Exception as e:
                log.warning("predictor.db_connect_retry",
                            attempt=attempt, error=str(e))
                await asyncio.sleep(5 * attempt)
        log.error("predictor.db_connect_failed")

    async def _ensure_consumer_group(self):
        try:
            await self.redis.xgroup_create(
                CMD_STREAM, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("predictor.group_create", error=str(e))

    async def _command_loop(self):
        log.info("predictor.command_loop_start")
        while self._running:
            try:
                if await self.is_halted():
                    await asyncio.sleep(5)
                    continue

                messages = await self.redis.xreadgroup(
                    groupname    = CONSUMER_GROUP,
                    consumername = CONSUMER_NAME,
                    streams      = {CMD_STREAM: ">"},
                    count        = 10,
                    block        = 5000,
                )
                if not messages:
                    continue

                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_command(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("predictor.command_loop_error", error=str(e))
                await asyncio.sleep(3)
                try:
                    await self.redis.ping()
                except Exception:
                    from shared.redis_client import get_redis
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, data: dict):
        job = data.get("job", "")
        try:
            if data.get("command") == "trigger" and job == "run_predictor":
                await self._run()
        except Exception as e:
            log.error("predictor.handle_error", job=job, error=str(e))
        finally:
            await self.redis.xack(CMD_STREAM, CONSUMER_GROUP, msg_id)

    async def _run(self):
        log.info("predictor.run.start")

        # ── 1. Load OVTLYR scanner data ──────────────────────────────────────
        ovtlyr_raw = await self.redis.hgetall("scanner:ovtlyr:latest")

        if not ovtlyr_raw:
            log.warning("predictor.no_ovtlyr_data")
            return

        ovtlyr_data = {t: json.loads(v) for t, v in ovtlyr_raw.items()}

        # ── 2. Load market breadth ────────────────────────────────────────────
        market_breadth: dict = {}
        try:
            breadth_raw = await self.redis.get("ovtlyr:market_breadth")
            if breadth_raw:
                market_breadth = json.loads(breadth_raw)
                log.info("predictor.market_breadth",
                         breadth_pct=market_breadth.get("breadth_pct"),
                         signal=market_breadth.get("signal"))
        except Exception as e:
            log.warning("predictor.market_breadth_load_error", error=str(e))

        # ── 3. Load aggregator intelligence for each candidate ───────────────
        from aggregator.models import TickerIntelligence
        intel_map: dict = {}
        for ticker in ovtlyr_data:
            raw = await self.redis.get(f"aggregator:intel:{ticker}")
            if raw:
                try:
                    intel_map[ticker] = TickerIntelligence.from_json(raw)
                except Exception:
                    pass

        log.info("predictor.data_loaded",
                 ovtlyr_tickers=len(ovtlyr_data),
                 intel_available=len(intel_map))

        # ── 4. Score ─────────────────────────────────────────────────────────
        min_conf = min(MIN_CONF_EQUITY, MIN_CONF_ETF) - 0.05
        candidates = score_tickers(ovtlyr_data, intel_map,
                                   market_breadth=market_breadth,
                                   min_confidence=min_conf)

        if not candidates:
            log.info("predictor.no_candidates")
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            msg = (
                f"*No Trades Today* 📭\n"
                f"Time: {now}\n"
                f"OVTLYR returned {len(ovtlyr_data)} tickers but none scored above the minimum confidence threshold."
            )
            notifier = Notifier("alerts")
            await asyncio.gather(
                notifier.telegram(msg),
                notifier.discord(msg),
                return_exceptions=True,
            )
            return

        log.info("predictor.scored", candidates=len(candidates))

        # ── 3. Optional LLM enhancement ──────────────────────────────────────
        if self._use_llm and candidates:
            candidates = await self._llm_enhance(candidates[:15])

        # ── 4. Apply strategy filters and compute stops ───────────────────────
        signals: list[ScoredTicker] = []
        for t in candidates:
            threshold = MIN_CONF_ETF if t.asset_class == "etf" else MIN_CONF_EQUITY
            if t.confidence < threshold:
                continue
            t = apply_stops(t, price=None,
                            stop_loss_pct=STOP_LOSS_PCT,
                            take_profit_pct=TAKE_PROFIT_PCT)
            signals.append(t)
            if len(signals) >= MAX_SIGNALS:
                break

        if not signals:
            best = round(candidates[0].confidence, 3) if candidates else 0
            log.info("predictor.below_threshold", best=best)
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            msg = (
                f"*No Trades Today* 📭\n"
                f"Time: {now}\n"
                f"{len(candidates)} candidate(s) evaluated — best confidence {best:.1%} did not clear the required threshold.\n"
                f"Equity min: {MIN_CONF_EQUITY:.0%}  ·  ETF min: {MIN_CONF_ETF:.0%}"
            )
            notifier = Notifier("alerts")
            await asyncio.gather(
                notifier.telegram(msg),
                notifier.discord(msg),
                return_exceptions=True,
            )
            return

        log.info("predictor.signals_ready", count=len(signals))

        # ── 5. Publish to predictor.signals stream ────────────────────────────
        for s in signals:
            payload = SignalPayload(
                ticker      = s.ticker,
                asset_class = s.asset_class,
                direction   = s.direction,
                confidence  = s.confidence,
                entry       = s.entry,
                stop        = s.stop,
                target      = s.target,
                source      = "predictor",
                ttl_ms      = 30 * 60 * 1000,  # 30 minutes
                metadata    = {
                    "ovtlyr_score":        s.ovtlyr_score,
                    "sources":             s.sources,
                    "analyst_consensus":   s.metadata.get("analyst_consensus", "none"),
                    "sentiment_label":     s.metadata.get("sentiment_label", "neutral"),
                    "earnings_date":       s.metadata.get("earnings_date"),
                    "earnings_days_away":  s.metadata.get("earnings_days_away"),
                    "intel_summary":       s.metadata.get("summary", ""),
                },
            )
            await self.redis.xadd(
                SIG_STREAM,
                {
                    "ticker":      payload.ticker,
                    "asset_class": payload.asset_class,
                    "direction":   payload.direction,
                    "confidence":  str(payload.confidence),
                    "source":      payload.source,
                    "ttl_ms":      str(payload.ttl_ms),
                    "metadata":    json.dumps(payload.metadata),
                },
                maxlen=5000,
            )

        log.info("predictor.published", count=len(signals))

        # ── 6. Persist to TimescaleDB ─────────────────────────────────────────
        await self._save_signals(signals)

    async def _llm_enhance(self, candidates: list[ScoredTicker]) -> list[ScoredTicker]:
        """
        Send top candidates to LLM for qualitative ranking and confidence adjustment.
        Returns the same list re-sorted/adjusted, never adds new tickers.
        Gracefully skips on any error.
        """
        from llm.connector import LLMConnector

        summary = "\n".join(
            f"- {t.ticker}: direction={t.direction}, conf={t.confidence:.2f}, "
            f"ovtlyr={t.ovtlyr_score:.0f}, asset={t.asset_class}, "
            f"analyst={t.metadata.get('analyst_consensus','none')}, "
            f"sentiment={t.metadata.get('sentiment_label','neutral')}, "
            f"intel={t.metadata.get('summary','')}"
            for t in candidates
        )

        system = (
            "You are a quantitative trading signal validator. "
            "Evaluate momentum signals for quality and rank them. "
            "Be conservative — prefer fewer high-conviction signals over many weak ones."
        )
        prompt = (
            f"Today's date: {__import__('datetime').date.today().isoformat()}\n\n"
            f"The following tickers have been flagged by OVTLYR momentum screener:\n\n{summary}\n\n"
            "Return a JSON array of objects with fields: "
            "ticker, keep (true/false), confidence_adjustment (float -0.15 to +0.15), reason (short string). "
            "Only include tickers where you have meaningful insight. "
            "Reject tickers with obvious concerns (very low float, earnings risk, "
            "contradictory signals, no volume context)."
        )

        try:
            llm = LLMConnector("predictor")
            result = await llm.complete_json(prompt=prompt, system=system, max_tokens=800)
        except Exception as e:
            log.warning("predictor.llm_failed", error=str(e))
            return candidates

        # Build lookup from LLM result
        adjustments: dict[str, dict] = {}
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and "ticker" in item:
                    adjustments[item["ticker"]] = item

        enhanced = []
        for t in candidates:
            adj = adjustments.get(t.ticker, {})
            if adj.get("keep") is False:
                log.info("predictor.llm_rejected",
                         ticker=t.ticker, reason=adj.get("reason", ""))
                continue
            delta = float(adj.get("confidence_adjustment", 0.0))
            t.confidence = round(max(0.0, min(1.0, t.confidence + delta)), 4)
            if adj.get("reason"):
                t.metadata["llm_reason"] = adj["reason"]
            enhanced.append(t)

        enhanced.sort(key=lambda x: x.confidence, reverse=True)
        log.info("predictor.llm_enhanced",
                 before=len(candidates), after=len(enhanced))
        return enhanced

    async def _save_signals(self, signals: list[ScoredTicker]):
        if not self._db:
            return
        try:
            await self._db.executemany(
                """
                INSERT INTO signals (source, ticker, direction, confidence, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                [
                    (
                        "predictor",
                        s.ticker,
                        s.direction,
                        s.confidence,
                        json.dumps({
                            "asset_class":   s.asset_class,
                            "ovtlyr_score":       s.ovtlyr_score,
                            "sources":            s.sources,
                            "analyst_consensus":  s.metadata.get("analyst_consensus", "none"),
                            "sentiment_label":    s.metadata.get("sentiment_label", "neutral"),
                            "earnings_date":      s.metadata.get("earnings_date"),
                            "intel_summary":      s.metadata.get("summary", ""),
                            "entry":         s.entry,
                            "stop":          s.stop,
                            "target":        s.target,
                            "llm_reason":    s.metadata.get("llm_reason"),
                        }),
                    )
                    for s in signals
                ],
            )
            log.info("predictor.db_saved", count=len(signals))
        except Exception as e:
            log.error("predictor.db_save_failed", error=str(e))

    async def shutdown(self):
        self._running = False
        if self._db:
            await self._db.close()
        if self.redis:
            await self.redis.aclose()


async def main():
    agent = PredictorAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
