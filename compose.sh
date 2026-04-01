#!/usr/bin/env bash
# =============================================================================
# OpenTrader — Fix compose.yml + save progress snapshot
# Run as: sudo bash fix_compose_and_save.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
hdr()  { echo -e "\n${BLUE}══════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}══════════════════════════════════════${NC}"; }

BASE="/opt/opentrader"

# =============================================================================
# STEP 1 — Backup existing compose.yml
# =============================================================================
hdr "Backing up compose.yml"
cp $BASE/compose.yml $BASE/compose.yml.bak
log "Backed up → compose.yml.bak"

# =============================================================================
# STEP 2 — Rewrite compose.yml cleanly with all services including webui
# =============================================================================
hdr "Rewriting compose.yml"

cat > $BASE/compose.yml << 'COMPOSE'
name: opentrader

networks:
  trading-net:
    driver: bridge

volumes:
  redis-data:
  timescale-data:
  vault-data:
  prometheus-data:
  grafana-data:

services:

  # ── Infrastructure ──────────────────────────────────────────────────────────

  redis:
    image: docker.io/redis:7-alpine
    container_name: ot-redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    volumes:
      - redis-data:/data
    networks: [trading-net]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

  timescaledb:
    image: docker.io/timescale/timescaledb:latest-pg16
    container_name: ot-timescaledb
    restart: unless-stopped
    environment:
      POSTGRES_DB: trading
      POSTGRES_USER: trading
      POSTGRES_PASSWORD: ${DB_PASSWORD:-trading_secret}
    volumes:
      - timescale-data:/var/lib/postgresql/data
      - ./config/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    networks: [trading-net]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U trading"]
      interval: 15s
      timeout: 5s
      retries: 5

  vault:
    image: docker.io/hashicorp/vault:1.15
    container_name: ot-vault
    restart: unless-stopped
    cap_add: [IPC_LOCK]
    environment:
      VAULT_DEV_ROOT_TOKEN_ID: ${VAULT_TOKEN:-vault_dev_token}
      VAULT_DEV_LISTEN_ADDRESS: 0.0.0.0:8200
    volumes:
      - vault-data:/vault/data
    networks: [trading-net]

  prometheus:
    image: docker.io/prom/prometheus:latest
    container_name: ot-prometheus
    restart: unless-stopped
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    networks: [trading-net]

  grafana:
    image: docker.io/grafana/grafana:latest
    container_name: ot-grafana
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}
    volumes:
      - grafana-data:/var/lib/grafana
    ports:
      - "3000:3000"
    networks: [trading-net]
    depends_on: [prometheus]

  # ── Python agents ───────────────────────────────────────────────────────────

  orchestrator:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-orchestrator
    restart: unless-stopped
    command: python -m orchestrator.main
    environment:
      REDIS_URL: redis://ot-redis:6379
      DB_URL: postgresql://trading:${DB_PASSWORD:-trading_secret}@ot-timescaledb/trading
      SERVICE_NAME: orchestrator
      HEARTBEAT_INTERVAL_SEC: "30"
      HEARTBEAT_TTL_SEC: "90"
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy
      timescaledb:
        condition: service_healthy

  scheduler:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-scheduler
    restart: unless-stopped
    command: python -m scheduler.main
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: scheduler
      SCRAPE_INTERVAL_MINUTES: "3"
      TIMEZONE: America/New_York
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy

  predictor:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-predictor
    restart: unless-stopped
    command: python -m predictor.main
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: predictor
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy

  trader-equity:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-trader-equity
    restart: unless-stopped
    command: python -m traders.equity_trader
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: trader-equity
      ASSET_CLASS: equity
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy

  trader-options:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-trader-options
    restart: unless-stopped
    command: python -m traders.options_trader
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: trader-options
      ASSET_CLASS: options
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy

  scraper:
    build:
      context: ./python
      dockerfile: Dockerfile.scraper
    container_name: ot-scraper
    restart: unless-stopped
    command: python -m scraper.main
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: scraper
    env_file: [.env]
    volumes:
      - ./logs/scraper:/app/logs
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy

  review-agent:
    build:
      context: ./python
      dockerfile: Dockerfile
    container_name: ot-review-agent
    restart: unless-stopped
    command: python -m review.main
    environment:
      REDIS_URL: redis://ot-redis:6379
      DB_URL: postgresql://trading:${DB_PASSWORD:-trading_secret}@ot-timescaledb/trading
      SERVICE_NAME: review-agent
    env_file: [.env]
    volumes:
      - ./logs/python:/app/logs
      - ./config:/app/config:ro
      - ./secrets:/app/secrets:ro
    networks: [trading-net]
    depends_on:
      timescaledb:
        condition: service_healthy

  # ── Command Center WebUI ────────────────────────────────────────────────────

  webui:
    build:
      context: ./python
      dockerfile: Dockerfile.webui
    container_name: ot-webui
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      REDIS_URL: redis://ot-redis:6379
      SERVICE_NAME: webui
      WEBUI_TOKEN: ${WEBUI_TOKEN:-opentrader}
      TIMEZONE: America/New_York
    env_file: [.env]
    volumes:
      - /run/user/1000/podman/podman.sock:/var/run/docker.sock:ro
    networks: [trading-net]
    depends_on:
      redis:
        condition: service_healthy
