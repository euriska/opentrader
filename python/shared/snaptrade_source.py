"""
SnapTrade portfolio data source.

Provides normalized brokerage account / position / balance data for the equities
dashboards (Buy-Borrow, etc.). Two interchangeable backends are supported and the
active one is chosen by whichever credentials are configured:

  1. SnapTrade official API — set SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY
     (app-level) plus per-user SNAPTRADE_USER_ID + SNAPTRADE_USER_SECRET. Uses
     SnapTrade's HMAC-signed request protocol to pull accounts/holdings/balances
     directly from https://api.snaptrade.com.

  2. Paycheck-to-Portfolio (Supabase) — set P2P_EMAIL + P2P_PASSWORD. Reads the
     SnapTrade-synced snapshots already aggregated by the p2p app via Supabase
     Auth + PostgREST. Used as a fallback when no SnapTrade app keys are present.

Both backends return the same normalized shapes so callers don't care which one
answered:

  account  = {id, name, institution, number_masked, type, currency}
  position = {account_id, symbol, quantity, price, market_value, cost_basis,
              avg_price, currency, unrealized_gain}
  balance  = {account_id, cash, buying_power, currency, as_of}

Public entry point: ``await fetch_portfolio()`` -> normalized dict.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# Paycheck2Portfolio public Supabase config (the anon key ships in the app's JS
# bundle; it is a public identifier, protected server-side by row-level security).
_P2P_SUPABASE_URL = os.environ.get(
    "P2P_SUPABASE_URL", "https://kqwatikcrxmjdxsetrei.supabase.co"
)
_P2P_ANON_KEY = os.environ.get(
    "P2P_SUPABASE_ANON",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtx"
    "d2F0aWtjcnhtamR4c2V0cmVpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTA1MzE4MjEsImV4cCI6"
    "MjA2NjEwNzgyMX0.",  # noqa: E501 — public anon key, RLS-protected
)

_SNAPTRADE_BASE = os.environ.get("SNAPTRADE_BASE_URL", "https://api.snaptrade.com")

# Short-lived P2P access-token cache: {email: (token, expires_at)}
_p2p_token_cache: dict = {}

# Common brokerage sweep money-market fund tickers — used as a cash_equivalent
# fallback for the P2P backend, which doesn't expose SnapTrade's cash_equivalent
# flag natively.
_CASH_EQUIV_SYMBOLS = {
    "SPAXX", "SPRXX", "FDRXX", "FZFXX", "FDLXX", "VMFXX", "VMRXX", "SWVXX",
    "SNVXX", "SNAXX", "GVMXX",
}


def _f(v, default=0.0) -> float:
    """Coerce possibly-None / string numerics to float."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _http_json(method: str, url: str, headers: dict = None,
                     params: dict = None, body: dict = None, timeout: int = 30):
    """Minimal async JSON HTTP via aiohttp (available in the webui image)."""
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.request(
            method, url, headers=headers, params=params,
            json=body, timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"{method} {url} -> {r.status}: {text[:200]}")
            return json.loads(text) if text else None


# ──────────────────────────────────────────────────────────────────────────────
# Backend 1: Paycheck-to-Portfolio (Supabase)
# ──────────────────────────────────────────────────────────────────────────────
async def _p2p_login(email: str, password: str) -> str:
    cached = _p2p_token_cache.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    data = await _http_json(
        "POST", f"{_P2P_SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": _P2P_ANON_KEY, "Content-Type": "application/json"},
        body={"email": email, "password": password},
    )
    tok = data["access_token"]
    _p2p_token_cache[email] = (tok, time.time() + int(data.get("expires_in", 3600)))
    return tok


async def _p2p_select(token: str, table: str, params: dict = None) -> list:
    return await _http_json(
        "GET", f"{_P2P_SUPABASE_URL}/rest/v1/{table}",
        headers={"apikey": _P2P_ANON_KEY, "Authorization": f"Bearer {token}"},
        params=params or {},
    ) or []


