"""
OpenTrader Command Center — FastAPI backend
Central station for all agent containers.
Sections: Overview · Agents · Scheduler · Trades · Signals · Sentiment · Logs · System
"""
import asyncio
import http.client
import json
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shared.redis_client import get_redis, STREAMS
from scheduler.calendar import (
    is_market_open, is_trading_day, is_active_session,
    minutes_to_open, minutes_to_close, now_et,
)

log = structlog.get_logger("command-center")
TZ  = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

app = FastAPI(title="OpenTrader Command Center", version="2.0.0")
app.mount("/static", StaticFiles(directory="/app/webui/static"), name="static")

WEBUI_TOKEN    = os.getenv("WEBUI_TOKEN", "opentrader")
JOB_KEY_PREFIX = "scheduler:job:"
JOB_INDEX_KEY  = "scheduler:jobs"
ENV_PATH        = os.getenv("ENV_FILE_PATH", "/app/.env")
ACCOUNTS_CONFIG = "/app/config/accounts.toml"
DB_URL                 = os.getenv("DB_URL", "")
STRATEGIES_CONFIG_PATH = "/app/config/strategies.json"
STRATEGY_VERSIONS_DIR  = "/app/config/strategy_versions"
os.makedirs(STRATEGY_VERSIONS_DIR, exist_ok=True)


# ── .env read / write helpers ────────────────────────────────────────────────

def _read_env_file() -> dict:
    result = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    result[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return result


def _write_env_file(updates: dict):
    """Update specific keys in .env, preserving comments and order. Removes duplicates."""
    lines = []
    try:
        with open(ENV_PATH) as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass

    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                if key not in written:
                    new_lines.append(f"{key}={updates[key]}\n")
                    written.add(key)
                # drop duplicate occurrences
                continue
        new_lines.append(line)

    # Append any new keys not already in file
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)

KNOWN_AGENTS = [
    "orchestrator", "scheduler", "predictor",
    "trader-equity", "trader-options",
    "scraper-ovtlyr", "scraper-wsb", "scraper-seekalpha", "scraper-yahoo",
    "aggregator", "review-agent", "broker-gateway",
    # MCP servers & chat agent — health derived from Podman (no heartbeat)
    "mcp-yahoo", "mcp-alpaca", "mcp-tradingview", "chat-agent",
]

# Containers that don't publish heartbeats — health is read from Podman status
PODMAN_HEALTH_ONLY = {"mcp-yahoo", "mcp-alpaca", "mcp-tradingview", "chat-agent"}

CONTAINER_MAP = {
    "orchestrator":    "ot-orchestrator",
    "scheduler":       "ot-scheduler",
    "predictor":       "ot-predictor",
    "trader-equity":   "ot-trader-equity",
    "trader-options":  "ot-trader-options",
    "scraper-ovtlyr":  "ot-scraper-ovtlyr",
    "scraper-wsb":     "ot-scraper-wsb",
    "scraper-seekalpha":"ot-scraper-seekalpha",
    "scraper-yahoo":   "ot-scraper-yahoo",
    "aggregator":      "ot-aggregator",
    "review-agent":    "ot-review-agent",
    "broker-gateway":  "ot-broker-gateway",
    "mcp-yahoo":        "ot-mcp-yahoo",
    "mcp-alpaca":       "ot-mcp-alpaca",
    "mcp-tradingview":  "ot-mcp-tradingview",
    "chat-agent":      "ot-chat-agent",
    "redis":           "ot-redis",
    "timescaledb":     "ot-timescaledb",
    "grafana":         "ot-grafana",
    "webui":           "ot-webui",
}


def check_token(token: str):
    if token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Helpers ──────────────────────────────────────────────────────────────────

def ts_to_age(ts_ms: int) -> int:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return round((now_ms - ts_ms) / 1000)


async def get_agent_states(redis) -> dict:
    hb_raw = await redis.xrevrange(STREAMS["heartbeat"], "+", "-", count=50)
    agents = {}
    for _, fields in hb_raw:
        svc = fields.get("sender", "")
        if svc and svc not in agents:
            ts  = int(fields.get("ts_utc", 0))
            age = ts_to_age(ts)
            # uptime_s is nested inside payload.payload (Envelope format)
            try:
                inner = json.loads(fields.get("payload", "{}"))
                hb    = inner.get("payload", inner)
            except Exception:
                hb = {}
            agents[svc] = {
                "name":          svc,
                "last_seen_sec": age,
                "status":        hb.get("status", fields.get("status", "unknown")),
                "health":        "healthy" if age < 90 else ("degraded" if age < 180 else "dead"),
                "uptime_s":      hb.get("uptime_s") or fields.get("uptime_s"),
                "pid":           hb.get("pid") or fields.get("pid"),
                "container":     CONTAINER_MAP.get(svc, f"ot-{svc}"),
            }
    # Fill in missing agents as unknown
    for name in KNOWN_AGENTS:
        if name not in agents:
            agents[name] = {
                "name": name, "last_seen_sec": 9999,
                "status": "unknown", "health": "dead",
                "container": CONTAINER_MAP.get(name, f"ot-{name}"),
            }

    # For Podman-health-only services, override health from container state
    ps = {c["name"]: c for c in podman_ps()}
    for name in PODMAN_HEALTH_ONLY:
        cname  = CONTAINER_MAP.get(name, f"ot-{name}")
        cstate = ps.get(cname, {}).get("status", "")
        is_up  = "running" in cstate.lower() or "up" in cstate.lower()
        agents[name]["health"]        = "healthy" if is_up else "dead"
        agents[name]["status"]        = "running" if is_up else "stopped"
        agents[name]["last_seen_sec"] = 0 if is_up else 9999

    return agents


