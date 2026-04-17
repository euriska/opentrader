"""
OpenTrader Options Monitor
==========================
Runs at EOD (16:05 ET) and during market hours (every 5 min) to:
  1. Scan all broker accounts for open option contracts
  2. Calculate 14-period ATR on the underlying stock
  3. Compute Emergency Exit / Exit Alert / Roll levels
  4. Log all data to option_positions + option_trade_log DB tables
  5. Fire alerts via Redis for threshold crossings
  6. Generate per-position matplotlib charts accessible from the WebUI

ATR level definitions (measured on the underlying stock price):
  Emergency Exit  : underlying_entry - 3 * ATR  → immediate close signal
  Exit Alert      : underlying_entry - 2 * ATR  → soft warning
  1st Roll        : underlying_entry + 0.5 * ATR
  2nd Roll        : underlying_entry + 1 * ATR
  3rd Roll        : underlying_entry + 2 * ATR
  (Extra rolls added at +3, +4, +5 … ATR as trade progresses)
"""
import asyncio
import base64
import io
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import asyncpg
import structlog

from shared.base_agent import BaseAgent
from shared.redis_client import STREAMS, REDIS_URL
from shared.mcp_client import call_mcp_tool, get_tv_indicators
from scheduler.calendar import is_trading_day, now_et

log = structlog.get_logger("options-monitor")

DB_URL               = os.getenv("DB_URL", "")
TRADINGVIEW_MCP_URL  = os.getenv("TRADINGVIEW_MCP_URL", "http://ot-mcp-tradingview:8000/mcp")
YAHOO_MCP_URL        = os.getenv("YAHOO_MCP_URL", "http://ot-mcp-yahoo:8000/mcp")
BROKER_GATEWAY_TIMEOUT = int(os.getenv("BROKER_GATEWAY_TIMEOUT_SEC", "20"))
SCAN_INTERVAL_MIN    = int(os.getenv("OPTIONS_SCAN_INTERVAL_MIN", "5"))
ATR_PERIOD           = 14
# Maximum extra roll levels to pre-compute beyond roll_3
MAX_EXTRA_ROLLS      = 7   # gives rolls at +3 … +9 ATR
# Default option type for Webull non-OCC positions where type isn't in raw data.
# Set to "put" if you hold puts, or "unknown" to search both (may misidentify).
WEBULL_DEFAULT_OPTION_TYPE = os.getenv("WEBULL_DEFAULT_OPTION_TYPE", "call")


# ── DB helpers ────────────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None

async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool:
        return _pool
    from urllib.parse import urlparse, unquote
    p = urlparse(DB_URL)
    _pool = await asyncpg.create_pool(
        host=p.hostname, port=p.port or 5432,
        user=p.username,
        password=unquote(p.password) if p.password else None,
        database=p.path.lstrip("/"),
        min_size=1, max_size=4,
    )
    return _pool


# ── ATR calculation ───────────────────────────────────────────────────────────

