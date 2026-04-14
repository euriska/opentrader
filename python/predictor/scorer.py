"""
Predictor Scorer
Scores OVTLYR momentum signals enriched with TickerIntelligence from the
aggregator. Applies earnings filter, confidence delta, nine_score weighting,
and market breadth directional filter.

Confidence formula:
  base_conf  = weighted blend of ovtlyr_score (70%) + nine_score (30%)
  confidence = clip(base_conf + intel_delta, 0, 1)

Market breadth filter:
  breadth_pct < 40%  → skip long signals (bearish regime)
  breadth_pct > 60%  → skip short signals (bullish regime)
  40–60%             → neutral, all signals allowed
"""
import math
from typing import Optional
from dataclasses import dataclass, field

from aggregator.models import TickerIntelligence

ETF_SET = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "GLD", "SLV", "TLT", "HYG",
    "LQD", "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLB", "XLRE",
    "XLY", "SMH", "ARKK", "ARKG", "ARKF", "ARKW", "ARKQ", "SOXX", "SPXL",
    "TQQQ", "SQQQ", "SOXS", "UVXY", "VXX", "BITO", "GBTC", "IBIT",
    "EEM", "EFA", "VEA", "VXUS", "BND", "AGG", "SCHD", "JEPI", "JEPQ",
    "MSTU", "MSTZ", "NVDL", "TSLL", "FNGU", "FNGS",
}


@dataclass
class ScoredTicker:
    ticker:       str
    direction:    str        # long | short
    confidence:   float      # 0.0 – 1.0  (final, after delta applied)
    asset_class:  str        # equity | etf
    ovtlyr_score: float      = 0.0
    sources:      list       = field(default_factory=list)
    metadata:     dict       = field(default_factory=dict)
    intelligence: Optional[TickerIntelligence] = None

    entry:  Optional[float] = None
    stop:   Optional[float] = None
    target: Optional[float] = None


def score_tickers(
    ovtlyr_data:    dict,   # ticker → {direction, score, nine_score, oscillator, fear_greed, ...}
    intel_map:      dict,   # ticker → TickerIntelligence | None
    market_breadth: dict = None,   # {breadth_pct, signal, bull_count, bear_count}
    min_confidence: float = 0.60,
) -> list[ScoredTicker]:
    """
    Score OVTLYR candidates, apply aggregator intelligence delta,
    nine_score weighting, and market breadth directional filter.
    """
    if market_breadth is None:
        market_breadth = {}

    breadth_pct    = float(market_breadth.get("breadth_pct", 50.0))
    breadth_signal = market_breadth.get("signal", "neutral")

    results: list[ScoredTicker] = []

    for ticker, ov in ovtlyr_data.items():
        ov_score     = float(ov.get("score", 50.0))
        ov_direction = ov.get("direction", "long")

        # ── Market breadth directional filter ────────────────────────────────
        # Hard skip when breadth strongly disagrees with signal direction
        if ov_direction == "long" and breadth_pct < 40.0:
            continue   # bearish market regime — don't add longs
        if ov_direction == "short" and breadth_pct > 60.0:
            continue   # bullish market regime — don't add shorts

        intel = intel_map.get(ticker)

        # Hard filter: skip if earnings are too close
        if intel and intel.earnings_too_close:
            continue

        # ── Confidence: blend ovtlyr_score + nine_score ───────────────────
        # nine_score is 0–9 from the dashboard panel; normalize to 0–100
        nine_score = ov.get("nine_score")
        if nine_score is not None:
            nine_pct   = (int(nine_score) / 9.0) * 100.0
            # 70% weight on OVTLYR signal score, 30% on nine-panel score
            blended = ov_score * 0.70 + nine_pct * 0.30
        else:
            blended = ov_score

        base_conf = blended / 100.0

        # Breadth alignment bonus/penalty (±5% nudge, stays within 0–1)
        if ov_direction == "long":
            breadth_adj = (breadth_pct - 50.0) / 1000.0   # +0.01 per 10% above 50
        else:
            breadth_adj = (50.0 - breadth_pct) / 1000.0   # +0.01 per 10% below 50

        # Apply intelligence confidence delta
        delta = intel.confidence_delta if intel else 0.0
        confidence = max(0.0, min(1.0, base_conf + delta + breadth_adj))

        if confidence < min_confidence:
            continue

        asset_class = "etf" if ticker in ETF_SET else "equity"

        metadata: dict = {
            "ov_ts":          ov.get("ts_utc"),
            "ovtlyr_score":   ov_score,
            "nine_score":     nine_score,
            "oscillator":     ov.get("oscillator"),
            "fear_greed":     ov.get("fear_greed"),
            "signal_active":  ov.get("signal_active"),
            "signal_date":    ov.get("signal_date"),
            "intel_delta":    delta,
            "breadth_pct":    breadth_pct,
            "breadth_signal": breadth_signal,
            "breadth_adj":    round(breadth_adj, 4),
        }
        if intel:
            metadata.update({
                "analyst_consensus":    intel.analyst_consensus,
                "analyst_target_price": intel.analyst_target_price,
                "sentiment_label":      intel.sentiment_label,
                "sentiment_score":      intel.sentiment_score,
                "earnings_date":        intel.earnings_date,
                "earnings_days_away":   intel.earnings_days_away,
                "summary":              intel.summary,
            })

        results.append(ScoredTicker(
            ticker       = ticker,
            direction    = ov_direction,
            confidence   = round(confidence, 4),
            asset_class  = asset_class,
            ovtlyr_score = ov_score,
            sources      = ["ovtlyr"] + (intel.sources if intel else []),
            metadata     = metadata,
            intelligence = intel,
        ))

    results.sort(key=lambda x: x.confidence, reverse=True)
    return results


def apply_stops(
    ticker:          ScoredTicker,
    price:           Optional[float],
    stop_loss_pct:   float,
    take_profit_pct: float,
) -> ScoredTicker:
    """Compute entry/stop/target. Uses analyst target price if no price given."""
    if price is None or price <= 0:
        return ticker
    if ticker.direction == "long":
        ticker.entry  = price
        ticker.stop   = round(price * (1 - stop_loss_pct / 100), 4)
        # Use analyst target if it's higher than calculated take-profit
        calc_target   = round(price * (1 + take_profit_pct / 100), 4)
        analyst_target = ticker.metadata.get("analyst_target_price", 0.0) or 0.0
        ticker.target  = max(calc_target, float(analyst_target)) if analyst_target > price else calc_target
    else:
        ticker.entry  = price
        ticker.stop   = round(price * (1 + stop_loss_pct / 100), 4)
        ticker.target = round(price * (1 - take_profit_pct / 100), 4)
    return ticker