COMPOSE

log "compose.yml rewritten with all services including webui"

# =============================================================================
# STEP 3 — Verify webui service is present
# =============================================================================
hdr "Verifying compose.yml"

if grep -q "ot-webui" $BASE/compose.yml; then
    log "webui service confirmed in compose.yml"
else
    echo -e "${RED}[x]${NC} webui missing — something went wrong"
    exit 1
fi

# List all services
echo ""
log "Services in compose.yml:"
grep "container_name:" $BASE/compose.yml | awk '{print "  "$2}'

# =============================================================================
# STEP 4 — Ensure Dockerfile.webui and requirements exist
# =============================================================================
hdr "Checking webui build files"

if [ ! -f "$BASE/python/Dockerfile.webui" ]; then
    warn "Dockerfile.webui missing — writing it now"
    cat > $BASE/python/Dockerfile.webui << 'DOCK'
FROM docker.io/python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.webui.txt .
RUN pip install --no-cache-dir -r requirements.webui.txt
COPY . .
CMD ["uvicorn", "webui.main:app", "--host", "0.0.0.0", "--port", "8080"]
DOCK
    log "Dockerfile.webui written"
else
    log "Dockerfile.webui exists"
fi

if [ ! -f "$BASE/python/requirements.webui.txt" ]; then
    warn "requirements.webui.txt missing — writing it now"
    cat > $BASE/python/requirements.webui.txt << 'REQS'
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
websockets>=12.0
redis>=5.0.0
pydantic>=2.5.0
structlog>=24.0.0
toml>=0.10.2
aiofiles>=23.2.1
apscheduler>=3.10.0
REQS
    log "requirements.webui.txt written"
else
    log "requirements.webui.txt exists"
fi

# Ensure webui module directory exists
mkdir -p $BASE/python/webui/static
[ -f "$BASE/python/webui/__init__.py" ] || touch $BASE/python/webui/__init__.py
log "webui module directory ready"

# =============================================================================
# STEP 5 — Fix podman socket path for container inspection
# =============================================================================
hdr "Checking Podman socket"

# Find the actual podman socket for the claude user
SOCK_PATH="/run/user/$(id -u claude)/podman/podman.sock"
if [ -S "$SOCK_PATH" ]; then
    log "Podman socket found: $SOCK_PATH"
    # Update compose.yml volume mount with correct UID
    CLAUDE_UID=$(id -u claude)
    sed -i "s|/run/user/1000/podman/podman.sock|/run/user/${CLAUDE_UID}/podman/podman.sock|g" $BASE/compose.yml
    log "Socket path updated in compose.yml (UID: $CLAUDE_UID)"