PODMAN_SOCK = "/var/run/podman.sock"


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects via a Unix domain socket."""
    def __init__(self, sock_path: str):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._sock_path)
        self.sock = s


def _podman_api(path: str, timeout: int = 5, raw: bool = False):
    """Call the Podman REST API over the Unix socket. Returns parsed JSON or None."""
    try:
        conn = _UnixSocketHTTPConnection(PODMAN_SOCK)
        conn.timeout = timeout
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if raw:
            return body
        return json.loads(body.decode())
    except Exception as e:
        log.warning("podman_api.failed", path=path, error=str(e))
        return None


def _parse_docker_log_stream(data: bytes) -> list[str]:
    """Parse Docker multiplexed log stream into lines."""
    lines = []
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset + 4:offset + 8], "big")
        if size == 0:
            offset += 8
            continue
        payload = data[offset + 8: offset + 8 + size]
        lines.append(payload.decode("utf-8", errors="replace").rstrip("\n"))
        offset += 8 + size
    # Fallback: if nothing parsed, try plain text
    if not lines and data:
        lines = data.decode("utf-8", errors="replace").splitlines()
    return lines


def podman_ps() -> list[dict]:
    """Get live container states from podman REST API."""
    data = _podman_api("/v4.0.0/libpod/containers/json?all=true")
    if not data:
        return []
    out = []
    for c in data:
        names = c.get("Names") or []
        name = names[0].lstrip("/") if names else c.get("Id", "")[:12]
        out.append({
            "name":       name,
            "status":     c.get("State", "unknown"),
            "image":      c.get("Image", ""),
            "created":    str(c.get("Created", "")),
            "started_at": c.get("StartedAt", 0),
        })
    return out


def podman_stats() -> list[dict]:
    """Get CPU/mem usage for running containers via podman REST API."""
    data = _podman_api("/v4.0.0/libpod/containers/stats?stream=false", timeout=10)
    if not data:
        return []
    items = data.get("Stats") if isinstance(data, dict) else data
    out = []
    for s in (items or []):
        cpu_pct   = f"{s.get('CPU', 0.0):.1f}%"
        mem_usage = s.get("MemUsage", 0)
        mem_limit = s.get("MemLimit", 1)
        mem_str   = f"{mem_usage // 1048576}MiB / {mem_limit // 1048576}MiB"
        out.append({
            "name": s.get("Name", ""),
            "cpu":  cpu_pct,
            "mem":  mem_str,
            "net":  "--",
        })
    return out


# ── API — Overview ────────────────────────────────────────────────────────────

@app.get("/api/overview")
async def get_overview():
    redis = await get_redis()
    agents = await get_agent_states(redis)

    healthy = sum(1 for a in agents.values() if a["health"] == "healthy")
    degraded= sum(1 for a in agents.values() if a["health"] == "degraded")
    dead    = sum(1 for a in agents.values() if a["health"] == "dead")

    circuit = await redis.get("system:circuit_broken") == "1"
    halted  = await redis.get("system:halted") == "1"

    # Trade counter
    trade_count = await redis.get("trade:count:total") or "0"

    # Recent signal count (last 100 stream entries)
    sig_len = await redis.xlen(STREAMS["signals"])

    # Job count and recent errors
    job_ids       = await redis.smembers(JOB_INDEX_KEY)
    import time as _time
    job_err_count = await redis.zcount("scheduler:job_errors", _time.time() - 3600, "+inf")

    return {
        "market": {
            "open":             is_market_open(),
            "trading_day":      is_trading_day(),
            "active_session":   is_active_session(),
            "time_et":          now_et().strftime("%H:%M:%S"),
            "date":             now_et().date().isoformat(),
            "minutes_to_open":  minutes_to_open(),
            "minutes_to_close": minutes_to_close() if is_market_open() else None,
        },
        "system": {
            "circuit_broken": circuit,
            "halted":         halted,
        },
        "agents": {
            "healthy":  healthy,
            "degraded": degraded,
            "dead":     dead,
            "total":    len(agents),
        },
        "metrics": {
            "total_trades":    int(trade_count),
            "signal_stream":   sig_len,
            "active_jobs":     len(job_ids),
            "job_errors_1h":   int(job_err_count),
        },
    }


# ── API — Agents ──────────────────────────────────────────────────────────────

@app.get("/api/agents")
async def get_agents():
    redis    = await get_redis()
    agents   = await get_agent_states(redis)
    stats    = {s["name"]: s for s in podman_stats()}
    ps       = {c["name"]: c for c in podman_ps()}

    for name, agent in agents.items():
        cname = agent.get("container", "")
        agent["cpu"]     = stats.get(cname, {}).get("cpu", "--")
        agent["mem"]     = stats.get(cname, {}).get("mem", "--")
        agent["podman"]  = ps.get(cname, {}).get("status", "not found")

    return list(agents.values())


@app.post("/api/agents/{agent}/restart")
async def restart_agent(agent: str, token: str = ""):
    check_token(token)
    cname = CONTAINER_MAP.get(agent, f"ot-{agent}")
    redis = await get_redis()
    await redis.xadd(
        STREAMS["commands"],
        {"command": "restart", "target": agent, "issued_by": "webui"},
        maxlen=500,
    )
    # Also trigger podman restart via REST API
    try:
        conn = _UnixSocketHTTPConnection(PODMAN_SOCK)
        conn.timeout = 5
        conn.request("POST", f"/v4.0.0/libpod/containers/{cname}/restart")
        conn.getresponse()
        log.info("agent.restart", agent=agent, container=cname)
        return {"restarting": agent, "container": cname}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/{agent}/logs")
async def get_agent_logs(agent: str, lines: int = 100):
    cname = CONTAINER_MAP.get(agent, f"ot-{agent}")
    try:
        raw = _podman_api(
            f"/v4.0.0/libpod/containers/{cname}/logs?stdout=true&stderr=true&tail={lines}",
            raw=True,
        )
        if raw is None:
            return {"agent": agent, "container": cname, "logs": ["[podman socket unavailable]"]}
        log_lines = _parse_docker_log_stream(raw)
        return {"agent": agent, "container": cname, "logs": log_lines}
    except Exception as e:
        return {"agent": agent, "container": cname, "logs": [str(e)]}


# ── API — Scheduler ───────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    redis = await get_redis()
    job_ids = await redis.smembers(JOB_INDEX_KEY)
    jobs = []
    for jid in job_ids:
        raw = await redis.get(f"{JOB_KEY_PREFIX}{jid}")
        if raw:
            jobs.append(json.loads(raw))
    return sorted(jobs, key=lambda j: j.get("id", ""))


class JobCreate(BaseModel):
    id:                str
    name:              str
    job_type:          str
    hour:              Optional[int]  = None
    minute:            Optional[int]  = None
    day_of_week:       Optional[str]  = None
    seconds:           Optional[int]  = None
    minutes:           Optional[int]  = None
    command:           str  = "trigger"
    payload:           dict = {}
    market_hours_only: bool = True
    enabled:           bool = True


class JobUpdate(BaseModel):
    name:              Optional[str]  = None
    enabled:           Optional[bool] = None
    notify:            Optional[bool] = None
    market_hours_only: Optional[bool] = None
    hour:              Optional[int]  = None
    minute:            Optional[int]  = None
    seconds:           Optional[int]  = None
    minutes:           Optional[int]  = None
    payload:           Optional[dict] = None


def _db_connect_kwargs() -> dict:
    """Parse DB_URL into asyncpg keyword args, handling special chars in password."""
    import re as _re
    m = _re.match(r'postgresql://([^:]+):(.+)@([^/]+)/(.+)', DB_URL)
    if not m:
        return {"dsn": DB_URL, "ssl": False}
    user, password, host, database = m.group(1), m.group(2), m.group(3), m.group(4)
    return {"user": user, "password": password, "host": host, "database": database, "ssl": False}


async def _db_upsert_job(job: dict):
    """Persist job overrides to TimescaleDB."""
    if not DB_URL:
        return
    try:
        conn = await asyncpg.connect(**_db_connect_kwargs())
        try:
            await conn.execute("""
                INSERT INTO scheduler_jobs (id, name, schedule, minutes, seconds, enabled, notify, command, payload, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name       = EXCLUDED.name,
                    schedule   = EXCLUDED.schedule,
                    minutes    = EXCLUDED.minutes,
                    seconds    = EXCLUDED.seconds,
                    enabled    = EXCLUDED.enabled,
                    notify     = EXCLUDED.notify,
                    command    = EXCLUDED.command,
                    payload    = EXCLUDED.payload,
                    updated_at = NOW()
            """,
                job["id"],
                job.get("name", job["id"]),
                job.get("schedule"),
                job.get("minutes"),
                job.get("seconds"),
                job.get("enabled", True),
                job.get("notify", True),
                job.get("command"),
                json.dumps(job.get("payload") or {}),
            )
        finally:
            await conn.close()
    except Exception as e:
        log.warning("db_upsert_job_failed", error=str(e))


async def _db_delete_job(job_id: str):
    """Remove a job from TimescaleDB."""
    if not DB_URL:
        return
    try:
        conn = await asyncpg.connect(**_db_connect_kwargs())
        try:
            await conn.execute("DELETE FROM scheduler_jobs WHERE id = $1", job_id)
        finally:
            await conn.close()
    except Exception as e:
        log.warning("db_delete_job_failed", error=str(e))


async def _load_jobs_from_db_to_redis(redis):
    """On startup, restore all persisted jobs from DB into Redis."""
    if not DB_URL:
        return
    try:
        conn = await asyncpg.connect(**_db_connect_kwargs())
        try:
            rows = await conn.fetch("SELECT * FROM scheduler_jobs")
        finally:
            await conn.close()
        for row in rows:
            job = dict(row)
            # asyncpg returns jsonb as str
            if isinstance(job.get("payload"), str):
                try:
                    job["payload"] = json.loads(job["payload"])
                except Exception:
                    job["payload"] = {}
            # Convert timestamps to isoformat strings
            for ts_field in ("created_at", "updated_at"):
                if job.get(ts_field) and hasattr(job[ts_field], "isoformat"):
                    job[ts_field] = job[ts_field].isoformat()
            await redis.set(f"{JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
            await redis.sadd(JOB_INDEX_KEY, job["id"])
        log.info("scheduler_jobs_restored_from_db", count=len(rows))
    except Exception as e:
        log.warning("load_jobs_from_db_failed", error=str(e))


@app.on_event("startup")
async def on_startup():
    redis = await get_redis()
    await _load_jobs_from_db_to_redis(redis)


async def save_job(redis, job: dict):
    await redis.set(f"{JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
    await redis.sadd(JOB_INDEX_KEY, job["id"])
    await _db_upsert_job(job)
    await redis.publish("scheduler:reload", job["id"])


@app.post("/api/jobs")
async def create_job(job: JobCreate, token: str = ""):
    check_token(token)
    redis = await get_redis()
    if await redis.get(f"{JOB_KEY_PREFIX}{job.id}"):
        raise HTTPException(status_code=409, detail=f"Job '{job.id}' already exists")
    record = {**job.model_dump(), "created_at": now_et().isoformat(),
              "updated_at": now_et().isoformat(), "run_count": 0,
              "last_run": None, "last_status": None}
    await save_job(redis, record)
    return record


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, update: JobUpdate, token: str = ""):
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Job not found")
    record = json.loads(raw)
    record.update(update.model_dump(exclude_none=True))
    record["updated_at"] = now_et().isoformat()
    await save_job(redis, record)
    return record


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, token: str = ""):
    check_token(token)
    redis = await get_redis()
    if not await redis.get(f"{JOB_KEY_PREFIX}{job_id}"):
        raise HTTPException(status_code=404, detail="Job not found")
    await redis.delete(f"{JOB_KEY_PREFIX}{job_id}")
    await redis.srem(JOB_INDEX_KEY, job_id)
    await _db_delete_job(job_id)
    await redis.publish("scheduler:reload", f"delete:{job_id}")
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/run")
async def run_job_now(job_id: str, token: str = ""):
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Job not found")
    job = json.loads(raw)
    await redis.xadd(STREAMS["commands"],
        {"command": job.get("command","trigger"), "job": job_id,
         "manual": "true", "ts_et": now_et().isoformat(),
         "payload": json.dumps(job.get("payload",{})), "issued_by": "webui"},
        maxlen=1000)
    return {"triggered": job_id}


@app.post("/api/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, token: str = ""):
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Job not found")
    record = json.loads(raw)
    record["enabled"] = not record.get("enabled", True)
    record["updated_at"] = now_et().isoformat()
    await save_job(redis, record)
    return {"job_id": job_id, "enabled": record["enabled"]}


# ── API — Trades ──────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Recent trade fills from orders.events stream."""
    redis = await get_redis()
    entries = await redis.xrevrange(STREAMS["orders"], "+", "-", count=limit)
    trades = []
    for entry_id, fields in entries:
        try:
            # Fields are written flat by equity_trader (no JSON payload wrapper)
            trades.append({
                "id":            entry_id,
                "ts":            fields.get("ts_utc") or fields.get("ts", ""),
                "ticker":        fields.get("ticker", ""),
                "asset_class":   fields.get("asset_class", ""),
                "direction":     fields.get("direction", ""),
                "qty":           fields.get("qty", ""),
                "price":         fields.get("price", ""),
                "pnl":           fields.get("pnl", ""),
                "account":       fields.get("account_id", ""),
                "broker":        fields.get("broker", ""),
                "mode":          fields.get("mode", ""),
                "strategy":      fields.get("strategy", ""),
                "event_type":    fields.get("event_type", ""),
                "reject_reason": fields.get("reject_reason", ""),
            })
        except Exception:
            pass
    return trades


@app.get("/api/trades/summary")
async def get_trade_summary():
    """P&L summary across all accounts."""
    redis  = await get_redis()
    trades = await get_trades(500)
    fills  = [t for t in trades if t["event_type"] == "fill"]
    total_pnl = sum(float(t["pnl"] or 0) for t in fills)
    long_trades  = [t for t in fills if t["direction"] == "long"]
    short_trades = [t for t in fills if t["direction"] == "short"]
    winners = [t for t in fills if float(t.get("pnl") or 0) > 0]
    return {
        "total_trades": len(fills),
        "total_pnl":    round(total_pnl, 2),
        "win_rate":     round(len(winners) / len(fills) * 100, 1) if fills else 0,
        "long_count":   len(long_trades),
        "short_count":  len(short_trades),
        "by_account":   {},
    }


# ── API — Signals ─────────────────────────────────────────────────────────────

@app.get("/api/signals")
async def get_signals(limit: int = 30):
    redis = await get_redis()
    entries = await redis.xrevrange(STREAMS["signals"], "+", "-", count=limit)
    signals = []
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields.get("payload", "{}"))
            p = payload.get("payload", payload)
            signals.append({
                "id":                entry_id,
                "ts":                fields.get("ts_utc", ""),
                "sender":            fields.get("sender", ""),
                "ticker":            p.get("ticker", ""),
                "asset_class":       p.get("asset_class", ""),
                "direction":         p.get("direction", ""),
                "confidence":        p.get("confidence", ""),
                "entry":             p.get("entry", ""),
                "stop":              p.get("stop", ""),
                "target":            p.get("target", ""),
                "source":            p.get("source", ""),
                "ttl_ms":            p.get("ttl_ms", ""),
                "analyst_consensus": p.get("analyst_consensus", ""),
                "sentiment":         p.get("sentiment", ""),
                "intel_summary":     p.get("intel_summary", ""),
            })
        except Exception:
            pass
    return signals


