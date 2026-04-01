"""
Response formatters — convert raw MCP JSON/text into readable Discord markdown.
Telegram conversion handled separately in telegram_bot.py.
"""
import json
import re


def _trunc(s: str, n: int = 1800) -> str:
    return s if len(s) <= n else s[:n] + "\n…*(truncated)*"


def fmt_quote(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    if raw.startswith("Error") or "not found" in raw:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return f"**{ticker}**\n{_trunc(raw)}"

    price   = data.get("currentPrice") or data.get("regularMarketPrice") or data.get("ask", "N/A")
    change  = data.get("regularMarketChangePercent")
    name    = data.get("longName") or data.get("shortName", ticker)
    high52  = data.get("fiftyTwoWeekHigh", "N/A")
    low52   = data.get("fiftyTwoWeekLow", "N/A")
    mktcap  = data.get("marketCap")
    pe      = data.get("trailingPE")
    vol     = data.get("regularMarketVolume")
    sector  = data.get("sector", "")
    summary = data.get("longBusinessSummary", "")

    chg_str = f" ({change:+.2f}%)" if isinstance(change, (int, float)) else ""
    cap_str = f"${mktcap/1e9:.1f}B" if isinstance(mktcap, (int, float)) else "N/A"
    vol_str = f"{vol:,}" if isinstance(vol, int) else "N/A"
    pe_str  = f"{pe:.1f}x" if isinstance(pe, (int, float)) else "N/A"
    sect_str = f"  ·  {sector}" if sector else ""

    lines = [
        f"**{name} ({ticker})**{sect_str}",
        f"Price: **${price}{chg_str}**",
        f"52w Range: ${low52} — ${high52}",
        f"Market Cap: {cap_str}  ·  P/E: {pe_str}  ·  Volume: {vol_str}",
    ]
    if summary:
        lines.append(f"\n{summary[:300]}…")
    return "\n".join(lines)


def fmt_news(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    if "No news found" in raw or raw.startswith("Error"):
        return raw
    articles = raw.strip().split("\n\n")
    lines = [f"**Latest news — {ticker}**\n"]
    for art in articles[:6]:
        title = url = ""
        for line in art.split("\n"):
            if line.startswith("Title:"):
                title = line[6:].strip()
            elif line.startswith("URL:"):
                url = line[4:].strip()
        if title:
            lines.append(f"• **{title}**")
            if url:
                lines.append(f"  {url}")
    return "\n".join(lines)


def fmt_trending(raw: str, cmd: str, args: list) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "error" in data:
            return f"Error: {data['error']}"
        tickers = data if isinstance(data, list) else []
        return "**Trending / Most Active**\n" + "  ".join(f"`{t}`" for t in tickers)
    except Exception:
        return _trunc(raw)


def fmt_history(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    try:
        rows = json.loads(raw)
        if not rows:
            return f"No history found for {ticker}"
        # Show last 5 rows
        lines = [f"**{ticker} Price History** (last {min(5,len(rows))} periods)"]
        for row in rows[-5:]:
            date = str(row.get("Date", ""))[:10]
            close = row.get("Close", "N/A")
            vol = row.get("Volume", "")
            vol_str = f"  vol {vol:,}" if isinstance(vol, int) else ""
            lines.append(f"`{date}`  Close: **${close:.2f}**{vol_str}" if isinstance(close, float) else f"`{date}`  {close}")
        return "\n".join(lines)
    except Exception:
        return _trunc(raw)


def fmt_options_expiry(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    try:
        dates = json.loads(raw)
        return f"**{ticker} Options Expiries**\n" + "\n".join(f"• `{d}`" for d in dates[:20])
    except Exception:
        return _trunc(raw)


def fmt_upgrades(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    try:
        rows = json.loads(raw)
        if not rows:
            return f"No recent upgrades/downgrades for {ticker}"
        lines = [f"**{ticker} Analyst Upgrades/Downgrades**\n"]
        for r in rows[:10]:
            date = str(r.get("GradeDate", ""))[:10]
            firm = r.get("Firm", "?")
            action = r.get("Action", "")
            to_g = r.get("ToGrade", "")
            from_g = r.get("FromGrade", "")
            chg = f"{from_g} → {to_g}" if from_g else to_g
            lines.append(f"• **{firm}** `{date}` — {action}: {chg}")
        return "\n".join(lines)
    except Exception:
        return _trunc(raw)


def _fmt_num(v) -> str:
    """Format a large number as $1.23B / $456M / $12K."""
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e12:
        return f"${v/1e12:.2f}T"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.2f}"


def _pct_chg(a, b) -> str:
    """YoY change from b to a."""
    try:
        if b and float(b) != 0:
            pct = (float(a) - float(b)) / abs(float(b)) * 100
            arrow = "▲" if pct >= 0 else "▼"
            return f" {arrow}{abs(pct):.1f}%"
    except Exception:
        pass
    return ""


# Key metrics to display per statement type
_INCOME_KEYS = [
    ("Total Revenue",    "Revenue"),
    ("Gross Profit",     "Gross Profit"),
    ("Operating Income", "Operating Income"),
    ("EBITDA",           "EBITDA"),
    ("Net Income",       "Net Income"),
    ("Diluted EPS",      "EPS (diluted)"),
]
_BALANCE_KEYS = [
    ("Cash Cash Equivalents And Short Term Investments", "Cash & Equivalents"),
    ("Total Assets",                        "Total Assets"),
    ("Total Liabilities Net Minority Interest", "Total Liabilities"),
    ("Common Stock Equity",                 "Stockholders' Equity"),
    ("Total Debt",                          "Total Debt"),
    ("Net Debt",                            "Net Debt"),
]
_CASHFLOW_KEYS = [
    ("Operating Cash Flow", "Operating Cash Flow"),
    ("Capital Expenditure", "CapEx"),
    ("Free Cash Flow",      "Free Cash Flow"),
    ("Cash Dividends Paid", "Dividends Paid"),
    ("Repurchase Of Capital Stock", "Buybacks"),
]

_TYPE_META = {
    "income_stmt":             ("Income Statement (Annual)", _INCOME_KEYS),
    "quarterly_income_stmt":   ("Income Statement (Quarterly)", _INCOME_KEYS),
    "balance_sheet":           ("Balance Sheet (Annual)", _BALANCE_KEYS),
    "quarterly_balance_sheet": ("Balance Sheet (Quarterly)", _BALANCE_KEYS),
    "cashflow":                ("Cash Flow Statement (Annual)", _CASHFLOW_KEYS),
    "quarterly_cashflow":      ("Cash Flow Statement (Quarterly)", _CASHFLOW_KEYS),
}


def fmt_financials(raw: str, cmd: str, args: list) -> str:
    ticker      = args[0].upper() if args else "?"
    stmt_type   = args[1] if len(args) > 1 else "income_stmt"
    title, keys = _TYPE_META.get(stmt_type, ("Financials", _INCOME_KEYS))

    if raw.startswith("Error") or "not found" in raw:
        return raw
    try:
        periods = json.loads(raw)
    except Exception:
        return _trunc(raw)

    if not periods:
        return f"No data returned for {ticker} {stmt_type}"

    # Show up to 4 most recent periods
    periods = periods[:4]
    dates   = [p.get("date", "?")[:7] for p in periods]  # YYYY-MM

    header = f"**{ticker} — {title}**\n"
    # Column header row
    col_w = 11
    pad   = " " * 26
    header += f"`{'Metric':<24}  " + "  ".join(f"{d:>{col_w}}" for d in dates) + "`\n"
    header += "`" + "─" * (26 + (col_w + 2) * len(dates)) + "`\n"

    rows = []
    for field, label in keys:
        vals = [p.get(field) for p in periods]
        # Skip if all null
        if all(v is None for v in vals):
            continue
        formatted = [_fmt_num(v) for v in vals]
        # YoY/QoQ change arrow on most recent vs prior
        chg = _pct_chg(vals[0], vals[1]) if len(vals) >= 2 else ""
        rows.append(f"`{label:<24}  " + "  ".join(f"{v:>{col_w}}" for v in formatted) + f"`{chg}")

    if not rows:
        return f"No matching metrics found for {ticker} {stmt_type}"

    return header + "\n".join(rows)


_MAJOR_LABELS = {
    "insidersPercentHeld":          "Insider Ownership",
    "institutionsPercentHeld":      "Institutional Ownership",
    "institutionsFloatPercentHeld": "Institutional (Float)",
    "institutionsCount":            "# of Institutions",
}


def fmt_holders(raw: str, cmd: str, args: list) -> str:
    ticker      = args[0].upper() if args else "?"
    holder_type = args[1] if len(args) > 1 else "institutional_holders"

    if raw.startswith("Error") or "not found" in raw:
        return raw
    try:
        rows = json.loads(raw)
    except Exception:
        return _trunc(raw)
    if not rows:
        return f"No holder data found for {ticker}"

    # ── Major holders ─────────────────────────────────────────────────────────
    if holder_type == "major_holders":
        lines = [f"**{ticker} — Ownership Summary**\n"]
        for row in rows:
            metric = row.get("metric", "")
            val    = row.get("Value")
            label  = _MAJOR_LABELS.get(metric, metric)
            if val is None:
                continue
            if metric == "institutionsCount":
                lines.append(f"`{label:<30}` **{int(val):,}**")
            else:
                lines.append(f"`{label:<30}` **{val*100:.2f}%**")
        return "\n".join(lines)

    # ── Institutional / Mutual fund holders ──────────────────────────────────
    if holder_type in ("institutional_holders", "mutualfund_holders"):
        kind  = "Institutional" if holder_type == "institutional_holders" else "Mutual Fund"
        lines = [f"**{ticker} — Top {kind} Holders**\n"]
        for r in rows[:10]:
            holder  = r.get("Holder", "Unknown")
            pct     = r.get("pctHeld", 0)
            shares  = r.get("Shares", 0)
            value   = r.get("Value", 0)
            chg     = r.get("pctChange", 0)
            chg_str = f"({'▲' if chg >= 0 else '▼'}{abs(chg)*100:.2f}%)" if chg is not None else ""
            # Truncate long fund names
            name = holder[:40] + "…" if len(holder) > 40 else holder
            lines.append(
                f"**{name}**\n"
                f"  `{pct*100:.2f}%`  ·  {int(shares):,} shares  ·  {_fmt_num(value)}  {chg_str}"
            )
        return "\n".join(lines)

    # ── Insider transactions / purchases ─────────────────────────────────────
    if holder_type in ("insider_transactions", "insider_purchases"):
        kind  = "Insider Transactions" if holder_type == "insider_transactions" else "Insider Purchases"
        lines = [f"**{ticker} — Recent {kind}**\n"]
        for r in rows[:10]:
            insider = r.get("Insider", r.get("Name", "Unknown"))
            pos     = r.get("Position", "")
            text    = r.get("Text", r.get("Transaction", ""))
            shares  = r.get("Shares", 0)
            value   = r.get("Value", 0)
            date    = str(r.get("Start Date", r.get("Date", "")))[:10]
            lines.append(
                f"**{insider}** _{pos}_  `{date}`\n"
                f"  {text or 'Transaction'}  ·  {int(shares):,} shares  ·  {_fmt_num(value)}"
            )
        return "\n".join(lines)

    # ── Insider roster ────────────────────────────────────────────────────────
    if holder_type == "insider_roster_holders":
        lines = [f"**{ticker} — Insider Roster**\n"]
        for r in rows[:15]:
            name  = r.get("Name", "Unknown")
            pos   = r.get("Position", "")
            url   = r.get("URL", "")
            lines.append(f"• **{name}** — _{pos}_")
        return "\n".join(lines)

    return fmt_generic(raw, cmd, args)


def fmt_generic(raw: str, cmd: str, args: list) -> str:
    """Pretty-print JSON or truncate plain text."""
    try:
        data = json.loads(raw)
        pretty = json.dumps(data, indent=2)
        return f"```json\n{_trunc(pretty, 1600)}\n```"
    except Exception:
        return _trunc(raw)


def _nested_get(data, *keys):
    """Safely traverse nested dicts/lists."""
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k)
        elif isinstance(data, list) and isinstance(k, int):
            data = data[k] if k < len(data) else None
        else:
            return None
    return data


def _parse_alpaca(raw: str):
    """Parse Alpaca MCP response — may be plain JSON or wrapped in text."""
    try:
        return json.loads(raw)
    except Exception:
        return None


def fmt_alpaca_account(raw: str, cmd: str, args: list) -> str:
    data = _parse_alpaca(raw)
    if not data:
        return _trunc(raw)
    # Handle list wrapper
    if isinstance(data, list):
        data = data[0] if data else {}

    equity    = _fmt_num(data.get("equity") or data.get("portfolio_value"))
    cash      = _fmt_num(data.get("cash"))
    buying    = _fmt_num(data.get("buying_power") or data.get("regt_buying_power"))
    pnl_day   = data.get("unrealized_pl") or data.get("equity") and data.get("last_equity") and \
                str(float(data.get("equity",0)) - float(data.get("last_equity",0)))
    status    = data.get("status", "")
    acct_type = data.get("account_type", data.get("pattern_day_trader") and "PDT" or "")

    lines = ["**Alpaca Account**\n"]
    lines.append(f"Portfolio Value: **{equity}**")
    lines.append(f"Cash:            **{cash}**")
    lines.append(f"Buying Power:    **{buying}**")
    if pnl_day:
        try:
            v = float(pnl_day)
            arrow = "▲" if v >= 0 else "▼"
            lines.append(f"Today's P&L:     **{arrow}{_fmt_num(abs(v))}**")
        except Exception:
            pass
    if status:
        lines.append(f"Status: `{status}`")
    return "\n".join(lines)


def fmt_alpaca_positions(raw: str, cmd: str, args: list) -> str:
    data = _parse_alpaca(raw)
    if not isinstance(data, list):
        return _trunc(raw)
    if not data:
        return "**Positions** — No open positions"

    lines = [f"**Open Positions ({len(data)})**\n"]
    for p in data:
        sym    = p.get("symbol", "?")
        qty    = p.get("qty") or p.get("quantity", "?")
        side   = p.get("side", "long")
        mv     = _fmt_num(p.get("market_value"))
        pl     = p.get("unrealized_pl")
        plpc   = p.get("unrealized_plpc")
        entry  = p.get("avg_entry_price") or p.get("cost_basis")

        try:
            pl_f    = float(pl) if pl is not None else None
            plpc_f  = float(plpc) if plpc is not None else None
            arrow   = "▲" if (pl_f or 0) >= 0 else "▼"
            pl_str  = f"{arrow}{_fmt_num(abs(pl_f))}" if pl_f is not None else "N/A"
            pct_str = f" ({plpc_f*100:+.2f}%)" if plpc_f is not None else ""
        except Exception:
            pl_str = pct_str = ""

        lines.append(
            f"**{sym}** `{side}` {qty} shares  ·  MV: {mv}\n"
            f"  Entry: ${entry}  ·  P&L: {pl_str}{pct_str}"
        )
    return "\n".join(lines)


def fmt_alpaca_orders(raw: str, cmd: str, args: list) -> str:
    data = _parse_alpaca(raw)
    if not isinstance(data, list):
        return _trunc(raw)
    if not data:
        return "**Orders** — No orders found"

    status_filter = args[0] if args else "open"
    lines = [f"**Orders ({status_filter})**\n"]
    for o in data[:15]:
        sym    = o.get("symbol", "?")
        side   = o.get("side", "?")
        qty    = o.get("qty") or o.get("notional", "?")
        otype  = o.get("type", "market")
        status = o.get("status", "?")
        filled = o.get("filled_avg_price")
        date   = str(o.get("submitted_at") or o.get("created_at") or "")[:10]

        price_str = f" @ ${filled}" if filled else ""
        lines.append(
            f"**{sym}** `{side}` {qty} · {otype} · `{status}`{price_str}  _{date}_"
        )
    return "\n".join(lines)


def fmt_alpaca_clock(raw: str, cmd: str, args: list) -> str:
    data = _parse_alpaca(raw)
    if not data:
        return _trunc(raw)
    if isinstance(data, list):
        data = data[0] if data else {}

    is_open   = data.get("is_open", False)
    now       = str(data.get("timestamp", ""))[:19].replace("T", " ")
    next_open = str(data.get("next_open", ""))[:16].replace("T", " ")
    next_close= str(data.get("next_close", ""))[:16].replace("T", " ")

    status = "🟢 **OPEN**" if is_open else "🔴 **CLOSED**"
    lines  = [f"**Market Clock** — {status}\n"]
    lines.append(f"Current time: `{now} ET`")
    if not is_open:
        lines.append(f"Next open:    `{next_open} ET`")
    else:
        lines.append(f"Closes at:    `{next_close} ET`")
    return "\n".join(lines)


def fmt_alpaca_bars(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    data   = _parse_alpaca(raw)
    if not data:
        return _trunc(raw)

    # Response may be {bars: {AAPL: [...]}} or a list directly
    bars = None
    if isinstance(data, dict):
        bars_map = data.get("bars", data)
        if isinstance(bars_map, dict):
            bars = list(bars_map.values())[0] if bars_map else []
        else:
            bars = bars_map
    elif isinstance(data, list):
        bars = data

    if not bars:
        return f"No bars found for {ticker}"

    lines = [f"**{ticker} Bars** (last {min(10, len(bars))})\n"]
    lines.append("`Date        Open      High      Low       Close     Volume`")
    for b in bars[-10:]:
        t = str(b.get("t") or b.get("timestamp", ""))[:10]
        o = f"{float(b.get('o') or b.get('open',0)):>8.2f}"
        h = f"{float(b.get('h') or b.get('high',0)):>8.2f}"
        l = f"{float(b.get('l') or b.get('low',0)):>8.2f}"
        c = f"{float(b.get('c') or b.get('close',0)):>8.2f}"
        v = b.get("v") or b.get("volume", 0)
        lines.append(f"`{t}  {o}  {h}  {l}  {c}  {int(v):>9,}`")
    return "\n".join(lines)


def fmt_alpaca_quote(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    data   = _parse_alpaca(raw)
    if not data:
        return _trunc(raw)

    # May be {quotes: {AAPL: {...}}} or direct
    q = None
    if isinstance(data, dict):
        quotes = data.get("quotes", data)
        if isinstance(quotes, dict):
            q = list(quotes.values())[0] if quotes else {}
        else:
            q = quotes
    elif isinstance(data, list):
        q = data[0] if data else {}
    else:
        q = {}

    bid  = q.get("bp") or q.get("bid_price", "N/A")
    ask  = q.get("ap") or q.get("ask_price", "N/A")
    bsz  = q.get("bs") or q.get("bid_size", "")
    asz  = q.get("as") or q.get("ask_size", "")
    ts   = str(q.get("t") or q.get("timestamp", ""))[:19].replace("T", " ")

    try:
        spread = f"${float(ask) - float(bid):.4f}" if bid != "N/A" and ask != "N/A" else "N/A"
    except Exception:
        spread = "N/A"

    return (
        f"**{ticker} Live Quote**\n"
        f"Bid: **${bid}** ({bsz} shares)\n"
        f"Ask: **${ask}** ({asz} shares)\n"
        f"Spread: {spread}  ·  `{ts} ET`"
    )


def fmt_alpaca_snapshot(raw: str, cmd: str, args: list) -> str:
    ticker = args[0].upper() if args else "?"
    data   = _parse_alpaca(raw)
    if not data:
        return _trunc(raw)

    # May be {snapshots: {AAPL: {...}}} or direct
    snap = None
    if isinstance(data, dict):
        snaps = data.get("snapshots", data)
        if isinstance(snaps, dict) and snaps:
            snap = list(snaps.values())[0]
        else:
            snap = data
    elif isinstance(data, list):
        snap = data[0] if data else {}
    else:
        snap = {}

    daily = snap.get("dailyBar") or snap.get("daily_bar") or {}
    quote = snap.get("latestQuote") or snap.get("latest_quote") or {}
    trade = snap.get("latestTrade") or snap.get("latest_trade") or {}
    prev  = snap.get("prevDailyBar") or snap.get("prev_daily_bar") or {}

    close  = daily.get("c") or daily.get("close")
    pclose = prev.get("c") or prev.get("close")
    hi     = daily.get("h") or daily.get("high")
    lo     = daily.get("l") or daily.get("low")
    vol    = daily.get("v") or daily.get("volume")
    last   = trade.get("p") or trade.get("price")
    bid    = quote.get("bp") or quote.get("bid_price")
    ask    = quote.get("ap") or quote.get("ask_price")

    chg_str = ""
    if close and pclose:
        try:
            chg = (float(close) - float(pclose)) / float(pclose) * 100
            arrow = "▲" if chg >= 0 else "▼"
            chg_str = f" {arrow}{abs(chg):.2f}%"
        except Exception:
            pass

    lines = [f"**{ticker} Snapshot**\n"]
    if last:   lines.append(f"Last Trade: **${last}**{chg_str}")
    if bid and ask: lines.append(f"Bid/Ask:    ${bid} / ${ask}")
    if hi and lo:   lines.append(f"Day Range:  ${lo} — ${hi}")
    if vol:         lines.append(f"Volume:     {int(float(vol)):,}")
    return "\n".join(lines)


def discord_to_telegram_html(text: str) -> str:
    """Convert Discord markdown to Telegram HTML parse mode."""
    # Code blocks first (before bold)
    text = re.sub(r'```\w*\n(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic (single *)
    text = re.sub(r'\*([^*\n]+)\*', r'<i>\1</i>', text)
    # Escape bare & that aren't part of HTML entities
    # (simple: replace & not followed by known entities)
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|#)', '&amp;', text)
    return text
