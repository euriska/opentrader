"""
Scraper data models — published to market.ticks stream.
Consumed by the predictor to generate trading signals.
"""
from typing import Optional
from pydantic import BaseModel, Field
import time


class OvtlyrTicker(BaseModel):
    """A ticker from the OVTLYR watchlist/screener."""
    ticker:     str
    direction:  str   = "long"    # long | short
    score:      float = 0.0       # 0–100 confidence
    price:      Optional[float] = None
    change_pct: Optional[float] = None
    volume:     Optional[int]   = None
    sector:     Optional[str]   = None
    ts_utc:     int = Field(default_factory=lambda: int(time.time() * 1000))
    metadata:   dict = Field(default_factory=dict)

    def to_stream_dict(self) -> dict:
        d: dict = {
            "ticker":    self.ticker,
            "source":    "ovtlyr",
            "direction": self.direction,
            "score":     str(round(self.score, 2)),
            "ts_utc":    str(self.ts_utc),
        }
        if self.price      is not None: d["price"]      = str(self.price)
        if self.change_pct is not None: d["change_pct"] = str(self.change_pct)
        if self.volume     is not None: d["volume"]     = str(self.volume)
        if self.sector:                 d["sector"]     = self.sector
        return d
