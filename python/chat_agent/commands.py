"""
Command-based interface: !command [args]

Maps friendly command names to MCP tool calls.
To add commands for a new MCP server, add entries to COMMAND_MAP below.

Format: "cmd": (tool_name, arg_builder_fn, formatter_fn)
  - tool_name:    must match the MCP tool name exactly
  - arg_builder:  lambda(args: list[str]) -> dict  (arguments for the tool)
  - formatter:    fn(raw: str, cmd: str, args: list) -> str
"""
import structlog
from .mcp_registry import MCPRegistry
from .formatter import (
    fmt_quote, fmt_news, fmt_trending, fmt_history,
    fmt_options_expiry, fmt_upgrades, fmt_financials, fmt_holders, fmt_generic,
    fmt_alpaca_account, fmt_alpaca_positions, fmt_alpaca_orders,
    fmt_alpaca_clock, fmt_alpaca_bars, fmt_alpaca_quote, fmt_alpaca_snapshot,
)

log = structlog.get_logger("chat-agent.commands")

# ── Command registry ──────────────────────────────────────────────────────────
# Add new MCP server commands here.
# Each entry: command_alias -> (tool_name, arg_builder, formatter)

COMMAND_MAP: dict[str, tuple] = {
    # ── Yahoo Finance ─────────────────────────────────────────────────────────
    "quote":      ("get_stock_info",
                   lambda a: {"ticker": a[0].upper()},
                   fmt_quote),
    "q":          ("get_stock_info",
                   lambda a: {"ticker": a[0].upper()},
                   fmt_quote),
    "news":       ("get_yahoo_finance_news",
                   lambda a: {"ticker": a[0].upper()},
                   fmt_news),
    "trending":   ("get_trending_tickers",
                   lambda a: {"count": int(a[0]) if a else 15},
                   fmt_trending),
    "history":    ("get_historical_stock_prices",
                   lambda a: {"ticker": a[0].upper(), "period": a[1] if len(a) > 1 else "1mo",
                              "interval": a[2] if len(a) > 2 else "1d"},
                   fmt_history),
    "options":    ("get_option_expiration_dates",
                   lambda a: {"ticker": a[0].upper()},
                   fmt_options_expiry),
    "chain":      ("get_option_chain",
                   lambda a: {"ticker": a[0].upper(),
                              "expiration_date": a[1],
                              "option_type": a[2] if len(a) > 2 else "calls"},
                   fmt_generic),
    "financials": ("get_financial_statement",
                   lambda a: {"ticker": a[0].upper(),
                              "financial_type": a[1] if len(a) > 1 else "income_stmt"},
                   fmt_financials),
    "holders":    ("get_holder_info",
                   lambda a: {"ticker": a[0].upper(),
                              "holder_type": a[1] if len(a) > 1 else "institutional_holders"},
                   fmt_holders),
    "upgrades":   ("get_recommendations",
                   lambda a: {"ticker": a[0].upper(),
                              "recommendation_type": "upgrades_downgrades"},
                   fmt_upgrades),
    "recommend":  ("get_recommendations",
                   lambda a: {"ticker": a[0].upper(),
                              "recommendation_type": "recommendations"},
                   fmt_generic),
    # ── Alpaca Trading ────────────────────────────────────────────────────────
    "account":    ("get_account_info",
                   lambda a: {},
                   fmt_alpaca_account),
    "positions":  ("get_all_positions",
                   lambda a: {},
                   fmt_alpaca_positions),
    "orders":     ("get_orders",
                   lambda a: {"status": a[0] if a else "open"},
                   fmt_alpaca_orders),
    "clock":      ("get_clock",
                   lambda a: {},
                   fmt_alpaca_clock),
    "movers":     ("get_market_movers",
                   lambda a: {"market_type": a[0] if a else "stocks"},
                   fmt_generic),
    "active":     ("get_most_active_stocks",
                   lambda a: {},
                   fmt_generic),
    "bars":       ("get_stock_bars",
                   lambda a: {"symbols": a[0].upper(),
                              "timeframe": a[1] if len(a) > 1 else "1Day",
                              "limit": int(a[2]) if len(a) > 2 else 10},
                   fmt_alpaca_bars),
    "lquote":     ("get_stock_latest_quote",
                   lambda a: {"symbols": a[0].upper()},
                   fmt_alpaca_quote),
    "snapshot":   ("get_stock_snapshot",
                   lambda a: {"symbols": a[0].upper()},
                   fmt_alpaca_snapshot),
    # ── Add more MCP server commands below ───────────────────────────────────
    # "mycommand": ("tool_name", lambda a: {...}, fmt_generic),
}

# Commands that don't need a ticker argument
NO_ARGS_REQUIRED = {"trending"}

HELP_TEXT = """\
**OpenTrader Finance Bot**

**📊 Market Data (Yahoo Finance)**
`!quote <ticker>` — Price, stats & company overview
`!news <ticker>` — Latest news headlines
`!trending [n]` — Most active tickers
`!history <ticker> [period]` — Price history *(1d/5d/1mo/3mo/1y)*
`!options <ticker>` — Options expiration dates
`!chain <ticker> <date> [calls|puts]` — Options chain
`!financials <ticker> [type]` — Financial statements *(income_stmt/balance_sheet/cashflow)*
`!holders <ticker> [type]` — Holder info *(institutional/major/insider)*
`!upgrades <ticker>` — Analyst upgrades & downgrades
`!recommend <ticker>` — Analyst recommendations

**💼 Alpaca Account & Trading**
`!account` — Account balances & buying power
`!positions` — Open positions with P&L
`!orders [open|closed|all]` — Recent orders
`!clock` — Market hours & next open/close
`!bars <ticker> [timeframe] [limit]` — OHLCV bars *(1Min/5Min/1Hour/1Day)*
`!lquote <ticker>` — Live bid/ask quote from broker
`!snapshot <ticker>` — Full market snapshot
`!movers [stocks|crypto]` — Top market movers
`!active` — Most active stocks

**🤖 AI Mode**
Mention me or DM with any natural language question.
*Examples:* `@bot What's my current P&L?` · `@bot Any bullish upgrades on NVDA?`
"""


async def handle_command(text: str, registry: MCPRegistry) -> str:
    """Parse and execute a !command. Returns formatted response string."""
    parts = text.strip().lstrip("!/").split()
    if not parts:
        return HELP_TEXT

    cmd  = parts[0].lower()
    args = parts[1:]

    if cmd in ("help", "h", "?", "start"):
        return HELP_TEXT

    if cmd not in COMMAND_MAP:
        close = [k for k in COMMAND_MAP if k.startswith(cmd[:2])]
        hint  = f"  Did you mean: {', '.join(f'`!{c}`' for c in close[:3])}?" if close else ""
        return f"Unknown command `!{cmd}`.{hint}\nTry `!help` for all commands."

    tool_name, arg_builder, formatter = COMMAND_MAP[cmd]

    if not args and cmd not in NO_ARGS_REQUIRED:
        return f"Usage: `!{cmd} <ticker>`"

    try:
        arguments = arg_builder(args)
    except (IndexError, ValueError) as e:
        return f"Invalid arguments for `!{cmd}`: {e}"

    log.info("commands.executing", cmd=cmd, tool=tool_name, args=arguments)
    raw = await registry.call_tool(tool_name, arguments)
    return formatter(raw, cmd, args)


def is_command(text: str) -> bool:
    return bool(text.strip()) and (text.strip()[0] in ("!", "/"))