def _stable_tx_id(raw_id, date, symbol, ttype, amount) -> str:
    """A transaction's own broker-assigned id is stable and preferred (used as the
    key for user-assigned categories). Falls back to a deterministic hash of its
    fields when the source doesn't expose one — not guaranteed unique for true
    duplicate transactions on the same day, but good enough for category tagging."""
    if raw_id:
        return str(raw_id)
    basis = f"{date}|{symbol}|{ttype}|{amount}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


async def _p2p_fetch_transactions(token: str, limit: int = 200) -> list:
    """Best-effort recent transaction fetch — table layout isn't guaranteed, so
    any failure (missing table, schema mismatch) degrades to an empty list."""
    try:
        raw = await _p2p_select(token, "brokerage_transactions_daily", {
            "select": "*", "order": "trade_date.desc", "limit": str(limit),
        })
    except Exception:
        return []
    out = []
    for t in raw:
        date = t.get("trade_date") or t.get("settlement_date")
        symbol = t.get("symbol")
        ttype = (t.get("type") or t.get("transaction_type") or "").upper()
        amount = _f(t.get("amount") or t.get("net_amount"))
        out.append({
            "id":          _stable_tx_id(t.get("id"), date, symbol, ttype, amount),
            "date":        date,
            "symbol":      symbol,
            "type":        ttype,
            "description": t.get("description") or t.get("memo"),
            "quantity":    _f(t.get("quantity")),
            "price":       _f(t.get("price")),
            "amount":      amount,
            "account_id":  t.get("account_id"),
        })
    return out


