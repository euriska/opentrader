"""
Aggregator Combiner
Merges per-ticker sentiment from multiple scrapers with yfinance
fundamentals (analyst ratings, dividends, earnings) into TickerIntelligence.
"""
import asyncio
import os
import time
from datetime import date, datetime, timezone
from typing import Optional
import structlog

from .models import TickerIntelligence

log = structlog.get_logger("aggregator.combiner")

EARNINGS_BUFFER_DAYS = int(os.getenv("EARNINGS_BUFFER_DAYS", "5"))

# Source weights for combined sentiment score
SOURCE_WEIGHTS = {
    "wsb":       0.25,   # social/retail crowd
    "seekalpha": 0.40,   # professional analysis
    "yahoo":     0.35,   # broad market consensus
}


def build_intelligence(
    ticker:         str,
    sentiment_data: dict,   # source → {mention_count, sentiment_score, sentiment_label, headlines}
    yf_data:        dict,   # from _fetch_yfinance()
    current_price:  float = 0.0,
) -> TickerIntelligence:
    """Combine all sources into a single TickerIntelligence object."""
    intel = TickerIntelligence(ticker=ticker)
    sources = []

    # ── Sentiment ─────────────────────────────────────────────────────────────
    weighted_sum   = 0.0
    weight_total   = 0.0
    all_headlines  = []
    wsb_mentions   = 0
    wsb_sentiment  = 0.0

    for source, w in SOURCE_WEIGHTS.items():
        d = sentiment_data.get(source)
        if not d:
            continue
        sources.append(source)
        score = float(d.get("sentiment_score", 0.0))
        weighted_sum  += score * w
        weight_total  += w
        headlines      = d.get("headlines", [])
        if isinstance(headlines, list):
            all_headlines.extend(headlines[:2])

        if source == "wsb":
            wsb_mentions  = int(d.get("mention_count", 0))
            wsb_sentiment = score

    if weight_total > 0:
        intel.sentiment_score = round(weighted_sum / weight_total, 4)
    intel.sentiment_label = (
        "bullish" if intel.sentiment_score > 0.05 else
        "bearish" if intel.sentiment_score < -0.05 else
        "neutral"
    )
    intel.news_headlines  = list(dict.fromkeys(all_headlines))[:5]  # dedup, keep 5
    intel.news_sentiment  = round(weighted_sum / weight_total, 4) if weight_total else 0.0
    intel.social_mentions  = wsb_mentions
    intel.social_sentiment = wsb_sentiment
    intel.social_momentum  = _compute_momentum(wsb_mentions)
    intel.sources          = sources

    # ── Analyst ratings ───────────────────────────────────────────────────────
    analyst = yf_data.get("analyst", {})
    if analyst:
        intel.analyst_count        = analyst.get("count", 0)
        intel.analyst_buy_pct      = analyst.get("buy_pct", 0.0)
        intel.analyst_target_price = analyst.get("target_price", 0.0)
        intel.analyst_consensus    = analyst.get("consensus", "none")
        if current_price > 0 and intel.analyst_target_price > 0:
            intel.analyst_upside_pct = round(
                (intel.analyst_target_price - current_price) / current_price * 100, 2
            )

    # ── Dividends ─────────────────────────────────────────────────────────────
    div = yf_data.get("dividend", {})
    if div:
        intel.dividend_yield   = div.get("yield", 0.0)
        intel.dividend_annual  = div.get("annual", 0.0)
        intel.dividend_ex_date = div.get("ex_date")

    # ── Earnings ──────────────────────────────────────────────────────────────
    earnings = yf_data.get("earnings", {})
    if earnings:
        intel.earnings_date      = earnings.get("date")
        intel.earnings_days_away = earnings.get("days_away")
        intel.earnings_too_close = (
            intel.earnings_days_away is not None
            and intel.earnings_days_away <= EARNINGS_BUFFER_DAYS
        )

    # ── Confidence delta ──────────────────────────────────────────────────────
    intel.confidence_delta = _compute_delta(intel)

    # ── Actionable flag ───────────────────────────────────────────────────────
    intel.actionable = bool(sources) and not intel.earnings_too_close

    # ── Summary ───────────────────────────────────────────────────────────────
    intel.summary = _build_summary(intel)

    return intel


def _compute_momentum(wsb_mentions: int) -> str:
    if wsb_mentions >= 10:
        return "rising"
    if wsb_mentions >= 3:
        return "flat"
    return "flat"