else
    warn "Podman socket not found at $SOCK_PATH"
    warn "Container stats will show '--' in the WebUI — not critical"
    # Remove the socket volume mount to avoid compose errors
    sed -i '/podman.sock/d' $BASE/compose.yml
    log "Socket mount removed from compose.yml"
fi

# =============================================================================
# STEP 6 — Set permissions
# =============================================================================
chown -R claude:claude $BASE
log "Permissions set"

# =============================================================================
# STEP 7 — Save project state snapshot
# =============================================================================
hdr "Saving project state snapshot"

cat > $BASE/PROJECT_STATE.md << 'SNAPSHOT'
# OpenTrader — Project State Snapshot
## Saved: End of Session — Phase 3 Complete

---

## Server
- Host:      hmsrvr-fang1
- OS:        Ubuntu 26 · kernel 7.0.0 · amd64
- Tailscale: 100.87.121.49
- Project:   /opt/opentrader
- User:      claude
- Runtime:   Podman 5.7 + podman-compose 1.5

---

## Completed Phases

### Phase 1 — Infrastructure ✅
- Redis 7 (streams, counter, pub/sub)
- TimescaleDB pg16 (trades, signals, sentiment, review_log, heartbeats)
- HashiCorp Vault (secrets)
- Prometheus + Grafana (port 3000)
- All volumes, networks, compose.yml

### Phase 2 — Orchestrator + Self-Healing ✅
- python/shared/envelope.py       — message schema (Envelope, HeartbeatPayload, SignalPayload, OrderEventPayload)
- python/shared/redis_client.py   — Redis factory, stream names, group names
- python/shared/base_agent.py     — base class all agents inherit (heartbeat, circuit check, publish)
- python/orchestrator/main.py     — 4 concurrent tasks: heartbeat publish, hb consumer, watchdog, commander
- python/orchestrator/watchdog.py — fault classifier (transient/degraded/fatal), circuit breaker, restart
- python/orchestrator/commander.py— system.commands stream listener (restart, circuit_break, reset, halt)

### Phase 3 — Scheduler ✅
- python/scheduler/calendar.py    — NYSE holiday calendar 2025-2026, market hours helpers
- python/scheduler/jobs.py        — job definitions (publish triggers to Redis)
- python/scheduler/main.py        — APScheduler runner, all jobs registered

#### Job Schedule
| Time / Interval | Job                              |
|-----------------|----------------------------------|
| 08:00 ET daily  | Morning summary + Telegram alert |
| 09:00 ET daily  | Pre-market scraper warmup        |
| 09:30 ET daily  | Market open signal               |
| Every 3 min     | OVTLYR + WSB scrape (mkt hours)  |
| Every 5 min     | Predictor signal run (mkt hours) |
| Every 30 sec    | Watchdog heartbeat check         |
| 16:00 ET daily  | Market close signal              |
| 16:05 ET daily  | EOD report trigger               |

### Phase 3b/3c — Command Center WebUI ✅ (compose fix applied tonight)
- python/webui/main.py            — FastAPI backend, full REST API + WebSocket
- python/webui/static/index.html  — Dark-themed SPA, 8 sections
- python/Dockerfile.webui         — Container image
- python/requirements.webui.txt   — Dependencies
- Port: 8080

#### WebUI Sections
| Section   | Capability                                              |
|-----------|---------------------------------------------------------|
| Overview  | Market clock, agent health, signal feed, stat cards     |
| Agents    | Per-agent CPU/mem/uptime, logs, restart button          |
| Streams   | Redis stream lengths, command history                   |
| Signals   | Full signal table (ticker, direction, confidence, etc.) |
| Trades    | P&L summary, fills table per account/strategy           |
| Scheduler | Full CRUD job manager (create/edit/delete/run)          |
| Logs      | Container log viewer with color-coded output            |
| System    | Circuit breaker, halt/resume, container table           |