async def _fetch_p2p(email: str, password: str) -> dict:
    token = await _p2p_login(email, password)

    accounts_raw = await _p2p_select(token, "brokerage_accounts", {"select": "*"})
    accounts = [{
        "id":            a.get("account_id") or a.get("id"),
        "name":          a.get("account_nickname") or a.get("account_name"),
        "institution":   a.get("institution_name"),
        "number_masked": a.get("account_number_masked"),
        "type":          a.get("account_type"),
        "currency":      a.get("currency") or "USD",
    } for a in accounts_raw if not a.get("is_hidden") and not a.get("removed_at")]

    # Positions & balances are daily snapshots — keep only the most recent date.
    pos_raw = await _p2p_select(token, "brokerage_positions_daily",
                                {"select": "*", "order": "as_of_date.desc"})
    latest_pos_date = pos_raw[0]["as_of_date"] if pos_raw else None
    positions = [{
        "account_id":      p.get("account_id"),
        "symbol":          p.get("symbol"),
        "quantity":        _f(p.get("quantity")),
        "price":           _f(p.get("price")),
        "market_value":    _f(p.get("market_value")),
        "cost_basis":      _f(p.get("cost_basis")),
        "avg_price":       _f(p.get("average_purchase_price")),
        "currency":        p.get("currency_code") or "USD",
        "unrealized_gain": _f(p.get("market_value")) - _f(p.get("cost_basis")),
        "cash_equivalent": (p.get("symbol") or "").upper() in _CASH_EQUIV_SYMBOLS,
        "security_type":   None,
    } for p in pos_raw if p.get("as_of_date") == latest_pos_date
        and not p.get("dismissed_at")]

    bal_raw = await _p2p_select(token, "brokerage_balances_daily",
                                {"select": "*", "order": "as_of_date.desc"})
    latest_bal_date = bal_raw[0]["as_of_date"] if bal_raw else None
    balances = [{
        "account_id":   b.get("account_id"),
        "cash":         _f(b.get("cash")),
        "buying_power": _f(b.get("buying_power")),
        "currency":     b.get("currency_code") or "USD",
        "as_of":        b.get("as_of_date"),
    } for b in bal_raw if b.get("as_of_date") == latest_bal_date]

    # Prior trading day's snapshot (same tables, second-most-recent date) — used
    # only for the day-change stat; absent if fewer than two days of history exist.
    pos_dates = sorted({p["as_of_date"] for p in pos_raw if p.get("as_of_date")}, reverse=True)
    prev_pos_date = pos_dates[1] if len(pos_dates) > 1 else None
    prev_positions = [{"market_value": _f(p.get("market_value"))}
                      for p in pos_raw if p.get("as_of_date") == prev_pos_date]

    bal_dates = sorted({b["as_of_date"] for b in bal_raw if b.get("as_of_date")}, reverse=True)
    prev_bal_date = bal_dates[1] if len(bal_dates) > 1 else None
    prev_balances = [{"cash": _f(b.get("cash"))}
                     for b in bal_raw if b.get("as_of_date") == prev_bal_date]

    transactions = await _p2p_fetch_transactions(token)

    return {
        "source":         "paycheck2portfolio",
        "as_of":          latest_pos_date,
        "accounts":       accounts,
        "positions":      positions,
        "balances":       balances,
        "prev_positions": prev_positions,
        "prev_balances":  prev_balances,
        "transactions":   transactions,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Backend 2: SnapTrade official API (HMAC-signed requests)
# ──────────────────────────────────────────────────────────────────────────────
def _snaptrade_sign(consumer_key: str, path: str, query: dict, body=None) -> str:
    """SnapTrade request signature: base64(HMAC-SHA256(consumerKey, sigContent))."""
    sig_object = {
        "content": body,
        "path":    path,
        "query":   urlencode(sorted(query.items())),
    }
    sig_content = json.dumps(sig_object, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(consumer_key.encode(), sig_content.encode(),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


async def _snaptrade_get(path: str, extra_query: dict) -> list:
    client_id    = os.environ["SNAPTRADE_CLIENT_ID"]
    consumer_key = os.environ["SNAPTRADE_CONSUMER_KEY"]
    query = {"clientId": client_id, "timestamp": str(int(time.time()))}
    query.update(extra_query)
    signature = _snaptrade_sign(consumer_key, path, query, None)
    # SnapTrade recomputes the signature from the query string exactly as sent, so
    # the wire order must match the sorted order used to sign it — aiohttp sends a
    # plain dict in insertion order, which only coincidentally matched sorted order
    # for the 4-key {clientId,timestamp,userId,userSecret} calls; any additional
    # param (limit, startDate, ...) broke it with a 401 "Unable to verify signature".
    sorted_query = sorted(query.items())
    return await _http_json(
        "GET", f"{_SNAPTRADE_BASE}{path}",
        headers={"Signature": signature, "Accept": "application/json"},
        params=sorted_query,
    ) or []


async def _snaptrade_fetch_activities(account_id: str, uq: dict, limit: int = 100,
                                      days: int = 365) -> list:
    """Activity history for one account via SnapTrade's account-level activities
    endpoint (the user-level /api/v1/activities endpoint returns 410 Gone — see
    https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getAccountActivities).
    Best-effort — response shape can vary by brokerage, so any parse failure
    degrades to []."""
    try:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        raw = await _snaptrade_get(f"/api/v1/accounts/{account_id}/activities",
                                   {**uq, "limit": str(limit), "startDate": start_date})
        items = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
        out = []
        for a in items:
            sym = a.get("symbol")
            if isinstance(sym, dict):
                sym = sym.get("symbol") or sym.get("raw_symbol")
            date = (a.get("trade_date") or a.get("settlement_date") or "")[:10]
            ttype = (a.get("type") or a.get("option_type") or "").upper()
            amount = _f(a.get("amount"))
            out.append({
                "id":          _stable_tx_id(a.get("id"), date, sym, ttype, amount),
                "date":        date,
                "symbol":      sym,
                "type":        ttype,
                "description": a.get("description"),
                "quantity":    _f(a.get("units")),
                "price":       _f(a.get("price")),
                "amount":      amount,
                "account_id":  account_id,
            })
        return out
    except Exception:
        return []


async def _fetch_snaptrade() -> dict:
    user_id     = os.environ["SNAPTRADE_USER_ID"]
    user_secret = os.environ["SNAPTRADE_USER_SECRET"]
    uq = {"userId": user_id, "userSecret": user_secret}

    accounts_raw = await _snaptrade_get("/api/v1/accounts", uq)
    accounts, positions, balances, transactions = [], [], [], []
    last_sync = None
    for a in accounts_raw:
        aid = a.get("id")
        accounts.append({
            "id":            aid,
            "name":          a.get("name"),
            "institution":   a.get("institution_name"),
            "number_masked": a.get("number"),
            "type":          (a.get("meta") or {}).get("type"),
            "currency":      (a.get("balance") or {}).get("total", {}).get("currency", "USD"),
        })
        sync_ts = ((a.get("sync_status") or {}).get("holdings") or {}).get("last_successful_sync")
        if sync_ts and (last_sync is None or sync_ts > last_sync):
            last_sync = sync_ts
        transactions.extend(await _snaptrade_fetch_activities(aid, uq))
        holdings = await _snaptrade_get(f"/api/v1/accounts/{aid}/positions", uq)
        for p in holdings:
            sym_obj = (p.get("symbol") or {}).get("symbol") or {}
            sym = sym_obj.get("symbol") or (p.get("symbol") or {}).get("symbol")
            qty = _f(p.get("units") or p.get("fractional_units"))
            price = _f(p.get("price"))
            cost = _f(p.get("average_purchase_price"))
            mv = qty * price
            cash_equiv = bool(p.get("cash_equivalent")) or \
                (sym or "").upper() in _CASH_EQUIV_SYMBOLS
            positions.append({
                "account_id":      aid,
                "symbol":          sym,
                "quantity":        qty,
                "price":           price,
                "market_value":    mv,
                "cost_basis":      cost * qty,
                "avg_price":       cost,
                "currency":        "USD",
                "unrealized_gain": mv - cost * qty,
                "cash_equivalent": cash_equiv,
                "security_type":   (sym_obj.get("type") or {}).get("code"),
            })
        bals = await _snaptrade_get(f"/api/v1/accounts/{aid}/balances", uq)
        for b in bals:
            balances.append({
                "account_id":   aid,
                "cash":         _f(b.get("cash")),
                "buying_power": _f(b.get("buying_power") or b.get("cash")),
                "currency":     (b.get("currency") or {}).get("code", "USD"),
                "as_of":        None,
            })
    transactions.sort(key=lambda t: t.get("date") or "", reverse=True)

    return {
        "source":         "snaptrade",
        "as_of":          last_sync,
        "accounts":       accounts,
        "positions":      positions,
        "balances":       balances,
        "prev_positions": [],
        "prev_balances":  [],
        "transactions":   transactions,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
def configured_source() -> str | None:
    """Return the backend name that credentials are configured for, else None."""
    if os.environ.get("SNAPTRADE_CLIENT_ID") and os.environ.get("SNAPTRADE_CONSUMER_KEY") \
            and os.environ.get("SNAPTRADE_USER_ID") and os.environ.get("SNAPTRADE_USER_SECRET"):
        return "snaptrade"
    if os.environ.get("P2P_EMAIL") and os.environ.get("P2P_PASSWORD"):
        return "paycheck2portfolio"
    return None


async def fetch_portfolio() -> dict:
    """Fetch normalized portfolio from whichever backend is configured.

    Raises RuntimeError if no source is configured.
    """
    src = configured_source()
    if src == "snaptrade":
        return await _fetch_snaptrade()
    if src == "paycheck2portfolio":
        return await _fetch_p2p(os.environ["P2P_EMAIL"], os.environ["P2P_PASSWORD"])
    raise RuntimeError(
        "No SnapTrade source configured. Set SNAPTRADE_CLIENT_ID/CONSUMER_KEY/"
        "USER_ID/USER_SECRET, or P2P_EMAIL/P2P_PASSWORD."
    )


def compute_buy_borrow(portfolio: dict, ltv: float = 0.50,
                       maint_ltv: float = 0.70, apr: float = 0.065,
                       cap_gains_rate: float = 0.238, draw: float = 0.0) -> dict:
    """Compute Buy-Borrow-Die metrics from a normalized portfolio.

    ltv            — advance rate of the securities-backed line (fraction of collateral).
    maint_ltv      — loan/value at which a maintenance call is triggered.
    apr            — annual borrowing rate on drawn funds.
    cap_gains_rate — combined LT cap-gains + NIIT rate used for tax-deferral estimate.
    draw           — additional amount to model borrowing on top of current debt.
    """
    positions = portfolio.get("positions", [])
    balances = portfolio.get("balances", [])

    collateral = sum(p["market_value"] for p in positions)
    cost_basis = sum(p["cost_basis"] for p in positions)
    unrealized = collateral - cost_basis

    # A negative cash balance is an outstanding margin loan.
    current_borrow = sum(max(0.0, -b["cash"]) for b in balances)
    cash = sum(max(0.0, b["cash"]) for b in balances)
    buying_power = sum(b["buying_power"] for b in balances)

    credit_line = collateral * ltv
    available = max(0.0, credit_line - current_borrow)
    proposed_borrow = current_borrow + max(0.0, draw)

    current_ltv = (current_borrow / collateral) if collateral else 0.0
    proposed_ltv = (proposed_borrow / collateral) if collateral else 0.0

    # Portfolio value at which loan/value hits the maintenance threshold.
    value_at_call = (proposed_borrow / maint_ltv) if maint_ltv else 0.0
    cushion_pct = ((collateral - value_at_call) / collateral) if collateral else 0.0

    annual_interest = proposed_borrow * apr
    tax_deferred = max(0.0, unrealized) * cap_gains_rate

    holdings = []
    for p in sorted(positions, key=lambda x: x["market_value"], reverse=True):
        mv = p["market_value"]
        # Cash-equivalent positions (sweep money-market funds) are already cash —
        # there's no margin requirement to pledge them and nothing to borrow against.
        cash_equiv = bool(p.get("cash_equivalent"))
        holdings.append({
            "symbol":          p["symbol"],
            "quantity":        round(p["quantity"], 4),
            "price":           round(p["price"], 4),
            "market_value":    round(mv, 2),
            "cost_basis":      round(p["cost_basis"], 2),
            "unrealized_gain": round(p["unrealized_gain"], 2),
            "unrealized_pct":  round((p["unrealized_gain"] / p["cost_basis"] * 100)
                                     if p["cost_basis"] else 0.0, 2),
            "weight":          round((mv / collateral * 100) if collateral else 0.0, 2),
            "borrowable":      0.0 if cash_equiv else round(mv * ltv, 2),
            "cash_equivalent": cash_equiv,
            # Initial margin requirement — the fraction of value that must be
            # covered by equity (not borrowed). Under Reg T this equals the
            # advance rate (ltv) for standard marginable securities.
            "margin_req_pct":  None if cash_equiv else round(ltv * 100, 1),
        })

    return {
        "source":   portfolio.get("source"),
        "as_of":    portfolio.get("as_of"),
        "accounts": portfolio.get("accounts", []),
        "assumptions": {
            "ltv": ltv, "maint_ltv": maint_ltv, "apr": apr,
            "cap_gains_rate": cap_gains_rate, "draw": max(0.0, draw),
        },
        "totals": {
            "collateral":       round(collateral, 2),
            "cost_basis":       round(cost_basis, 2),
            "unrealized_gain":  round(unrealized, 2),
            "cash":             round(cash, 2),
            "buying_power":     round(buying_power, 2),
            "num_positions":    len(positions),
        },
        "borrow": {
            "credit_line":      round(credit_line, 2),
            "current_borrow":   round(current_borrow, 2),
            "available":        round(available, 2),
            "proposed_borrow":  round(proposed_borrow, 2),
            "current_ltv":      round(current_ltv, 4),
            "proposed_ltv":     round(proposed_ltv, 4),
            "value_at_call":    round(value_at_call, 2),
            "cushion_pct":      round(cushion_pct, 4),
            "annual_interest":  round(annual_interest, 2),
            "monthly_interest": round(annual_interest / 12.0, 2),
        },
        "tax": {
            "unrealized_gain":  round(max(0.0, unrealized), 2),
            "deferred_tax":     round(tax_deferred, 2),
            "cap_gains_rate":   cap_gains_rate,
        },
        "holdings": holdings,
    }


def compute_fire_summary(portfolio: dict) -> dict:
    """Overview metrics for the FIRE dashboard landing panels: gross/net portfolio
    value, margin balance, equity %, day change, total gain/return, position
    allocation, and recently synced transactions."""
    positions = portfolio.get("positions", [])
    balances = portfolio.get("balances", [])

    collateral = sum(p["market_value"] for p in positions)
    cost_basis = sum(p["cost_basis"] for p in positions)
    total_gain = collateral - cost_basis

    cash_raw = sum(b["cash"] for b in balances)
    available_cash = sum(max(0.0, b["cash"]) for b in balances)
    margin_balance = sum(max(0.0, -b["cash"]) for b in balances)

    gross = collateral + available_cash
    net = collateral + cash_raw
    equity_pct = (net / gross * 100.0) if gross else 0.0
    total_return_pct = (total_gain / cost_basis * 100.0) if cost_basis else 0.0

    unique_positions = len({p["symbol"] for p in positions if p.get("symbol") and p.get("quantity")})

    # Day change requires a prior-day snapshot; absent for live-API (SnapTrade)
    # sources or when fewer than two days of P2P history exist.
    prev_positions = portfolio.get("prev_positions") or []
    prev_balances = portfolio.get("prev_balances") or []
    day_change = day_change_pct = None
    if prev_positions or prev_balances:
        prev_net = sum(_f(p.get("market_value")) for p in prev_positions) + \
            sum(_f(b.get("cash")) for b in prev_balances)
        if prev_net:
            day_change = net - prev_net
            day_change_pct = day_change / prev_net * 100.0

    by_symbol: dict = {}
    for p in positions:
        sym = p.get("symbol") or "?"
        by_symbol[sym] = by_symbol.get(sym, 0.0) + p["market_value"]
    allocation = sorted((
        {"symbol": s, "value": round(v, 2),
         "pct": round((v / collateral * 100.0) if collateral else 0.0, 2)}
        for s, v in by_symbol.items()
    ), key=lambda x: x["value"], reverse=True)

    return {
        "source":            portfolio.get("source"),
        "last_sync":         portfolio.get("as_of"),
        "gross_portfolio":   round(gross, 2),
        "net_portfolio":     round(net, 2),
        "margin_balance":    round(margin_balance, 2),
        "equity_pct":        round(equity_pct, 2),
        "unique_positions":  unique_positions,
        "available_cash":    round(available_cash, 2),
        "day_change":        round(day_change, 2) if day_change is not None else None,
        "day_change_pct":    round(day_change_pct, 2) if day_change_pct is not None else None,
        "total_gain":        round(total_gain, 2),
        "total_return_pct":  round(total_return_pct, 2),
        "allocation":        allocation,
        "transactions":      (portfolio.get("transactions") or [])[:25],
    }


def compute_positions_summary(portfolio: dict) -> dict:
    """Positions dashboard: total value, position/symbol counts, largest holding,
    and a per-position table (shares, last price, cost basis/share, gain, return %,
    weight). One row per raw position — the same symbol held across multiple
    accounts appears as separate rows, which is why num_positions can exceed
    unique_symbols."""
    positions = portfolio.get("positions", [])
    total_value = sum(p["market_value"] for p in positions)
    unique_symbols = len({p["symbol"] for p in positions if p.get("symbol")})

    rows = []
    for p in positions:
        qty = p["quantity"]
        mv = p["market_value"]
        cost_basis_total = p["cost_basis"]
        gain = p["unrealized_gain"]
        rows.append({
            "symbol":               p["symbol"],
            "shares":               round(qty, 4),
            "last_price":           round(p["price"], 4),
            "cost_basis_per_share": round(cost_basis_total / qty, 4) if qty else 0.0,
            "total_gain":           round(gain, 2),
            "total_return_pct":     round((gain / cost_basis_total * 100.0)
                                          if cost_basis_total else 0.0, 2),
            "pct_of_portfolio":     round((mv / total_value * 100.0) if total_value else 0.0, 2),
            "market_value":         round(mv, 2),
        })
    rows.sort(key=lambda r: r["market_value"], reverse=True)
    largest = rows[0] if rows else None

    return {
        "source": portfolio.get("source"),
        "as_of":  portfolio.get("as_of"),
        "totals": {
            "total_value":    round(total_value, 2),
            "num_positions":  len(positions),
            "unique_symbols": unique_symbols,
        },
        "largest_holding": {
            "symbol":           largest["symbol"],
            "market_value":     largest["market_value"],
            "pct_of_portfolio": largest["pct_of_portfolio"],
        } if largest else None,
        "positions": rows,
    }


def compute_cash_flow_summary(portfolio: dict, start_date: str | None = None,
                              end_date: str | None = None, category: str | None = None) -> dict:
    """Cash flow dashboard, derived from the synced transaction history.

    Category rules (based on the normalized transaction ``type``):
      total_income     — DIVIDEND, plus positive-amount INTEREST (interest earned)
      total_expenses   — FEE
      margin_cost      — negative-amount INTEREST (margin loan interest charged)
      contributions    — CONTRIBUTION
      cash_withdrawals — WITHDRAWAL (absolute value)
      capital_deployed — BUY (absolute value of cash spent)
      net_operating    — total_income - total_expenses - margin_cost

    SELL, JOURNALED, PAYMENT, and other types aren't attributed to any bucket:
    SELL is an investing (not operating) activity, and JOURNALED/PAYMENT rows
    observed in practice are offsetting internal transfers that net to zero.

    ``start_date``/``end_date`` (ISO ``YYYY-MM-DD``, inclusive) scope every
    figure to that window; default is the trailing 30 days. ``category``
    restricts to transactions the caller has tagged with that exact name (the
    caller is expected to have joined user categories onto each transaction
    dict before calling this).

    ``daily_series`` spans the full requested date range (one entry per
    calendar day), for the sliding chart. ``by_category`` sums transaction
    amounts by tag — independent of the type-based buckets above, since a
    user's category doesn't have a defined mapping onto them. ``transactions``
    is the same date/category-filtered set (newest first), for the Cash Flow
    dashboard's transaction table — type/further filtering is left to the caller.
    """
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    end_d = _date.fromisoformat(end_date) if end_date else today
    start_d = _date.fromisoformat(start_date) if start_date else (end_d - _td(days=29))
    start_iso, end_iso = start_d.isoformat(), end_d.isoformat()

    txs = portfolio.get("transactions") or []
    txs = [t for t in txs if t.get("date") and start_iso <= t["date"] <= end_iso]
    if category:
        txs = [t for t in txs if t.get("category") == category]

    income = expenses = margin_cost = contributions = withdrawals = capital_deployed = 0.0
    daily: dict = {}
    by_category: dict = {}

    for t in txs:
        ttype = (t.get("type") or "").upper()
        amt = _f(t.get("amount"))
        date = t.get("date")
        day_delta = 0.0

        cat = t.get("category")
        if cat:
            by_category[cat] = by_category.get(cat, 0.0) + amt

        if ttype == "DIVIDEND":
            income += amt
            day_delta = amt
        elif ttype == "INTEREST":
            if amt >= 0:
                income += amt
                day_delta = amt
            else:
                margin_cost += -amt
                day_delta = amt
        elif ttype == "FEE":
            fee = abs(amt)
            expenses += fee
            day_delta = -fee
        elif ttype == "CONTRIBUTION":
            contributions += amt
        elif ttype == "WITHDRAWAL":
            withdrawals += abs(amt)
        elif ttype == "BUY":
            capital_deployed += abs(amt)

        if date and day_delta:
            daily[date] = daily.get(date, 0.0) + day_delta

    net_operating = income - expenses - margin_cost

    series = []
    d = start_d
    while d <= end_d:
        iso = d.isoformat()
        series.append({"date": iso, "net_operating": round(daily.get(iso, 0.0), 2)})
        d += _td(days=1)

    return {
        "source": portfolio.get("source"),
        "range": {"start_date": start_iso, "end_date": end_iso, "category": category},
        "totals": {
            "total_income":     round(income, 2),
            "total_expenses":   round(expenses, 2),
            "margin_cost":      round(margin_cost, 2),
            "contributions":    round(contributions, 2),
            "cash_withdrawals": round(withdrawals, 2),
            "capital_deployed": round(capital_deployed, 2),
            "net_operating":    round(net_operating, 2),
        },
        "daily_series": series,
        "by_category": sorted((
            {"category": c, "amount": round(v, 2)} for c, v in by_category.items()
        ), key=lambda x: abs(x["amount"]), reverse=True),
        "transactions": sorted(txs, key=lambda t: t.get("date") or "", reverse=True),
    }
