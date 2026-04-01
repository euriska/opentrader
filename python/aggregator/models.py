"""
Aggregator data model — standardized per-ticker intelligence payload.
Written to aggregator:intel:{ticker} Redis key and consumed by the predictor.
"""
import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TickerIntelligence:
    ticker: str
    ts_utc: int = field(default_factory=lambda: int(time.time() * 1000))

    # ── Sentiment (WSB + SeekingAlpha + Yahoo news) ───────────────────────────
    sentiment_score:  float      = 0.0       # -1.0 → +1.0 combined weighted
    sentiment_label:  str        = "neutral" # bullish | bearish | neutral
    news_headlines:   List[str]  = field(default_factory=list)  # top headlines
    news_sentiment:   float      = 0.0

    # ── Social (WSB) ─────────────────────────────────────────────────────────
    social_mentions:  int        = 0
    social_sentiment: float      = 0.0
    social_momentum:  str        = "flat"    # rising | falling | flat

    # ── Analyst ratings (yfinance) ────────────────────────────────────────────
    analyst_consensus:    str    = "none"    # strong_buy|buy|hold|sell|strong_sell|none
    analyst_buy_pct:      float  = 0.0       # fraction of analysts rated buy or better
    analyst_target_price: float  = 0.0       # consensus mean price target
    analyst_upside_pct:   float  = 0.0       # % upside vs last known price
    analyst_count:        int    = 0

    # ── Dividends (yfinance) ──────────────────────────────────────────────────
    dividend_yield:   float          = 0.0
    dividend_ex_date: Optional[str]  = None  # ISO date string
    dividend_annual:  float          = 0.0   # annual $ per share

    # ── Earnings (yfinance) ───────────────────────────────────────────────────
    earnings_date:      Optional[str] = None  # next earnings ISO date
    earnings_days_away: Optional[int] = None
    earnings_too_close: bool          = False  # hard filter — skip if True

    # ── Aggregated predictor output ───────────────────────────────────────────
    confidence_delta: float     = 0.0    # -0.20 → +0.20 applied to OVTLYR score
    actionable:       bool      = False  # False if earnings_too_close or no data
    sources:          List[str] = field(default_factory=list)  # scrapers that contributed
    summary:          str       = ""     # one-line human-readable intelligence summary

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "TickerIntelligence":
        d = json.loads(raw)
        return cls(**d)