### Tradier Multi-Account Layer ✅
- python/brokers/tradier/client.py    — HTTP client, rate limiter, retry
- python/brokers/tradier/accounts.py  — AccountManager, env var resolution
- python/brokers/tradier/orders.py    — place/cancel/modify equity + options
- python/brokers/tradier/positions.py — balances, positions, P&L
- python/brokers/tradier/market.py    — quotes, options chain, expirations
- python/brokers/tradier/gateway.py   — multi-account router, circuit breaker

### LLM + Notifications ✅
- python/llm/connector.py             — OpenRouter connector, model routing, fallback, retry
- python/notifier/agentmail.py        — AgentMail + Telegram + Discord multi-channel

---

## All Compose Services
| Container         | Port  | Status   |
|-------------------|-------|----------|
| ot-redis          | —     | ✅ built  |
| ot-timescaledb    | —     | ✅ built  |
| ot-vault          | 8200  | ✅ built  |
| ot-prometheus     | —     | ✅ built  |
| ot-grafana        | 3000  | ✅ built  |
| ot-orchestrator   | —     | ✅ built  |
| ot-scheduler      | —     | ✅ built  |
| ot-predictor      | —     | stub     |
| ot-trader-equity  | —     | stub     |
| ot-trader-options | —     | stub     |
| ot-scraper        | —     | stub     |
| ot-review-agent   | —     | stub     |
| ot-webui          | 8080  | ✅ fixed  |

---

## Key Config Files
| File                        | Purpose                           |
|-----------------------------|-----------------------------------|
| compose.yml                 | Full Podman Compose stack         |
| .env                        | API keys + passwords              |
| config/system.toml          | Scheduler, LLM, AgentMail config  |
| config/strategies.toml      | Per-strategy params + accounts    |
| config/accounts.toml        | Tradier multi-account registry    |
| config/init.sql             | TimescaleDB schema                |
| config/prometheus.yml       | Prometheus scrape config          |

---

## .env Keys Needed
- OPENROUTER_API_KEY
- AGENTMAIL_API_KEY
- REPORT_RECIPIENT_EMAIL
- WEBUI_TOKEN
- DB_PASSWORD / VAULT_TOKEN / GRAFANA_PASSWORD
- TRADIER_ACCESS_TOKEN
- TRADIER_SANDBOX_ACCOUNT_ID
- TRADIER_LIVE_ACCOUNT_1_ID / LABEL / ENABLED
- WEBULL_* credentials
- TELEGRAM_BOT_TOKEN / CHAT_ID
- DISCORD_WEBHOOK_URL / ALERTS / TRADES / EOD
- REDDIT_CLIENT_ID / SECRET
- OVTLYR_EMAIL / PASSWORD

---

## Pending Phases
| Phase | Component                        |
|-------|----------------------------------|
| 4     | OVTLYR Playwright scraper + WSB  |
| 5     | Predictor + trader hands         |
| 6     | Review agent + EOD report        |
| 7     | Live broker connections          |

---

## Shell Aliases (claude user)
    ot          cd /opt/opentrader
    otstart     podman-compose up -d
    otstop      podman-compose down
    otstatus    bash status.sh
    otlogs      podman-compose logs -f --tail=50
    otdeploy    bash deploy.sh
    otenv       nano .env

## Resume Command
To resume next session:
    su - claude
    cd /opt/opentrader
    podman-compose build webui
    podman-compose up -d
    bash status.sh
SNAPSHOT

chown claude:claude $BASE/PROJECT_STATE.md
log "PROJECT_STATE.md saved at /opt/opentrader/PROJECT_STATE.md"

# =============================================================================
# Done
# =============================================================================
hdr "All Done"
echo ""
log "compose.yml fixed — webui service added correctly"
log "PROJECT_STATE.md saved on server"
echo ""
warn "TO RESUME TOMORROW:"
warn "  su - claude"
warn "  cd /opt/opentrader"
warn "  podman-compose build webui"
warn "  podman-compose up -d"
warn "  bash status.sh"
warn "  Open: http://192.168.x.x:8080"
