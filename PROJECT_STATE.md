# OpenTrader — Project State

## Stack
- Runtime: Podman + podman-compose
- OS: Linux (Ubuntu 24+)
- Project path: `/opt/opentrader`

---

## Completed Components

### Infrastructure
- Redis 7 (streams, pub/sub, counters)
- TimescaleDB pg16 (trades, signals, sentiment, review_log, heartbeats, scheduler_jobs)
- HashiCorp Vault (secrets)
- Prometheus + Grafana (port 3000)

### Core Agents
- **Orchestrator** — heartbeat monitor, watchdog, circuit breaker, commander
- **Scheduler** — APScheduler, market-hours aware, DB-persisted job overrides
- **Predictor** — LLM signal generation via OpenRouter
- **Trader (Equity/Options)** — order routing via broker gateway
- **Scrapers** — OVTLYR, WSB, SeekAlpha, Yahoo Finance
- **Review Agent** — EOD trade review + recommendations
- **Broker Gateway** — multi-broker router (Tradier, Alpaca, Webull)

### WebUI (port 8080)
- FastAPI backend + WebSocket live updates
- Dark-themed SPA with 10 sections: Overview, Agents, Scheduler, Trades, Signals, Sentiment, Logs, System, Strategy Engineer, Brokers
- Broker config UI with auto-restart on credential save
- Strategy version control with snapshots and backtest storage

### MCP Servers
- `mcp-yahoo` — Yahoo Finance market data
- `mcp-alpaca` — Alpaca trading API
- `mcp-tradingview` — TradingView chart data

---

## Scheduler Jobs
| Time / Interval | Job |
|---|---|
| 08:00 ET daily | Morning summary + alert |
| 09:00 ET daily | Pre-market scraper warmup |
| 09:30 ET daily | Market open signal |
| Every N min | OVTLYR + sentiment scrape |
| Every 5 min | Predictor signal run |
| Every 30 sec | Watchdog heartbeat check |
| 16:00 ET daily | Market close signal |
| 16:05 ET daily | EOD report trigger |

---

## Key Config Files
| File | Purpose |
|---|---|
| `compose.yml` | Full Podman Compose stack |
| `.env` | API keys + passwords (gitignored — copy from `.env.sample`) |
| `config/system.toml` | Scheduler, LLM, AgentMail config |
| `config/strategies.toml` | Per-strategy params |
| `config/accounts.toml` | Broker account registry (uses `${ENV_VAR}` references) |
| `config/init.sql` | TimescaleDB schema |
| `config/prometheus.yml` | Prometheus scrape config |

---

## Getting Started
1. Copy `.env.sample` to `.env` and fill in your credentials
2. Review `config/accounts.toml` and set corresponding env vars
3. `podman-compose up -d`
4. Open `http://localhost:8080`
