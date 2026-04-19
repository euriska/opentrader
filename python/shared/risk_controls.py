"""
Shared risk controls enforcement for all trader agents.

Reads config:risk_controls from Redis and enforces:
  - Max slippage % (bid-ask spread / mid price × 100)
  - Min liquidity (average daily volume in thousands of shares)
  - Max daily loss USD (circuit breaker — trips system:circuit_broken)

Fails open on missing/invalid data so risk controls never silently
block all trades due to a data-source outage.
"""
import json
import os
import structlog

log = structlog.get_logger("shared.risk_controls")

RISK_CONTROLS_KEY    = "config:risk_controls"
DAILY_LOSS_KEY       = "trading:daily_loss_usd"
DAILY_LOSS_DATE_KEY  = "trading:daily_loss_date"
CIRCUIT_BROKEN_KEY   = "system:circuit_broken"
CIRCUIT_REASON_KEY   = "system:circuit_reason"

_DEFAULTS: dict = {
    "max_slippage_pct":  0.0,   # 0 = disabled
    "min_volume_k":      0.0,   # 0 = disabled
    "max_daily_loss_usd": 0.0,  # 0 = disabled
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


async def get_daily_loss(redis) -> float:
    """Return today's cumulative realized loss (negative = loss) from Redis."""
    try:
        raw = await redis.get(DAILY_LOSS_KEY)
        return float(raw) if raw else 0.0
    except Exception:
        return 0.0


async def record_trade_pnl(redis, pnl: float) -> float:
    """Add pnl to the intraday loss counter. Returns new cumulative total."""
    try:
        new_total = await redis.incrbyfloat(DAILY_LOSS_KEY, pnl)
        await redis.expire(DAILY_LOSS_KEY, 86400)
        return float(new_total)
    except Exception as e:
        log.warning("risk_controls.record_pnl_failed", error=str(e))
        return 0.0


async def check_daily_loss(redis, max_daily_loss_usd: float) -> tuple[bool, float]:
    """
    Returns (passes, current_loss_usd).
    Trips system:circuit_broken if cumulative loss exceeds the limit.
    passes=False means trading should be halted for the day.
    """
    if not max_daily_loss_usd or max_daily_loss_usd <= 0:
        return True, 0.0   # disabled
    current = await get_daily_loss(redis)
    # Loss is stored as negative number; limit is positive USD amount
    loss_magnitude = abs(min(current, 0.0))
    if loss_magnitude >= max_daily_loss_usd:
        reason = f"Daily loss limit hit: ${loss_magnitude:.2f} >= ${max_daily_loss_usd:.2f}"
        try:
            await redis.set(CIRCUIT_BROKEN_KEY, "1")
            await redis.set(CIRCUIT_REASON_KEY, reason)
            log.warning("risk_controls.daily_loss_circuit_tripped",
                        loss=loss_magnitude, limit=max_daily_loss_usd)
        except Exception:
            pass
        return False, current
    return True, current
