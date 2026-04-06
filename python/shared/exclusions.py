"""
Shared exclusion enforcement for all trader agents.

Reads user:exclusions from Redis and blocks trades on:
  - Excluded tickers (exact match)
  - Excluded sectors (e.g. "Healthcare")
  - Excluded industries / sub-sectors (e.g. "Biotechnology")

Classification source priority:
  1. Redis cache  (ticker:sectors / ticker:industries)
  2. Yahoo Finance MCP  (primary — GICS sector + industry)
  3. Massive MCP        (fallback — sector only via SIC mapping)
"""
import json
import structlog
from shared.mcp_client import get_classification

log = structlog.get_logger("shared.exclusions")

USER_EXCLUSIONS_KEY = "user:exclusions"


def _norm(s: str) -> str:
    """Normalize for comparison: lowercase, drop spaces/hyphens/ampersands."""
    return s.lower().replace(" ", "").replace("-", "").replace("&", "and")


async def _resolve_classification(redis, ticker: str) -> tuple[str, str]:
    """
    Return (sector, industry) for ticker.
    Checks Redis cache first; calls Yahoo → Massive on miss and backfills cache.
    """
    t = ticker.upper()
    sector   = await redis.hget("ticker:sectors",    t) or ""
    industry = await redis.hget("ticker:industries", t) or ""

    if sector and industry:
        return sector, industry

    cls = await get_classification(ticker)
    new_sector   = cls.get("sector", "")
    new_industry = cls.get("industry", "")

    if new_sector and not sector:
        await redis.hset("ticker:sectors",    t, new_sector)
        sector = new_sector
    if new_industry and not industry:
        await redis.hset("ticker:industries", t, new_industry)
        industry = new_industry

    return sector, industry


async def is_excluded(redis, ticker: str) -> bool:
    """
    Returns True (and logs) if the ticker, its sector, or its industry is in
    user:exclusions.  Fails open (False) on Redis error or missing config.
    """
    try:
        raw = await redis.get(USER_EXCLUSIONS_KEY)
        if not raw:
            return False
        excl = json.loads(raw)

        excluded_tickers    = {t.upper() for t in excl.get("tickers", [])}
        excluded_sectors    = {_norm(s) for s in excl.get("sectors",    [])}
        excluded_industries = {_norm(i) for i in excl.get("industries", [])}

        if ticker.upper() in excluded_tickers:
            log.info("exclusion.ticker_blocked", ticker=ticker)
            return True

        if excluded_sectors or excluded_industries:
            sector, industry = await _resolve_classification(redis, ticker)

            if sector and _norm(sector) in excluded_sectors:
                log.info("exclusion.sector_blocked",
                         ticker=ticker, sector=sector)
                return True

            if industry and _norm(industry) in excluded_industries:
                log.info("exclusion.industry_blocked",
                         ticker=ticker, industry=industry)
                return True

    except Exception as e:
        log.warning("exclusion.check_failed", ticker=ticker, error=str(e))

    return False