# ── API — Positions + OVTLYR signals ─────────────────────────────────────────

@app.get("/api/positions/signals")
async def get_positions_signals():
    """
    Open positions from the broker gateway cross-referenced with OVTLYR signal data.
    Returns one row per (account, symbol) with all available OVTLYR data points.
    """
    import uuid as _uuid, json as _json

    try:
        import redis.asyncio as _aioredis
        _redis_url = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            _redis_url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    pos_id = str(_uuid.uuid4())
    await redis.xadd(STREAMS["broker_commands"], {
        "command": "get_positions", "request_id": pos_id, "issued_by": "webui",
    })

    pos_raw = await redis.blpop([f"broker:reply:{pos_id}"], timeout=15)
    pos_results = []
    if pos_raw:
        parsed = _json.loads(pos_raw[1])
        pos_results = parsed if isinstance(parsed, list) else [parsed]

    # Read all OVTLYR signals from hash
    ovtlyr_raw = await redis.hgetall("scanner:ovtlyr:latest")
    await redis.aclose()

    ovtlyr: dict = {}
    for ticker, val in ovtlyr_raw.items():
        try:
            ovtlyr[ticker.upper()] = _json.loads(val)
        except Exception:
            pass

    _env = _read_env_file()
    def _ev(k): return _env.get(k) or os.getenv(k, "")

    rows = []
    for r in pos_results:
        if r.get("status") != "ok":
            continue
        label  = r.get("account_label", "")
        dn_key = label.upper().replace("-", "_") + "_DISPLAY_NAME"
        data   = r.get("data", {})
        items  = data.get("items", data.get("positions", []))
        if not isinstance(items, list):
            continue

        for p in items:
            sym    = (p.get("symbol") or "").upper()
            signal = ovtlyr.get(sym, {})
            rows.append({
                "symbol":           sym,
                "account_label":    label,
                "display_name":     _ev(dn_key),
                "broker":           r.get("broker", ""),
                "mode":             r.get("mode", ""),
                "qty":              p.get("qty", 0),
                "avg_entry_price":  p.get("avg_entry_price", 0),
                "current_price":    p.get("current_price", 0),
                "market_value":     p.get("market_value", 0),
                "unrealized_pl":    p.get("unrealized_pl", 0),
                # OVTLYR data points
                "has_signal":       bool(signal),
                "signal_direction": signal.get("direction"),
                "signal_score":     signal.get("score"),     # 0–100
                "signal_price":     signal.get("price"),
                "signal_sector":    signal.get("sector"),
                "signal_ts":        signal.get("ts_utc"),
            })

    rows.sort(key=lambda x: (x["symbol"], x["account_label"]))

    latest_ts = max((v.get("ts_utc", 0) for v in ovtlyr.values()), default=0)
    return {
        "positions":    rows,
        "ovtlyr_count": len(ovtlyr),
        "ovtlyr_ts":    latest_ts,
    }


# ── API — Stream stats ────────────────────────────────────────────────────────

@app.get("/api/streams")
async def get_streams():
    redis = await get_redis()
    result = {}
    for name, stream in STREAMS.items():
        try:
            result[name] = {
                "stream": stream,
                "length": await redis.xlen(stream),
            }
        except Exception:
            result[name] = {"stream": stream, "length": 0}
    return result


# ── API — Logs ────────────────────────────────────────────────────────────────

@app.get("/api/logs/{agent}")
async def get_logs(agent: str, lines: int = 200):
    return await get_agent_logs(agent, lines)


# ── API — System controls ─────────────────────────────────────────────────────

@app.get("/api/system")
async def get_system():
    redis = await get_redis()
    cb     = await redis.get("system:circuit_broken") == "1"
    reason = await redis.get("system:circuit_reason") or ""
    halted = await redis.get("system:halted") == "1"
    containers = podman_ps()
    stats      = podman_stats()
    return {
        "circuit_broken":  cb,
        "circuit_reason":  reason,
        "halted":          halted,
        "containers":      containers,
        "stats":           stats,
    }


@app.post("/api/system/reset_circuit")
async def reset_circuit(token: str = ""):
    check_token(token)
    redis = await get_redis()
    await redis.delete("system:circuit_broken")
    await redis.delete("system:circuit_reason")
    await redis.xadd(STREAMS["commands"],
        {"command": "reset_circuit", "issued_by": "webui"}, maxlen=500)
    return {"reset": True}


@app.post("/api/system/halt")
async def halt_system(token: str = ""):
    check_token(token)
    redis = await get_redis()
    await redis.set("system:halted", "1")
    await redis.xadd(STREAMS["commands"],
        {"command": "halt", "issued_by": "webui"}, maxlen=500)
    return {"halted": True}


@app.post("/api/system/resume")
async def resume_system(token: str = ""):
    check_token(token)
    redis = await get_redis()
    await redis.delete("system:halted")
    return {"resumed": True}


@app.get("/api/history")
async def get_history(limit: int = 100):
    redis = await get_redis()
    entries = await redis.xrevrange(STREAMS["commands"], "+", "-", count=limit)
    return [
        {"id": eid, **{k: v for k, v in fields.items()}}
        for eid, fields in entries
    ]


# ── API — Broker connections ──────────────────────────────────────────────────

def _is_placeholder(val: str) -> bool:
    return not val or val.startswith("your_") or val.startswith("${")


def _masked(val: str) -> str:
    """Return '***' if set, '' if not."""
    return "***" if val and not _is_placeholder(val) else ""


