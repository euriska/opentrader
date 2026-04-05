"""
Per-Ticker Fear & Greed Scorer
Computes a 0-100 composite score from price history alone.
  0–20   Extreme Fear   (exit immediately)
 21–49   Fear           (stay out / reduce)
  50     Neutral        (hold / watch)
 51–79   Greed          (entry / ride)
 80–100  Extreme Greed  (ride but tighten stops)
"""
import math
from typing import Optional


def _rsi(closes: list[float], period: int = 14) -> float:
    """RSI on a 0–100 scale using simple moving average smoothing."""
    if len(closes) < period + 2:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return max(0.0, min(100.0, 100.0 - 100.0 / (1.0 + rs)))


def _ma_score(closes: list[float]) -> float:
    """
    Price position relative to 20d and 50d moving averages.
    ±10% from both MAs → maps to 0–100; flat → 50.
    """
    if len(closes) < 50:
        return 50.0
    price = closes[-1]
    ma20  = sum(closes[-20:]) / 20
    ma50  = sum(closes[-50:]) / 50
    pct20 = (price - ma20) / ma20 * 100
    pct50 = (price - ma50) / ma50 * 100
    avg   = (pct20 + pct50) / 2
    return max(0.0, min(100.0, 50.0 + avg * 5.0))


def _momentum(closes: list[float], period: int = 10) -> float:
    """
    10-day rate of change.
    ±10% maps to 0–100; flat → 50.
    """
    if len(closes) < period + 1:
        return 50.0
    roc = (closes[-1] - closes[-(period + 1)]) / closes[-(period + 1)] * 100
    return max(0.0, min(100.0, 50.0 + roc * 5.0))


def _vol_score(closes: list[float], window: int = 20) -> float:
    """
    20-day realised vol percentile vs available history (~252d).
    High vol = fear = low score (inverted).
    """
    if len(closes) < window + 2:
        return 50.0
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]

    def _rv(r: list[float]) -> float:
        if len(r) < window:
            return 0.0
        w = r[-window:]
        m = sum(w) / window
        v = sum((x - m) ** 2 for x in w) / window
        return math.sqrt(v * 252)

    vol_now  = _rv(rets)
    all_vols = [_rv(rets[:i]) for i in range(window, len(rets) + 1)]
    all_vols = [v for v in all_vols if v > 0]
    if not all_vols:
        return 50.0
    lo, hi = min(all_vols), max(all_vols)
    if hi == lo:
        return 50.0
    pct = (vol_now - lo) / (hi - lo) * 100
    return max(0.0, min(100.0, 100.0 - pct))   # invert: calm = greed


def score_ticker(closes: list[float]) -> dict:
    """
    Compute composite Fear & Greed score from a list of closing prices.
    Returns a dict with 'score' and component breakdowns (all 0–100).
    Requires at least 15 bars; returns neutral 50 if insufficient data.
    """
    if len(closes) < 15:
        return {
            "score":     50.0,
            "rsi":       50.0,
            "ma_score":  50.0,
            "momentum":  50.0,
            "vol_score": 50.0,
        }

    rsi      = _rsi(closes)
    ma       = _ma_score(closes)
    momentum = _momentum(closes)
    vol      = _vol_score(closes)

    # Weighted composite
    score = 0.30 * rsi + 0.25 * ma + 0.25 * momentum + 0.20 * vol

    return {
        "score":     round(score,    1),
        "rsi":       round(rsi,      1),
        "ma_score":  round(ma,       1),
        "momentum":  round(momentum, 1),
        "vol_score": round(vol,      1),
    }


def score_label(score: float) -> str:
    s = float(score)
    if s >= 80:
        return "Extreme Greed"
    if s >= 51:
        return "Greed"
    if s >= 21:
        return "Fear"
    return "Extreme Fear"
