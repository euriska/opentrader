# OpenTrader

An AI-driven algorithmic trading platform built on a microservices architecture using Podman, Redis, and TimescaleDB. Supports multiple brokers (Tradier, Alpaca, Webull) with a real-time web dashboard, LLM-powered signals, automated trade execution, and real backtesting.

![Dashboard](artwork/opentrader-dashboard.png)

[![Release](https://img.shields.io/github/v/release/euriska/opentrader)](https://github.com/euriska/opentrader/releases)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## Features

- **Multi-broker support** — Tradier, Alpaca, and Webull (paper + live accounts)
- **AI-powered signals** — LLM predictor via OpenRouter (Claude, GPT-4o, and more)
- **Real-time WebUI** — Dark-themed SPA dashboard with live WebSocket updates
- **TradingView Charts** — Embedded charts with EMA/SMA/BB/RSI/MACD overlays, live position picker, and per-ticker sentiment sub-panel (F&G score, component breakdown, 30-day trend sparkline)
- **Market Breadth** — OVTLYR bull/bear breadth gauge with crossover detection and sparkline history
- **Equity / Options separation** — Active Positions, Trades, and Dividends pages show equity-only data; Options Dashboard is a dedicated section
- **Options Dashboard** — Live options position tracker with DTE, strike, delta, ATR levels, underlying price, buy/sell signal, Yahoo Finance chain enrichment; Portfolio Greeks panel (Δ/Θ/ν/Γ per underlying); YTD Performance panel (trades, P&L, win rate, alpha vs SPY); stat card hover tooltips; download and scheduled email report
- **Options Trader** — Full-featured options trading dashboard: account selector, open positions panel (with expiration date + DTE), OVTLYR buy-signal list, LightweightCharts candlestick chart with EMA 10/20/50 + earnings/ex-dividend markers, broker-native options chain (Tradier → Webull → Alpaca → Yahoo fallback) with extrinsic value, IV, greeks, blue position highlighting; multi-leg order builder with BUY/SELL chips on strike cells; Risk & Sizing Calculator with per-account default risk % (configurable in Broker dashboard) and deviation warning
- **Options Trading Log** — Full P&L history as broker → account → ticker tree; milestone chains (Open → Roll → Closed/Expired); per-event P&L; post-close AI analysis via Claude Haiku; YTD performance panel; 18-month retention
- **Options Expiry Calendar** — Active positions grouped by expiration date with DTE urgency color coding (critical ≤3d, warning ≤7d, caution ≤14d); per-expiry Greeks totals
- **Strategy Engineer** — AI-assisted strategy builder with version control and real Backtrader backtesting
- **Backtrader Engine** — EMA 10/21 crossover strategy with stop-loss/take-profit, full trade log, PDF + CSV exports, and equity/chart tabs
- **Trade Directives** — Natural-language GTC directives evaluated every 5 minutes by an LLM agent and executed automatically
- **Market Intelligence** — Per-ticker intelligence pipeline: WSB sentiment, SeekingAlpha, Yahoo Finance news, analyst ratings, earnings proximity, and Unusual Whales options flow + dark pool data
- **Unusual Whales MCP** — Real-time options flow, dark pool prints, market tide, greek exposure, and short interest via MCP server
- **Portfolio NAV History** — 90-day equity curve on Trading Dashboard from daily broker snapshots; drawdown tracking
- **Daily P&L / Loss Limit** — Trading Dashboard widget with color-coded budget bar and circuit breaker banner
- **Scheduler** — Market-hours-aware job runner with DB-persisted configuration and per-job execution history (last run, status chip, error, run count)
- **MCP Agents** — Model Context Protocol servers for Yahoo Finance, Alpaca, TradingView, Webull, and Unusual Whales
- **Equity Dividend Income** — Full dividend tracking dashboard with per-broker filtering throughout:
  - **Income projection** from actual payment history — no synthetic rates; forward rate = recent payment × annual frequency from DB records
  - **Rolling 12-month bar chart** blending actual received (green) with projected remaining (blue); future bars from history-based monthly avg
  - **Upcoming ex-dividend panel** (7-day) — three-tier data: Massive.com API → dividendchannel.com projection → DB cache; per-broker qty and estimated total
  - **Received history** table and chart (month / ticker / account grouping)
  - **Per-broker filter** — clicking any broker card filters bar chart, holdings, upcoming events, and history simultaneously
  - **Diagnostics panel** — expandable per-account breakdown showing payment count, total received, monthly avg, and forward rate
- **Library** — Trading book library with ISBN lookup, cover art, ratings, and reader rank achievement system
- **Notifications** — Telegram, Discord, and AgentMail alerts
- **EOD Review** — Automated end-of-day trade analysis and recommendations
- **Self-healing** — Orchestrator watchdog with circuit breaker and auto-restart

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     WebUI (port 8080)                       │
│           FastAPI + WebSocket + Static SPA                  │
└─────────────────────┬───────────────────────────────────────┘
                      │ Redis Streams / Pub-Sub
      ┌───────────────┼───────────────────────┐
      │               │                       │
┌─────▼──────┐  ┌─────▼───────┐  ┌───────────▼──────────┐
│ Scheduler  │  │Orchestrator │  │   Broker Gateway     │
│ APScheduler│  │ Watchdog    │  │ Tradier/Alpaca/Webull │
│ + DB jobs  │  │ Circuit Bkr │  │ connectors           │
└────────────┘  └─────────────┘  └──────────────────────┘
      │
┌─────▼──────────────────────────────────────────────────┐
│  Agents: Predictor · Traders · Scrapers · Review        │
└────────────────────────────────────────────────────────┘
      │
┌─────▼──────────────┐  ┌────────────────────┐  ┌──────────────────────────────┐
│  Aggregator        │  │  Directive Agent   │  │  TimescaleDB (pg16)          │
│  Sentiment + UW    │  │  LLM GTC evaluator │  │  trades, signals, sentiment, │
│  intel pipeline    │  │  order executor    │  │  scheduler_jobs, dividends   │
└────────────────────┘  └────────────────────┘  └──────────────────────────────┘
      │
┌─────▼───────────────┐
│  Redis 7            │
│  Streams, pub/sub   │
│  job + intel cache  │
└─────────────────────┘

MCP Layer: Yahoo Finance · Alpaca · TradingView · Unusual Whales · Massive.com
```

---

## Services

| Container | Description | Port |
|---|---|---|
| `ot-webui` | Command Center dashboard | 8080 |
| `ot-scheduler` | APScheduler job runner | — |
| `ot-broker-gateway` | Multi-broker position/order router | — |
| `ot-directive-agent` | LLM-evaluated GTC trade directives | — |
| `ot-trader-equity` | Equity order executor | — |
| `ot-trader-options` | Options order executor | — |
| `ot-options-monitor` | Options position tracker + ATR level manager | — |
| `ot-chat-agent` | AI chat with MCP tool access | — |
| `ot-review-agent` | EOD trade review | — |
| `ot-sentiment-agent` | WSB/SeekingAlpha/Yahoo sentiment aggregator | — |
| `ot-dividend-agent` | Dividend tracking and DB population | — |
| `ot-ovtlyr-agent` | OVTLYR market intelligence integration | — |
| `ot-strategy-engine` | Strategy execution engine | — |
| `ot-mcp-yahoo` | Yahoo Finance MCP server | — |
| `ot-mcp-alpaca` | Alpaca MCP server | — |
| `ot-mcp-tradingview` | TradingView MCP server | — |
| `ot-mcp-unusualwhales` | Unusual Whales MCP server | — |
| `ot-mcp-massive` | Massive.com MCP server | — |
| `ot-redis` | Redis 7 | — |
| `ot-timescaledb` | TimescaleDB (PostgreSQL) | — |
| `ot-vault` | HashiCorp Vault (secrets) | — |
| `ot-prometheus` | Metrics collection | — |
| `ot-grafana` | Metrics dashboard | 3000 |

---

## Quick Start

### Prerequisites
- Podman 4.0+ and podman-compose 1.0+
- Linux (tested on Ubuntu 24+)

### Install from source

```bash
git clone https://github.com/euriska/opentrader.git
cd opentrader
git submodule update --init --recursive

# Configure credentials
cp .env.sample .env
nano .env  # fill in your API keys

# Configure broker accounts
cp config/accounts.toml.sample config/accounts.toml
# accounts.toml uses ${ENV_VAR} references — set vars in .env

# Build and start
podman-compose up -d

# Open dashboard
open http://localhost:8080
```

### Install from pre-built images

Pre-built container images are published to GitHub Container Registry on every release.

```bash
git clone https://github.com/euriska/opentrader.git
cd opentrader
git submodule update --init --recursive

cp .env.sample .env && nano .env
cp config/accounts.toml.sample config/accounts.toml

# Pull images (replace X.Y.Z with the release version)
export OT_VERSION=3.6.33
podman pull ghcr.io/euriska/ot-webui:${OT_VERSION}
podman pull ghcr.io/euriska/ot-python:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-yahoo:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-tradingview:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-unusualwhales:${OT_VERSION}

podman-compose up -d
```

---

## Releasing

Releases use semantic versioning (`MAJOR.MINOR.PATCH`). Patch resets at 99 (e.g. `3.5.99 → 3.6.0`). The `VERSION` file is the single source of truth.

```bash
echo "X.Y.Z" > VERSION
# Edit CHANGELOG.md with release notes
git add VERSION CHANGELOG.md <changed-files>
git commit --no-verify -m "feat/fix: description vX.Y.Z"
git push
gh release create vX.Y.Z --title "vX.Y.Z" --notes "Release notes here"
```

---

## Configuration

### `.env` — Required keys

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | LLM provider — get at openrouter.ai |
| `WEBUI_TOKEN` | Dashboard auth token (any string) |
| `DB_PASSWORD` | TimescaleDB password |
| `MASSIVE_API_KEY` | Massive.com API key (dividend data, market bars, ticker reference) |
| `TRADIER_SANDBOX_API_KEY` | Tradier paper trading key |
| `TRADIER_PRODUCTION_API_KEY` | Tradier live trading key |
| `ALPACA_API_KEY` | Alpaca paper API key |
| `ALPACA_API_SECRET` | Alpaca paper API secret |
| `ALPACA_LIVE_API_KEY` | Alpaca live API key |
| `ALPACA_LIVE_API_SECRET` | Alpaca live API secret |
| `WEBULL_API_KEY` | Webull API key |
| `WEBULL_SECRET_KEY` | Webull secret key |
| `UNUSUAL_WHALES_API_KEY` | Unusual Whales API key (options flow + dark pool) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) |
| `DISCORD_WEBHOOK_URL` | Discord webhook (optional) |
| `AGENTMAIL_API_KEY` | AgentMail key for email reports (optional) |
| `OVTLYR_EMAIL` / `OVTLYR_PASSWORD` | OVTLYR credentials (optional) |

See `.env.sample` for the full list.

### Broker accounts — `config/accounts.toml`

Copy from `config/accounts.toml.sample`. All account IDs reference `${ENV_VAR}` so no credentials are stored in the file itself.

---

## WebUI Navigation

The dashboard is organized into six sections:

### Trading
| Page | Description |
|---|---|
| Trading Dashboard | Live stat cards (equity/options split), market breadth, NAV history, daily P&L |
| Trade Directives | Natural-language GTC directives with LLM evaluation and order execution |
| Charts | TradingView charts with indicator overlays, position picker, and sentiment sub-panel |
| Broker | Broker credential configuration, account management, per-account risk % defaults |

### Equities
| Page | Description |
|---|---|
| Equity Trades | Equity fills and open orders grouped by week, with per-account tally and friendly reject reasons |
| Equity Dashboard | Live equity positions across all broker accounts with heatmaps, P&L, and liquidate action |
| Equity Dividend Income | Dividend tracking with per-broker filtering, actual-vs-projected bar chart, upcoming events, history |

### Options
| Page | Description |
|---|---|
| Options Dashboard | Live options positions with DTE, strike, delta, ATR levels, Yahoo chain enrichment, Portfolio Greeks, Expiry Calendar, and YTD Performance |
| Options Trader | Full trading dashboard — account selector, positions panel, OVTLYR signals, EMA chart, live broker chain, multi-leg order builder, risk calculator |
| Options Trading Log | Full P&L tree (broker → account → ticker) with milestone chains, AI post-close analysis, and YTD performance |

### Trading Plan
| Page | Description |
|---|---|
| Strategy Engineer | AI-assisted strategy builder with version history and Backtrader backtesting |
| Strategy Library | All saved strategy versions with backtest results |
| Strategy Assignment | Assign strategies to tickers for live execution |

### Resources
| Page | Description |
|---|---|
| Library | Trading book library with ISBN lookup, cover art, star ratings, and reader rank achievement system |

### Platform
| Page | Description |
|---|---|
| Platform Dashboard | Agent health, topology diagram, job error counts |
| Agents | Per-container status, log viewer, and health indicators |
| Configuration | Connector credentials, sector/stock exclusions, risk controls |
| Logs | Live container log viewer |
| Scheduler | Job manager — create, edit, enable/disable, run now; per-job execution history with last run, status chip, and run count |
| System | Circuit breaker, halt/resume, container table |
| User Configuration | API key management (22 keys), clock color + 12hr/24hr, sector/stock exclusions, risk controls |

---

## Backtesting

The Strategy Engineer includes a real **Backtrader** backtesting engine:

- **EMA 10/21 crossover** strategy with configurable stop-loss and take-profit
- **Benchmark ticker** saved per strategy (default: SPY)
- **Full trade log** — entry/exit dates, prices, qty, P&L, exit reason
- **Exports** — PDF and CSV trade reports available from the Trades tab
- **Charts** — price + EMA lines with trade markers, volume panel, equity curve
- **Version-linked** — each strategy version stores its own backtest results for comparison

---

## Market Intelligence Pipeline

The aggregator enriches each candidate ticker with data from multiple sources before the predictor scores it:

| Source | Data |
|---|---|
| WSB scraper | Mention count, sentiment score, top headlines |
| SeekingAlpha scraper | Professional analysis sentiment |
| Yahoo Finance news | Broad market sentiment |
| Unusual Whales | Options flow (bullish/bearish counts, net premium), dark pool prints |
| Massive.com | Dividend history, ex-dates, reference data |

Intelligence is cached in Redis (`aggregator:intel:{ticker}`) and used to adjust predictor confidence by up to ±0.20.

---

## Dividend Data Sources

| Purpose | Source | Notes |
|---|---|---|
| Income projection | `dividend_history` DB table | Actual payments backfilled from broker history; `forward_annual_rate = recent_aps × annual_count` |
| Ex/pay dates, frequency | Massive.com API (primary) | `api.massive.com/stocks/v1/dividends`, MASSIVE_API_KEY |
| Ex/pay dates fallback | dividend.com scrape | Usually Cloudflare-blocked; falls through gracefully |
| Last resort metadata | yfinance | Only when both above return nothing |
| Upcoming events | Massive.com → dividendchannel.com → DB | Three-tier for 7-day forward calendar |

---

## Supported Brokers

| Broker | Paper | Live | Notes |
|---|---|---|---|
| Tradier | ✅ | ✅ | Equities + options |
| Alpaca | ✅ | ✅ | Equities, crypto |
| Webull | ✅ | ✅ | Equities, options |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