@app.get("/api/broker/connections")
@app.get("/api/broker/status")
async def get_broker_status():
    """Return per-broker configuration status and accounts from accounts.toml + .env."""
    env = _read_env_file()

    # Merge env file values with live process env (process env takes priority for running values)
    def ev(key: str) -> str:
        return env.get(key) or os.getenv(key, "")

    # Credential env vars required per broker+mode (mirrors broker_gateway/registry.py)
    _BROKER_CREDS = {
        ("tradier", "sandbox"):  ["TRADIER_SANDBOX_API_KEY"],
        ("tradier", "live"):     ["TRADIER_PRODUCTION_API_KEY"],
        ("alpaca",  "paper"):    ["ALPACA_API_SECRET_KEY"],
        ("alpaca",  "live"):     ["ALPACA_LIVE_API_SECRET_KEY"],
        ("webull",  "paper"):    ["WEBULL_API_KEY", "WEBULL_SECRET_KEY"],
        ("webull",  "live"):     ["WEBULL_API_KEY", "WEBULL_SECRET_KEY"],
    }

    # Parse accounts.toml for account definitions per broker
    accounts_by_broker: dict = {"tradier": [], "alpaca": [], "webull": []}
    try:
        import toml as _toml
        raw = _toml.load(ACCOUNTS_CONFIG)
        import re as _re
        def _resolve(val: str) -> str:
            return _re.sub(r'\$\{(\w+)\}', lambda m: ev(m.group(1)) or "", val or "")
        for a in raw.get("accounts", []):
            b    = a.get("broker", "")
            mode = a.get("mode", "")
            if b in accounts_by_broker:
                resolved_id = _resolve(a.get("id", ""))
                # Auto-enable: enabled=false is explicit opt-out; otherwise check credentials
                if a.get("enabled") is False:
                    active = False
                else:
                    cred_keys = _BROKER_CREDS.get((b, mode), [])
                    active = bool(resolved_id) and all(
                        ev(k) and not _is_placeholder(ev(k)) for k in cred_keys
                    )
                lbl = a.get("label", "")
                dn_key = lbl.upper().replace("-", "_") + "_DISPLAY_NAME"
                accounts_by_broker[b].append({
                    "label":        lbl,
                    "display_name": ev(dn_key),
                    "mode":         mode,
                    "id":           resolved_id,
                    "enabled":      active,
                    "tags":         a.get("strategy_tags", []),
                })
    except Exception:
        pass

    # Tradier
    t_token = ev("TRADIER_SANDBOX_API_KEY") or ev("TRADIER_PRODUCTION_API_KEY")
    tradier_ok = bool(t_token) and not _is_placeholder(t_token)

    # Alpaca
    a_secret = ev("ALPACA_API_SECRET_KEY")
    alpaca_ok = bool(a_secret) and not _is_placeholder(a_secret)

    # Webull
    w_api_key = ev("WEBULL_API_KEY")
    webull_ok = bool(w_api_key) and not _is_placeholder(w_api_key)

    redis = await get_redis()
    stored_mode = await redis.get("config:trade_mode")
    trade_mode  = stored_mode or ev("TRADE_MODE") or "sandbox"

    return {
        "trade_mode": trade_mode,
        "brokers": {
            "tradier": {
                "connected": tradier_ok,
                "accounts":  accounts_by_broker["tradier"],
                "env": {
                    "TRADIER_SANDBOX_API_KEY":         _masked(ev("TRADIER_SANDBOX_API_KEY")),
                    "TRADIER_SANDBOX_ACCOUNT_NUMBER":  ev("TRADIER_SANDBOX_ACCOUNT_NUMBER"),
                    "TRADIER_PRODUCTION_API_KEY":      _masked(ev("TRADIER_PRODUCTION_API_KEY")),
                    "TRADIER_PROD_ACCOUNT_1":          ev("TRADIER_PROD_ACCOUNT_1"),
                    "TRADIER_PROD_ACCOUNT_1_IRA":      ev("TRADIER_PROD_ACCOUNT_1_IRA"),
                    "TRADIER_PROD_ACCOUNT_2":          ev("TRADIER_PROD_ACCOUNT_2"),
                    "TRADIER_PROD_ACCOUNT_2_IRA":      ev("TRADIER_PROD_ACCOUNT_2_IRA"),
                    "TRADIER_PROD_ACCOUNT_3":          ev("TRADIER_PROD_ACCOUNT_3"),
                    "TRADIER_PROD_ACCOUNT_3_IRA":      ev("TRADIER_PROD_ACCOUNT_3_IRA"),
                    "TRADIER_PROD_ACCOUNT_4":          ev("TRADIER_PROD_ACCOUNT_4"),
                    "TRADIER_PROD_ACCOUNT_4_IRA":      ev("TRADIER_PROD_ACCOUNT_4_IRA"),
                },
            },
            "alpaca": {
                "connected": alpaca_ok,
                "accounts":  accounts_by_broker["alpaca"],
                "env": {
                    "ALPACA_API_SECRET_KEY":      _masked(a_secret),
                    "ALPACA_PAPER_ACCOUNT_ID":    ev("ALPACA_PAPER_ACCOUNT_ID"),
                    "ALPACA_LIVE_API_SECRET_KEY": _masked(ev("ALPACA_LIVE_API_SECRET_KEY")),
                    "ALPACA_LIVE_ACCOUNT_ID":     ev("ALPACA_LIVE_ACCOUNT_ID"),
                    "ALPACA_DATA_FEED":           ev("ALPACA_DATA_FEED") or "iex",
                },
            },
            "webull": {
                "connected": webull_ok,
                "accounts":  accounts_by_broker["webull"],
                "env": {
                    "WEBULL_API_KEY":              _masked(w_api_key),
                    "WEBULL_SECRET_KEY":           _masked(ev("WEBULL_SECRET_KEY")),
                    "WEBULL_PAPER_ACCOUNT_ID":     ev("WEBULL_PAPER_ACCOUNT_ID"),
                    "WEBULL_LIVE_ACCOUNT_1":       ev("WEBULL_LIVE_ACCOUNT_1"),
                    "WEBULL_LIVE_ACCOUNT_1_IRA":   ev("WEBULL_LIVE_ACCOUNT_1_IRA"),
                    "WEBULL_LIVE_ACCOUNT_2":       ev("WEBULL_LIVE_ACCOUNT_2"),
                    "WEBULL_LIVE_ACCOUNT_2_IRA":   ev("WEBULL_LIVE_ACCOUNT_2_IRA"),
                    "WEBULL_LIVE_ACCOUNT_3":       ev("WEBULL_LIVE_ACCOUNT_3"),
                    "WEBULL_LIVE_ACCOUNT_3_IRA":   ev("WEBULL_LIVE_ACCOUNT_3_IRA"),
                    "WEBULL_LIVE_ACCOUNT_4":       ev("WEBULL_LIVE_ACCOUNT_4"),
                    "WEBULL_LIVE_ACCOUNT_4_IRA":   ev("WEBULL_LIVE_ACCOUNT_4_IRA"),
                    "WEBULL_LIVE_ACCOUNT_5":       ev("WEBULL_LIVE_ACCOUNT_5"),
                    "WEBULL_LIVE_ACCOUNT_5_IRA":   ev("WEBULL_LIVE_ACCOUNT_5_IRA"),
                },
            },
        },
    }


@app.get("/api/broker/positions")
async def get_broker_positions():
    """
    Fetch live positions and balances for all enabled accounts via the broker gateway.
    Sends get_positions + get_balances commands to Redis and blpops replies.
    """
    import uuid as _uuid, json as _json

    try:
        import redis.asyncio as _aioredis
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=100,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    pos_id = str(_uuid.uuid4())
    bal_id = str(_uuid.uuid4())
    stream = STREAMS["broker_commands"]

    await redis.xadd(stream, {"command": "get_positions", "request_id": pos_id, "issued_by": "webui"})
    await redis.xadd(stream, {"command": "get_balances",  "request_id": bal_id, "issued_by": "webui"})

    async def _blpop(key: str):
        result = await redis.blpop([key], timeout=90)
        if not result:
            return []
        raw = _json.loads(result[1])
        return raw if isinstance(raw, list) else [raw]

    pos_results, bal_results = await asyncio.gather(
        _blpop(f"broker:reply:{pos_id}"),
        _blpop(f"broker:reply:{bal_id}"),
    )
    await redis.aclose()

    # Index by account_label
    balances  = {r["account_label"]: r.get("data", {}) for r in bal_results if r.get("status") == "ok"}
    positions = {}
    for r in pos_results:
        if r.get("status") == "ok":
            d = r.get("data", {})
            positions[r["account_label"]] = d.get("items", d.get("positions", []))

    _pos_env = _read_env_file()
    def _pos_ev(k): return _pos_env.get(k) or os.getenv(k, "")

    all_labels = sorted(set(balances) | set(positions))
    accounts = []
    for label in all_labels:
        bal = dict(balances.get(label, {}))
        pos = positions.get(label, [])
        # Some brokers embed positions in balances (Tradier/Alpaca)
        if not pos and "positions" in bal:
            pos = bal.pop("positions", [])
        bal.pop("raw", None)   # strip verbose raw field
        dn_key = label.upper().replace("-", "_") + "_DISPLAY_NAME"
        accounts.append({
            "label":        label,
            "display_name": _pos_ev(dn_key),
            "broker":       next((r["broker"] for r in bal_results + pos_results if r.get("account_label") == label), ""),
            "mode":         next((r["mode"]   for r in bal_results + pos_results if r.get("account_label") == label), ""),
            "balances":     bal,
            "positions":    pos,
        })

    return {"accounts": accounts}


@app.get("/api/broker/orders")
async def get_broker_orders(status: str = "open"):
    """Fetch open/all orders for all accounts via the broker gateway."""
    import uuid as _uuid, json as _json

    try:
        import redis.asyncio as _aioredis
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=100,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    req_id = str(_uuid.uuid4())
    await redis.xadd(STREAMS["broker_commands"], {
        "command": "get_orders", "request_id": req_id,
        "status": status, "issued_by": "webui",
    })
    result = await redis.blpop([f"broker:reply:{req_id}"], timeout=90)
    await redis.aclose()
    if not result:
        return {"orders": []}
    raw = _json.loads(result[1])
    results = raw if isinstance(raw, list) else [raw]

    orders = []
    for r in results:
        if r.get("status") != "ok":
            continue
        data = r.get("data", {})
        items = data.get("items", data.get("orders", []))
        if isinstance(items, list):
            for o in items:
                if isinstance(o, dict):
                    o["_account_label"] = r.get("account_label", "")
                    o["_broker"]        = r.get("broker", "")
                    o["_mode"]          = r.get("mode", "")
                    orders.append(o)
    return {"orders": orders}