def _compute_delta(intel: TickerIntelligence) -> float:
    """
    Compute confidence adjustment (-0.20 → +0.20) based on intel signals.
    Positive factors: strong sentiment, analyst buy, upside, social momentum
    Negative factors: bearish sentiment, sell consensus, earnings risk
    """
    delta = 0.0

    # Sentiment contribution (max ±0.08)
    delta += intel.sentiment_score * 0.08

    # Analyst consensus (max ±0.06)
    consensus_map = {
        "strong_buy":  0.06,
        "buy":         0.04,
        "hold":        0.00,
        "sell":        -0.04,
        "strong_sell": -0.06,
        "none":        0.00,
    }
    delta += consensus_map.get(intel.analyst_consensus, 0.0)

    # Analyst upside > 15% adds +0.03, < -5% subtracts 0.03
    if intel.analyst_upside_pct > 15:
        delta += 0.03
    elif intel.analyst_upside_pct < -5:
        delta -= 0.03

    # Social momentum
    if intel.social_momentum == "rising" and intel.social_sentiment > 0:
        delta += 0.03

    # Earnings proximity penalty
    if intel.earnings_days_away is not None:
        if intel.earnings_days_away <= EARNINGS_BUFFER_DAYS:
            delta -= 0.20  # hard penalty — predictor will filter this out anyway

    return round(max(-0.20, min(0.20, delta)), 4)


def _build_summary(intel: TickerIntelligence) -> str:
    parts = []

    if intel.sentiment_label != "neutral":
        parts.append(f"{intel.sentiment_label} sentiment ({intel.sentiment_score:+.2f})")

    if intel.analyst_consensus not in ("none", "hold", ""):
        upside_str = f", {intel.analyst_upside_pct:+.1f}% upside" if intel.analyst_upside_pct else ""
        parts.append(
            f"{intel.analyst_consensus.replace('_', ' ')} consensus "
            f"({intel.analyst_buy_pct:.0%} buy{upside_str})"
        )

    if intel.social_mentions >= 3:
        parts.append(f"WSB {intel.social_mentions} mentions")

    if intel.earnings_too_close and intel.earnings_date:
        parts.append(f"⚠ earnings in {intel.earnings_days_away}d ({intel.earnings_date})")

    if intel.dividend_yield > 0:
        parts.append(f"div yield {intel.dividend_yield:.1f}%")

    return " | ".join(parts) if parts else "no actionable intelligence"


async def fetch_yfinance(ticker: str) -> dict:
    """
    Async wrapper around yfinance — runs blocking calls in executor.
    Returns dict with analyst, dividend, earnings sub-dicts.
    """
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _fetch_yfinance_sync, ticker)
    except Exception as e:
        log.warning("combiner.yfinance_error", ticker=ticker, error=str(e))
        return {}


def _fetch_yfinance_sync(ticker: str) -> dict:
    """Synchronous yfinance data fetch — run in thread pool."""
    import yfinance as yf

    stock  = yf.Ticker(ticker)
    result = {"analyst": {}, "dividend": {}, "earnings": {}}

    # ── Analyst ratings ───────────────────────────────────────────────────────
    try:
        recs = stock.recommendations_summary
        if recs is not None and not recs.empty:
            # Use most recent period
            latest = recs.iloc[0]
            strong_buy = int(latest.get("strongBuy", 0))
            buy        = int(latest.get("buy", 0))
            hold       = int(latest.get("hold", 0))
            sell       = int(latest.get("sell", 0))
            strong_sell= int(latest.get("strongSell", 0))
            total      = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                buy_pct  = (strong_buy + buy) / total
                consensus = (
                    "strong_buy"  if buy_pct >= 0.70 else
                    "buy"         if buy_pct >= 0.55 else
                    "hold"        if buy_pct >= 0.35 else
                    "sell"        if buy_pct >= 0.20 else
                    "strong_sell"
                )
                result["analyst"] = {
                    "count":     total,
                    "buy_pct":   round(buy_pct, 4),
                    "consensus": consensus,
                }
    except Exception:
        pass

    try:
        targets = stock.analyst_price_targets
        if isinstance(targets, dict):
            mean = targets.get("mean") or targets.get("current") or 0.0
            result["analyst"]["target_price"] = float(mean) if mean else 0.0
    except Exception:
        pass

    # ── Dividends ─────────────────────────────────────────────────────────────
    try:
        info = stock.fast_info
        div_yield  = getattr(info, "dividend_yield",  None) or 0.0
        div_annual = getattr(info, "last_dividend_value", None) or 0.0
        if div_yield > 0 or div_annual > 0:
            result["dividend"] = {
                "yield":  round(float(div_yield) * 100, 2),
                "annual": round(float(div_annual), 4),
            }
        # Ex-dividend date
        cal = stock.calendar
        if isinstance(cal, dict) and "Ex-Dividend Date" in cal:
            ex = cal["Ex-Dividend Date"]
            result["dividend"]["ex_date"] = str(ex) if ex else None
    except Exception:
        pass

    # ── Earnings ──────────────────────────────────────────────────────────────
    try:
        cal = stock.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                if hasattr(ed, "__iter__") and not isinstance(ed, str):
                    ed = list(ed)[0] if ed else None
                if ed is not None:
                    if hasattr(ed, "date"):
                        ed = ed.date()
                    earnings_date = str(ed)[:10]
                    today         = date.today()
                    target        = date.fromisoformat(earnings_date)
                    days_away     = (target - today).days
                    result["earnings"] = {
                        "date":      earnings_date,
                        "days_away": days_away,
                    }
    except Exception:
        pass

    return result