def _compute_atr(candles: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
    """
    Compute ATR-14 from a list of OHLCV candle dicts.
    Each dict must have keys: open, high, low, close.
    Returns None if not enough data.
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h   = float(candles[i]["high"])
        l   = float(candles[i]["low"])
        pc  = float(candles[i - 1]["close"])
        tr  = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    # Simple moving average for first ATR, then Wilder's smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


async def _fetch_atr_and_price(ticker: str) -> tuple[Optional[float], Optional[float]]:
    """
    Fetch 30 daily candles from TradingView MCP.
    Returns (atr_14, last_close_price).  Both may be None if unavailable.
    Tries NASDAQ then NYSE exchange labels.
    """
    candles = None
    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        raw = await call_mcp_tool(
            TRADINGVIEW_MCP_URL,
            "get_historical_data",
            {"symbol": ticker, "exchange": exchange, "timeframe": "1d", "max_records": 30},
        )
        if not raw:
            continue
        try:
            data = json.loads(raw)
            c = data.get("candles") or data.get("data") or (data if isinstance(data, list) else [])
            if c:
                candles = c
                break
        except Exception as e:
            log.warning("options_monitor.atr_parse_failed", ticker=ticker,
                        exchange=exchange, error=str(e))

    if not candles:
        return None, None

    atr        = _compute_atr(candles)
    last_close = float(candles[-1]["close"]) if candles else None
    return atr, last_close


# Keep backward-compat alias used in _refresh_chart
async def _fetch_atr(ticker: str) -> Optional[float]:
    atr, _ = await _fetch_atr_and_price(ticker)
    return atr


async def _fetch_underlying_price(ticker: str) -> Optional[float]:
    """Fetch latest underlying stock price via Yahoo Finance MCP (get_stock_info)."""
    raw = await call_mcp_tool(YAHOO_MCP_URL, "get_stock_info", {"ticker": ticker})
    if not raw:
        return None
    try:
        data = json.loads(raw)
        price = (
            data.get("currentPrice")
            or data.get("regularMarketPrice")
            or data.get("price")
            or data.get("previousClose")
        )
        return float(price) if price else None
    except Exception:
        return None


async def _fetch_earnings_date(ticker: str) -> Optional[date]:
    """
    Fetch next earnings date via Yahoo Finance MCP.
    Uses get_option_expiration_dates as a proxy for upcoming events,
    or get_stock_info which sometimes includes earningsTimestamp.
    """
    raw = await call_mcp_tool(YAHOO_MCP_URL, "get_stock_info", {"ticker": ticker})
    if not raw:
        return None
    try:
        data = json.loads(raw)
        # earningsTimestamp is a Unix timestamp in seconds
        ts = data.get("earningsTimestamp") or data.get("earningsTimestampStart")
        if ts:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        return None
    except Exception:
        return None


def _bs_delta(S: float, K: float, T: float, sigma: float,
              r: float = 0.05, option_type: str = "call") -> Optional[float]:
    """Black-Scholes delta using stdlib math only (no scipy)."""
    import math
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        ncdf = lambda x: (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0
        delta = ncdf(d1) if option_type == "call" else ncdf(d1) - 1.0
        return round(delta, 4)
    except Exception:
        return None


async def _fetch_option_chain_details(
    underlying: str,
    current_option_price: float,
    hint_option_type: str = "unknown",
    current_underlying_price: float = 0.0,
    entry_date: Optional[date] = None,
) -> Optional[dict]:
    """
    Look up option contract details (strike, type, expiry, delta) via Yahoo Finance.
    - Yahoo get_option_chain requires option_type='calls' or 'puts' and returns a list.
    - Delta is computed via Black-Scholes from the chain's impliedVolatility field.
    - When current_option_price > 0, matches by lastPrice proximity.
    - When current_option_price == 0, matches by nearest-ATM strike.
    - entry_date: if provided, skips expiry dates that would have been < 14 DTE when
      the position was opened (prevents ITM calls from matching near-weekly expiries).
    Returns dict with strike, option_type, expiration_date, delta — or None.
    """
    raw_dates = await call_mcp_tool(
        YAHOO_MCP_URL, "get_option_expiration_dates", {"ticker": underlying}
    )
    if not raw_dates or raw_dates.startswith("Error"):
        return None
    try:
        dates_data = json.loads(raw_dates)
        exp_dates = dates_data if isinstance(dates_data, list) else (
            dates_data.get("expiration_dates") or dates_data.get("dates") or []
        )
    except Exception:
        return None

    if not exp_dates:
        return None

    # Yahoo uses 'calls'/'puts' (not 'call'/'put').
    # When hint is "unknown" or "call", always check calls first.
    # Only fall back to puts if hint is explicitly "put" or calls return no match.
    if hint_option_type == "put":
        side_order = [("puts", "put")]
    else:
        # call (or unknown) — calls only; unknown never reaches here since
        # WEBULL_DEFAULT_OPTION_TYPE defaults to "call"
        side_order = [("calls", "call")]

    best: Optional[dict] = None
    best_score = float("inf")

    # Scan up to 16 expiry dates (~4 months of weeklies) to cover LEAPS and
    # longer-dated positions.  Do NOT break early on a "good enough" score —
    # deep-ITM calls have similar intrinsic value across expiries, so we must
    # compare all candidates globally and pick the closest price match.
    for yahoo_type, opt_type in side_order:
        for exp_date_str in exp_dates[:16]:
            try:
                exp_d = date.fromisoformat(str(exp_date_str)[:10])
            except Exception:
                exp_d = None

            # Skip expiries that would have given < 14 DTE on the entry date.
            # Prevents ITM calls from matching near-weekly expiries whose price
            # is close due to intrinsic value dominance.
            if entry_date and exp_d and (exp_d - entry_date).days < 14:
                continue

            raw_chain = await call_mcp_tool(
                YAHOO_MCP_URL, "get_option_chain",
                {"ticker": underlying, "expiration_date": str(exp_date_str),
                 "option_type": yahoo_type},
            )
            if not raw_chain or raw_chain.startswith("Error"):
                continue
            try:
                contracts = json.loads(raw_chain)
            except Exception:
                continue
            if not isinstance(contracts, list) or not contracts:
                continue

            for c in contracts:
                contract_strike = float(c.get("strike", 0) or 0)
                iv = float(c.get("impliedVolatility", 0) or 0)

                # Compute Black-Scholes delta
                delta = None
                if current_underlying_price > 0 and contract_strike > 0 and exp_d and iv > 0:
                    T = max((exp_d - date.today()).days, 1) / 365.0
                    delta = _bs_delta(current_underlying_price, contract_strike,
                                      T, iv, option_type=opt_type)

                # Score: match by price or by nearest-ATM strike.
                # Use bid/ask midpoint as the reference price — it reflects the CURRENT
                # market (not a stale last-trade price that could equal the old entry cost).
                if current_option_price > 0:
                    bid  = float(c.get("bid") or 0)
                    ask  = float(c.get("ask") or 0)
                    last = float(c.get("lastPrice") or 0)
                    if bid > 0 and ask > 0:
                        ref = (bid + ask) / 2.0
                    elif bid > 0:
                        ref = bid
                    elif last > 0:
                        ref = last
                    else:
                        continue  # no usable price — skip
                    score = abs(ref - current_option_price) / current_option_price
                    threshold = 0.40
                elif current_underlying_price > 0 and contract_strike > 0:
                    score = abs(contract_strike - current_underlying_price) / current_underlying_price
                    threshold = 0.25
                else:
                    oi = float(c.get("openInterest") or 1)
                    score = 1.0 / max(oi, 1)
                    threshold = 1.0

                # Require a meaningfully better score to displace the current best.
                # A 10% improvement threshold means ties (and near-ties) keep the
                # EARLIER expiry (since we iterate expiry dates in chronological order).
                improvement_required = best_score * 0.90
                if score < improvement_required and score < threshold:
                    best_score = score
                    best = {
                        "strike":          contract_strike,
                        "option_type":     opt_type,
                        "expiration_date": exp_d,
                        "delta":           delta,
                    }

        if best:
            break   # found a match, don't try the other side

    return best


# ── ATR level builder ─────────────────────────────────────────────────────────

def _build_levels(entry_price: float, atr: float) -> dict:
    """Return all ATR price levels given underlying entry price and ATR value."""
    return {
        "level_emergency":  round(entry_price - 3.0 * atr, 4),
        "level_exit_alert": round(entry_price - 2.0 * atr, 4),
        "level_roll_1":     round(entry_price + 0.5 * atr, 4),
        "level_roll_2":     round(entry_price + 1.0 * atr, 4),
        "level_roll_3":     round(entry_price + 2.0 * atr, 4),
        "extra_roll_levels": [
            {"label": f"Roll {i+4}", "multiplier": float(i + 3),
             "price": round(entry_price + (i + 3) * atr, 4)}
            for i in range(MAX_EXTRA_ROLLS)
        ],
    }


# ── Chart generation ──────────────────────────────────────────────────────────

async def _generate_chart(pos: dict, candles: list[dict]) -> Optional[str]:
    """
    Generate a matplotlib chart for an option position.
    Returns base64-encoded PNG string or None.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np

        underlying    = pos["underlying"]
        entry_price   = float(pos["underlying_entry"] or 0)
        atr           = float(pos["atr_14"] or 0)
        entry_date    = pos["entry_date"]
        exp_date      = pos["expiration_date"]

        # Build price series from candles (last 60 days)
        prices = [float(c["close"]) for c in candles[-60:]]
        dates  = list(range(len(prices)))

        fig, ax = plt.subplots(figsize=(12, 6), facecolor="#0d1117")
        ax.set_facecolor("#0d1117")

        # Price line
        ax.plot(dates, prices, color="#58a6ff", linewidth=1.5, label=underlying, zorder=3)

        if entry_price and atr:
            levels = _build_levels(entry_price, atr)

            # Entry price (buy point)
            ax.axhline(entry_price, color="#3fb950", linewidth=1.5, linestyle="--",
                       label=f"Entry ${entry_price:.2f}", zorder=4)

            # Emergency exit
            ax.axhline(levels["level_emergency"], color="#f85149", linewidth=1.5,
                       linestyle="-", label=f"Emergency Exit ${levels['level_emergency']:.2f}", zorder=4)
            ax.axhspan(levels["level_emergency"] - atr * 0.3, levels["level_emergency"],
                       alpha=0.08, color="#f85149")

            # Exit alert
            ax.axhline(levels["level_exit_alert"], color="#d29922", linewidth=1.2,
                       linestyle="--", label=f"Exit Alert ${levels['level_exit_alert']:.2f}", zorder=4)

            # Roll levels
            roll_colors = ["#79c0ff", "#a5d6ff", "#cae8ff"]
            for i, (key, color) in enumerate(zip(
                ["level_roll_1", "level_roll_2", "level_roll_3"], roll_colors
            )):
                roll_n = ["1st", "2nd", "3rd"][i]
                ax.axhline(levels[key], color=color, linewidth=1.0, linestyle=":",
                           label=f"{roll_n} Roll ${levels[key]:.2f}", zorder=4)

        ax.set_title(f"{underlying} — Options Trade | Entry: {entry_date} | Exp: {exp_date}",
                     color="#e6edf3", fontsize=12, pad=10)
        ax.set_xlabel("Days", color="#8b949e")
        ax.set_ylabel("Price ($)", color="#8b949e")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")
        ax.grid(True, color="#21262d", linewidth=0.5, zorder=0)
        legend = ax.legend(loc="upper left", framealpha=0.4,
                           facecolor="#161b22", edgecolor="#30363d",
                           labelcolor="#e6edf3", fontsize=8)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()
    except Exception as e:
        log.warning("options_monitor.chart_failed", error=str(e))
        return None


# ── Option detection ──────────────────────────────────────────────────────────

def _is_option_position(pos: dict) -> bool:
    """
    Detect whether a normalised position dict is an option contract.
    Handles all three brokers:
      - Webull:  raw.instrument_type == "OPTION"
      - Alpaca:  raw.asset_class == "us_option"
      - Tradier: OCC-format symbol (e.g. AAPL250418C00200000)
    """
    import re
    raw = pos.get("raw", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}

    # Webull: instrument_type field in raw
    if raw.get("instrument_type", "").upper() == "OPTION":
        return True

    # Alpaca: asset_class field in raw
    if raw.get("asset_class", "").lower() in ("us_option", "option"):
        return True

    # Normalised asset_class on the position itself
    asset_cls = pos.get("asset_class", "").lower()
    if asset_cls in ("option", "options", "us_option"):
        return True

    # OCC symbol format: 1-6 uppercase letters + 6 digits + C/P + 8 digits
    symbol = pos.get("symbol", "")
    if re.match(r'^[A-Z]{1,6}\d{6}[CP]\d{8}$', symbol.upper()):
        return True

    return False


def _normalise_option_position(pos: dict, acct_label: str, broker: str, mode: str) -> dict:
    """
    Build a normalised option position dict from a raw broker position.
    For Webull (non-OCC), uses instrument_id as part of the synthetic contract symbol,
    and attempts to extract option-specific fields from the raw API response.
    """
    raw = pos.get("raw", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}

    symbol = pos.get("symbol", "")

    # Attempt OCC parse
    parsed = _parse_occ_symbol(symbol)
    if parsed:
        contract_symbol  = symbol
        underlying       = parsed["underlying"]
        option_type      = parsed["option_type"]
        strike           = parsed["strike"]
        expiration_date  = parsed["expiration_date"]
    else:
        # Non-OCC (Webull): use instrument_id to make a unique synthetic symbol
        instrument_id    = raw.get("instrument_id", "")
        underlying       = symbol   # symbol IS the underlying ticker for Webull options
        contract_symbol  = f"WBL:{instrument_id}" if instrument_id else f"OPT:{symbol}"

        # Try to extract option-specific fields from Webull raw data
        # Webull API uses various field name conventions across versions
        raw_otype = (
            raw.get("option_type") or raw.get("optionType") or
            raw.get("contract_type") or raw.get("contractType") or
            raw.get("put_call") or raw.get("putCall") or ""
        )
        if str(raw_otype).upper() in ("CALL", "C"):
            option_type = "call"
        elif str(raw_otype).upper() in ("PUT", "P"):
            option_type = "put"
        else:
            # Default to WEBULL_DEFAULT_OPTION_TYPE when not in raw data
            option_type = WEBULL_DEFAULT_OPTION_TYPE

        raw_strike = (
            raw.get("strike_price") or raw.get("strikePrice") or
            raw.get("strike") or raw.get("exercise_price") or
            raw.get("exercisePrice")
        )
        try:
            strike = float(raw_strike) if raw_strike else None
        except (ValueError, TypeError):
            strike = None

        raw_expiry = (
            raw.get("expiry_date") or raw.get("expiryDate") or
            raw.get("expiration_date") or raw.get("expirationDate") or
            raw.get("expire_date") or raw.get("expireDate") or
            raw.get("maturity_date") or raw.get("maturityDate")
        )
        try:
            expiration_date = date.fromisoformat(str(raw_expiry)[:10]) if raw_expiry else None
        except Exception:
            expiration_date = None

    return {
        "contract_symbol":  contract_symbol,
        "underlying":       underlying,
        "option_type":      option_type,
        "strike":           strike,
        "expiration_date":  expiration_date,
        "account_label":    acct_label,
        "broker":           broker,
        "mode":             mode,
        "qty":              float(pos.get("qty", pos.get("quantity", 0)) or 0),
        "current_price":    float(pos.get("current_price", pos.get("last_price", 0)) or 0),
        "entry_price":      float(pos.get("avg_entry_price", pos.get("unit_cost",
                                  pos.get("avg_cost", pos.get("cost_basis", 0)))) or 0),
        "market_value":     float(pos.get("market_value", 0) or 0),
        "raw":              pos,
    }


# ── Broker position fetcher ───────────────────────────────────────────────────

async def _fetch_option_positions(redis) -> list[dict]:
    """
    Send get_positions to all accounts via broker gateway and extract option contracts.
    Returns list of normalised position dicts.
    """
    request_id = str(uuid.uuid4())
    await redis.xadd(
        STREAMS["broker_commands"],
        {
            "command":       "get_positions",
            "request_id":    request_id,
            "account_label": "",    # empty = all accounts
            "mode":          "",
            "issued_by":     "options-monitor",
        },
        maxlen=10_000,
    )
    reply_raw = await redis.blpop(
        f"broker:reply:{request_id}", timeout=BROKER_GATEWAY_TIMEOUT
    )
    if reply_raw is None:
        log.warning("options_monitor.gateway_timeout")
        return []

    _, reply_json = reply_raw
    try:
        results = json.loads(reply_json)
    except Exception:
        return []
    if not isinstance(results, list):
        results = [results]

    positions = []
    for account_result in results:
        if account_result.get("status") == "error":
            log.debug("options_monitor.account_error",
                      account=account_result.get("account_label"),
                      error=account_result.get("error", "")[:80])
            continue

        acct_label = account_result.get("account_label", "unknown")
        broker     = account_result.get("broker", "")
        mode       = account_result.get("mode", "live")

        # Gateway wraps list results in {"items": [...]} — check both keys
        data    = account_result.get("data", {})
        raw_pos = data.get("items") or data.get("positions") or []
        if not isinstance(raw_pos, list):
            raw_pos = []

        for pos in raw_pos:
            if not _is_option_position(pos):
                continue
            norm = _normalise_option_position(pos, acct_label, broker, mode)
            log.info("options_monitor.option_detected",
                     contract=norm["contract_symbol"],
                     underlying=norm["underlying"],
                     account=acct_label)
            positions.append(norm)

    return positions


# ── OCC symbol parser ─────────────────────────────────────────────────────────

def _parse_occ_symbol(symbol: str) -> Optional[dict]:
    """
    Parse an OCC-format option symbol: AAPL250418C00200000
    Returns dict with underlying, expiration_date, option_type, strike or None.
    """
    import re
    m = re.match(
        r'^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$',
        symbol.upper()
    )
    if not m:
        return None
    underlying, yy, mm, dd, otype, strike_raw = m.groups()
    try:
        exp_date  = date(2000 + int(yy), int(mm), int(dd))
        strike    = int(strike_raw) / 1000.0
        opt_type  = "call" if otype == "C" else "put"
        return {
            "underlying":      underlying,
            "expiration_date": exp_date,
            "option_type":     opt_type,
            "strike":          strike,
        }
    except Exception:
        return None


# ── Account name mapping ──────────────────────────────────────────────────────

def _load_account_names() -> dict[str, str]:
    """
    Load account label → friendly display name.
    Priority: {LABEL_UPPER}_DISPLAY_NAME env var → accounts.toml notes → formatted label.
    Matches the same convention used by the webui broker/positions pages.
    """
    try:
        import tomllib
        with open("/app/config/accounts.toml", "rb") as f:
            cfg = tomllib.load(f)
        mapping = {}
        for acct in cfg.get("accounts", []):
            label = acct.get("label", "")
            if not label:
                continue
            # Check env var first: WEBULL_LIVE_2_DISPLAY_NAME etc.
            env_key = label.upper().replace("-", "_") + "_DISPLAY_NAME"
            env_name = os.getenv(env_key, "").strip()
            if env_name:
                mapping[label] = env_name
                continue
            # Fall back to notes field
            notes = acct.get("notes", "").strip()
            if notes:
                name = notes.split("—")[0].strip() if "—" in notes else notes
            else:
                name = label.replace("-", " ").title()
            mapping[label] = name
        return mapping
    except Exception:
        return {}


# ── Main agent ────────────────────────────────────────────────────────────────

class OptionsMonitor(BaseAgent):

    def __init__(self):
        super().__init__("options-monitor")
        self._account_names: dict[str, str] = {}

    async def run(self):
        await self.setup()
        import redis.asyncio as aioredis
        self.redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=10, socket_timeout=20,
            retry_on_timeout=True, health_check_interval=30,
        )
        self._account_names = _load_account_names()
        log.info("options_monitor.starting")
        await asyncio.gather(
            self.heartbeat_loop(),
            self._scan_loop(),
        )

    async def _scan_loop(self):
        """
        Scan logic:
        - Every SCAN_INTERVAL_MIN during market hours + 1 h after close
        - Always run once at startup (EOD import on first boot)
        """
        # Run initial scan immediately
        await self._run_scan()
        while self._running:
            await asyncio.sleep(SCAN_INTERVAL_MIN * 60)
            await self._run_scan()

    async def _run_scan(self):
        log.info("options_monitor.scan_start")
        pool = await _get_pool()

        # ── 1. Fetch all option positions from brokers ─────────────────────────
        broker_positions = await _fetch_option_positions(self.redis)
        if not broker_positions:
            log.info("options_monitor.no_option_positions")
            return

        log.info("options_monitor.positions_found", count=len(broker_positions))

        for bp in broker_positions:
            try:
                await self._process_position(pool, bp)
            except Exception as e:
                log.error("options_monitor.position_error",
                           contract=bp.get("contract_symbol"), error=str(e))

        # ── 2. Mark any DB-active positions not seen in this scan ─────────────
        seen_keys = {
            f"{p['contract_symbol']}:{p['account_label']}"
            for p in broker_positions
        }
        active_rows = await pool.fetch(
            """SELECT id, contract_symbol, account_label, underlying,
                      entry_price, qty
               FROM option_positions WHERE status='active'"""
        )
        for row in active_rows:
            key = f"{row['contract_symbol']}:{row['account_label']}"
            if key not in seen_keys:
                # Fetch last known contract price from most recent scan event
                last_scan = await pool.fetchrow(
                    """SELECT contract_price FROM option_trade_log
                       WHERE position_id=$1 AND event_type='scan'
                         AND contract_price IS NOT NULL
                       ORDER BY ts DESC LIMIT 1""",
                    row["id"],
                )
                last_cp = float(last_scan["contract_price"]) if last_scan else None
                ep      = float(row["entry_price"]) if row["entry_price"] else None
                qty     = float(row["qty"]) if row["qty"] else None

                # Short option P&L: profit = premium collected − cost to close
                pnl = None
                if last_cp is not None and ep is not None and qty is not None:
                    pnl = round((ep - last_cp) * abs(qty) * 100, 2)

                await pool.execute(
                    """UPDATE option_positions
                       SET status='closed', closed_at=NOW(), close_reason='not_in_scan',
                           total_realized_pnl=$2, updated_at=NOW()
                       WHERE id=$1""",
                    row["id"], pnl,
                )

                # Write a closed event so the trading log has a price + P&L record
                await pool.execute(
                    """INSERT INTO option_trade_log
                       (position_id, contract_symbol, underlying, event_type,
                        contract_price, realized_pnl, notes)
                       VALUES ($1,$2,$3,'closed',$4::NUMERIC,$5::NUMERIC,$6)""",
                    row["id"], row["contract_symbol"], row["underlying"],
                    last_cp, pnl,
                    f"Position closed — no longer in broker scan (last price: "
                    f"{'${:.2f}'.format(last_cp) if last_cp else 'unknown'})",
                )

                log.info("options_monitor.position_closed_not_seen",
                         contract=row["contract_symbol"], account=row["account_label"],
                         last_price=last_cp, pnl=pnl)

        log.info("options_monitor.scan_complete")

    async def _process_position(self, pool: asyncpg.Pool, bp: dict):
        contract_symbol = bp["contract_symbol"]
        account_label   = bp["account_label"]
        account_name    = self._account_names.get(account_label, account_label)

        # Fields already resolved by _normalise_option_position
        underlying      = bp["underlying"]
        expiration_date = bp.get("expiration_date")   # may be None for non-OCC
        option_type     = bp.get("option_type", "unknown")
        strike          = bp.get("strike")
        today           = date.today()

        # ── Look up existing DB record ─────────────────────────────────────────
        existing = await pool.fetchrow(
            """SELECT * FROM option_positions
               WHERE contract_symbol=$1 AND account_label=$2 AND status='active'""",
            contract_symbol, account_label,
        )

        # ── Fetch ATR + current price (one TV MCP call, refresh if >4 h old) ───
        atr = None
        current_underlying = None
        if existing and existing["atr_14"] and existing["atr_calculated_at"]:
            age = (datetime.now(timezone.utc) - existing["atr_calculated_at"]).total_seconds()
            if age < 4 * 3600:
                atr = float(existing["atr_14"])

        if atr is None:
            # Fetch both ATR and current price from TradingView historical data
            atr, current_underlying = await _fetch_atr_and_price(underlying)
        else:
            # ATR fresh — still need current price; try Yahoo first, fall back to TV
            current_underlying = await _fetch_underlying_price(underlying)
            if current_underlying is None:
                _, current_underlying = await _fetch_atr_and_price(underlying)

        # ── Fetch earnings date (once per position) ───────────────────────────
        earnings_date = None
        if existing and existing["next_earnings_date"]:
            earnings_date = existing["next_earnings_date"]
        else:
            earnings_date = await _fetch_earnings_date(underlying)

        # Initialise entry_date early (needed by chain lookup below).
        # Will be overridden below if an existing DB row is found.
        entry_date = existing["entry_date"] if existing else today

        # ── Enrich contract details via Yahoo option chain ─────────────────────
        # Runs whenever: type unknown, strike missing, expiry missing, or no delta yet
        delta = None
        current_opt_price = bp["current_price"]
        # Use entry_price as reference when market is closed and current price is 0
        price_ref = current_opt_price if current_opt_price > 0 else bp.get("entry_price", 0)
        expiry_locked = bool(existing and existing.get("expiry_locked"))
        # Run chain enrichment when any key field is missing.
        # Check BOTH the incoming bp (broker position) AND the existing DB row:
        # Webull v1 positions never carry strike/expiry in bp, so check the DB first.
        db_strike  = existing.get("strike")           if existing else None
        db_expiry  = existing.get("expiration_date")  if existing else None
        db_delta   = existing.get("delta")            if existing else None
        effective_strike  = strike           or db_strike
        effective_expiry  = expiration_date  or db_expiry
        needs_enrichment = (
            not expiry_locked and (
                option_type == "unknown"
                or effective_strike is None
                or effective_expiry is None
                or existing is None
                or db_delta is None
            )
        )
        if needs_enrichment:
            chain_details = await _fetch_option_chain_details(
                underlying, float(price_ref or 0), hint_option_type=option_type,
                current_underlying_price=float(current_underlying or 0),
                entry_date=entry_date,
            )
            if chain_details:
                if option_type == "unknown" and chain_details.get("option_type"):
                    option_type = chain_details["option_type"]
                    log.info("options_monitor.type_resolved", contract=contract_symbol,
                             option_type=option_type)
                if strike is None and chain_details.get("strike"):
                    strike = chain_details["strike"]
                if expiration_date is None and chain_details.get("expiration_date"):
                    expiration_date = chain_details["expiration_date"]
                if chain_details.get("delta") is not None:
                    delta = chain_details["delta"]
                log.info("options_monitor.chain_enriched", contract=contract_symbol,
                         strike=strike, option_type=option_type, delta=delta)
            else:
                log.debug("options_monitor.chain_no_match", contract=contract_symbol,
                          underlying=underlying, price_ref=price_ref)

        # Fall back to DB values when chain lookup didn't run or found nothing
        if strike is None and db_strike is not None:
            strike = float(db_strike)
        if expiration_date is None and db_expiry is not None:
            expiration_date = db_expiry
        if delta is None and existing and existing.get("delta") is not None:
            try:
                delta = float(existing["delta"])
            except Exception:
                pass

        # ── Determine entry price / date ──────────────────────────────────────
        entry_price_option    = bp["entry_price"]  # option premium
        underlying_entry      = None
        entry_date            = today

        if existing:
            underlying_entry = float(existing["underlying_entry"]) if existing["underlying_entry"] else None
            entry_date       = existing["entry_date"]
            entry_price_option = float(existing["entry_price"] or entry_price_option)

        # If this is a new position and we have a current price, use it as entry
        if underlying_entry is None and current_underlying:
            underlying_entry = current_underlying

        # ── Compute ATR levels ────────────────────────────────────────────────
        levels = {}
        if underlying_entry and atr:
            levels = _build_levels(underlying_entry, atr)

        # ── Upsert into DB ────────────────────────────────────────────────────
        if existing:
            pos_id = existing["id"]
            await pool.execute(
                """UPDATE option_positions SET
                    updated_at          = NOW(),
                    qty                 = $2::NUMERIC,
                    atr_14              = $3::NUMERIC,
                    atr_calculated_at   = CASE WHEN $3::NUMERIC IS NOT NULL THEN NOW()
                                              ELSE atr_calculated_at END,
                    underlying_entry    = COALESCE(underlying_entry, $12::NUMERIC),
                    level_emergency     = $4::NUMERIC,
                    level_exit_alert    = $5::NUMERIC,
                    level_roll_1        = $6::NUMERIC,
                    level_roll_2        = $7::NUMERIC,
                    level_roll_3        = $8::NUMERIC,
                    extra_roll_levels   = $9::JSONB,
                    next_earnings_date  = $10,
                    account_name        = $17,
                    last_scan_at        = NOW(),
                    raw                 = $11::JSONB,
                    delta               = $13::NUMERIC,
                    option_type         = CASE WHEN option_type='unknown' AND $14::TEXT IS NOT NULL
                                              THEN $14::TEXT ELSE option_type END,
                    strike              = COALESCE(strike, $15::NUMERIC),
                    expiration_date     = COALESCE(expiration_date, $16::DATE)
                   WHERE id=$1""",
                pos_id,
                bp["qty"],
                atr,
                levels.get("level_emergency"),
                levels.get("level_exit_alert"),
                levels.get("level_roll_1"),
                levels.get("level_roll_2"),
                levels.get("level_roll_3"),
                json.dumps(levels.get("extra_roll_levels", [])),
                earnings_date,
                json.dumps(bp["raw"]),
                underlying_entry,   # $12 — only fills in if currently NULL
                delta,              # $13
                option_type if option_type != "unknown" else None,  # $14
                strike,             # $15
                expiration_date,    # $16
                account_name,       # $17 — always refresh from accounts.toml
            )
        else:
            # New position
            pos_id = await pool.fetchval(
                """INSERT INTO option_positions (
                    contract_symbol, underlying, option_type, strike, expiration_date,
                    account_label, account_name, broker, mode,
                    qty, entry_price, underlying_entry, entry_date,
                    atr_14, atr_calculated_at,
                    level_emergency, level_exit_alert, level_roll_1, level_roll_2, level_roll_3,
                    extra_roll_levels, next_earnings_date, last_scan_at, raw, delta
                ) VALUES (
                    $1,$2,$3,$4::NUMERIC,$5,
                    $6,$7,$8,$9,
                    $10::NUMERIC,$11::NUMERIC,$12::NUMERIC,$13,
                    $14::NUMERIC, CASE WHEN $14::NUMERIC IS NOT NULL THEN NOW() ELSE NULL END,
                    $15::NUMERIC,$16::NUMERIC,$17::NUMERIC,$18::NUMERIC,$19::NUMERIC,
                    $20::JSONB,$21,NOW(),$22::JSONB,$23::NUMERIC
                ) ON CONFLICT (contract_symbol, account_label) WHERE status='active'
                DO UPDATE SET
                    qty=$10::NUMERIC, atr_14=$14::NUMERIC,
                    atr_calculated_at = CASE WHEN $14::NUMERIC IS NOT NULL THEN NOW()
                                            ELSE option_positions.atr_calculated_at END,
                    underlying_entry = COALESCE(option_positions.underlying_entry, $12::NUMERIC),
                    level_emergency=$15::NUMERIC, level_exit_alert=$16::NUMERIC,
                    level_roll_1=$17::NUMERIC, level_roll_2=$18::NUMERIC, level_roll_3=$19::NUMERIC,
                    extra_roll_levels=$20::JSONB, next_earnings_date=$21,
                    last_scan_at=NOW(), updated_at=NOW(), raw=$22::JSONB,
                    delta=$23::NUMERIC,
                    option_type = CASE WHEN option_positions.option_type='unknown' AND $3 IS NOT NULL AND $3 != 'unknown'
                                      THEN $3 ELSE option_positions.option_type END,
                    strike = COALESCE(option_positions.strike, $4::NUMERIC),
                    expiration_date = COALESCE(option_positions.expiration_date, $5)
                RETURNING id""",
                contract_symbol, underlying, option_type, strike, expiration_date,
                account_label, account_name, bp["broker"], bp["mode"],
                bp["qty"], entry_price_option, underlying_entry, entry_date,
                atr,
                levels.get("level_emergency"), levels.get("level_exit_alert"),
                levels.get("level_roll_1"), levels.get("level_roll_2"), levels.get("level_roll_3"),
                json.dumps(levels.get("extra_roll_levels", [])),
                earnings_date,
                json.dumps(bp["raw"]),
                delta,
            )
            log.info("options_monitor.position_imported",
                     contract=contract_symbol, account=account_label, underlying=underlying)

        # ── Log scan event ────────────────────────────────────────────────────
        dist_emergency  = None
        dist_exit_alert = None
        dist_roll_1     = None
        if current_underlying and levels:
            dist_emergency  = round(current_underlying - levels["level_emergency"], 4)
            dist_exit_alert = round(current_underlying - levels["level_exit_alert"], 4)
            dist_roll_1     = round(current_underlying - levels["level_roll_1"], 4)

        await pool.execute(
            """INSERT INTO option_trade_log
               (position_id, contract_symbol, underlying, event_type,
                underlying_price, contract_price, atr_value,
                distance_emergency, distance_exit_alert, distance_roll_1)
               VALUES ($1,$2,$3,'scan',
                       $4::NUMERIC,$5::NUMERIC,$6::NUMERIC,
                       $7::NUMERIC,$8::NUMERIC,$9::NUMERIC)""",
            pos_id, contract_symbol, underlying,
            current_underlying, bp["current_price"], atr,
            dist_emergency, dist_exit_alert, dist_roll_1,
        )

        # ── Check alert thresholds ────────────────────────────────────────────
        if current_underlying and levels and underlying_entry:
            await self._check_alerts(pool, pos_id, contract_symbol, underlying,
                                     current_underlying, levels, existing)

        # ── Generate/refresh chart (async, don't block scan) ─────────────────
        asyncio.create_task(
            self._refresh_chart(pool, pos_id, contract_symbol, underlying,
                                underlying_entry, atr, expiration_date, entry_date)
        )

    async def _check_alerts(
        self,
        pool: asyncpg.Pool,
        pos_id: uuid.UUID,
        contract: str,
        underlying: str,
        current_price: float,
        levels: dict,
        existing,
    ):
        """Fire Redis alerts when price crosses ATR thresholds (once per level)."""
        alerts_fired = {}
        if existing and existing["alerts_fired"]:
            try:
                alerts_fired = json.loads(existing["alerts_fired"])
            except Exception:
                pass

        alert_map = [
            ("emergency",  levels["level_emergency"],  "EMERGENCY EXIT",  "🚨"),
            ("exit_alert", levels["level_exit_alert"], "Exit Alert",      "⚠️"),
            ("roll_1",     levels["level_roll_1"],     "1st Roll Signal", "📈"),
            ("roll_2",     levels["level_roll_2"],     "2nd Roll Signal", "📈"),
            ("roll_3",     levels["level_roll_3"],     "3rd Roll Signal", "📈"),
        ]
        for extra in levels.get("extra_roll_levels", []):
            key   = f"roll_extra_{extra['multiplier']:.0f}"
            label = extra["label"]
            alert_map.append((key, extra["price"], f"{label} Signal", "📈"))

        for alert_key, threshold, label, emoji in alert_map:
            if alerts_fired.get(alert_key):
                continue  # already fired

            triggered = False
            if "emergency" in alert_key or "exit" in alert_key:
                triggered = current_price <= threshold
            else:
                triggered = current_price >= threshold

            if not triggered:
                continue

            log.warning("options_monitor.alert_triggered",
                        contract=contract, underlying=underlying,
                        alert=alert_key, price=current_price, threshold=threshold)

            # Publish alert to Redis
            await self.redis.xadd(
                STREAMS.get("alerts", "system.alerts"),
                {
                    "source":   "options-monitor",
                    "level":    "critical" if "emergency" in alert_key else "warning",
                    "title":    f"Options {label}: {underlying}",
                    "message":  (
                        f"{emoji} {contract} | {label} at ${threshold:.2f} | "
                        f"Current: ${current_price:.2f}"
                    ),
                    "ticker":   underlying,
                    "contract": contract,
                },
                maxlen=5_000,
            )

            # Log the alert event
            await pool.execute(
                """INSERT INTO option_trade_log
                   (position_id, contract_symbol, underlying, event_type,
                    underlying_price, atr_value, notes)
                   VALUES ($1,$2,$3,$4,$5::NUMERIC,$6::NUMERIC,$7)""",
                pos_id, contract, underlying,
                f"alert_{alert_key}", current_price,
                None,
                f"{label} triggered at underlying=${current_price:.2f}, threshold=${threshold:.2f}",
            )

            alerts_fired[alert_key] = True

        # Persist updated alerts_fired
        if alerts_fired:
            await pool.execute(
                "UPDATE option_positions SET alerts_fired=$2 WHERE id=$1",
                pos_id, json.dumps(alerts_fired),
            )

    async def _refresh_chart(
        self,
        pool: asyncpg.Pool,
        pos_id,
        contract: str,
        underlying: str,
        underlying_entry: Optional[float],
        atr: Optional[float],
        expiration_date,
        entry_date,
    ):
        """Generate chart and store as base64 PNG in DB raw column."""
        try:
            # Fetch OHLCV for chart
            raw = await call_mcp_tool(
                TRADINGVIEW_MCP_URL,
                "get_historical_data",
                {"symbol": underlying, "exchange": "NASDAQ",
                 "timeframe": "1d", "max_records": 60},
            )
            if not raw:
                raw = await call_mcp_tool(
                    TRADINGVIEW_MCP_URL,
                    "get_historical_data",
                    {"symbol": underlying, "exchange": "NYSE",
                     "timeframe": "1d", "max_records": 60},
                )
            if not raw:
                return

            data    = json.loads(raw)
            candles = data.get("candles") or data.get("data") or (data if isinstance(data, list) else [])
            if not candles:
                return

            pos_mock = {
                "underlying":      underlying,
                "underlying_entry": underlying_entry,
                "atr_14":          atr,
                "entry_date":      entry_date,
                "expiration_date": expiration_date,
            }
            chart_b64 = await _generate_chart(pos_mock, candles)
            if chart_b64:
                # Store chart in a dedicated column via raw JSONB update
                await pool.execute(
                    """UPDATE option_positions
                       SET raw = COALESCE(raw, '{}'::jsonb) || jsonb_build_object('chart_b64', $2::text)
                       WHERE id=$1""",
                    pos_id, chart_b64,
                )
                log.info("options_monitor.chart_saved", contract=contract)
        except Exception as e:
            log.warning("options_monitor.chart_refresh_failed", contract=contract, error=str(e))

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()
        if _pool:
            await _pool.close()


async def main():
    agent = OptionsMonitor()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