@app.get("/api/broker/quote")
async def get_broker_quote(symbol: str, account_label: str = ""):
    """Fetch bid/ask/last for a symbol via the broker gateway."""
    import uuid as _uuid, json as _json

    try:
        import redis.asyncio as _aioredis
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    req_id = str(_uuid.uuid4())
    cmd = {"command": "get_quote", "request_id": req_id, "symbol": symbol, "issued_by": "webui"}
    if account_label:
        cmd["account_label"] = account_label
    await redis.xadd(STREAMS["broker_commands"], cmd)
    result = await redis.blpop([f"broker:reply:{req_id}"], timeout=10)
    await redis.aclose()
    if not result:
        raise HTTPException(status_code=504, detail="Quote timeout — broker gateway did not respond")
    raw = _json.loads(result[1])
    r = raw[0] if isinstance(raw, list) else raw
    if r.get("status") != "ok":
        raise HTTPException(status_code=502, detail=r.get("error", "Quote failed"))
    data = r.get("data", {})
    bid  = data.get("bid")
    ask  = data.get("ask")
    last = data.get("last") or data.get("close")
    # Fallback: if only last is available, synthesise a tight spread
    if bid is None and ask is None and last:
        bid = round(float(last) - 0.01, 2)
        ask = round(float(last) + 0.01, 2)
    return {
        "symbol": symbol,
        "bid":    float(bid)  if bid  is not None else None,
        "ask":    float(ask)  if ask  is not None else None,
        "last":   float(last) if last is not None else None,
    }


class LiquidateBody(BaseModel):
    token:         str
    symbol:        str
    account_label: str
    quantity:      float
    price:         float
    duration:      str   # day | gtc
    side:          str   # sell | buy_to_cover


@app.post("/api/broker/liquidate")
async def liquidate_position(body: LiquidateBody):
    """Submit a limit order to liquidate a position via the broker gateway."""
    check_token(body.token)
    if body.duration not in ("day", "gtc"):
        raise HTTPException(status_code=400, detail="duration must be 'day' or 'gtc'")
    if body.side not in ("sell", "buy_to_cover"):
        raise HTTPException(status_code=400, detail="side must be 'sell' or 'buy_to_cover'")
    if body.price <= 0 or body.quantity <= 0:
        raise HTTPException(status_code=400, detail="price and quantity must be positive")

    import uuid as _uuid, json as _json

    try:
        import redis.asyncio as _aioredis
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    req_id = str(_uuid.uuid4())
    await redis.xadd(STREAMS["broker_commands"], {
        "command":       "place_order",
        "request_id":    req_id,
        "account_label": body.account_label,
        "symbol":        body.symbol,
        "side":          body.side,
        "quantity":      str(int(body.quantity)),
        "order_type":    "limit",
        "price":         str(body.price),
        "duration":      body.duration,
        "tag":           "webui-liquidate",
        "issued_by":     "webui",
    })
    result = await redis.blpop([f"broker:reply:{req_id}"], timeout=15)
    if not result:
        await redis.aclose()
        raise HTTPException(status_code=504, detail="Order timeout — broker gateway did not respond")

    from datetime import datetime, timezone as _tz

    raw = _json.loads(result[1])
    r   = raw[0] if isinstance(raw, list) else raw

    async def _write_order_event(event_type: str, order_id: str = "", reject_reason: str = ""):
        fields = {
            "event_type":  event_type,
            "account_id":  body.account_label,
            "broker":      r.get("broker", ""),
            "mode":        r.get("mode", ""),
            "ticker":      body.symbol,
            "asset_class": "equity",
            "direction":   "short" if body.side in ("sell", "sell_short") else "long",
            "qty":         str(int(body.quantity)),
            "price":       str(body.price),
            "order_id":    order_id,
            "strategy":    "webui-liquidate",
            "ts_utc":      datetime.now(_tz.utc).isoformat(),
        }
        if reject_reason:
            fields["reject_reason"] = reject_reason
        await redis.xadd(STREAMS["orders"], fields, maxlen=10_000)

    # Gateway-level error (connector raised an exception)
    if r.get("status") != "ok":
        err_msg = r.get("error", "Order failed")
        await _write_order_event("reject", reject_reason=err_msg)
        await redis.aclose()
        raise HTTPException(status_code=502, detail=err_msg)

    order_data   = r.get("data", {}) or {}
    inner_status = str(order_data.get("status", "")).lower()
    REJECTED     = {"rejected", "error", "canceled", "cancelled", "denied", "failed"}
    order_id     = str(order_data.get("id", order_data.get("orderId", "")))

    # Extract broker error message wherever the broker may have embedded it
    raw_errors = order_data.get("errors") or {}
    if isinstance(raw_errors, dict):
        raw_errors = raw_errors.get("error") or {}
    broker_err = (
        raw_errors
        or order_data.get("error")
        or order_data.get("message")
        or order_data.get("reason")
    )
    if isinstance(broker_err, list):
        broker_err = "; ".join(str(e) for e in broker_err)
    broker_err = str(broker_err).strip() if broker_err else ""

    # Broker-level rejection: bad status, embedded error, or null/zero order ID
    # (Tradier returns id=0 when the order is silently rejected at HTTP 200)
    null_id = not order_id or order_id in ("0", "None", "null")
    if inner_status in REJECTED or broker_err or null_id:
        err_msg = broker_err or f"Order {inner_status or 'rejected'} by broker"
        await _write_order_event("reject", order_id=order_id, reject_reason=err_msg)
        await redis.aclose()
        raise HTTPException(status_code=422, detail=err_msg)

    # Order submitted successfully — write pending (limit orders aren't filled until executed)
    await _write_order_event("pending", order_id=order_id)
    await redis.aclose()
    return {"status": "ok", "order": order_data}


def _podman_post(path: str, timeout: int = 10) -> bool:
    """POST to the Podman REST API over the Unix socket. Returns True on success."""
    try:
        conn = _UnixSocketHTTPConnection(PODMAN_SOCK)
        conn.timeout = timeout
        conn.request("POST", path, headers={"Content-Length": "0"})
        resp = conn.getresponse()
        resp.read()
        return resp.status < 400
    except Exception as e:
        log.warning("podman_post.failed", path=path, error=str(e))
        return False


async def _restart_broker_gateway() -> None:
    """Restart broker-gateway (and dependents) so new credentials take effect."""
    await asyncio.sleep(1)  # let the HTTP response return first
    dependents = [
        "ot-trader-equity", "ot-trader-options", "ot-chat-agent",
        "ot-mcp-tradingview", "ot-mcp-alpaca", "ot-mcp-yahoo",
    ]
    restart_order = dependents + ["ot-broker-gateway"]
    try:
        loop = asyncio.get_event_loop()
        # Stop dependents first, then gateway
        for name in restart_order:
            await loop.run_in_executor(
                None, _podman_post,
                f"/v4.0.0/libpod/containers/{name}/stop?t=5",
            )
        await asyncio.sleep(2)
        # Restart gateway first, then dependents
        for name in ["ot-broker-gateway"] + dependents:
            await loop.run_in_executor(
                None, _podman_post,
                f"/v4.0.0/libpod/containers/{name}/restart",
            )
        log.info("broker_gateway.auto_restarted")
    except Exception as e:
        log.warning("broker_gateway.auto_restart_failed", error=str(e))


async def _notify_broker_update(broker: str, updated_keys: list[str]) -> None:
    """Fire-and-forget notification to configured messaging connectors."""
    import aiohttp as _aiohttp
    env = _read_env_file()
    def ev(k): return env.get(k) or os.getenv(k, "")

    # Determine which credential categories changed
    categories = set()
    for k in updated_keys:
        if "API_KEY" in k or "ACCESS_TOKEN" in k or "SECRET" in k or "TOKEN" in k:
            categories.add("credentials")
        elif "ACCOUNT" in k:
            categories.add("account numbers")
        else:
            categories.add("settings")
    summary = " & ".join(sorted(categories)) if categories else "configuration"
    msg = f"\U0001f511 OpenTrader — {broker.title()} broker {summary} updated via Command Center"

    async with _aiohttp.ClientSession() as s:
        # Telegram
        tg_token = ev("TELEGRAM_BOT_TOKEN")
        tg_chat  = ev("TELEGRAM_CHAT_ID")
        if tg_token and tg_chat and not _is_placeholder(tg_token):
            try:
                await s.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": msg},
                    timeout=_aiohttp.ClientTimeout(total=6),
                )
            except Exception:
                pass

        # Discord
        dc_url = ev("DISCORD_WEBHOOK_URL")
        if dc_url and not _is_placeholder(dc_url):
            try:
                await s.post(dc_url, json={"content": msg}, timeout=_aiohttp.ClientTimeout(total=6))
            except Exception:
                pass


class EnvReveal(BaseModel):
    token: str
    keys:  list


class TradeModeBody(BaseModel):
    token: str
    mode:  str  # "sandbox" | "live"


@app.get("/api/trade-mode")
async def get_trade_mode():
    redis = await get_redis()
    stored = await redis.get("config:trade_mode")
    mode   = stored or _read_env_file().get("TRADE_MODE", "sandbox") or "sandbox"
    return {"mode": mode}


