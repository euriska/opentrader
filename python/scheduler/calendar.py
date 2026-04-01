"""
Market Calendar
Knows when US equity markets are open, including holidays.
All times in America/New_York.
"""
import os
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
PRE_MARKET   = dtime(9, 0)   # scraper starts early

# NYSE holidays 2025-2026 (extend as needed)
NYSE_HOLIDAYS = {
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents' Day
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


def now_et() -> datetime:
    return datetime.now(tz=TZ)


def is_holiday(d: date = None) -> bool:
    return (d or now_et().date()) in NYSE_HOLIDAYS


def is_weekend(d: date = None) -> bool:
    return (d or now_et().date()).weekday() >= 5


def is_trading_day(d: date = None) -> bool:
    return not is_weekend(d) and not is_holiday(d)


def is_market_open() -> bool:
    now = now_et()
    if not is_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def is_pre_market() -> bool:
    """True during pre-market scrape window (9:00–9:30 ET)."""
    now = now_et()
    if not is_trading_day(now.date()):
        return False
    return PRE_MARKET <= now.time() < MARKET_OPEN


def is_active_session() -> bool:
    """True during pre-market OR regular session."""
    return is_pre_market() or is_market_open()


def minutes_to_open() -> int:
    """Minutes until next market open (0 if already open)."""
    if is_market_open():
        return 0
    now = now_et()
    today_open = datetime.combine(now.date(), MARKET_OPEN, tzinfo=TZ)
    if now < today_open and is_trading_day():
        return int((today_open - now).total_seconds() / 60)
    # Find next trading day
    from datetime import timedelta
    d = now.date() + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    next_open = datetime.combine(d, MARKET_OPEN, tzinfo=TZ)
    return int((next_open - now).total_seconds() / 60)


def minutes_to_close() -> int:
    """Minutes until market close (negative if already closed)."""
    now = now_et()
    today_close = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=TZ)
    return int((today_close - now).total_seconds() / 60)
