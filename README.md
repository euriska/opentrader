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
- **TradingView Charts** — Embedded charts with EMA/SMA/BB/RSI/MACD overlays and live position picker
- **Market Breadth** — OVTLYR bull/bear breadth gauge with crossover detection and sparkline history
- **Strategy Engineer** — AI-assisted strategy builder with version control and real Backtrader backtesting
- **Backtrader Engine** — EMA 10/21 crossover strategy with stop-loss/take-profit, full trade log, PDF + CSV exports, and equity/chart tabs
- **Trade Directives** — Natural-language GTC directives evaluated every 5 minutes by an LLM agent and executed automatically
- **Market Intelligence** — Per-ticker intelligence pipeline: WSB sentiment, SeekingAlpha, Yahoo Finance news, analyst ratings, earnings proximity, and Unusual Whales options flow + dark pool data
- **Unusual Whales MCP** — Real-time options flow, dark pool prints, market tide, greek exposure, and short interest via MCP server
- **Scheduler** — Market-hours-aware job runner with DB-persisted configuration
- **MCP Agents** — Model Context Protocol servers for Yahoo Finance, Alpaca, TradingView, Massive, and Unusual Whales
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
│  intel pipeline    │  │  order executor    │  │  scheduler_jobs, breadth     │
└────────────────────┘  └────────────────────┘  └──────────────────────────────┘
      │
┌─────▼───────────────┐
│  Redis 7            │
│  Streams, pub/sub   │
│  job + intel cache  │
└─────────────────────┘

MCP Layer: Yahoo Finance · Alpaca · TradingView · Massive · Unusual Whales
```

---

## Services

| Container | Description | Port |
|---|---|---|
| `ot-webui` | Command Center dashboard | 8080 |
| `ot-orchestrator` | Watchdog + circuit breaker | — |
| `ot-scheduler` | APScheduler job runner | — |
| `ot-predictor` | LLM signal generator | — |
| `ot-trader-equity` | Equity order executor | — |
| `ot-trader-options` | Options order executor | — |
| `ot-broker-gateway` | Multi-broker router | — |
| `ot-directive-agent` | LLM-evaluated GTC trade directives | — |
| `ot-scraper-ovtlyr` | OVTLYR market scanner + breadth | — |
| `ot-scraper-wsb` | Reddit WSB sentiment | — |
| `ot-scraper-seekalpha` | SeekAlpha news scraper | — |
| `ot-scraper-yahoo` | Yahoo Finance OHLCV data | — |
| `ot-scraper-yahoo-sentiment` | Yahoo Finance sentiment | — |
| `ot-aggregator` | Signal aggregator + market intelligence | — |
| `ot-review-agent` | EOD trade review | — |
| `ot-chat-agent` | Telegram/Discord bot | — |
| `ot-mcp-yahoo` | Yahoo Finance MCP server | — |
| `ot-mcp-alpaca` | Alpaca MCP server | — |
| `ot-mcp-tradingview` | TradingView MCP server | — |
| `ot-mcp-massive` | Massive MCP server | — |
| `ot-mcp-unusualwhales` | Unusual Whales MCP server | — |
| `ot-redis` | Redis 7 | — |
| `ot-timescaledb` | TimescaleDB pg16 | — |
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
export OT_VERSION=3.5.19
podman pull ghcr.io/euriska/ot-webui:${OT_VERSION}
podman pull ghcr.io/euriska/ot-python:${OT_VERSION}
podman pull ghcr.io/euriska/ot-scraper:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-yahoo:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-massive:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-tradingview:${OT_VERSION}
podman pull ghcr.io/euriska/ot-mcp-unusualwhales:${OT_VERSION}

podman-compose up -d
```

---

## Releasing

Releases use semantic versioning (`MAJOR.MINOR.PATCH`). The `VERSION` file is the single source of truth.

```bash
./scripts/release.sh patch    # 3.5.1 → 3.5.2
./scripts/release.sh minor    # 3.5.1 → 3.6.0
./scripts/release.sh major    # 3.5.1 → 4.0.0
./scripts/release.sh 3.7.0    # explicit version
```

The script bumps `VERSION`, opens `CHANGELOG.md` for release notes, commits, tags, and pushes. GitHub Actions then creates the GitHub Release and builds all container images to `ghcr.io/euriska/`.

---

## Configuration

### `.env` — Required keys

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | LLM provider — get at openrouter.ai |
| `WEBUI_TOKEN` | Dashboard auth token (any string) |
| `DB_PASSWORD` | TimescaleDB password |
| `TRADIER_SANDBOX_API_KEY` | Tradier paper trading key |
| `TRADIER_PRODUCTION_API_KEY` | Tradier live trading key |
| `ALPACA_API_KEY` | Alpaca paper API key |
| `ALPACA_API_SECRET` | Alpaca paper API secret |
| `ALPACA_LIVE_API_KEY` | Alpaca live API key |
| `ALPACA_LIVE_API_SECRET` | Alpaca live API secret |
| `WEBULL_API_KEY` | Webull API key |
| `WEBULL_SECRET_KEY` | Webull secret key |
| `UNUSUAL_WHALES_API_KEY` | Unusual Whales API key (options flow + dark pool) |
| `MASSIVE_API_KEY` | Massive AI API key (optional) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) |
| `DISCORD_WEBHOOK_URL` | Discord webhook (optional) |
| `AGENTMAIL_API_KEY` | AgentMail key for email reports (optional) |
| `OVTLYR_EMAIL` / `OVTLYR_PASSWORD` | OVTLYR credentials (optional) |

See `.env.sample` for the full list.

### Broker accounts — `config/accounts.toml`

Copy from `config/accounts.toml.sample`. All account IDs reference `${ENV_VAR}` so no credentials are stored in the file itself.

---

## WebUI Sections

| Section | Description |
|---|---|
| Overview | Market clock, agent health, Fear & Greed, live signal feed |
| Agents | Per-container CPU/mem/uptime, log viewer, restart |
| Scheduler | Job manager — create, edit, enable/disable, run now |
| Trades | P&L summary and fill history per account |
| Active Positions | Live open positions across all broker accounts |
| Trade Directives | Natural-language GTC directives with LLM evaluation and order execution |
| Charts | TradingView charts with indicator overlays and position picker |
| Signals | Signal table with ticker, direction, confidence |
| Sentiment | WSB/news sentiment scores and Fear & Greed trend |
| Logs | Live container log viewer |
| System | Circuit breaker, halt/resume, container table, topology diagram |
| Strategy Engineer | AI-assisted strategy builder with version history and Backtrader backtesting |
| Brokers | Broker credential configuration and account management |
| Risk Controls | Slippage %, minimum volume, and position size limits |

---

## Backtesting

The Strategy Engineer includes a real **Backtrader** backtesting engine:

- **EMA 10/21 crossover** strategy with configurable stop-loss and take-profit
- **Benchmark ticker** saved per strategy (default: SPY) — no prompts
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
| yfinance | Analyst ratings, price targets, dividend yield, earnings date |
| Unusual Whales | Options flow (bullish/bearish counts, net premium), dark pool prints |

Intelligence is cached in Redis (`aggregator:intel:{ticker}`) and used to adjust predictor confidence by up to ±0.20.

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