@app.post("/api/trade-mode")
async def set_trade_mode(body: TradeModeBody):
    check_token(body.token)
    if body.mode not in ("sandbox", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'sandbox' or 'live'")
    redis = await get_redis()
    await redis.set("config:trade_mode", body.mode)
    _write_env_file({"TRADE_MODE": body.mode})
    return {"mode": body.mode}


@app.post("/api/broker/env/reveal")
async def reveal_broker_env(body: EnvReveal):
    """Return unmasked env values for the given keys (requires token)."""
    if body.token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    env = _read_env_file()
    return {k: env.get(k) or os.getenv(k, "") for k in body.keys}


class EnvUpdate(BaseModel):
    token: str
    vars:  dict


@app.post("/api/broker/env")
async def update_broker_env(body: EnvUpdate):
    """Write broker credential env vars to the .env file."""
    if body.token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    if not body.vars:
        raise HTTPException(status_code=400, detail="No vars provided")
    try:
        _write_env_file(body.vars)
        keys = list(body.vars.keys())
        # Detect broker from key prefixes
        for broker in ("tradier", "alpaca", "webull"):
            if any(k.upper().startswith(broker.upper()) for k in keys):
                asyncio.create_task(_notify_broker_update(broker, keys))
                break
        # Auto-restart broker-gateway so new credentials take effect immediately
        asyncio.create_task(_restart_broker_gateway())
        return {"ok": True, "updated": keys}
    except PermissionError:
        raise HTTPException(status_code=500, detail=".env file is not writable — check volume mount")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CfgTestBody(BaseModel):
    vars: dict = {}


@app.post("/api/config/test/{service}")
async def test_config_connector(service: str, body: CfgTestBody = CfgTestBody()):
    """Send a test message/ping to Telegram, Discord, or AgentMail.
    Optional body.vars override live field values from the modal."""
    import aiohttp as _aiohttp
    env = _read_env_file()
    # Override with any values passed directly from the modal form
    env.update(body.vars)
    def ev(k): return env.get(k) or os.getenv(k, "")

    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            if service == "telegram":
                token   = ev("TELEGRAM_BOT_TOKEN")
                chat_id = ev("TELEGRAM_CHAT_ID")
                if not token or not chat_id:
                    raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
                url  = f"https://api.telegram.org/bot{token}/sendMessage"
                resp = await s.post(url, json={"chat_id": chat_id, "text": "✅ OpenTrader — Telegram connection test successful"})
                body = await resp.json()
                if not body.get("ok"):
                    raise HTTPException(status_code=400, detail=body.get("description", "Telegram API error"))
                return {"ok": True, "message": "Test message sent to Telegram"}

            elif service == "discord":
                token      = ev("DISCORD_BOT_TOKEN")
                channel_id = ev("DISCORD_CHANNEL_ID")
                if not token:
                    raise HTTPException(status_code=400, detail="DISCORD_BOT_TOKEN is required")
                if not channel_id:
                    raise HTTPException(status_code=400, detail="DISCORD_CHANNEL_ID is required")
                headers = {"Authorization": f"Bot {token}"}
                resp = await s.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers=headers,
                    json={"content": "✅ OpenTrader — Discord connection test successful"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid Discord bot token")
                if resp.status == 403:
                    raise HTTPException(status_code=400, detail="Bot lacks permission to send messages in that channel")
                if resp.status == 404:
                    raise HTTPException(status_code=400, detail="Channel not found — check DISCORD_CHANNEL_ID")
                if resp.status in (200, 201):
                    return {"ok": True, "message": "Test message sent to Discord channel"}
                body_text = await resp.text()
                raise HTTPException(status_code=400, detail=f"Discord API returned {resp.status}: {body_text[:200]}")

            elif service == "agentmail":
                api_key  = ev("AGENTMAIL_API_KEY")
                base_url = (ev("AGENTMAIL_BASE_URL") or "https://api.agentmail.to").rstrip("/").removesuffix("/v1").removesuffix("/v0")
                if not api_key:
                    raise HTTPException(status_code=400, detail="AGENTMAIL_API_KEY is required")
                resp = await s.get(f"{base_url}/v0/inboxes", headers={"Authorization": f"Bearer {api_key}"})
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid AgentMail API key")
                if resp.status not in (200, 201):
                    raise HTTPException(status_code=400, detail=f"AgentMail returned {resp.status}")
                return {"ok": True, "message": "AgentMail API key is valid"}

            elif service == "ovtlyr":
                email    = ev("OVTLYR_EMAIL")
                password = ev("OVTLYR_PASSWORD")
                base_url = (ev("OVTLYR_BASE_URL") or "https://console.ovtlyr.com").rstrip("/")
                if not email:
                    raise HTTPException(status_code=400, detail="OVTLYR_EMAIL is required")
                if not password or _is_placeholder(password):
                    raise HTTPException(status_code=400, detail="OVTLYR_PASSWORD is required")
                # Test reachability — a 200/redirect on the login page confirms the service is up
                # and credentials are stored. Full login requires Playwright (browser automation).
                resp = await s.get(
                    f"{base_url}/login",
                    timeout=_aiohttp.ClientTimeout(total=10),
                    allow_redirects=True,
                )
                if resp.status in (200, 301, 302):
                    return {"ok": True, "message": f"OVTLYR login page reachable — credentials saved (full login requires scraper container)"}
                else:
                    return {"ok": False, "message": f"OVTLYR returned HTTP {resp.status}"}

            elif service == "openrouter":
                api_key  = ev("OPENROUTER_API_KEY")
                base_url = (ev("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
                if not api_key:
                    raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY is required")
                resp = await s.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid OpenRouter API key")
                if resp.status == 200:
                    data = await resp.json()
                    count = len(data.get("data", []))
                    return {"ok": True, "message": f"API key valid — {count} models available"}
                raise HTTPException(status_code=400, detail=f"OpenRouter returned HTTP {resp.status}")

            else:
                raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/agentmail/provision")
async def provision_agentmail_inboxes(body: CfgTestBody = CfgTestBody()):
    """Create all AgentMail inboxes defined in env vars. Safe to call repeatedly — 409 = already exists."""
    import aiohttp as _aiohttp
    env = _read_env_file()
    env.update(body.vars)
    def ev(k): return env.get(k) or os.getenv(k, "")

    api_key  = ev("AGENTMAIL_API_KEY")
    base_url = (ev("AGENTMAIL_BASE_URL") or "https://api.agentmail.to").rstrip("/").removesuffix("/v1").removesuffix("/v0")
    if not api_key:
        raise HTTPException(status_code=400, detail="AGENTMAIL_API_KEY is required")

    # Deduplicate — review may share the alerts inbox on free tier
    seen: set = set()
    inbox_keys = {}
    for role, key in [
        ("orchestrator", "AGENTMAIL_ORCHESTRATOR_INBOX"),
        ("review",       "AGENTMAIL_REVIEW_INBOX"),
        ("eod",          "AGENTMAIL_EOD_INBOX"),
        ("alerts",       "AGENTMAIL_ALERTS_INBOX"),
    ]:
        username = ev(key) or role
        if username not in seen:
            inbox_keys[role] = username
            seen.add(username)
        else:
            inbox_keys[role] = None  # shared — skip creation

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    results = []
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=15)) as s:
            # Fetch existing inboxes first to avoid hitting the limit on re-provision
            existing: set = set()
            existing_emails: dict = {}
            try:
                r = await s.get(f"{base_url}/v0/inboxes", headers=headers)
                if r.status == 200:
                    data = await r.json()
                    for ib in data.get("inboxes", []):
                        uid = ib.get("inbox_id", "").split("@")[0]
                        existing.add(uid)
                        existing_emails[uid] = ib.get("email", f"{uid}@agentmail.to")
            except Exception:
                pass

            for role, username in inbox_keys.items():
                if not username:
                    results.append({"role": role, "status": "shared",
                                    "reason": "shares another inbox"})
                    continue
                # Already in account — no need to create
                if username in existing:
                    results.append({"role": role, "username": username,
                                    "email": existing_emails.get(username, f"{username}@agentmail.to"),
                                    "status": "exists"})
                    continue
                resp = await s.post(f"{base_url}/v0/inboxes", json={"username": username}, headers=headers)
                rdata = {}
                try:
                    rdata = await resp.json()
                except Exception:
                    pass
                err_name = rdata.get("name", "")
                if resp.status in (200, 201):
                    results.append({"role": role, "username": username,
                                    "email": rdata.get("email", f"{username}@agentmail.to"),
                                    "status": "created"})
                elif err_name == "IsTakenError":
                    # Inbox name taken by another user — needs a unique name
                    results.append({"role": role, "username": username,
                                    "status": "error",
                                    "reason": f"Name '{username}' is taken — choose a unique inbox name"})
                elif err_name == "LimitExceededError":
                    results.append({"role": role, "username": username,
                                    "status": "error",
                                    "reason": "Inbox limit reached — upgrade AgentMail plan or reuse existing inboxes"})
                else:
                    results.append({"role": role, "username": username,
                                    "status": "error",
                                    "reason": rdata.get("message") or f"HTTP {resp.status}"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    errors = [r for r in results if r["status"] == "error"]
    return {
        "ok": len(errors) == 0,
        "results": results,
        "message": f"{len(results) - len(errors)}/{len(results)} inboxes ready" + (f" — {len(errors)} error(s)" if errors else ""),
    }


@app.post("/api/broker/test/{broker}")
async def test_broker_connection(broker: str):
    """Test broker API reachability with current credentials."""
    import aiohttp as _aiohttp
    env = _read_env_file()

    def ev(key: str) -> str:
        return env.get(key) or os.getenv(key, "")

    try:
        if broker == "tradier":
            sandbox_key = ev("TRADIER_SANDBOX_API_KEY")
            prod_key    = ev("TRADIER_PRODUCTION_API_KEY")
            results = []
            async with _aiohttp.ClientSession() as s:
                for label, key, url in [
                    ("sandbox", sandbox_key, "https://sandbox.tradier.com/v1/user/profile"),
                    ("production", prod_key,  "https://api.tradier.com/v1/user/profile"),
                ]:
                    if not key or _is_placeholder(key):
                        continue
                    async with s.get(
                        url,
                        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                        timeout=_aiohttp.ClientTimeout(total=8),
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            name = data.get("profile", {}).get("name", "")
                            results.append(f"{label}: {name or 'OK'}")
                        elif r.status == 401:
                            results.append(f"{label}: invalid key")
                        else:
                            results.append(f"{label}: HTTP {r.status}")
            if not results:
                return {"ok": False, "message": "No API keys configured"}
            ok = any("invalid" not in r and "HTTP" not in r for r in results)
            return {"ok": ok, "message": " | ".join(results)}

        elif broker == "alpaca":
            paper_key = ev("ALPACA_API_SECRET_KEY")
            live_key  = ev("ALPACA_LIVE_API_SECRET_KEY")
            results = []
            async with _aiohttp.ClientSession() as s:
                for label, key, url in [
                    ("paper", paper_key, "https://paper-api.alpaca.markets/v2/account"),
                    ("live",  live_key,  "https://api.alpaca.markets/v2/account"),
                ]:
                    key_id, secret = key, key
                    if not secret or _is_placeholder(secret):
                        continue
                    async with s.get(
                        url,
                        headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
                        timeout=_aiohttp.ClientTimeout(total=8),
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            equity = data.get("equity", 0)
                            results.append(f"{label}: ${float(equity or 0):,.2f} equity")
                        elif r.status == 403:
                            results.append(f"{label}: invalid credentials")
                        else:
                            results.append(f"{label}: HTTP {r.status}")
            if not results:
                return {"ok": False, "message": "No API credentials configured"}
            ok = any("invalid" not in r and "HTTP" not in r for r in results)
            return {"ok": ok, "message": " | ".join(results)}

        elif broker == "webull":
            import base64 as _b64, hashlib as _hl, hmac as _hmac, uuid as _uuid, json as _json
            from datetime import datetime as _dt, timezone as _tz
            from urllib.parse import quote as _quote
            api_key = ev("WEBULL_API_KEY")
            secret  = ev("WEBULL_SECRET_KEY")
            if not api_key or _is_placeholder(api_key):
                return {"ok": False, "message": "API key not configured"}
            if not secret or _is_placeholder(secret):
                return {"ok": False, "message": "Secret key not configured"}

            path      = "/app/subscriptions/list"
            host      = "api.webull.com"
            nonce     = str(_uuid.uuid4())
            timestamp = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sign_params = {
                "x-app-key":             api_key,
                "x-timestamp":           timestamp,
                "x-signature-version":   "1.0",
                "x-signature-algorithm": "HMAC-SHA1",
                "x-signature-nonce":     nonce,
                "host":                  host,
            }
            sorted_pairs   = "&".join(f"{k}={v}" for k, v in sorted(sign_params.items()))
            string_to_sign = path + "&" + sorted_pairs
            encoded        = _quote(string_to_sign, safe="")
            key_bytes      = (secret + "&").encode("utf-8")
            sig            = _b64.b64encode(
                _hmac.new(key_bytes, encoded.encode("utf-8"), _hl.sha1).digest()
            ).decode("utf-8")
            headers = {
                "x-app-key":             api_key,
                "x-signature":           sig,
                "x-signature-algorithm": "HMAC-SHA1",
                "x-signature-version":   "1.0",
                "x-signature-nonce":     nonce,
                "x-timestamp":           timestamp,
                "Accept":                "application/json",
            }
            async with _aiohttp.ClientSession() as s:
                async with s.get(
                    f"https://{host}{path}",
                    headers=headers,
                    timeout=_aiohttp.ClientTimeout(total=8),
                ) as r:
                    data = {}
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        pass
                    if r.status == 200:
                        # Response may be a list of subscriptions or a dict
                        if isinstance(data, list) and data:
                            acct = data[0].get("account_number", data[0].get("account_id", ""))
                        elif isinstance(data, dict):
                            acct = data.get("account_number", data.get("account_id", ""))
                        else:
                            acct = ""
                        return {"ok": True, "message": f"Connected{(' — account: ' + str(acct)) if acct else ''}"}
                    elif r.status == 401:
                        return {"ok": False, "message": "Invalid credentials — check API key and secret"}
                    elif r.status == 403:
                        return {"ok": False, "message": "Access denied — verify API key permissions"}
                    else:
                        msg = (data.get("msg", data.get("message", f"HTTP {r.status}"))
                               if isinstance(data, dict) else f"HTTP {r.status}")
                        return {"ok": False, "message": str(msg)}
        else:
            return {"ok": False, "message": f"Unknown broker: {broker}"}

    except Exception as e:
        return {"ok": False, "message": f"Connection error: {str(e)[:100]}"}


# ── Strategy Engineer — AI chat ──────────────────────────────────────────────

class StrategyMessage(BaseModel):
    message: str
    history: list = []
    strategy_text: str = ""

@app.post("/api/strategy-engineer/chat")
async def strategy_engineer_chat(body: StrategyMessage, token: str = ""):
    check_token(token)

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    if not openrouter_key or openrouter_key.startswith("your_"):
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY not configured")

    # Fetch live TradingView indicators if a ticker is mentioned
    tv_context = ""
    tv_context_map = {}
    import re
    tickers = re.findall(r'\b([A-Z]{2,5})\b', body.message)
    if tickers:
        try:
            from shared.mcp_client import get_tv_indicators
            for ticker in tickers[:3]:
                tv = await get_tv_indicators(ticker)
                if tv:
                    tv_context_map[ticker] = tv
                    tv_context += (
                        f"\nLive TradingView data for {ticker}: "
                        f"recommendation={tv['recommendation']}, "
                        f"buy={tv['buy']}, sell={tv['sell']}, neutral={tv['neutral']}"
                    )
        except Exception:
            pass  # MCP not available in this container — skip TV data

    system_prompt = """You are an expert quantitative strategy engineer for the OpenTrader platform.
Your job is to help design, refine, and document algorithmic trading strategies.

When you have enough information, produce a complete strategy document in the right panel using this exact format:

---STRATEGY---
Name: <strategy name>
Asset Class: equity | etf | options
Direction: long | short | both
Min Confidence: <0.50–1.00>
Max Position USD: <amount>
Stop Loss %: <value>
Take Profit %: <value>
Entry Signals: <comma-separated signal sources>
Indicators: <TradingView indicators used>
Hypothesis: <1-3 sentences describing the edge>
Rules:
  - <entry rule 1>
  - <entry rule 2>
  - <exit rule>
Notes: <any additional context>
---END---

Guidelines:
- Use OpenTrader's available signal sources: ovtlyr, wsb_sentiment, seekalpha, yahoo_finance
- Entry signals must be quantifiable and testable
- Reference TradingView indicators where relevant
- Keep rules concise and implementable
- Always include stop loss and take profit
- If live market data is provided, incorporate it into your analysis""" + (
    f"\n\nLive market context:{tv_context}" if tv_context else ""
)
    if body.strategy_text.strip():
        system_prompt += (
            "\n\nThe user currently has this strategy document open in their editor:\n"
            f"{body.strategy_text}\n\n"
            "CRITICAL: Whenever the user asks to add, modify, or refine ANY element of this "
            "strategy, you MUST respond by emitting the COMPLETE updated strategy document in "
            "the ---STRATEGY---...---END--- format with every field filled in. "
            "Never describe a change without also emitting the full updated document."
        )

    messages = [{"role": "system", "content": system_prompt}]
    for h in body.history[-10:]:  # last 10 turns for context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": body.message})

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-sonnet-4-5"),
                    "messages": messages,
                    "max_tokens": 1500,
                    "temperature": 0.3,
                },
                timeout=_aiohttp.ClientTimeout(total=45),
            ) as resp:
                data = await resp.json()

        if "error" in data:
            raise HTTPException(status_code=502, detail=data["error"].get("message", "LLM error"))

        reply = data["choices"][0]["message"]["content"]

        # Extract strategy block if present
        strategy_text = body.strategy_text
        if "---STRATEGY---" in reply and "---END---" in reply:
            start = reply.index("---STRATEGY---")
            end   = reply.index("---END---") + len("---END---")
            strategy_text = reply[start:end]

        return {
            "ok":            True,
            "reply":         reply,
            "strategy_text": strategy_text,
            "tv_context":    tv_context_map,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI request failed: {str(e)[:120]}")


@app.post("/api/strategy-engineer/chat/stream")
async def strategy_engineer_chat_stream(body: StrategyMessage, token: str = ""):
    check_token(token)

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    if not openrouter_key or openrouter_key.startswith("your_"):
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY not configured")

    # Fetch TradingView data for any tickers mentioned
    tv_context = ""
    tv_context_map = {}
    tickers = re.findall(r'\b([A-Z]{2,5})\b', body.message)
    if tickers:
        try:
            from shared.mcp_client import get_tv_indicators
            for ticker in tickers[:3]:
                tv = await get_tv_indicators(ticker)
                if tv:
                    tv_context_map[ticker] = tv
                    tv_context += (
                        f"\nLive TradingView data for {ticker}: "
                        f"recommendation={tv['recommendation']}, "
                        f"buy={tv['buy']}, sell={tv['sell']}, neutral={tv['neutral']}"
                    )
        except Exception:
            pass

    system_prompt = """You are an expert quantitative strategy engineer for the OpenTrader platform.
Your job is to help design, refine, and document algorithmic trading strategies.

When you have enough information, produce a complete strategy document in the right panel using this exact format:

---STRATEGY---
Name: <strategy name>
Asset Class: equity | etf | options
Direction: long | short | both
Min Confidence: <0.50–1.00>
Max Position USD: <amount>
Stop Loss %: <value>
Take Profit %: <value>
Entry Signals: <comma-separated signal sources>
Indicators: <TradingView indicators used>
Hypothesis: <1-3 sentences describing the edge>
Rules:
  - <entry rule 1>
  - <entry rule 2>
  - <exit rule>
Notes: <any additional context>
---END---

Guidelines:
- Use OpenTrader's available signal sources: ovtlyr, wsb_sentiment, seekalpha, yahoo_finance
- Entry signals must be quantifiable and testable
- Reference TradingView indicators where relevant
- Keep rules concise and implementable
- Always include stop loss and take profit
- If live market data is provided, incorporate it into your analysis""" + (
        f"\n\nLive market context:{tv_context}" if tv_context else ""
    )
    if body.strategy_text.strip():
        system_prompt += (
            "\n\nThe user currently has this strategy document open in their editor:\n"
            f"{body.strategy_text}\n\n"
            "CRITICAL: Whenever the user asks to add, modify, or refine ANY element of this "
            "strategy, you MUST respond by emitting the COMPLETE updated strategy document in "
            "the ---STRATEGY---...---END--- format with every field filled in. "
            "Never describe a change without also emitting the full updated document."
        )

    messages = [{"role": "system", "content": system_prompt}]
    for h in body.history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": body.message})

    async def event_stream():
        import aiohttp as _aiohttp
        # Send TV context first if available
        if tv_context_map:
            yield f"data: {json.dumps({'type': 'tv', 'context': tv_context_map})}\n\n"

        try:
            async with _aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-sonnet-4-5"),
                        "messages":    messages,
                        "max_tokens":  1500,
                        "temperature": 0.3,
                        "stream":      True,
                    },
                    timeout=_aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body_txt = await resp.text()
                        yield f"data: {json.dumps({'type': 'error', 'message': body_txt[:200]})}\n\n"
                        return

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            break
                        try:
                            chunk   = json.loads(payload)
                            content = chunk["choices"][0]["delta"].get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                        except Exception:
                            pass

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:120]})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Strategy Library persistence ─────────────────────────────────────────────

def _read_strategies() -> list:
    try:
        with open(STRATEGIES_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def _write_strategies(strategies: list):
    import tempfile
    tmp = STRATEGIES_CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(strategies, f, indent=2)
    os.replace(tmp, STRATEGIES_CONFIG_PATH)

@app.get("/api/strategies")
async def get_strategies_list():
    return _read_strategies()

class StrategiesBody(BaseModel):
    strategies: list = []

class SessionBody(BaseModel):
    name: str = "session"
    saved_at: str = ""
    history: list = []
    strategy_text: str = ""

@app.post("/api/strategies")
async def save_strategies_list(body: StrategiesBody, token: str = ""):
    check_token(token)
    _write_strategies(body.strategies)
    return {"ok": True}

@app.post("/api/strategies/session")
async def save_strategy_session(body: SessionBody, token: str = ""):
    check_token(token)
    safe_name = re.sub(r'[^a-z0-9_]', '_', body.name.lower())[:40] or "session"
    path = STRATEGIES_CONFIG_PATH.replace("strategies.json", f"session_{safe_name}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(body.model_dump(), f, indent=2)
    os.replace(tmp, path)
    return {"ok": True}

@app.get("/api/strategies/sessions")
async def list_strategy_sessions():
    import glob as _glob
    sessions_dir = os.path.dirname(STRATEGIES_CONFIG_PATH)
    sessions = []
    for fpath in sorted(
        _glob.glob(os.path.join(sessions_dir, "session_*.json")),
        key=os.path.getmtime, reverse=True
    ):
        try:
            with open(fpath) as f:
                data = json.load(f)
            filename = os.path.basename(fpath).replace("session_", "").replace(".json", "")
            sessions.append({
                "filename":      filename,
                "name":          data.get("name", filename),
                "saved_at":      data.get("saved_at", ""),
                "message_count": len(data.get("history", [])),
                "has_strategy":  bool((data.get("strategy_text") or "").strip()),
            })
        except Exception:
            pass
    return sessions

@app.get("/api/strategies/session/{name}")
async def get_strategy_session(name: str):
    safe_name = re.sub(r'[^a-z0-9_]', '_', name.lower())[:40] or "session"
    path = STRATEGIES_CONFIG_PATH.replace("strategies.json", f"session_{safe_name}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


# ── Strategy Version Control ──────────────────────────────────────────────────

def _versions_path(family_id: str) -> str:
    safe = re.sub(r'[^a-z0-9_-]', '_', str(family_id))[:60]
    return os.path.join(STRATEGY_VERSIONS_DIR, f"{safe}.json")

def _read_versions(family_id: str) -> list:
    try:
        with open(_versions_path(family_id)) as f:
            return json.load(f)
    except Exception:
        return []

def _write_versions(family_id: str, versions: list):
    path = _versions_path(family_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(versions, f, indent=2)
    os.replace(tmp, path)

class SnapshotBody(BaseModel):
    strategy: dict
    label: str = ""

class BacktestResultsBody(BaseModel):
    results: dict
    run_at: str = ""

@app.get("/api/strategies/{family_id}/versions")
async def get_strategy_versions(family_id: str):
    return _read_versions(family_id)

@app.post("/api/strategies/{family_id}/snapshot")
async def create_strategy_snapshot(family_id: str, body: SnapshotBody, token: str = ""):
    check_token(token)
    versions = _read_versions(family_id)
    next_ver  = (max(v["version"] for v in versions) + 1) if versions else 1
    snapshot  = {
        **body.strategy,
        "family_id":      family_id,
        "version":        next_ver,
        "snapshot_label": body.label or f"Version {next_ver}",
        "snapshot_at":    datetime.now(timezone.utc).isoformat(),
        "backtest_results": None,
        "backtest_run_at":  None,
    }
    versions.append(snapshot)
    _write_versions(family_id, versions)
    return {"ok": True, "version": next_ver}

@app.put("/api/strategies/{family_id}/versions/{version}/restore")
async def restore_strategy_version(family_id: str, version: int, token: str = ""):
    check_token(token)
    versions = _read_versions(family_id)
    target   = next((v for v in versions if v["version"] == version), None)
    if not target:
        raise HTTPException(status_code=404, detail="Version not found")
    strategies = _read_strategies()
    idx = next((i for i, s in enumerate(strategies) if s.get("family_id") == family_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Strategy not found in library")
    current = strategies[idx]
    restored = dict(target)
    # Preserve runtime state — position memory is tied to strategy name, not version
    restored["status"]           = current.get("status", "draft")
    restored["deployed_version"] = current.get("deployed_version")
    restored["id"]               = current.get("id")
    strategies[idx] = restored
    _write_strategies(strategies)
    return {"ok": True, "restored_version": version}

@app.post("/api/strategies/{family_id}/versions/{version}/backtest")
async def save_version_backtest(
    family_id: str, version: int, body: BacktestResultsBody, token: str = ""
):
    check_token(token)
    versions = _read_versions(family_id)
    target   = next((v for v in versions if v["version"] == version), None)
    if not target:
        raise HTTPException(status_code=404, detail="Version not found")
    target["backtest_results"] = body.results
    target["backtest_run_at"]  = body.run_at or datetime.now(timezone.utc).isoformat()
    _write_versions(family_id, versions)
    return {"ok": True}


# ── WebSocket — live push ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            redis    = await get_redis()
            overview = await get_overview()
            agents   = await get_agents()
            signals  = await get_signals(10)
            streams  = await get_streams()
            trades   = await get_trades(5)
            stored_mode = await redis.get("config:trade_mode")
            trade_mode  = stored_mode or _read_env_file().get("TRADE_MODE", "sandbox") or "sandbox"
            await websocket.send_json({
                "type":       "update",
                "overview":   overview,
                "agents":     agents,
                "signals":    signals,
                "streams":    streams,
                "trades":     trades,
                "trade_mode": trade_mode,
            })
            await asyncio.sleep(4)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("ws.error", error=str(e))


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("/app/webui/static/index.html") as f:
        html = f.read()
    # Inject token meta tag so the frontend can seed sessionStorage without prompting
    html = html.replace(
        "<head>",
        f'<head>\n<meta name="ot-token" content="{WEBUI_TOKEN}">',
        1,
    )
    return html
