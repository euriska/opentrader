"""
Shared risk controls enforcement for all trader agents.

Reads config:risk_controls from Redis and enforces:
  - Max slippage % (bid-ask spread / mid price × 100)
  - Min liquidity (average daily volume in thousands of shares)

Fails open on missing/invalid data so risk controls never silently
block all trades due to a data-source outage.
"""
import json
import structlog

log = structlog.get_logger("shared.risk_controls")

RISK_CONTROLS_KEY = "config:risk_controls"

_DEFAULTS: dict = {
    "max_slippage_pct": 0.0,   # 0 = disabled
    "min_volume_k":     0.0,   # 0 = disabled
}


async def get_risk_controls(redis) -> dict:
    """Load risk controls from Redis, falling back to defaults."""
    try:
        raw = await redis.get(RISK_CONTROLS_KEY)
        if raw:
            stored = json.loads(raw)
            return {**_DEFAULTS, **stored}
    except Exception as e:
        log.warning("risk_controls.load_failed", error=str(e))
    return dict(_DEFAULTS)


def check_slippage(
    bid: float | None,
    ask: float | None,
    max_slippage_pct: float,
) -> tuple[bool, float]:
    """
    Returns (passes, spread_pct).
    passes=True means slippage is within limits or check is disabled.
    """
    if not max_slippage_pct or max_slippage_pct <= 0:
        return True, 0.0   # disabled
    if not bid or not ask or bid <= 0 or ask <= 0:
        return True, 0.0   # can't compute — allow trade
    mid = (bid + ask) / 2
    if mid <= 0:
        return True, 0.0
    spread_pct = (ask - bid) / mid * 100
    return spread_pct <= max_slippage_pct, round(spread_pct, 4)


def check_liquidity(
    avg_volume: float | None,
    min_volume_k: float,
) -> tuple[bool, float]:
    """
    Returns (passes, avg_volume_k).
    passes=True means volume is above minimum or check is disabled.
    """
    if not min_volume_k or min_volume_k <= 0:
        return True, 0.0   # disabled
    if not avg_volume or avg_volume <= 0:
        return True, 0.0   # unknown volume — fail open
    avg_k = avg_volume / 1000
    return avg_k >= min_volume_k, round(avg_k, 1)
