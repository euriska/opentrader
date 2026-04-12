"""
OpenTrader Command Center — FastAPI backend
Central station for all agent containers.
Sections: Overview · Agents · Scheduler · Trades · Signals · Sentiment · Logs · System
"""
import asyncio
import csv
import http.client
import io
import json
import os
import re
import socket
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shared.redis_client import get_redis, STREAMS
from scheduler.calendar import (
    is_market_open, is_trading_day, is_active_session,
    minutes_to_open, minutes_to_close, now_et,
)

log = structlog.get_logger("command-center")

# ── Backtest job store (in-memory, single-process) ────────────────────────────
_bt_jobs: dict[str, dict] = {}   # job_id → {status, family_id, version, results?, error?}
TZ  = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

app = FastAPI(title="OpenTrader Command Center", version="2.0.0")
app.mount("/static", StaticFiles(directory="/app/webui/static"), name="static")

WEBUI_TOKEN    = os.getenv("WEBUI_TOKEN", "opentrader")

def _read_app_version() -> str:
    """Read version from VERSION file (local dev + Docker) or APP_VERSION env var (CI fallback)."""
    for path in (
        "/app/VERSION",                                                  # Docker container
        os.path.join(os.path.dirname(__file__), "..", "..", "VERSION"),  # local dev
    ):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    return os.getenv("APP_VERSION", "dev")

APP_VERSION = _read_app_version()
JOB_KEY_PREFIX = "scheduler:job:"
JOB_INDEX_KEY  = "scheduler:jobs"
ENV_PATH        = os.getenv("ENV_FILE_PATH", "/app/.env")
ACCOUNTS_CONFIG = "/app/config/accounts.toml"
DB_URL                 = os.getenv("DB_URL", "")
STRATEGIES_CONFIG_PATH = "/app/config/strategies.json"
STRATEGY_VERSIONS_DIR  = "/app/config/strategy_versions"
ASSIGNMENTS_PATH       = "/app/config/assignments.json"
EXCLUSIONS_PATH        = "/app/config/exclusions.json"
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
    "trader-equity", "trader-options", "options-monitor",
    "scraper-ovtlyr", "scraper-wsb", "scraper-seekalpha", "scraper-yahoo",
    "scraper-yahoo-sentiment",
    "aggregator", "review-agent", "broker-gateway", "directive-agent",
    # MCP servers & chat agent — health derived from Podman (no heartbeat)
    "mcp-yahoo", "mcp-alpaca", "mcp-tradingview", "mcp-massive", "mcp-unusualwhales", "mcp-webull", "chat-agent",
]

# Containers that don't publish heartbeats — health is read from Podman status
PODMAN_HEALTH_ONLY = {"mcp-yahoo", "mcp-alpaca", "mcp-tradingview", "mcp-massive", "mcp-unusualwhales", "mcp-webull", "chat-agent"}

CONTAINER_MAP = {
    "orchestrator":    "ot-orchestrator",
    "scheduler":       "ot-scheduler",
    "predictor":       "ot-predictor",
    "trader-equity":   "ot-trader-equity",
    "trader-options":  "ot-trader-options",
    "options-monitor": "ot-options-monitor",
    "scraper-ovtlyr":  "ot-scraper-ovtlyr",
    "scraper-wsb":     "ot-scraper-wsb",
    "scraper-seekalpha":"ot-scraper-seekalpha",
    "scraper-yahoo":            "ot-scraper-yahoo",
    "scraper-yahoo-sentiment":  "ot-scraper-yahoo-sentiment",
    "aggregator":      "ot-aggregator",
    "review-agent":    "ot-review-agent",
    "broker-gateway":  "ot-broker-gateway",
    "directive-agent": "ot-directive-agent",
    "mcp-yahoo":          "ot-mcp-yahoo",
    "mcp-alpaca":         "ot-mcp-alpaca",
    "mcp-tradingview":    "ot-mcp-tradingview",
    "mcp-massive":        "ot-mcp-massive",
    "mcp-unusualwhales":  "ot-mcp-unusualwhales",
    "mcp-webull":         "ot-mcp-webull",
    "chat-agent":         "ot-chat-agent",
    "redis":           "ot-redis",
    "timescaledb":     "ot-timescaledb",
    "grafana":         "ot-grafana",
    "webui":           "ot-webui",
}


def check_token(token: str):
    if token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Helpers ──────────────────────────────────────────────────────────────────

_OCC_SYMBOL_RE = re.compile(r'^[A-Z]{1,6}\d{6}[CP]\d{8}$', re.IGNORECASE)
_OPTION_ASSET_CLASSES = {"option", "options", "us_option"}

def _is_equity_position(p: dict) -> bool:
    """Return True if a broker position is a stock (equity), not an option contract."""
    raw = p.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if str(raw.get("instrument_type", "")).upper() == "OPTION":
        return False
    ac = str(raw.get("asset_class") or p.get("asset_class") or "").lower()
    if ac in _OPTION_ASSET_CLASSES:
        return False
    if _OCC_SYMBOL_RE.match(p.get("symbol") or ""):
        return False
    return True


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


# ── API — Market Calendar ─────────────────────────────────────────────────────

@app.get("/api/market/calendar")
async def get_market_calendar(year: int = None, month: int = None):
    """Return day-by-day trading status for a given month."""
    import calendar as _cal
    from datetime import date as _date
    now   = now_et()
    year  = year  or now.year
    month = month or now.month
    _, days_in_month = _cal.monthrange(year, month)
    today = now.date()
    result = []
    for day in range(1, days_in_month + 1):
        d         = _date(year, month, day)
        is_wknd   = d.weekday() >= 5
        is_hol    = d in __import__('scheduler.calendar', fromlist=['NYSE_HOLIDAYS']).NYSE_HOLIDAYS
        is_trade  = not is_wknd and not is_hol
        result.append({
            "date":         d.isoformat(),
            "day":          day,
            "weekday":      d.strftime("%a"),
            "trading":      is_trade,
            "weekend":      is_wknd,
            "holiday":      is_hol,
            "today":        d == today,
        })
    return {"year": year, "month": month, "month_name": now_et().replace(year=year, month=month).strftime("%B"), "days": result}


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
    name:                   Optional[str]  = None
    enabled:                Optional[bool] = None
    notify:                 Optional[bool] = None
    market_hours_only:      Optional[bool] = None
    schedule:               Optional[str]  = None
    hour:                   Optional[int]  = None
    minute:                 Optional[int]  = None
    seconds:                Optional[int]  = None
    minutes:                Optional[int]  = None
    payload:                Optional[dict] = None
    intraday_start:         Optional[str]  = None
    intraday_end:           Optional[str]  = None
    intraday_interval_min:  Optional[int]  = None
    intraday_days:          Optional[str]  = None


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
                INSERT INTO scheduler_jobs
                    (id, name, schedule, minutes, seconds, enabled, notify, command, payload,
                     intraday_start, intraday_end, intraday_interval_min, intraday_days, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name                  = EXCLUDED.name,
                    schedule              = EXCLUDED.schedule,
                    minutes               = EXCLUDED.minutes,
                    seconds               = EXCLUDED.seconds,
                    enabled               = EXCLUDED.enabled,
                    notify                = EXCLUDED.notify,
                    command               = EXCLUDED.command,
                    payload               = EXCLUDED.payload,
                    intraday_start        = EXCLUDED.intraday_start,
                    intraday_end          = EXCLUDED.intraday_end,
                    intraday_interval_min = EXCLUDED.intraday_interval_min,
                    intraday_days         = EXCLUDED.intraday_days,
                    updated_at            = NOW()
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
                job.get("intraday_start"),
                job.get("intraday_end"),
                job.get("intraday_interval_min"),
                job.get("intraday_days"),
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
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS library_categories (
                    id         UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
                    name       TEXT        NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Migrate any categories already stored in books
            await pool.execute("""
                INSERT INTO library_categories (name)
                    SELECT DISTINCT category FROM library_books WHERE category IS NOT NULL
                    ON CONFLICT (name) DO NOTHING
            """)
        except Exception:
            pass
    # Dividend tables
    await _div_ensure_tables()


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
            # Derive timestamp from stream entry ID (format: {ms}-{seq}) if no ts field
            ts_from_id = int(entry_id.split("-")[0]) if "-" in entry_id else 0
            ts = fields.get("ts_utc") or fields.get("ts") or ts_from_id or ""
            # Fields are written flat by equity_trader (no JSON payload wrapper)
            trades.append({
                "id":            entry_id,
                "ts":            ts,
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

    # Collect all position tickers so we can do a targeted DB lookup
    all_syms: set[str] = set()
    for r in pos_results:
        if r.get("status") != "ok":
            continue
        items = r.get("data", {})
        items = items.get("items", items.get("positions", []))
        for p in (items if isinstance(items, list) else []):
            sym = (p.get("symbol") or "").upper()
            if sym:
                all_syms.add(sym)

    # Layer 1: position-specific intel (scraped per-ticker by scrape_position_intel job)
    pos_intel_raw = await redis.hgetall("ovtlyr:position_intel")
    # Layer 2: general screener results (up to ~30 tickers from OVTLYR screener)
    screener_raw  = await redis.hgetall("scanner:ovtlyr:latest")
    await redis.aclose()

    def _parse_hash(raw: dict) -> dict:
        out = {}
        for k, v in raw.items():
            try:
                out[k.upper()] = _json.loads(v)
            except Exception:
                pass
        return out

    pos_intel = _parse_hash(pos_intel_raw)
    screener  = _parse_hash(screener_raw)

    # Layer 3: DB fallback — latest signal per ticker from ovtlyr_intel table
    db_signals: dict = {}
    if all_syms:
        try:
            import asyncpg as _asyncpg
            from urllib.parse import urlparse as _urlparse, unquote as _unquote
            _db_url = os.getenv("DB_URL", "")
            if _db_url:
                _p = _urlparse(_db_url)
                _conn = await _asyncpg.connect(
                    host=_p.hostname, port=_p.port or 5432,
                    user=_p.username,
                    password=_unquote(_p.password) if _p.password else None,
                    database=_p.path.lstrip("/"),
                )
                try:
                    _rows = await _conn.fetch(
                        """
                        SELECT DISTINCT ON (ticker)
                            ticker, signal, signal_active, signal_date,
                            nine_score, oscillator, last_close
                        FROM ovtlyr_intel
                        WHERE ticker = ANY($1::text[])
                        ORDER BY ticker, ts DESC
                        """,
                        list(all_syms),
                    )
                    for row in _rows:
                        db_signals[row["ticker"].upper()] = {
                            "direction":    row["signal"],
                            "signal_active": row["signal_active"],
                            "signal_date":  str(row["signal_date"]) if row["signal_date"] else None,
                            "score":        row["nine_score"],
                            "oscillator":   row["oscillator"],
                            "price":        row["last_close"],
                            "source":       "db",
                        }
                finally:
                    await _conn.close()
        except Exception:
            pass  # DB fallback is best-effort

    def _normalize(raw: dict) -> dict:
        """Normalize signal dict to common field names regardless of source."""
        if not raw:
            return {}
        return {
            "direction": raw.get("direction") or raw.get("signal"),
            "score":     raw.get("score") or raw.get("nine_score"),
            "price":     raw.get("price") or raw.get("last_close"),
            "sector":    raw.get("sector"),
            "ts_utc":    raw.get("ts_utc") or raw.get("ts"),
            "source":    raw.get("source", "redis"),
        }

    def _resolve_signal(sym: str) -> dict:
        """Priority: position_intel > screener > db"""
        raw = pos_intel.get(sym) or screener.get(sym) or db_signals.get(sym)
        return _normalize(raw) if raw else {}

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
            signal = _resolve_signal(sym)
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
                "signal_score":     signal.get("score"),
                "signal_price":     signal.get("price"),
                "signal_sector":    signal.get("sector"),
                "signal_ts":        signal.get("ts_utc"),
                "signal_source":    signal.get("source", "redis") if signal else None,
            })

    rows.sort(key=lambda x: (x["symbol"], x["account_label"]))

    all_signals = {**db_signals, **screener, **pos_intel}
    latest_ts = max((v.get("ts_utc", 0) for v in all_signals.values() if v.get("ts_utc")), default=0)
    return {
        "positions":    rows,
        "ovtlyr_count": len(all_signals),
        "ovtlyr_ts":    latest_ts,
    }


_SECTOR_STATIC: dict = {
    # ETFs
    "SPY":"ETF","QQQ":"ETF","IWM":"ETF","DIA":"ETF","VTI":"ETF","VOO":"ETF",
    "VEA":"ETF","VWO":"ETF","VYMI":"ETF","VIG":"ETF","VYM":"ETF","SCHD":"ETF",
    "AGG":"ETF","BND":"ETF","TLT":"ETF","IEF":"ETF","SHY":"ETF","SGOV":"ETF",
    "GLD":"ETF","SLV":"ETF","USO":"ETF","XLE":"ETF","XLF":"ETF","XLK":"ETF",
    "XLV":"ETF","XLI":"ETF","XLP":"ETF","XLY":"ETF","XLU":"ETF","XLB":"ETF",
    "ARKK":"ETF","ARKW":"ETF","ARKG":"ETF","ARKF":"ETF",
    # Technology
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology","GOOGL":"Technology",
    "GOOG":"Technology","META":"Technology","AMZN":"Technology","TSLA":"Technology",
    "AMD":"Technology","INTC":"Technology","AVGO":"Technology","QCOM":"Technology",
    "TXN":"Technology","MU":"Technology","AMAT":"Technology","LRCX":"Technology",
    "KLAC":"Technology","MRVL":"Technology","SNPS":"Technology","CDNS":"Technology",
    "AEIS":"Technology","AMSC":"Technology","HOOW":"Technology",
    "CRM":"Technology","ORCL":"Technology","SAP":"Technology","ADBE":"Technology",
    "NOW":"Technology","INTU":"Technology","PANW":"Technology","CRWD":"Technology",
    "ZS":"Technology","FTNT":"Technology","NET":"Technology","OKTA":"Technology",
    "SNOW":"Technology","DDOG":"Technology","MDB":"Technology","PLTR":"Technology",
    # Healthcare
    "JNJ":"Healthcare","UNH":"Healthcare","PFE":"Healthcare","ABBV":"Healthcare",
    "MRK":"Healthcare","TMO":"Healthcare","ABT":"Healthcare","DHR":"Healthcare",
    "BMY":"Healthcare","LLY":"Healthcare","AMGN":"Healthcare","GILD":"Healthcare",
    "BIIB":"Healthcare","REGN":"Healthcare","VRTX":"Healthcare","ISRG":"Healthcare",
    "ALNY":"Healthcare","ARWR":"Healthcare","APLS":"Healthcare","AQST":"Healthcare",
    "ARCT":"Healthcare","ALGS":"Healthcare","ALKS":"Healthcare","ALLO":"Healthcare",
    "ABUS":"Healthcare","BMEA":"Healthcare","CYTK":"Healthcare","DARE":"Healthcare",
    "BFAM":"Consumer Cyclical",
    # Financial Services
    "JPM":"Financial Services","BAC":"Financial Services","WFC":"Financial Services",
    "GS":"Financial Services","MS":"Financial Services","C":"Financial Services",
    "BLK":"Financial Services","SPGI":"Financial Services","ICE":"Financial Services",
    "CME":"Financial Services","V":"Financial Services","MA":"Financial Services",
    "AXP":"Financial Services","BK":"Financial Services","BBAR":"Financial Services",
    "BBT":"Financial Services","STT":"Financial Services","NTRS":"Financial Services",
    "USB":"Financial Services","TFC":"Financial Services","PNC":"Financial Services",
    # Consumer Cyclical
    "AMZN":"Consumer Cyclical","TSLA":"Consumer Cyclical","HD":"Consumer Cyclical",
    "MCD":"Consumer Cyclical","NKE":"Consumer Cyclical","SBUX":"Consumer Cyclical",
    "ABNB":"Consumer Cyclical","BKNG":"Consumer Cyclical","MAR":"Consumer Cyclical",
    "HLT":"Consumer Cyclical","CCL":"Consumer Cyclical","RCL":"Consumer Cyclical",
    "CHWY":"Consumer Cyclical","DAN":"Consumer Cyclical","BYD":"Consumer Cyclical",
    # Consumer Defensive
    "PG":"Consumer Defensive","KO":"Consumer Defensive","PEP":"Consumer Defensive",
    "WMT":"Consumer Defensive","COST":"Consumer Defensive","CL":"Consumer Defensive",
    "ADM":"Consumer Defensive","AVO":"Consumer Defensive",
    # Industrials
    "CAT":"Industrials","GE":"Industrials","HON":"Industrials","UPS":"Industrials",
    "FDX":"Industrials","LMT":"Industrials","RTX":"Industrials","NOC":"Industrials",
    "BA":"Industrials","AL":"Industrials","OC":"Industrials","DAR":"Basic Materials",
    # Basic Materials
    "ALB":"Basic Materials","FCX":"Basic Materials","NEM":"Basic Materials",
    "VALE":"Basic Materials","BHP":"Basic Materials","RIO":"Basic Materials",
    # Energy
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy","EOG":"Energy",
    "PXD":"Energy","OXY":"Energy","VLO":"Energy","MPC":"Energy","PSX":"Energy",
    # Utilities
    "NEE":"Utilities","DUK":"Utilities","SO":"Utilities","D":"Utilities",
    "AEP":"Utilities","XEL":"Utilities","WEC":"Utilities","ES":"Utilities",
    "CWT":"Utilities","BHE":"Utilities","CEPU":"Utilities",
    # Communication Services
    "NFLX":"Communication Services","DIS":"Communication Services",
    "CMCSA":"Communication Services","T":"Communication Services",
    "VZ":"Communication Services","TMUS":"Communication Services",
    # Real Estate
    "AMT":"Real Estate","PLD":"Real Estate","EQIX":"Real Estate",
    "SPG":"Real Estate","O":"Real Estate","WELL":"Real Estate",
}


async def _fetch_sector_yahoo(ticker: str, session) -> str | None:
    """Fetch sector from Yahoo Finance chart API (free, no auth required)."""
    import asyncio as _asyncio
    _SECTOR_FROM_TYPE = {
        "ETF": "ETF", "MUTUALFUND": "Mutual Fund", "INDEX": "Index",
        "CRYPTOCURRENCY": "Crypto", "CURRENCY": "Currency",
    }
    try:
        hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        async with session.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d",
            headers=hdrs, timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return None
            d = await r.json(content_type=None)
            meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
            itype = (meta.get("instrumentType") or "").upper()
            return _SECTOR_FROM_TYPE.get(itype)  # returns None for EQUITY (no sector in chart API)
    except Exception:
        return None


@app.get("/api/positions/sector-map")
async def get_position_sector_map(token: str = ""):
    """
    Return { ticker: sector } for all current position tickers.
    Priority: Redis cache → OVTLYR signal data → DB → static map → Yahoo Finance type.
    Results cached in Redis hash ticker:sectors (30 day TTL per field).
    """
    check_token(token)
    import aiohttp as _aiohttp

    _redis = await get_redis()

    # Get current position tickers
    tickers_raw = await _redis.get("broker:position_tickers")
    tickers: list = json.loads(tickers_raw) if tickers_raw else []

    result: dict = {}

    # 1. OVTLYR Redis sources
    try:
        for key in ("ovtlyr:position_intel", "scanner:ovtlyr:latest"):
            raw = await _redis.hgetall(key)
            for sym, val in raw.items():
                try:
                    d = json.loads(val) if isinstance(val, str) else val
                    sec = d.get("sector") or d.get("Sector")
                    if sec:
                        result[sym] = sec
                except Exception:
                    pass
    except Exception:
        pass

    # 2. Redis sector cache
    try:
        cached = await _redis.hgetall("ticker:sectors")
        for sym, sec in cached.items():
            if sym not in result and sec:
                result[sym] = sec
    except Exception:
        pass

    # 3. DB historical signals
    if DB_URL:
        try:
            conn = await asyncpg.connect(**_db_connect_kwargs())
            try:
                rows = await conn.fetch(
                    "SELECT DISTINCT ON (ticker) ticker, sector FROM ovtlyr_signals "
                    "WHERE sector IS NOT NULL ORDER BY ticker, ts DESC"
                )
                for row in rows:
                    if row["ticker"] not in result:
                        result[row["ticker"]] = row["sector"]
            finally:
                await conn.close()
        except Exception:
            pass

    # 4. Static map fallback
    for sym in tickers:
        if sym not in result and sym in _SECTOR_STATIC:
            result[sym] = _SECTOR_STATIC[sym]

    # 5. Yahoo Finance chart API for any still-missing tickers
    missing = [sym for sym in tickers if sym not in result]
    if missing:
        try:
            async with _aiohttp.ClientSession() as session:
                for sym in missing[:20]:  # cap to avoid long waits
                    sec = await _fetch_sector_yahoo(sym, session)
                    if sec:
                        result[sym] = sec

        except Exception:
            pass

    # Cache everything we found
    if result:
        try:
            pipe = _redis.pipeline()
            for sym, sec in result.items():
                pipe.hset("ticker:sectors", sym, sec)
            await pipe.execute()
        except Exception:
            pass

    return result


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
        ("alpaca",  "paper"):    ["ALPACA_API_SECRET"],
        ("alpaca",  "live"):     ["ALPACA_LIVE_API_SECRET"],
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
    a_secret = ev("ALPACA_API_SECRET")
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
                    "ALPACA_API_SECRET":      _masked(a_secret),
                    "ALPACA_PAPER_ACCOUNT_ID":    ev("ALPACA_PAPER_ACCOUNT_ID"),
                    "ALPACA_LIVE_API_SECRET": _masked(ev("ALPACA_LIVE_API_SECRET")),
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


# ── Positions cache — avoids 20-second broker-gateway wait on every page load ──
import time as _time

_positions_cache: dict = {"data": None, "ts": 0.0, "refreshing": False}
_POSITIONS_CACHE_TTL = 120  # serve cache for up to 2 minutes


async def _fetch_positions_from_gateway() -> dict:
    """Core broker-gateway fetch — no caching logic here."""
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

    # Cache position tickers so the OVTLYR scraper can find them without a DB query
    try:
        tickers: set[str] = set()
        for r in pos_results:
            if r.get("status") == "ok":
                d = r.get("data", {})
                for p in d.get("items", d.get("positions", [])):
                    sym = (p.get("symbol") or "").upper().strip()
                    if sym:
                        tickers.add(sym)
        if tickers:
            await redis.set(
                "broker:position_tickers",
                _json.dumps(sorted(tickers)),
                ex=14400,  # 4 hours
            )
    except Exception:
        pass

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

    # Build sector lookup: Redis cache → static map
    sector_lookup: dict = {}
    try:
        _sr = await get_redis()
        cached_sectors = await _sr.hgetall("ticker:sectors")
        sector_lookup.update(cached_sectors)
        for key in ("ovtlyr:position_intel", "scanner:ovtlyr:latest"):
            raw = await _sr.hgetall(key)
            for sym, val in raw.items():
                try:
                    d = json.loads(val) if isinstance(val, str) else val
                    sec = d.get("sector") or d.get("Sector")
                    if sec:
                        sector_lookup[sym] = sec
                except Exception:
                    pass
    except Exception:
        pass
    for sym, sec in _SECTOR_STATIC.items():
        if sym not in sector_lookup:
            sector_lookup[sym] = sec

    sent_prices: dict = {}
    try:
        sent_raw = await get_redis()
        raw_sent = await sent_raw.hgetall("sentiment:latest")
        for sym, val in raw_sent.items():
            try:
                d = json.loads(val)
                close = d.get("close")
                if close is not None:
                    sent_prices[sym] = float(close)
            except Exception:
                pass
    except Exception:
        pass

    all_labels = sorted(set(balances) | set(positions))
    accounts = []
    for label in all_labels:
        bal = dict(balances.get(label, {}))
        pos = positions.get(label, [])
        if not pos and "positions" in bal:
            pos = bal.pop("positions", [])
        bal.pop("raw", None)

        enriched_pos = []
        for p in pos:
            p = dict(p)
            sym = (p.get("symbol") or "").upper().strip()
            if sym and "sector" not in p:
                p["sector"] = sector_lookup.get(sym)
            if not p.get("market_value"):
                qty  = abs(float(p.get("qty") or p.get("quantity") or 0))
                last = float(p.get("current_price") or p.get("last_price") or 0)
                if not last and sym in sent_prices:
                    last = sent_prices[sym]
                if qty and last:
                    p["market_value"] = round(qty * last, 2)
                elif not p.get("market_value"):
                    p["market_value"] = abs(float(p.get("cost_basis") or 0))
            if not p.get("current_price") and not p.get("last_price"):
                if sym in sent_prices:
                    p["current_price"] = sent_prices[sym]
            enriched_pos.append(p)

        dn_key = label.upper().replace("-", "_") + "_DISPLAY_NAME"
        accounts.append({
            "label":        label,
            "display_name": _pos_ev(dn_key),
            "broker":       next((r["broker"] for r in bal_results + pos_results if r.get("account_label") == label), ""),
            "mode":         next((r["mode"]   for r in bal_results + pos_results if r.get("account_label") == label), ""),
            "balances":     bal,
            "positions":    enriched_pos,
        })

    return {"accounts": accounts}


async def _refresh_positions_cache():
    """Background task: fetch positions and update the module-level cache."""
    global _positions_cache
    if _positions_cache["refreshing"]:
        return
    _positions_cache["refreshing"] = True
    try:
        data = await _fetch_positions_from_gateway()
        _positions_cache["data"] = data
        _positions_cache["ts"]   = _time.monotonic()
    except Exception:
        pass
    finally:
        _positions_cache["refreshing"] = False


@app.get("/api/broker/positions")
async def get_broker_positions(force: bool = False):
    """
    Fetch live positions and balances for all enabled accounts via the broker gateway.
    Returns cached data (≤ 2 min old) immediately; refreshes in background when stale.
    Pass ?force=true to bypass cache and wait for a fresh fetch.
    """
    global _positions_cache
    now = _time.monotonic()
    age = now - _positions_cache["ts"]

    if not force and _positions_cache["data"] is not None and age < _POSITIONS_CACHE_TTL:
        # Fresh cache — return immediately
        data = dict(_positions_cache["data"])
        data["cached"] = True
        data["cache_age_s"] = int(age)
        return data

    if not force and _positions_cache["data"] is not None:
        # Stale cache — return what we have and kick off a background refresh
        if not _positions_cache["refreshing"]:
            asyncio.create_task(_refresh_positions_cache())
        data = dict(_positions_cache["data"])
        data["cached"] = True
        data["cache_age_s"] = int(age)
        return data

    # No cache (first load) or forced refresh — wait for live fetch
    data = await _fetch_positions_from_gateway()
    _positions_cache["data"] = data
    _positions_cache["ts"]   = _time.monotonic()
    _positions_cache["refreshing"] = False
    result = dict(data)
    result["cached"] = False
    result["cache_age_s"] = 0
    return result


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
        "ot-mcp-tradingview", "ot-mcp-alpaca", "ot-mcp-yahoo", "ot-mcp-webull",
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

            elif service == "massive":
                api_key = ev("MASSIVE_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="MASSIVE_API_KEY is required")
                resp = await s.get(
                    "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-02",
                    params={"apiKey": api_key},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 403:
                    raise HTTPException(status_code=400, detail="Invalid Massive API key — access forbidden")
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid Massive API key — unauthorized")
                if resp.status == 200:
                    data = await resp.json()
                    plan = data.get("queryCount", "?")
                    return {"ok": True, "message": f"Massive API key valid — market data accessible"}
                raise HTTPException(status_code=400, detail=f"Massive API returned HTTP {resp.status}")

            elif service == "alpaca_mcp":
                api_key    = ev("ALPACA_API_KEY")
                secret_key = ev("ALPACA_SECRET_KEY") or ev("ALPACA_API_SECRET")
                if not api_key or not secret_key:
                    raise HTTPException(status_code=400, detail="ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
                paper = ev("ALPACA_PAPER_TRADE") or "true"
                endpoint = "https://paper-api.alpaca.markets/v2" if paper.lower() == "true" else "https://api.alpaca.markets/v2"
                resp = await s.get(
                    f"{endpoint}/account",
                    headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid Alpaca credentials")
                if resp.status == 403:
                    raise HTTPException(status_code=400, detail="Alpaca account access forbidden — check subscription")
                if resp.status == 200:
                    data = await resp.json()
                    acct = data.get("account_number", "")
                    status = data.get("status", "")
                    mode = "paper" if paper.lower() == "true" else "live"
                    return {"ok": True, "message": f"Alpaca {mode} account {acct} — status: {status}"}
                raise HTTPException(status_code=400, detail=f"Alpaca API returned HTTP {resp.status}")

            elif service == "webull_mcp":
                app_key    = ev("WEBULL_APP_KEY")
                app_secret = ev("WEBULL_APP_SECRET")
                if not app_key or not app_secret:
                    raise HTTPException(status_code=400, detail="WEBULL_APP_KEY and WEBULL_APP_SECRET are required")
                environment = ev("WEBULL_ENVIRONMENT") or "prod"
                region      = ev("WEBULL_REGION_ID") or "us"
                # Ping the MCP server health endpoint (internal container network)
                mcp_url = ev("WEBULL_MCP_URL") or "http://ot-mcp-webull:8000"
                base = mcp_url.rstrip("/").removesuffix("/mcp")
                try:
                    resp = await s.get(f"{base}/", timeout=_aiohttp.ClientTimeout(total=5))
                    reachable = resp.status < 500
                except Exception:
                    reachable = False
                if reachable:
                    return {"ok": True, "message": f"Webull MCP server reachable — env: {environment}, region: {region}. Run 'webull-openapi-mcp auth' in the container if not yet authenticated."}
                raise HTTPException(status_code=400, detail="Webull MCP container not reachable — ensure ot-mcp-webull is running and credentials are set")

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
            paper_key = ev("ALPACA_API_SECRET")
            live_key  = ev("ALPACA_LIVE_API_SECRET")
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


@app.get("/api/broker/tradier/accounts")
async def get_tradier_accounts(token: str = ""):
    """Fetch all Tradier accounts from both sandbox and production."""
    check_token(token)
    import aiohttp as _aiohttp
    env         = _read_env_file()
    sandbox_key = env.get("TRADIER_SANDBOX_API_KEY") or os.getenv("TRADIER_SANDBOX_API_KEY", "")
    prod_key    = env.get("TRADIER_PRODUCTION_API_KEY") or os.getenv("TRADIER_PRODUCTION_API_KEY", "")

    results = []
    async with _aiohttp.ClientSession() as s:
        for env_name, label, key, url in [
            ("TRADIER_SANDBOX_API_KEY",    "sandbox",    sandbox_key, "https://sandbox.tradier.com/v1/user/profile"),
            ("TRADIER_PRODUCTION_API_KEY", "production", prod_key,    "https://api.tradier.com/v1/user/profile"),
        ]:
            if not key or _is_placeholder(key):
                continue
            try:
                async with s.get(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        results.append({"env": env_name, "label": label, "error": f"HTTP {r.status}"})
                        continue
                    data    = await r.json(content_type=None)
                    profile = data.get("profile", {})
                    name    = profile.get("name", "")
                    raw     = profile.get("account", [])
                    # Tradier returns a dict for single account, list for multiple
                    accounts = raw if isinstance(raw, list) else ([raw] if raw else [])
                    results.append({
                        "env":      env_name,
                        "label":    label,
                        "name":     name,
                        "accounts": [
                            {
                                "account_number": a.get("account_number", ""),
                                "classification": a.get("classification", ""),
                                "type":           a.get("type", ""),
                                "status":         a.get("status", ""),
                                "option_level":   a.get("option_level", ""),
                            }
                            for a in accounts
                        ],
                    })
            except Exception as e:
                results.append({"env": env_name, "label": label, "error": str(e)[:100]})

    if not results:
        raise HTTPException(status_code=400, detail="No Tradier API keys configured")
    return {"environments": results}


@app.get("/api/broker/alpaca/accounts")
async def get_alpaca_accounts(token: str = ""):
    """Fetch Alpaca paper and live account details."""
    check_token(token)
    import aiohttp as _aiohttp
    env        = _read_env_file()
    paper_key  = env.get("ALPACA_API_KEY")        or os.getenv("ALPACA_API_KEY", "")
    paper_sec  = env.get("ALPACA_API_SECRET")      or os.getenv("ALPACA_API_SECRET", "")
    live_key   = env.get("ALPACA_LIVE_API_KEY")    or os.getenv("ALPACA_LIVE_API_KEY", "")
    live_sec   = env.get("ALPACA_LIVE_API_SECRET") or os.getenv("ALPACA_LIVE_API_SECRET", "")

    results = []
    async with _aiohttp.ClientSession() as s:
        for label, key, sec, url in [
            ("paper", paper_key, paper_sec, "https://paper-api.alpaca.markets/v2/account"),
            ("live",  live_key,  live_sec,  "https://api.alpaca.markets/v2/account"),
        ]:
            if not key or _is_placeholder(key) or not sec or _is_placeholder(sec):
                continue
            try:
                async with s.get(
                    url,
                    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        results.append({"label": label, "error": f"HTTP {r.status}"})
                        continue
                    a = await r.json(content_type=None)
                    results.append({
                        "label":          label,
                        "account_number": a.get("account_number", ""),
                        "id":             a.get("id", ""),
                        "status":         a.get("status", ""),
                        "equity":         a.get("equity", ""),
                        "buying_power":   a.get("buying_power", ""),
                        "cash":           a.get("cash", ""),
                        "currency":       a.get("currency", "USD"),
                        "options_level":  a.get("options_trading_level", ""),
                    })
            except Exception as e:
                results.append({"label": label, "error": str(e)[:100]})

    if not results:
        raise HTTPException(status_code=400, detail="No Alpaca API keys configured")
    return {"accounts": results}


@app.get("/api/ovtlyr/market-signals")
async def get_ovtlyr_market_signals(token: str = ""):
    """
    Return latest OVTLYR signals for SPY and QQQ, enriched with daily price change.
    Price change (close vs prev_close) comes from the sentiment scraper's Redis cache.
    """
    check_token(token)
    import json as _json
    _redis = await get_redis()

    # Fetch OVTLYR intel
    result = {}
    for ticker in ("SPY", "QQQ"):
        raw = await _redis.hget("ovtlyr:position_intel", ticker)
        if raw:
            try:
                result[ticker] = _json.loads(raw)
            except Exception:
                pass

    # Enrich with daily price change from sentiment scraper cache (no yfinance needed)
    for ticker in ("SPY", "QQQ"):
        raw = await _redis.hget("sentiment:latest", ticker)
        if not raw:
            continue
        try:
            s = _json.loads(raw)
            close      = s.get("close")
            prev_close = s.get("prev_close")
            if close is not None and prev_close:
                change     = round(float(close) - float(prev_close), 2)
                change_pct = round(change / float(prev_close) * 100, 2)
                pdata = {
                    "close":      round(float(close), 2),
                    "prev_close": round(float(prev_close), 2),
                    "change":     change,
                    "change_pct": change_pct,
                }
                if ticker in result:
                    result[ticker].update(pdata)
                else:
                    result[ticker] = pdata
        except Exception as e:
            log.warning("market_signals.price_enrich_error", ticker=ticker, error=str(e))

    return result


@app.get("/api/ovtlyr/signals")
async def get_ovtlyr_signals(list_type: str = "bull", limit: int = 100, token: str = ""):
    """Return latest OVTLYR list signals from Redis cache (falls back to DB)."""
    check_token(token)
    import json as _json
    valid = {"bull", "bear", "market_leaders", "alpha_picks"}
    if list_type not in valid:
        raise HTTPException(status_code=400, detail=f"list_type must be one of {valid}")
    _redis = await get_redis()
    raw = await _redis.get(f"ovtlyr:list:{list_type}")
    if raw:
        try:
            entries = _json.loads(raw)
            return {"list_type": list_type, "entries": entries[:limit], "count": len(entries), "source": "cache"}
        except Exception:
            pass
    # Fallback: query DB for latest snapshot
    if DB_URL:
        try:
            conn = await asyncpg.connect(**_db_connect_kwargs())
            try:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (ticker) ticker, name, sector, signal, signal_date, last_price, avg_vol_30d, ts
                    FROM ovtlyr_lists
                    WHERE list_type = $1
                    ORDER BY ticker, ts DESC
                    LIMIT $2
                    """,
                    list_type, limit,
                )
                entries = [dict(r) for r in rows]
                # Convert date/datetime to string for JSON serialization
                for e in entries:
                    for k, v in e.items():
                        if hasattr(v, 'isoformat'):
                            e[k] = v.isoformat()
                return {"list_type": list_type, "entries": entries, "count": len(entries), "source": "db"}
            finally:
                await conn.close()
        except Exception as ex:
            log.error("ovtlyr_signals.db_error", error=str(ex))
    return {"list_type": list_type, "entries": [], "count": 0, "source": "empty"}


@app.get("/api/sentiment")
async def get_sentiment():
    """
    Return latest per-ticker F&G sentiment scores + 30-day trend.
    Scores are computed daily at 16:20 ET by scraper-yahoo-sentiment.
    Response: { "AAPL": { score, label, rsi, ma_score, momentum, vol_score, close, date, trend } }
    """
    import json as _json
    _redis = await get_redis()

    # Latest scores from Redis (written by scraper after each daily run)
    raw_scores = await _redis.hgetall("sentiment:latest")
    scores: dict = {}
    for ticker, raw in raw_scores.items():
        try:
            scores[ticker] = _json.loads(raw)
        except Exception:
            pass

    if not scores:
        return {}

    # Attach 30-day trend from Redis cache (written by scraper after scoring)
    pipe = _redis.pipeline()
    ticker_list = list(scores.keys())
    for t in ticker_list:
        pipe.get(f"sentiment:trend:{t}")
    trend_raws = await pipe.execute()
    for ticker, trend_raw in zip(ticker_list, trend_raws):
        if trend_raw:
            try:
                scores[ticker]["trend"] = _json.loads(trend_raw)
            except Exception:
                scores[ticker]["trend"] = []
        else:
            scores[ticker]["trend"] = []

    # Fallback: query DB directly if trend cache is empty
    if DB_URL and any(not scores[t].get("trend") for t in scores):
        try:
            conn = await asyncpg.connect(**_db_connect_kwargs())
            try:
                rows = await conn.fetch(
                    """
                    SELECT ticker, date, score
                    FROM ticker_sentiment
                    WHERE ticker = ANY($1)
                      AND date >= CURRENT_DATE - INTERVAL '30 days'
                    ORDER BY ticker, date ASC
                    """,
                    ticker_list,
                )
                trend_map: dict = {}
                for row in rows:
                    t = row["ticker"]
                    if t not in trend_map:
                        trend_map[t] = []
                    trend_map[t].append({
                        "date":  row["date"].isoformat(),
                        "score": float(row["score"]),
                    })
                for ticker in scores:
                    if not scores[ticker].get("trend"):
                        scores[ticker]["trend"] = trend_map.get(ticker, [])
            finally:
                await conn.close()
        except Exception as ex:
            log.warning("sentiment.db_trend_error", error=str(ex))

    return scores


@app.get("/api/ovtlyr/breadth")
async def get_ovtlyr_breadth():
    """
    Return current market breadth + rolling history.
    Breadth = bull_count / (bull + bear) * 100.
    Updated every 3 min during market hours by scraper-ovtlyr.
    """
    import json as _json
    _redis = await get_redis()

    current_raw = await _redis.get("ovtlyr:market_breadth")
    current = _json.loads(current_raw) if current_raw else None

    history_raws = await _redis.lrange("ovtlyr:market_breadth:history", 0, 199)
    history = []
    for r in history_raws:
        try:
            history.append(_json.loads(r))
        except Exception:
            pass
    # History is stored newest-first (LPUSH); reverse for chronological order
    history = list(reversed(history))

    # Fallback: query DB for history if Redis is empty (e.g. after restart)
    if not history and DB_URL:
        try:
            conn = await asyncpg.connect(**_db_connect_kwargs())
            try:
                rows = await conn.fetch(
                    """
                    SELECT ts, breadth_pct, bull_count, bear_count, signal
                    FROM ovtlyr_breadth
                    ORDER BY ts ASC
                    LIMIT 200
                    """
                )
                history = [
                    {
                        "ts":          row["ts"].isoformat(),
                        "breadth_pct": float(row["breadth_pct"]),
                        "bull_count":  row["bull_count"],
                        "bear_count":  row["bear_count"],
                        "signal":      row["signal"],
                    }
                    for row in rows
                ]
            finally:
                await conn.close()
        except Exception as ex:
            log.warning("breadth.db_history_error", error=str(ex))

    return {"current": current, "history": history}


# ── TradingView Charts ────────────────────────────────────────────────────────

# Timeframe label → TradingView scraper format (indicators) + stream format
_TV_TF_MAP = {
    "1m": ("1m",  "1"),
    "5m": ("5m",  "5"),
    "15m":("15m", "15"),
    "1h": ("1h",  "60"),
    "4h": ("4h",  "240"),
    "1d": ("1d",  "1D"),
    "1w": ("1w",  "1W"),
}

# US exchange fallback order for auto-detection
_TV_EXCHANGES = ["NASDAQ", "NYSE", "AMEX", "NYSE_ARCA", "NYSE_MKT"]


def _tv_resolve_exchange(symbol: str, preferred: str) -> str:
    """
    Return the correct TradingView exchange for a symbol.
    Tries the preferred exchange first, then falls back through common US exchanges.
    Returns the first exchange that TradingView accepts, or the preferred if all fail.
    """
    import requests
    exchanges = [preferred] + [e for e in _TV_EXCHANGES if e != preferred]
    for exch in exchanges:
        try:
            r = requests.get(
                "https://scanner.tradingview.com/symbol",
                params={"symbol": f"{exch}:{symbol}", "fields": "market"},
                timeout=5,
            )
            if r.status_code == 200:
                return exch
        except Exception:
            pass
    return preferred


def _tv_fetch_indicators(symbol: str, exchange: str, tf_ind: str) -> tuple:
    """
    Synchronous — run in subprocess.
    Returns (resolved_exchange, indicators_dict).
    """
    from tradingview_scraper.symbols.technicals import Indicators
    resolved = _tv_resolve_exchange(symbol, exchange)
    ind = Indicators()
    result = ind.scrape(symbol=symbol, exchange=resolved, timeframe=tf_ind, allIndicators=True)
    data = result.get("data", result) if isinstance(result, dict) else {}
    return resolved, (data if isinstance(data, dict) else {})


def _tv_fetch_ohlcv(symbol: str, exchange: str, tf_stream: str, bars: int) -> list:
    """Synchronous — run in subprocess via ProcessPoolExecutor."""
    import os, glob as _glob
    from tradingview_scraper.symbols.stream import Streamer
    streamer = Streamer(export_result=True, export_type="json")
    result = streamer.stream(
        exchange=exchange,
        symbol=symbol,
        timeframe=tf_stream,
        numb_price_candles=bars,
    )
    # Clean up exported JSON files (library always writes one)
    try:
        for f in _glob.glob(os.path.join(os.getcwd(), "export", f"ohlc_{symbol.lower()}_*.json")):
            os.remove(f)
    except Exception:
        pass
    return result.get("ohlc", []) if isinstance(result, dict) else []


@app.get("/api/charts/data")
async def get_chart_data(
    symbol: str,
    exchange: str = "NASDAQ",
    timeframe: str = "1d",
    bars: int = 200,
):
    """
    Return OHLCV candles + key indicators for a symbol via tradingview_scraper.
    timeframe: 1m | 5m | 15m | 1h | 4h | 1d | 1w
    """
    import asyncio
    from concurrent.futures import ProcessPoolExecutor
    tf_ind, tf_stream = _TV_TF_MAP.get(timeframe, ("1d", "1D"))
    loop = asyncio.get_event_loop()

    # tradingview_scraper uses signal.alarm (main-thread only) — must run in subprocess
    # Resolve exchange first (fast HTTP check), then fetch indicators + OHLCV in parallel
    try:
        with ProcessPoolExecutor(max_workers=3) as pool:
            # indicators fetch also resolves the exchange and returns it
            ind_future  = loop.run_in_executor(pool, _tv_fetch_indicators, symbol.upper(), exchange.upper(), tf_ind)
            resolved_exchange, raw_ind = await ind_future
            # use confirmed exchange for OHLCV
            raw_ohlcv = await loop.run_in_executor(pool, _tv_fetch_ohlcv, symbol.upper(), resolved_exchange, tf_stream, bars)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"TradingView fetch error: {ex}")

    # Pull the indicator keys we care about
    def _f(k):
        v = raw_ind.get(k)
        return round(float(v), 4) if v is not None else None

    indicators = {
        "close":        _f("close"),
        "EMA10":        _f("EMA10"),
        "EMA20":        _f("EMA20"),
        "EMA50":        _f("EMA50"),
        "EMA200":       _f("EMA200"),
        "SMA20":        _f("SMA20"),
        "SMA50":        _f("SMA50"),
        "RSI":          _f("RSI"),
        "MACD_macd":    _f("MACD.macd"),
        "MACD_signal":  _f("MACD.signal"),
        "ADX":          _f("ADX"),
        "BBPower":      _f("BBPower"),
        "Recommend_All":_f("Recommend.All"),
        "Recommend_MA": _f("Recommend.MA"),
    }

    # Normalise OHLCV: ensure timestamps are in seconds
    ohlcv = []
    for c in raw_ohlcv:
        ts = c.get("timestamp") or c.get("time") or 0
        ohlcv.append({
            "time":   int(float(ts)),
            "open":   float(c.get("open", 0)),
            "high":   float(c.get("high", 0)),
            "low":    float(c.get("low", 0)),
            "close":  float(c.get("close", 0)),
            "volume": float(c.get("volume", 0)),
        })

    # Attach OVTLYR intel for this ticker if available
    import json as _json
    _redis = await get_redis()
    ovtlyr_raw = await _redis.hget("ovtlyr:position_intel", symbol.upper())
    ovtlyr = _json.loads(ovtlyr_raw) if ovtlyr_raw else {}

    return {
        "symbol":     symbol.upper(),
        "exchange":   resolved_exchange,
        "timeframe":  timeframe,
        "ohlcv":      ohlcv,
        "indicators": indicators,
        "ovtlyr":     ovtlyr,
    }


@app.get("/api/charts/positions")
async def get_chart_positions():
    """
    Return a flat list of open position tickers across all connected broker accounts.
    Calls the broker gateway live (same as /api/broker/positions) and flattens to
    { symbol, broker, account, display_name, mode, qty, side }.
    """
    # Reuse the live positions fetch
    pos_data = await get_broker_positions()
    positions = []
    for acct in pos_data.get("accounts", []):
        label   = acct.get("label", "")
        broker  = acct.get("broker", "")
        mode    = acct.get("mode", "")
        display = acct.get("display_name") or label
        for p in acct.get("positions", []):
            sym = (p.get("symbol") or "").upper()
            if not sym:
                continue
            qty = float(p.get("qty") or p.get("quantity") or 0)
            positions.append({
                "symbol":       sym,
                "broker":       broker,
                "account":      label,
                "display_name": display,
                "mode":         mode,
                "qty":          qty,
                "side":         "long" if qty >= 0 else "short",
            })
    return {"positions": positions}


@app.get("/api/broker/webull/subscriptions")
async def get_webull_subscriptions(token: str = ""):
    """Fetch all Webull account subscriptions from the developer API."""
    check_token(token)
    import aiohttp as _aiohttp
    import base64 as _b64, hashlib as _hl, hmac as _hmac, uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz
    from urllib.parse import quote as _quote

    env     = _read_env_file()
    api_key = env.get("WEBULL_API_KEY") or os.getenv("WEBULL_API_KEY", "")
    secret  = env.get("WEBULL_SECRET_KEY") or os.getenv("WEBULL_SECRET_KEY", "")

    if not api_key or _is_placeholder(api_key):
        raise HTTPException(status_code=400, detail="WEBULL_API_KEY not configured")
    if not secret or _is_placeholder(secret):
        raise HTTPException(status_code=400, detail="WEBULL_SECRET_KEY not configured")

    path  = "/app/subscriptions/list"
    host  = "api.webull.com"
    nonce = str(_uuid.uuid4())
    ts    = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sign_params = {
        "x-app-key":             api_key,
        "x-timestamp":           ts,
        "x-signature-version":   "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     nonce,
        "host":                  host,
    }
    sts = path + "&" + "&".join(f"{k}={v}" for k, v in sorted(sign_params.items()))
    sig = _b64.b64encode(
        _hmac.new((secret + "&").encode(), _quote(sts, safe="").encode(), _hl.sha1).digest()
    ).decode()

    headers = {
        "x-app-key":             api_key,
        "x-signature":           sig,
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-version":   "1.0",
        "x-signature-nonce":     nonce,
        "x-timestamp":           ts,
        "Accept":                "application/json",
    }

    try:
        async with _aiohttp.ClientSession() as s:
            async with s.get(
                f"https://{host}{path}",
                headers=headers,
                timeout=_aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    msg = data.get("msg") or data.get("message") or f"HTTP {r.status}" if isinstance(data, dict) else f"HTTP {r.status}"
                    raise HTTPException(status_code=r.status, detail=str(msg))
                accounts = data if isinstance(data, list) else data.get("items", data.get("data", []))
                return {"accounts": accounts, "count": len(accounts)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Strategy Engineer — AI chat ──────────────────────────────────────────────

class StrategyMessage(BaseModel):
    message: str
    history: list = []
    strategy_text: str = ""

_STRATEGY_ENGINEER_SYSTEM = """\
You are an expert quantitative strategy engineer for the OpenTrader platform.
Your job is to help design, refine, and document algorithmic trading strategies.

A strategy document can describe a pure entry, a pure exit, or a complete strategy.
Produce a strategy document in the right panel using this exact format:

---STRATEGY---
Name: <strategy name>
Type: entry | exit | full
Asset Class: equity | etf | options        (entry and full only — omit for exit)
Direction: long | short | both             (entry and full only — omit for exit)
Min Confidence: <0.50–1.00>               (entry and full only — omit for exit)
Max Position USD: <dollar amount>          (entry and full only — omit for exit)
Stop Loss %: <value>                       (exit and full only — omit for entry)
Take Profit %: <value>                     (exit and full only — omit for entry)
Entry Signals: <comma-separated sources>   (entry and full only — omit for exit)
Indicators: <TradingView indicators used>
Risk Controls:
  Max Slippage %: <max bid-ask spread as % of mid, e.g. 0.5>   (0 = disabled)
  Min Volume K:   <minimum avg daily volume in thousands, e.g. 100>  (0 = disabled)
Hypothesis: <1-3 sentences describing the edge>
Rules:
  - <rule 1>
  - <rule 2>
Notes: <any additional context>
---END---

Type guidance:
- entry  — Defines when to open a position. Include Asset Class, Direction, Min Confidence,
           Max Position USD, and Entry Signals. Omit Stop Loss and Take Profit.
- exit   — Defines when to close a position. Include Stop Loss, Take Profit, and exit rules.
           Omit Asset Class, Direction, Min Confidence, Max Position USD, and Entry Signals.
- full   — A complete self-contained strategy with both entry and exit logic. Include all fields.

Risk Controls guidance:
- Max Slippage %: blocks trades when the bid-ask spread exceeds this % of the mid price.
  Tight spreads indicate liquid markets. Typical values: 0.25–1.0 for large-caps, 1.0–3.0 for
  small-caps. Set to 0 to disable.
- Min Volume K: blocks trades in stocks with less than this many thousand shares of average daily
  volume. Typical values: 50–500 for equity strategies. Set to 0 to disable.

Guidelines:
- Use OpenTrader's available signal sources: ovtlyr, wsb_sentiment, seekalpha, yahoo_finance
- Entry signals must be quantifiable and testable
- Reference TradingView indicators where relevant
- Keep rules concise and implementable
- For full and exit strategies, always define a stop loss and a take profit
- For entry and full strategies, always specify the asset class and direction
- Always include Risk Controls with appropriate values for the strategy's target universe
- If live market data is provided, incorporate it into your analysis\
"""

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

    # Load user exclusions from Redis
    exclusion_prompt = ""
    try:
        excl_raw = await redis.get("user:exclusions")
        if excl_raw:
            excl = json.loads(excl_raw)
            excl_sectors    = excl.get("sectors",    [])
            excl_industries = excl.get("industries", [])
            excl_tickers    = excl.get("tickers",    [])
            parts = []
            if excl_sectors:    parts.append(f"Excluded sectors: {', '.join(excl_sectors)}")
            if excl_industries: parts.append(f"Excluded industries: {', '.join(excl_industries)}")
            if excl_tickers:    parts.append(f"Excluded tickers: {', '.join(excl_tickers)}")
            if parts:
                exclusion_prompt = (
                    "\n\nUSER EXCLUSIONS — MANDATORY: The user has configured the following "
                    "exclusions that MUST be respected in ALL strategies. Never recommend, "
                    "include, or analyze any excluded sector, industry, or ticker:\n"
                    + "\n".join(parts)
                )
    except Exception:
        pass

    system_prompt = _STRATEGY_ENGINEER_SYSTEM + exclusion_prompt + (
        f"\n\nLive market context:{tv_context}" if tv_context else ""
    )
    if body.strategy_text.strip():
        system_prompt += (
            "\n\nThe user currently has this strategy document open in their editor:\n"
            f"{body.strategy_text}\n\n"
            "CRITICAL: Whenever the user asks to add, modify, or refine ANY element of this "
            "strategy, you MUST respond by emitting the COMPLETE updated strategy document in "
            "the ---STRATEGY---...---END--- format with every field appropriate to its Type. "
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

    # Load user exclusions from Redis
    exclusion_prompt = ""
    try:
        excl_raw = await redis.get("user:exclusions")
        if excl_raw:
            excl = json.loads(excl_raw)
            excl_sectors    = excl.get("sectors",    [])
            excl_industries = excl.get("industries", [])
            excl_tickers    = excl.get("tickers",    [])
            parts = []
            if excl_sectors:    parts.append(f"Excluded sectors: {', '.join(excl_sectors)}")
            if excl_industries: parts.append(f"Excluded industries: {', '.join(excl_industries)}")
            if excl_tickers:    parts.append(f"Excluded tickers: {', '.join(excl_tickers)}")
            if parts:
                exclusion_prompt = (
                    "\n\nUSER EXCLUSIONS — MANDATORY: The user has configured the following "
                    "exclusions that MUST be respected in ALL strategies. Never recommend, "
                    "include, or analyze any excluded sector, industry, or ticker:\n"
                    + "\n".join(parts)
                )
    except Exception:
        pass

    system_prompt = _STRATEGY_ENGINEER_SYSTEM + exclusion_prompt + (
        f"\n\nLive market context:{tv_context}" if tv_context else ""
    )
    if body.strategy_text.strip():
        system_prompt += (
            "\n\nThe user currently has this strategy document open in their editor:\n"
            f"{body.strategy_text}\n\n"
            "CRITICAL: Whenever the user asks to add, modify, or refine ANY element of this "
            "strategy, you MUST respond by emitting the COMPLETE updated strategy document in "
            "the ---STRATEGY---...---END--- format with every field appropriate to its Type. "
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


# ── Mentor AI chat ───────────────────────────────────────────────────────────

class MentorMessage(BaseModel):
    message:      str
    history:      list = []
    account_label: str = ""
    positions:    list = []   # enriched position dicts from frontend

@app.post("/api/mentor/chat/stream")
async def mentor_chat_stream(body: MentorMessage, token: str = ""):
    check_token(token)

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    if not openrouter_key or openrouter_key.startswith("your_"):
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY not configured")

    # Build positions context block
    pos_lines = []
    for p in body.positions[:30]:
        sym  = p.get("symbol", "?")
        qty  = p.get("qty") or p.get("quantity") or "?"
        mv   = p.get("market_value")
        pl   = p.get("unrealized_pl") or p.get("unrealized_profit_loss")
        cost = p.get("avg_entry_price") or p.get("cost_price")
        cur  = p.get("current_price") or p.get("last_price")
        sec  = p.get("sector", "")
        parts = [f"{sym} qty={qty}"]
        if cost:  parts.append(f"entry=${float(cost):.2f}")
        if cur:   parts.append(f"last=${float(cur):.2f}")
        if mv:    parts.append(f"mv=${float(mv):,.0f}")
        if pl is not None: parts.append(f"uPnL=${float(pl):+,.2f}")
        if sec:   parts.append(f"sector={sec}")
        pos_lines.append("  " + "  ".join(parts))
    pos_context = "\n".join(pos_lines) if pos_lines else "  (no open positions)"

    # Load user exclusions
    exclusion_prompt = ""
    try:
        excl_raw = await redis.get("user:exclusions")
        if excl_raw:
            excl = json.loads(excl_raw)
            excl_sectors = excl.get("sectors", [])
            excl_tickers = excl.get("tickers", [])
            parts = []
            if excl_sectors:    parts.append(f"Excluded sectors: {', '.join(excl_sectors)}")
            if excl_industries: parts.append(f"Excluded industries: {', '.join(excl_industries)}")
            if excl_tickers:    parts.append(f"Excluded tickers: {', '.join(excl_tickers)}")
            if parts:
                exclusion_prompt = (
                    "\n\nUSER EXCLUSIONS — MANDATORY: Never recommend any excluded sector, industry, or ticker:\n"
                    + "\n".join(parts)
                )
    except Exception:
        pass

    acct_name = body.account_label or "this account"
    system_prompt = f"""You are a trading mentor and portfolio coach for the OpenTrader platform.
You are reviewing the portfolio for account: {acct_name}

Current open positions:
{pos_context}

Your role is to:
- Provide clear, actionable mentorship on open positions
- Identify risk concentrations, sector exposure, and P&L patterns
- Suggest entry/exit timing, position sizing adjustments, and risk management
- Explain trading concepts when asked
- Flag positions showing significant unrealized loss or unusual behavior
- Give honest, direct feedback — do not sugarcoat risks

Communication style:
- Be concise and specific — reference actual positions and numbers
- Use plain language, avoid jargon unless the user seems experienced
- When recommending an action, explain the reasoning briefly
- Always note when a recommendation depends on information you don't have (e.g., user's time horizon, risk tolerance)

Do NOT produce strategy documents or code. Focus on mentoring the trader on their current book.""" + exclusion_prompt

    messages = [{"role": "system", "content": system_prompt}]
    for h in body.history[-12:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": body.message})

    async def event_stream():
        import aiohttp as _aiohttp
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
                        "max_tokens":  1200,
                        "temperature": 0.4,
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
                        if not line.startswith("data: "): continue
                        payload = line[6:]
                        if payload == "[DONE]": break
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

MENTOR_SESSIONS_DIR = "/app/config/mentor_sessions"

class MentorSessionBody(BaseModel):
    account_label: str
    history:       list = []
    positions:     list = []

@app.post("/api/mentor/save-session")
async def save_mentor_session(body: MentorSessionBody, token: str = ""):
    check_token(token)
    os.makedirs(MENTOR_SESSIONS_DIR, exist_ok=True)
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', body.account_label)[:40] or "account"
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(MENTOR_SESSIONS_DIR, f"mentor_{safe}_{ts}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({
            "account_label": body.account_label,
            "saved_at":      datetime.utcnow().isoformat(),
            "history":       body.history,
            "positions":     body.positions,
        }, f, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "path": path}

@app.get("/api/mentor/sessions")
async def list_mentor_sessions(account_label: str = "", token: str = ""):
    check_token(token)
    os.makedirs(MENTOR_SESSIONS_DIR, exist_ok=True)
    files = sorted(os.listdir(MENTOR_SESSIONS_DIR), reverse=True)
    sessions = []
    for fn in files:
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(MENTOR_SESSIONS_DIR, fn)) as f:
                d = json.load(f)
            if account_label and d.get("account_label") != account_label:
                continue
            sessions.append({
                "filename":      fn,
                "account_label": d.get("account_label",""),
                "saved_at":      d.get("saved_at",""),
                "message_count": len(d.get("history",[])),
            })
        except Exception:
            pass
    return sessions

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


# ── Strategy Assignments ──────────────────────────────────────────────────────

def _read_assignments() -> list:
    try:
        with open(ASSIGNMENTS_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def _write_assignments(assignments: list):
    tmp = ASSIGNMENTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(assignments, f, indent=2)
    os.replace(tmp, ASSIGNMENTS_PATH)

def _read_exclusions() -> dict:
    try:
        with open(EXCLUSIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return {"sectors": [], "tickers": []}

def _write_exclusions(excl: dict):
    tmp = EXCLUSIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(excl, f, indent=2)
    os.replace(tmp, EXCLUSIONS_PATH)

def _enrich_assignments(assignments: list, strategies: list) -> list:
    """Attach latest_version and update_available flags from the strategy library."""
    lib = {s["family_id"]: s for s in strategies}
    enriched = []
    for a in assignments:
        a = dict(a)
        lib_entry = lib.get(a.get("strategy_family_id", ""))
        if lib_entry:
            a["latest_version"]   = lib_entry.get("version", 1)
            a["strategy_name"]    = lib_entry.get("name", a.get("strategy_name", ""))
            a["update_available"] = a.get("pinned_version", 1) < lib_entry.get("version", 1)
        else:
            a["latest_version"]   = a.get("pinned_version", 1)
            a["update_available"] = False
        enriched.append(a)
    return enriched

class AssignmentBody(BaseModel):
    account_label:      str
    broker:             str
    mode:               str
    strategy_family_id: str
    strategy_name:      str
    pinned_version:     int = 1

class ExclusionsBody(BaseModel):
    sectors: list = []
    tickers: list = []

@app.get("/api/assignments")
async def get_assignments():
    assignments = _read_assignments()
    strategies  = _read_strategies()
    return _enrich_assignments(assignments, strategies)

@app.post("/api/assignments")
async def create_assignment(body: AssignmentBody, token: str = ""):
    check_token(token)
    assignments = _read_assignments()

    # Prevent duplicate: same account + same strategy
    for a in assignments:
        if (a["account_label"] == body.account_label and
                a["strategy_family_id"] == body.strategy_family_id and
                a.get("status") != "inactive"):
            raise HTTPException(status_code=409,
                detail="Strategy already assigned to this account")

    import uuid as _uuid
    new = {
        "id":                 str(_uuid.uuid4()),
        "account_label":      body.account_label,
        "broker":             body.broker,
        "mode":               body.mode,
        "strategy_family_id": body.strategy_family_id,
        "strategy_name":      body.strategy_name,
        "pinned_version":     body.pinned_version,
        "status":             "active",
        "assigned_at":        datetime.utcnow().isoformat() + "Z",
        "updated_at":         datetime.utcnow().isoformat() + "Z",
    }
    assignments.append(new)
    _write_assignments(assignments)
    strategies = _read_strategies()
    return _enrich_assignments([new], strategies)[0]

class AssignmentPatch(BaseModel):
    status:         str | None = None
    pinned_version: int | None = None

@app.patch("/api/assignments/{assignment_id}")
async def patch_assignment(assignment_id: str, body: AssignmentPatch, token: str = ""):
    check_token(token)
    assignments = _read_assignments()
    idx = next((i for i, a in enumerate(assignments) if a["id"] == assignment_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    a = dict(assignments[idx])
    if body.status is not None:
        a["status"] = body.status
    if body.pinned_version is not None:
        a["pinned_version"] = body.pinned_version
    a["updated_at"] = datetime.utcnow().isoformat() + "Z"
    assignments[idx] = a
    _write_assignments(assignments)
    strategies = _read_strategies()
    return _enrich_assignments([a], strategies)[0]

@app.delete("/api/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str, token: str = ""):
    check_token(token)
    assignments = _read_assignments()
    before = len(assignments)
    assignments = [a for a in assignments if a["id"] != assignment_id]
    if len(assignments) == before:
        raise HTTPException(status_code=404, detail="Assignment not found")
    _write_assignments(assignments)
    return {"ok": True}

@app.get("/api/assignments/exclusions")
async def get_exclusions():
    return _read_exclusions()

@app.post("/api/assignments/exclusions")
async def save_exclusions(body: ExclusionsBody, token: str = ""):
    check_token(token)
    excl = {
        "sectors": [s.strip() for s in body.sectors if s.strip()],
        "tickers": [t.strip().upper() for t in body.tickers if t.strip()],
    }
    _write_exclusions(excl)
    return excl

@app.get("/api/assignments/conflicts")
async def check_conflicts(account_label: str, family_id: str):
    """Return tickers that would conflict (traded by another active strategy on same account)."""
    assignments = _read_assignments()
    strategies  = _read_strategies()
    lib = {s["family_id"]: s for s in strategies}

    # Active assignments on this account excluding the strategy being checked
    active = [a for a in assignments
              if a["account_label"] == account_label
              and a["strategy_family_id"] != family_id
              and a.get("status") == "active"]

    # Collect tickers currently in positions for those strategies
    # (placeholder — in practice would query broker positions)
    conflicts = []
    for a in active:
        entry = lib.get(a["strategy_family_id"], {})
        conflicts.append({
            "strategy": entry.get("name", a["strategy_family_id"]),
            "note": "Active on same account",
        })
    return {"account_label": account_label, "conflicts": conflicts}


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


# ── Real Backtrader Backtesting ───────────────────────────────────────────────

class BacktestRunBody(BaseModel):
    ticker:          str
    period:          str   = "2y"
    initial_capital: float = 10_000.0
    token:           str   = ""


def _bt_run_in_process(params: dict) -> dict:
    """Top-level wrapper so ProcessPoolExecutor can pickle it."""
    from webui.backtest_runner import run_backtest
    return run_backtest(params)


async def _run_backtest_task(job_id: str, version_dict: dict, body: BacktestRunBody,
                              family_id: str, version: int):
    _bt_jobs[job_id]["status"] = "running"
    params = {
        "ticker":           body.ticker,
        "period":           body.period,
        "stop_pct":         version_dict.get("stop_pct",   1.5),
        "tp_pct":           version_dict.get("tp_pct",     3.0),
        "confidence":       version_dict.get("confidence", 0.70),
        "direction":        version_dict.get("direction",  "long"),
        "max_pos":          version_dict.get("max_pos") or 500,
        "initial_capital":  body.initial_capital,
    }
    try:
        loop = asyncio.get_event_loop()
        with ProcessPoolExecutor(max_workers=1) as pool:
            results = await loop.run_in_executor(pool, _bt_run_in_process, params)
        versions = _read_versions(family_id)
        target   = next((v for v in versions if v["version"] == version), None)
        if target:
            target["backtest_results"] = results
            target["backtest_run_at"]  = datetime.now(timezone.utc).isoformat()
            _write_versions(family_id, versions)
        _bt_jobs[job_id]["status"]  = "done"
        _bt_jobs[job_id]["results"] = results
    except Exception as e:
        log.error("backtest.task_error", job_id=job_id, error=str(e))
        _bt_jobs[job_id]["status"] = "error"
        _bt_jobs[job_id]["error"]  = str(e)


@app.post("/api/strategies/{family_id}/versions/{version}/backtest/run")
async def run_version_backtest(family_id: str, version: int, body: BacktestRunBody):
    check_token(body.token)
    versions = _read_versions(family_id)
    target   = next((v for v in versions if v["version"] == version), None)
    if not target:
        raise HTTPException(status_code=404, detail="Version not found")
    job_id = str(uuid.uuid4())
    _bt_jobs[job_id] = {"status": "queued", "family_id": family_id, "version": version}
    asyncio.create_task(_run_backtest_task(job_id, target, body, family_id, version))
    return {"job_id": job_id}


@app.get("/api/strategies/{family_id}/backtest/status/{job_id}")
async def backtest_status_stream(family_id: str, job_id: str, token: str = ""):
    check_token(token)

    async def _stream():
        yield f"data: {json.dumps({'type': 'started', 'job_id': job_id})}\n\n"
        pct = 0
        messages = ["Fetching OHLCV data…", "Running strategy…", "Computing metrics…",
                    "Generating charts…", "Saving results…"]
        msg_idx = 0
        while True:
            await asyncio.sleep(1.5)
            job = _bt_jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
                return
            if job["status"] == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': job.get('error', 'Unknown error')})}\n\n"
                return
            if job["status"] == "done":
                r = job["results"]
                # Strip large chart PNG from SSE payload — client fetches modal separately
                summary = {k: v for k, v in r.items() if k not in ("chart_png_b64", "trade_log")}
                summary["trade_count"] = len(r.get("trade_log", []))
                yield f"data: {json.dumps({'type': 'done', 'results': summary})}\n\n"
                return
            # Progress heartbeat
            pct = min(pct + 15, 85)
            msg = messages[min(msg_idx, len(messages) - 1)]
            msg_idx += 1
            yield f"data: {json.dumps({'type': 'progress', 'pct': pct, 'message': msg})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/strategies/{family_id}/versions/{version}/backtest/trades.csv")
async def download_trades_csv(family_id: str, version: int, token: str = ""):
    check_token(token)
    versions = _read_versions(family_id)
    target   = next((v for v in versions if v["version"] == version), None)
    if not target or not target.get("backtest_results"):
        raise HTTPException(status_code=404, detail="No backtest results for this version")
    trade_log = target["backtest_results"].get("trade_log", [])
    buf = io.StringIO()
    if trade_log:
        writer = csv.DictWriter(buf, fieldnames=list(trade_log[0].keys()))
        writer.writeheader()
        writer.writerows(trade_log)
    else:
        buf.write("No trades recorded\n")
    filename = f"backtest_{family_id[:8]}_v{version}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/strategies/{family_id}/versions/{version}/backtest/trades.pdf")
async def download_trades_pdf(family_id: str, version: int, token: str = ""):
    check_token(token)
    versions = _read_versions(family_id)
    target   = next((v for v in versions if v["version"] == version), None)
    if not target or not target.get("backtest_results"):
        raise HTTPException(status_code=404, detail="No backtest results for this version")
    r         = target["backtest_results"]
    trade_log = r.get("trade_log", [])
    pdf_bytes = _build_trades_pdf(trade_log, r, family_id, version,
                                  target.get("backtest_run_at", ""))
    filename  = f"backtest_{family_id[:8]}_v{version}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_trades_pdf(trade_log: list, results: dict, family_id: str,
                      version: int, run_at: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable)

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                leftMargin=12*mm, rightMargin=12*mm,
                                topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    h1     = ParagraphStyle("h1", fontSize=14, fontName="Helvetica-Bold",
                             textColor=colors.HexColor("#e2e8f0"))
    sub    = ParagraphStyle("sub", fontSize=9, fontName="Helvetica",
                             textColor=colors.HexColor("#94a3b8"))
    story  = []

    ticker  = results.get("ticker", "")
    period  = results.get("period", "")
    run_str = run_at[:19].replace("T", " ") if run_at else "unknown"
    story.append(Paragraph(f"Backtest Trade Log — {ticker} v{version}", h1))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f"Period: {period}  ·  Run: {run_str}  ·  "
        f"Total Return: {results.get('total_return', 0):+.2f}%  ·  "
        f"Trades: {results.get('total_trades', 0)}  ·  "
        f"Win Rate: {results.get('win_rate', 0):.1f}%  ·  "
        f"Sharpe: {results.get('sharpe', 0):.3f}  ·  "
        f"Max DD: {results.get('max_drawdown', 0):.2f}%", sub))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#334155")))
    story.append(Spacer(1, 4*mm))

    cols = ["#", "Entry Date", "Exit Date", "Ticker", "Dir",
            "Entry $", "Exit $", "Qty", "P&L $", "P&L %", "Exit Reason"]
    rows = [cols]
    total_pnl = 0.0
    for i, t in enumerate(trade_log, 1):
        pnl = t.get("pnl", 0) or 0
        total_pnl += pnl
        rows.append([
            str(i),
            t.get("entry_date", ""),
            t.get("exit_date",  ""),
            t.get("ticker",     ""),
            t.get("direction",  "long").upper(),
            f"${t.get('entry_price', 0):,.4f}",
            f"${t.get('exit_price',  0):,.4f}",
            str(t.get("qty", 0)),
            f"${pnl:+,.2f}",
            f"{t.get('pnl_pct', 0):+.2f}%",
            t.get("exit_reason", ""),
        ])
    # Summary footer row
    rows.append(["", "", "", "", "TOTAL", "", "", "",
                 f"${total_pnl:+,.2f}", "", ""])

    col_widths = [18, 62, 62, 42, 28, 62, 62, 28, 62, 52, 60]
    tbl = Table(rows, colWidths=[w*mm for w in col_widths], repeatRows=1)
    dark_bg   = colors.HexColor("#0f172a")
    row_alt   = colors.HexColor("#1e293b")
    header_bg = colors.HexColor("#1e3a5f")
    green     = colors.HexColor("#4ade80")
    red       = colors.HexColor("#f87171")
    muted     = colors.HexColor("#94a3b8")

    style = [
        ("BACKGROUND",  (0, 0), (-1, 0),  header_bg),
        ("TEXTCOLOR",   (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",    (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0),  8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [dark_bg, row_alt]),
        ("TEXTCOLOR",   (0, 1), (-1, -1), muted),
        ("FONTNAME",    (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",    (0, 1), (-1, -1), 7.5),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("ALIGN",       (1, 1), (2, -1),  "CENTER"),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0,0), (-1, -1), 3),
        ("GRID",        (0, 0), (-1, -1), 0.3, colors.HexColor("#334155")),
        ("BACKGROUND",  (0, -1), (-1, -1), colors.HexColor("#1e3a5f")),
        ("FONTNAME",    (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR",   (0, -1), (-1, -1), colors.white),
    ]
    # Colour P&L column by sign
    for i, t in enumerate(trade_log, 1):
        pnl = t.get("pnl", 0) or 0
        c   = green if pnl >= 0 else red
        style.append(("TEXTCOLOR", (8, i), (9, i), c))

    tbl.setStyle(TableStyle(style))
    story.append(tbl)

    doc.build(story)
    return buf.getvalue()


@app.post("/api/backtest/quick")
async def quick_backtest(body: BacktestRunBody):
    """Run a backtest without a saved strategy version (used by AI Engineer panel)."""
    check_token(body.token)
    params = {
        "ticker":          body.ticker,
        "period":          body.period,
        "stop_pct":        1.5,
        "tp_pct":          3.0,
        "confidence":      0.70,
        "direction":       "long",
        "max_pos":         500,
        "initial_capital": body.initial_capital,
    }
    loop = asyncio.get_event_loop()
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            results = await asyncio.wait_for(
                loop.run_in_executor(pool, _bt_run_in_process, params),
                timeout=120,
            )
        return results
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Backtest timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class QuickBacktestPdfBody(BaseModel):
    results: dict
    token:   str = ""

@app.post("/api/backtest/quick/trades.pdf")
async def quick_backtest_pdf(body: QuickBacktestPdfBody):
    """Generate a PDF trade report from quick (unsaved) backtest results."""
    check_token(body.token)
    r         = body.results
    trade_log = r.get("trade_log", [])
    pdf_bytes = _build_trades_pdf(trade_log, r, "ai", 0, r.get("period", ""))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="backtest_ai.pdf"'},
    )


# ── Stock Search ──────────────────────────────────────────────────────────────

@app.get("/api/search/stocks")
async def search_stocks(q: str = "", token: str = ""):
    check_token(token)
    if not q.strip():
        return []
    import urllib.request as _req
    import urllib.parse   as _parse
    import asyncio        as _asyncio

    # ── Industry keyword → major tickers map ──────────────────────────────────
    _INDUSTRY_MAP = {
        "automotive":     ["F","GM","TSLA","TM","STLA","HMC","RIVN","LCID","NIO","LI","XPEV","RACE"],
        "auto":           ["F","GM","TSLA","TM","STLA","HMC","RIVN","LCID"],
        "car":            ["F","GM","TSLA","TM","STLA","HMC","RIVN"],
        "truck":          ["F","GM","PCAR","CMI","NAV","WKHS"],
        "electric vehicle":["TSLA","RIVN","LCID","NIO","LI","XPEV","FSR"],
        "ev":             ["TSLA","RIVN","LCID","NIO","LI","XPEV","FSR"],
        "airline":        ["AAL","UAL","DAL","LUV","ALK","JBLU","SAVE","HA","ULCC"],
        "airlines":       ["AAL","UAL","DAL","LUV","ALK","JBLU","SAVE","HA"],
        "bank":           ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF"],
        "banks":          ["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF"],
        "semiconductor":  ["NVDA","AMD","INTC","QCOM","AVGO","MU","AMAT","LRCX","KLAC","TSM","MRVL","ON"],
        "chip":           ["NVDA","AMD","INTC","QCOM","AVGO","MU","AMAT","LRCX"],
        "tech":           ["AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","ORCL","CRM","ADBE"],
        "technology":     ["AAPL","MSFT","GOOGL","AMZN","META","NVDA","ORCL","CRM","ADBE","IBM"],
        "oil":            ["XOM","CVX","COP","OXY","EOG","PSX","VLO","SLB","MPC","HES"],
        "energy":         ["XOM","CVX","COP","OXY","EOG","NEE","D","SO","DUK","AEP"],
        "pharma":         ["JNJ","PFE","MRK","ABBV","BMY","LLY","AMGN","GILD","BIIB","REGN"],
        "pharmaceutical": ["JNJ","PFE","MRK","ABBV","BMY","LLY","AMGN","GILD","BIIB","REGN"],
        "biotech":        ["AMGN","GILD","BIIB","REGN","VRTX","MRNA","BNTX","ILMN","SGEN"],
        "retail":         ["WMT","AMZN","COST","TGT","HD","LOW","TJX","ROST","KR","DG"],
        "insurance":      ["BRK-B","MET","PRU","AFL","AIG","CB","ALL","HIG","TRV","PGR"],
        "defense":        ["LMT","RTX","NOC","GD","BA","HII","L3H","LDOS","SAIC","KTOS"],
        "aerospace":      ["BA","LMT","RTX","NOC","GD","HII","TDG","SPR","AXON"],
        "media":          ["DIS","NFLX","PARA","WBD","FOX","CMCSA","NYT","AMC"],
        "streaming":      ["NFLX","DIS","PARA","WBD","ROKU","SPOT","FUBO"],
        "mining":         ["BHP","RIO","NEM","FCX","GOLD","AA","CLF","MP","VALE"],
        "real estate":    ["AMT","PLD","CCI","EQIX","SPG","O","WELL","DLR","PSA","AVB"],
        "reit":           ["AMT","PLD","CCI","EQIX","SPG","O","WELL","DLR","PSA","AVB"],
        "telecom":        ["VZ","T","TMUS","LUMN","DISH","SHEN"],
        "cloud":          ["AMZN","MSFT","GOOGL","CRM","SNOW","MDB","DDOG","NET","ZS"],
        "cybersecurity":  ["CRWD","PANW","ZS","FTNT","NET","S","OKTA","SAIL","TPVG"],
        "crypto":         ["COIN","MSTR","MARA","RIOT","HUT","CLSK","BTBT"],
        "restaurant":     ["MCD","SBUX","CMG","YUM","QSR","DPZ","WEN","JACK","DENN"],
        "food":           ["KO","PEP","MDLZ","GIS","K","HSY","SJM","CAG","MKC","CPB"],
        "healthcare":     ["UNH","JNJ","ABBV","MRK","LLY","CVS","CI","HUM","CNC","ELV"],
        "hospital":       ["HCA","UHS","THC","CYH","ENSG","AMED","SGRY"],
        "shipping":       ["ZIM","DAC","MATX","GSL","SFL","EGLE","SBLK","NMM"],
        "railroad":       ["UNP","CSX","NSC","CP","CNI","WAB","KSU"],
        "logistics":      ["UPS","FDX","XPO","SAIA","ODFL","JBHT","KNX","CHRW"],
    }

    def _yf_search(query: str, count: int = 30) -> list:
        url = (
            "https://query2.finance.yahoo.com/v1/finance/search"
            f"?q={_parse.quote(query)}&quotesCount={count}&newsCount=0&enableFuzzyQuery=false"
        )
        try:
            r = _req.urlopen(_req.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=8)
            return json.loads(r.read()).get("quotes", [])
        except Exception:
            return []

    def _filter_quote(qt: dict) -> dict | None:
        sym = qt.get("symbol", "")
        if qt.get("quoteType") not in ("EQUITY", "ETF"):
            return None
        if any(c in sym for c in ("^", "=", ".")):
            return None
        return {
            "symbol":   sym,
            "name":     qt.get("shortname") or qt.get("longname") or sym,
            "exchange": qt.get("exchDisp", ""),
            "type":     qt.get("quoteType", ""),
        }

    # Primary text search
    seen:    set[str] = set()
    results: list     = []

    for qt in _yf_search(q):
        r = _filter_quote(qt)
        if r and r["symbol"] not in seen:
            seen.add(r["symbol"])
            results.append(r)

    # Industry keyword expansion
    q_lower = q.strip().lower()
    extra_tickers = []
    for kw, tickers in _INDUSTRY_MAP.items():
        if kw in q_lower or q_lower in kw:
            extra_tickers.extend(tickers)

    # Fetch info for any extra tickers not already in results
    missing = [t for t in dict.fromkeys(extra_tickers) if t not in seen]
    for ticker in missing:
        for qt in _yf_search(ticker, count=3):
            if qt.get("symbol") == ticker:
                r = _filter_quote(qt)
                if r and r["symbol"] not in seen:
                    seen.add(r["symbol"])
                    results.append(r)
                break

    return results


# ── User Exclusions ───────────────────────────────────────────────────────────

USER_EXCLUSIONS_KEY = "user:exclusions"

# Map legacy S&P/MSCI names → Yahoo Finance GICS names
_SECTOR_LEGACY_MAP = {
    "Health Care":            "Healthcare",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Staples":       "Consumer Defensive",
    "Information Technology": "Technology",
    "Financials":             "Financial Services",
    "Materials":              "Basic Materials",
}

def _normalize_sectors(sectors: list) -> list:
    return [_SECTOR_LEGACY_MAP.get(s, s) for s in sectors]

@app.get("/api/user/exclusions")
async def get_user_exclusions(token: str = ""):
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(USER_EXCLUSIONS_KEY)
    if raw:
        data = json.loads(raw)
        data["sectors"] = _normalize_sectors(data.get("sectors", []))
        return data
    return {"sectors": [], "industries": [], "tickers": [], "ticker_meta": {}}


async def _merge_exclusions(redis, patch: dict) -> dict:
    raw = await redis.get(USER_EXCLUSIONS_KEY)
    current = json.loads(raw) if raw else {"sectors": [], "industries": [], "tickers": [], "ticker_meta": {}}
    if "sectors" in patch:
        patch["sectors"] = _normalize_sectors(patch["sectors"])
    current.update(patch)
    await redis.set(USER_EXCLUSIONS_KEY, json.dumps(current))
    return current


@app.post("/api/user/exclusions/sectors")
async def save_exclusion_sectors(body: dict, token: str = ""):
    check_token(token)
    redis = await get_redis()
    sectors = [s.strip() for s in body.get("sectors", []) if s.strip()]
    return await _merge_exclusions(redis, {"sectors": sectors})


@app.post("/api/user/exclusions/industries")
async def save_exclusion_industries(body: dict, token: str = ""):
    check_token(token)
    redis = await get_redis()
    industries = [i.strip() for i in body.get("industries", []) if i.strip()]
    return await _merge_exclusions(redis, {"industries": industries})


@app.post("/api/user/exclusions/tickers")
async def save_exclusion_tickers(body: dict, token: str = ""):
    check_token(token)
    redis = await get_redis()
    tickers     = [t.strip().upper() for t in body.get("tickers", []) if t.strip()]
    ticker_meta = {k.upper(): v for k, v in (body.get("ticker_meta") or {}).items()}
    return await _merge_exclusions(redis, {"tickers": tickers, "ticker_meta": ticker_meta})


# ── Risk Controls ────────────────────────────────────────────────────────────

_RISK_CONTROLS_KEY     = "config:risk_controls"
_RISK_CONTROLS_DEFAULT = {"max_slippage_pct": 0.0, "min_volume_k": 0.0}


@app.get("/api/config/risk-controls")
async def get_risk_controls_api(token: str = ""):
    check_token(token)
    redis = await get_redis()
    try:
        raw = await redis.get(_RISK_CONTROLS_KEY)
        if raw:
            stored = json.loads(raw)
            return {**_RISK_CONTROLS_DEFAULT, **stored}
    except Exception:
        pass
    return dict(_RISK_CONTROLS_DEFAULT)


@app.post("/api/config/risk-controls")
async def save_risk_controls_api(body: dict, token: str = ""):
    check_token(token)
    redis = await get_redis()
    controls = {
        "max_slippage_pct": float(body.get("max_slippage_pct", 0.0)),
        "min_volume_k":     float(body.get("min_volume_k", 0.0)),
    }
    await redis.set(_RISK_CONTROLS_KEY, json.dumps(controls))
    return {"ok": True, **controls}


# ── Trade Directives ──────────────────────────────────────────────────────────

_DIRECTIVES_KEY = "trade:directives"


@app.post("/api/directives/preview")
async def preview_directive(body: dict, token: str = ""):
    """
    Pre-process a directive text through an LLM to confirm it is understood,
    extract structured fields, and surface any ambiguities before saving.
    Returns a preview dict the UI shows for user confirmation.
    """
    check_token(token)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    use_llm = bool(openrouter_key) and not openrouter_key.startswith("your_")

    if not use_llm:
        # No LLM — return a minimal parse so the UI can still proceed
        return {
            "understood":      True,
            "interpretation":  text,
            "tickers":         [],
            "action":          {},
            "issues":          [],
            "warnings":        ["LLM not configured — directive will be saved as-is and evaluated at runtime."],
            "llm_available":   False,
        }

    prompt = f"""You are a trade directive parser for an algorithmic trading platform.

Parse the following natural-language trade directive and return ONLY valid JSON.

Directive: "{text}"

Extract and return:
{{
  "understood": true | false,
  "interpretation": "one sentence plain-English summary of exactly what will happen and when",
  "tickers": ["LIST", "OF", "TICKERS"],
  "action": {{
    "direction": "long" | "sell" | "short" | null,
    "quantity": <integer shares or null>,
    "dollars": <dollar amount or null>,
    "condition": "plain-English description of the trigger condition",
    "order_type": "market" | "limit" | null,
    "limit_price": <number or null>,
    "duration": "gtc" | "today" | "gtc"
  }},
  "issues": ["list any ambiguities, missing details, or reasons the directive cannot execute"],
  "warnings": ["list any non-blocking observations, e.g. risk notes, unclear price levels"]
}}

Direction values:
- "long"  = buy (open or add to a long position)
- "sell"  = sell existing long position (close/reduce — NOT a short sale)
- "short" = sell short (open a short position)

Rules:
- Set understood=false if: ticker is not identifiable, action is contradictory, directive is too vague to execute safely
- Do not invent tickers — only include clearly named ones
- issues are blockers; warnings are informational
- Keep interpretation concise and specific (under 25 words)
- Return raw JSON only, no markdown
"""
    system = "You are a precise trade directive parser. Return only valid JSON."

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                json={
                    "model":       os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-sonnet-4-5"),
                    "messages":    [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "max_tokens":  400,
                    "temperature": 0.1,
                },
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
        reply = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if reply.startswith("```"):
            reply = reply.split("```")[1]
            if reply.startswith("json"):
                reply = reply[4:]
            reply = reply.strip()
        result = json.loads(reply)
        result["llm_available"] = True
        return result
    except Exception as e:
        # LLM call failed — return a safe fallback so the UI can still proceed
        return {
            "understood":     True,
            "interpretation": text,
            "tickers":        [],
            "action":         {},
            "issues":         [],
            "warnings":       [f"LLM parse failed ({str(e)[:80]}) — directive will be evaluated at runtime."],
            "llm_available":  True,
        }


@app.get("/api/directives")
async def get_directives(token: str = ""):
    check_token(token)
    redis = await get_redis()
    try:
        raw = await redis.get(_DIRECTIVES_KEY)
        return json.loads(raw) if raw else []
    except Exception:
        return []


@app.post("/api/directives")
async def create_directive(body: dict, token: str = ""):
    check_token(token)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    redis = await get_redis()
    try:
        raw = await redis.get(_DIRECTIVES_KEY)
        directives = json.loads(raw) if raw else []
    except Exception:
        directives = []
    from datetime import datetime, timezone
    directive = {
        "id":             str(uuid.uuid4()),
        "text":           text,
        "interpretation": (body.get("interpretation") or "").strip() or None,
        "tickers":        body.get("tickers") or [],
        "parsed_action":  body.get("parsed_action") or {},
        "status":         "active",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "executed_at":    None,
        "result":         None,
    }
    directives.append(directive)
    await redis.set(_DIRECTIVES_KEY, json.dumps(directives))
    return directive


@app.delete("/api/directives/{directive_id}")
async def delete_directive(directive_id: str, token: str = ""):
    check_token(token)
    redis = await get_redis()
    try:
        raw = await redis.get(_DIRECTIVES_KEY)
        directives = json.loads(raw) if raw else []
    except Exception:
        directives = []
    new_list = [d for d in directives if d.get("id") != directive_id]
    await redis.set(_DIRECTIVES_KEY, json.dumps(new_list))
    return {"ok": True}


@app.patch("/api/directives/{directive_id}")
async def update_directive(directive_id: str, body: dict, token: str = ""):
    """Cancel or reactivate a directive."""
    check_token(token)
    redis = await get_redis()
    try:
        raw = await redis.get(_DIRECTIVES_KEY)
        directives = json.loads(raw) if raw else []
    except Exception:
        directives = []
    for d in directives:
        if d.get("id") == directive_id:
            if "status" in body:
                d["status"] = body["status"]
            break
    else:
        raise HTTPException(status_code=404, detail="Directive not found")
    await redis.set(_DIRECTIVES_KEY, json.dumps(directives))
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


# ── Library ──────────────────────────────────────────────────────────────────

class LibraryBookBody(BaseModel):
    isbn:           str | None = None
    title:          str
    author:         str | None = None
    description:    str | None = None
    category:       str | None = None
    publisher:      str | None = None
    published_date: str | None = None
    pages:          int | None = None
    cover_url:      str | None = None
    price:          float | None = None
    rating:         int | None = None
    review:         str | None = None
    status:         str = "purchased"
    notes:          str | None = None

class LibraryBookPatch(BaseModel):
    title:          str | None = None
    author:         str | None = None
    description:    str | None = None
    category:       str | None = None
    publisher:      str | None = None
    published_date: str | None = None
    pages:          int | None = None
    cover_url:      str | None = None
    price:          float | None = None
    rating:         int | None = None
    review:         str | None = None
    status:         str | None = None
    notes:          str | None = None

@app.get("/api/library/isbn/{isbn}")
async def lookup_isbn(isbn: str):
    """Fetch book metadata from Open Library (jscmd=data, then search API fallback)."""
    clean = isbn.replace("-", "").replace(" ", "")
    result = {}

    async def _ol_data():
        """Open Library /api/books with full data."""
        import aiohttp as aiohttp_
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{clean}&format=json&jscmd=data"
        async with aiohttp_.ClientSession() as s:
            async with s.get(url, timeout=aiohttp_.ClientTimeout(total=12)) as r:
                data = await r.json(content_type=None)
                entry = data.get(f"ISBN:{clean}", {})
                if not entry:
                    return {}
                authors  = [a.get("name","") for a in entry.get("authors",[])]
                subjects = entry.get("subjects", [])
                category = ""
                if subjects:
                    category = subjects[0].get("name","") if isinstance(subjects[0], dict) else subjects[0]
                publishers = entry.get("publishers", [])
                pub = publishers[0].get("name","") if publishers and isinstance(publishers[0], dict) else (publishers[0] if publishers else "")
                cover = (entry.get("cover",{}) or {})
                return {
                    "isbn":           clean,
                    "title":          entry.get("title",""),
                    "author":         ", ".join(authors),
                    "description":    entry.get("notes","") if isinstance(entry.get("notes"), str) else "",
                    "category":       category,
                    "publisher":      pub,
                    "published_date": entry.get("publish_date",""),
                    "pages":          entry.get("number_of_pages"),
                    "cover_url":      cover.get("large") or cover.get("medium") or
                                      f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg",
                }

    async def _ol_search():
        """Open Library search API — broader coverage."""
        import aiohttp as aiohttp_
        url = f"https://openlibrary.org/search.json?isbn={clean}&limit=1"
        async with aiohttp_.ClientSession() as s:
            async with s.get(url, timeout=aiohttp_.ClientTimeout(total=12)) as r:
                data = await r.json(content_type=None)
                docs = data.get("docs", [])
                if not docs:
                    return {}
                d = docs[0]
                authors = d.get("author_name") or []
                subjects = d.get("subject") or []
                cover_id = d.get("cover_i")
                cover_url = (f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
                             if cover_id else
                             f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg")
                return {
                    "isbn":           clean,
                    "title":          d.get("title",""),
                    "author":         ", ".join(authors),
                    "description":    "",
                    "category":       subjects[0] if subjects else "",
                    "publisher":      (d.get("publisher") or [""])[0],
                    "published_date": str(d.get("first_publish_year","")) if d.get("first_publish_year") else "",
                    "pages":          d.get("number_of_pages_median"),
                    "cover_url":      cover_url,
                }

    async def _google_books():
        """Google Books API — requires GOOGLE_BOOKS_API_KEY in env."""
        import aiohttp as aiohttp_
        api_key = os.getenv("GOOGLE_BOOKS_API_KEY", "")
        if not api_key:
            return {}
        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{clean}&key={api_key}"
        async with aiohttp_.ClientSession() as s:
            async with s.get(url, timeout=aiohttp_.ClientTimeout(total=12)) as r:
                data = await r.json(content_type=None)
                items = data.get("items", [])
                if not items:
                    return {}
                info = items[0].get("volumeInfo", {})
                thumb = info.get("imageLinks", {}).get("thumbnail", "")
                cover = thumb.replace("http://", "https://") if thumb else \
                        f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg"
                return {
                    "isbn":           clean,
                    "title":          info.get("title", ""),
                    "author":         ", ".join(info.get("authors", [])),
                    "description":    info.get("description", ""),
                    "category":       (info.get("categories") or [""])[0],
                    "publisher":      info.get("publisher", ""),
                    "published_date": info.get("publishedDate", ""),
                    "pages":          info.get("pageCount"),
                    "cover_url":      cover,
                }

    # 1. Open Library full data
    try:
        result = await _ol_data()
    except Exception as e:
        log.warning("library.isbn_ol_data_error", isbn=clean, error=str(e))

    # 2. Open Library search fallback
    if not result.get("title"):
        try:
            result = await _ol_search()
        except Exception as e:
            log.warning("library.isbn_ol_search_error", isbn=clean, error=str(e))

    # 3. Google Books fallback (if API key configured)
    if not result.get("title"):
        try:
            result = await _google_books()
        except Exception as e:
            log.warning("library.isbn_google_error", isbn=clean, error=str(e))

    if not result.get("title"):
        raise HTTPException(status_code=404, detail="Book not found for this ISBN")
    return result

@app.get("/api/library/books")
async def list_library_books(sort: str = "title", status: str = "", category: str = ""):
    if not DB_URL:
        return []
    pool = await _get_db_pool()
    where_clauses = []
    args = []
    if status:
        args.append(status); where_clauses.append(f"status = ${len(args)}")
    if category:
        args.append(category); where_clauses.append(f"category = ${len(args)}")
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    order_col = {"title": "title", "author": "author", "category": "category"}.get(sort, "title")
    rows = await pool.fetch(
        f"SELECT * FROM library_books {where} ORDER BY {order_col} ASC NULLS LAST, title ASC",
        *args
    )
    return [dict(r) for r in rows]

@app.get("/api/library/categories")
async def get_library_categories():
    if not DB_URL:
        return []
    pool = await _get_db_pool()
    rows = await pool.fetch("SELECT name FROM library_categories ORDER BY name")
    return [r["name"] for r in rows]

class LibraryCategoryBody(BaseModel):
    name: str

@app.post("/api/library/categories")
async def add_library_category(body: LibraryCategoryBody, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name required")
    pool = await _get_db_pool()
    await pool.execute(
        "INSERT INTO library_categories (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
        name
    )
    rows = await pool.fetch("SELECT name FROM library_categories ORDER BY name")
    return [r["name"] for r in rows]

@app.delete("/api/library/categories/{name}")
async def delete_library_category(name: str, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")
    pool = await _get_db_pool()
    await pool.execute("DELETE FROM library_categories WHERE name = $1", name)
    rows = await pool.fetch("SELECT name FROM library_categories ORDER BY name")
    return [r["name"] for r in rows]

@app.get("/api/library/stats")
async def library_stats():
    if not DB_URL:
        return {"total": 0, "reading": 0, "read": 0, "purchased": 0, "reference": 0, "total_cost": 0.0, "categories": []}
    pool = await _get_db_pool()
    rows = await pool.fetch("SELECT status, COUNT(*) as cnt FROM library_books GROUP BY status")
    counts = {r["status"]: r["cnt"] for r in rows}
    cost_row = await pool.fetchrow("SELECT COALESCE(SUM(price), 0) as total_cost FROM library_books WHERE price IS NOT NULL")
    cats   = await pool.fetch("SELECT name FROM library_categories ORDER BY name")
    return {
        "total":      sum(counts.values()),
        "reading":    counts.get("reading", 0),
        "read":       counts.get("read", 0),
        "purchased":  counts.get("purchased", 0),
        "reference":  counts.get("reference", 0),
        "total_cost": float(cost_row["total_cost"]),
        "categories": [r["name"] for r in cats],
    }

@app.post("/api/library/books")
async def add_library_book(body: LibraryBookBody, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        """INSERT INTO library_books
            (isbn, title, author, description, category, publisher, published_date,
             pages, cover_url, price, rating, review, status, notes)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
           RETURNING *""",
        body.isbn, body.title, body.author, body.description, body.category,
        body.publisher, body.published_date, body.pages, body.cover_url,
        body.price, body.rating, body.review, body.status, body.notes
    )
    if body.category:
        await pool.execute(
            "INSERT INTO library_categories (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            body.category.strip()
        )
    return dict(row)

@app.patch("/api/library/books/{book_id}")
async def update_library_book(book_id: str, body: LibraryBookPatch, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")
    pool = await _get_db_pool()
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    fields["updated_at"] = datetime.utcnow()
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    row = await pool.fetchrow(
        f"UPDATE library_books SET {set_clause} WHERE id = $1 RETURNING *",
        book_id, *fields.values()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    if body.category:
        await pool.execute(
            "INSERT INTO library_categories (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            body.category.strip()
        )
    return dict(row)

@app.delete("/api/library/books/{book_id}")
async def delete_library_book(book_id: str, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")
    pool = await _get_db_pool()
    r = await pool.execute("DELETE FROM library_books WHERE id = $1", book_id)
    if r == "DELETE 0":
        raise HTTPException(status_code=404, detail="Book not found")
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
# Dividend Tracker  —  /api/dividends/*
# ══════════════════════════════════════════════════════════════════════════════

class _DivHistoryCreate(BaseModel):
    account_label:    str
    ticker:           str
    pay_date:         str
    amount_per_share: float
    qty:              float
    total_received:   float
    broker:           str = ""
    source:           str = "manual"


async def _div_ensure_tables():
    """Idempotent migration for dividend tables."""
    if not DB_URL:
        return
    try:
        pool = await _get_db_pool()
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS dividend_meta (
                ticker                TEXT        NOT NULL PRIMARY KEY,
                fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ex_date               DATE,
                pay_date              DATE,
                amount_per_share      NUMERIC,
                frequency             INT,
                forward_annual_rate   NUMERIC,
                forward_yield_pct     NUMERIC,
                sector                TEXT,
                industry              TEXT,
                payout_ratio          NUMERIC,
                five_yr_avg_yield_pct NUMERIC,
                raw                   JSONB
            )
        """)
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS dividend_history (
                id               UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
                account_label    TEXT        NOT NULL,
                ticker           TEXT        NOT NULL,
                pay_date         DATE        NOT NULL,
                amount_per_share NUMERIC     NOT NULL,
                qty              NUMERIC     NOT NULL,
                total_received   NUMERIC     NOT NULL,
                broker           TEXT,
                source           TEXT        DEFAULT 'manual',
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (account_label, ticker, pay_date)
            )
        """)
        await pool.execute("CREATE INDEX IF NOT EXISTS div_hist_ticker ON dividend_history (ticker, pay_date DESC)")
        await pool.execute("CREATE INDEX IF NOT EXISTS div_hist_acct   ON dividend_history (account_label, pay_date DESC)")
    except Exception as e:
        log.warning("dividend_migration_failed", error=str(e))


async def _div_fetch_yahoo(ticker: str) -> dict:
    """Fetch dividend metadata for one ticker using yfinance (runs in thread pool)."""
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    return await loop.run_in_executor(None, _div_fetch_yahoo_sync, ticker)


def _div_fetch_yahoo_sync(ticker: str) -> dict:
    """Synchronous yfinance fetch — called via run_in_executor."""
    try:
        import yfinance as yf
        from datetime import datetime as _dt, timedelta as _td
        t = yf.Ticker(ticker)
        info = t.info or {}

        # ex_date: yfinance returns Unix timestamp
        ex_raw = info.get("exDividendDate")
        ex_date = None
        if ex_raw:
            try:
                ex_date = _dt.fromtimestamp(float(ex_raw)).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Frequency: count dividend payments in trailing 18 months
        frequency = 4
        aps = None
        try:
            import pandas as _pd
            actions = t.dividends
            if actions is not None and len(actions) > 0:
                cutoff = _pd.Timestamp.now(tz="America/New_York") - _pd.Timedelta(days=548)
                if actions.index.tz is None:
                    actions.index = actions.index.tz_localize("America/New_York")
                recent = actions[actions.index >= cutoff]
                n = len(recent)
                if   n >= 10: frequency = 12
                elif n >= 3:  frequency = 4
                elif n >= 1:  frequency = 2
                else:         frequency = 1
                if n > 0:
                    aps = float(recent.iloc[-1])
        except Exception:
            pass

        # Forward annual rate — try multiple fields, then derive from yield × price
        far = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
        fyp = info.get("dividendYield")
        if fyp and fyp < 1:
            fyp = fyp * 100  # convert 0.0399 → 3.99

        # Derive far from yield × current price (common for ETFs)
        if not far and fyp:
            price_now = (info.get("currentPrice") or info.get("regularMarketPrice")
                         or info.get("navPrice") or info.get("previousClose"))
            if price_now:
                far = float(fyp) / 100.0 * float(price_now)

        # Derive far from sum of trailing-12-month dividends
        if not far:
            try:
                actions = t.dividends
                if actions is not None and len(actions) > 0:
                    cutoff_12m = _pd.Timestamp.now(tz="America/New_York") - _pd.Timedelta(days=365)
                    if actions.index.tz is None:
                        actions.index = actions.index.tz_localize("America/New_York")
                    last_12m = actions[actions.index >= cutoff_12m]
                    if len(last_12m) > 0:
                        far = float(last_12m.sum())
            except Exception:
                pass

        if not aps:
            aps = info.get("lastDividendValue") or (float(far) / frequency if far else None)

        # Derive fyp from far if still missing
        if not fyp and far:
            price_now = (info.get("currentPrice") or info.get("regularMarketPrice")
                         or info.get("navPrice") or info.get("previousClose"))
            if price_now and float(price_now) > 0:
                fyp = float(far) / float(price_now) * 100.0

        return {
            "ex_date":               ex_date,
            "pay_date":              None,
            "amount_per_share":      float(aps) if aps else None,
            "frequency":             frequency,
            "forward_annual_rate":   float(far) if far else None,
            "forward_yield_pct":     float(fyp) if fyp else None,
            "sector":                info.get("sector"),
            "industry":              info.get("industry"),
            "payout_ratio":          float(info.get("payoutRatio")) if info.get("payoutRatio") else None,
            "five_yr_avg_yield_pct": float(info.get("fiveYearAvgDividendYield") or 0) or None,
            "raw":                   {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool, type(None)))},
        }
    except Exception as e:
        log.warning("dividend_yfinance_fetch_failed", ticker=ticker, error=str(e))
        return {}


def _div_parse_date(val):
    """Convert a date string like '2026-03-16' to a datetime.date object, or None."""
    if not val:
        return None
    if hasattr(val, "toordinal"):
        return val  # already a date
    from datetime import date as _date
    try:
        return _date.fromisoformat(str(val))
    except Exception:
        return None


async def _div_get_meta(tickers: list[str]) -> dict[str, dict]:
    """Return dividend metadata for each ticker, using DB cache (24h TTL)."""
    if not DB_URL or not tickers:
        return {}
    await _div_ensure_tables()
    pool = await _get_db_pool()

    # Load cached rows
    rows = await pool.fetch(
        "SELECT * FROM dividend_meta WHERE ticker = ANY($1) AND fetched_at > NOW() - INTERVAL '24 hours'",
        tickers,
    )
    cached = {r["ticker"]: dict(r) for r in rows}
    stale  = [t for t in tickers if t not in cached]

    if stale:
        import asyncio as _asyncio
        sem = _asyncio.Semaphore(5)
        async def _fetch_one(t):
            async with sem:
                return t, await _div_fetch_yahoo(t)
        results = await _asyncio.gather(*[_fetch_one(t) for t in stale], return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                continue
            ticker, meta = res
            if not meta:
                cached[ticker] = {}
                continue
            try:
                await pool.execute("""
                    INSERT INTO dividend_meta
                        (ticker, fetched_at, ex_date, pay_date, amount_per_share, frequency,
                         forward_annual_rate, forward_yield_pct, sector, industry,
                         payout_ratio, five_yr_avg_yield_pct, raw)
                    VALUES ($1, NOW(), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (ticker) DO UPDATE SET
                        fetched_at=NOW(), ex_date=EXCLUDED.ex_date, pay_date=EXCLUDED.pay_date,
                        amount_per_share=EXCLUDED.amount_per_share, frequency=EXCLUDED.frequency,
                        forward_annual_rate=EXCLUDED.forward_annual_rate, forward_yield_pct=EXCLUDED.forward_yield_pct,
                        sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                        payout_ratio=EXCLUDED.payout_ratio, five_yr_avg_yield_pct=EXCLUDED.five_yr_avg_yield_pct,
                        raw=EXCLUDED.raw
                """,
                    ticker,
                    _div_parse_date(meta.get("ex_date")),
                    _div_parse_date(meta.get("pay_date")),
                    meta.get("amount_per_share"),
                    meta.get("frequency"),
                    meta.get("forward_annual_rate"),
                    meta.get("forward_yield_pct"),
                    meta.get("sector"),
                    meta.get("industry"),
                    meta.get("payout_ratio"),
                    meta.get("five_yr_avg_yield_pct"),
                    json.dumps(meta.get("raw", {})),
                )
            except Exception as e:
                log.warning("dividend_meta_upsert_failed", ticker=ticker, error=str(e))
            cached[ticker] = meta
    return cached


def _div_project_payments(positions_flat: list[dict], months: list[str]) -> dict:
    """
    Project dividend income for each calendar month.
    positions_flat: list of {symbol, qty, forward_annual_rate, amount_per_share,
                              frequency, ex_date, sector, account_label, projected_annual_income}
    months: ['2026-04', '2026-05', …] (12 items)
    Returns {month_key: {symbol: income, …}, …}
    """
    from datetime import date as _date, timedelta as _td
    monthly: dict[str, dict] = {m: {} for m in months}

    for p in positions_flat:
        far  = float(p.get("forward_annual_rate") or 0)
        qty  = float(p.get("qty") or 0)
        freq = int(p.get("frequency") or 4)
        aps  = float(p.get("amount_per_share") or 0)
        sym  = p["symbol"]
        if far <= 0 or qty <= 0 or freq <= 0:
            continue
        if not aps:
            aps = far / freq

        # Determine first projected payment date
        ex_str = p.get("ex_date")
        try:
            ex = _date.fromisoformat(ex_str) if ex_str else _date.today()
        except Exception:
            ex = _date.today()
        interval = int(365 / freq)

        # Advance to a future ex_date
        today = _date.today()
        while ex < today - _td(days=interval):
            ex += _td(days=interval)

        # Project freq payments across the 12-month window
        pay_lag = 14  # typical ex→pay lag in days
        for _ in range(freq + 2):
            pay_dt = ex + _td(days=pay_lag)
            mk = pay_dt.strftime("%Y-%m")
            if mk in monthly:
                monthly[mk][sym] = monthly[mk].get(sym, 0) + qty * aps
            ex += _td(days=interval)

    return monthly


def _div_account_display_names() -> dict[str, str]:
    """Return {label: display_name} using connector env vars (LABEL_DISPLAY_NAME)."""
    try:
        import toml as _toml
        cfg = _toml.load(os.getenv("ACCOUNTS_CONFIG", "/app/config/accounts.toml"))
        result = {}
        for a in cfg.get("accounts", []):
            label = a.get("label")
            if not label:
                continue
            dn_key = label.upper().replace("-", "_") + "_DISPLAY_NAME"
            display = os.getenv(dn_key) or a.get("notes") or label
            result[label] = display
        return result
    except Exception:
        return {}


@app.get("/api/dividends/holdings")
async def div_holdings(token: str = ""):
    check_token(token)

    # Reuse the existing broker positions helper — it already handles display names
    # from env vars (e.g. TRADIER_SANDBOX_DISPLAY_NAME) and normalises position fields.
    broker_data = await get_broker_positions()

    # Collect all unique equity tickers across every account
    all_tickers: list[str] = []
    for acct in broker_data.get("accounts", []):
        for p in acct.get("positions", []):
            if not _is_equity_position(p):
                continue
            sym = (p.get("symbol") or "").upper().strip()
            if sym and sym not in all_tickers:
                all_tickers.append(sym)

    # Enrich with dividend metadata (DB cache + yfinance)
    meta = await _div_get_meta(all_tickers)

    total_value = 0.0
    total_annual = 0.0
    total_payers = 0
    accounts_out = []

    for acct in broker_data.get("accounts", []):
        lbl          = acct["label"]
        display_name = acct.get("display_name") or lbl
        positions_out = []

        for p in acct.get("positions", []):
            if not _is_equity_position(p):
                continue
            sym  = (p.get("symbol") or "").upper().strip()
            if not sym:
                continue
            qty  = float(p.get("qty") or p.get("quantity") or p.get("shares") or 0)
            cost = float(p.get("cost_basis") or p.get("cost") or 0)
            price= float(p.get("current_price") or p.get("last_price") or p.get("mark") or 0)
            mval = float(p.get("market_value") or (qty * price))
            total_value += mval
            m = meta.get(sym, {})
            far  = float(m.get("forward_annual_rate") or 0)
            fyp  = float(m.get("forward_yield_pct") or 0)
            aps  = float(m.get("amount_per_share") or 0)
            freq = int(m.get("frequency") or 4)
            is_payer = far > 0
            ann_income = qty * far if is_payer else 0.0
            if is_payer:
                total_payers += 1
                total_annual  += ann_income
            positions_out.append({
                "symbol":                sym,
                "qty":                   qty,
                "cost_basis":            cost,
                "market_value":          mval,
                "ex_date":               str(m.get("ex_date") or "") or None,
                "pay_date":              str(m.get("pay_date") or "") or None,
                "amount_per_share":      aps,
                "frequency":             freq,
                "forward_annual_rate":   far,
                "forward_yield_pct":     fyp,
                "sector":                m.get("sector") or p.get("sector"),
                "industry":              m.get("industry"),
                "is_dividend_payer":     is_payer,
                "projected_annual_income":  ann_income,
                "projected_monthly_income": ann_income / 12,
            })
        accounts_out.append({
            "label":        lbl,
            "display_name": display_name,
            "positions":    positions_out,
        })

    fwd_yield = (total_annual / total_value * 100) if total_value > 0 else 0.0
    return {
        "accounts": accounts_out,
        "summary": {
            "total_holdings":               sum(len(a["positions"]) for a in accounts_out),
            "dividend_payers":              total_payers,
            "annual_projected_income":      round(total_annual, 2),
            "forward_yield_on_portfolio_pct": round(fwd_yield, 4),
            "total_portfolio_value":        round(total_value, 2),
        },
    }


@app.get("/api/dividends/forecast")
async def div_forecast(token: str = ""):
    check_token(token)
    redis = await get_redis()
    cached = await redis.get("dividend:forecast:cache")
    if cached:
        return json.loads(cached)

    # Build from holdings
    holdings = await div_holdings(token=token)
    from datetime import date as _date
    today = _date.today()
    months = [(today.replace(day=1) if i == 0
               else (_date(today.year + (today.month + i - 1) // 12,
                           (today.month + i - 1) % 12 + 1, 1)))
              for i in range(12)]
    month_keys   = [m.strftime("%Y-%m") for m in months]
    month_labels = [m.strftime("%b %Y") for m in months]

    # Flatten all positions
    positions_flat = []
    for acct in holdings["accounts"]:
        for p in acct["positions"]:
            if p["is_dividend_payer"]:
                positions_flat.append({**p, "account_label": acct["label"]})

    monthly_raw = _div_project_payments(positions_flat, month_keys)

    # Reshape monthly
    monthly_out = []
    for mk, lbl in zip(month_keys, month_labels):
        month_total = sum(monthly_raw[mk].values())
        monthly_out.append({
            "month":            mk,
            "label":            lbl,
            "projected_income": round(month_total, 2),
            "breakdown":        [{"symbol": s, "income": round(v, 2)}
                                 for s, v in sorted(monthly_raw[mk].items(), key=lambda x:-x[1])],
        })

    # by_ticker (annual)
    ticker_totals: dict[str, float] = {}
    ticker_yield:  dict[str, float] = {}
    for p in positions_flat:
        sym = p["symbol"]
        ticker_totals[sym] = ticker_totals.get(sym, 0) + p["projected_annual_income"]
        fyp = float(p.get("forward_yield_pct") or 0)
        if fyp > ticker_yield.get(sym, 0):
            ticker_yield[sym] = fyp
    total_annual = sum(ticker_totals.values()) or 1
    by_ticker = sorted(
        [{"symbol": s, "annual_income": round(v, 2), "pct_of_total": round(v/total_annual*100, 2),
          "forward_yield_pct": round(ticker_yield.get(s, 0), 2)}
         for s, v in ticker_totals.items()], key=lambda x: -x["annual_income"])

    # by_yield — top tickers ranked by forward dividend yield %
    by_yield = sorted(
        [{"symbol": s, "forward_yield_pct": round(fyp, 2)}
         for s, fyp in ticker_yield.items() if fyp > 0],
        key=lambda x: -x["forward_yield_pct"])[:12]

    # by_sector
    sector_totals: dict[str, float] = {}
    for p in positions_flat:
        sec = p.get("sector") or "Unknown"
        sector_totals[sec] = sector_totals.get(sec, 0) + p["projected_annual_income"]
    by_sector = sorted(
        [{"sector": s, "annual_income": round(v, 2), "pct_of_total": round(v/total_annual*100, 2)}
         for s, v in sector_totals.items()], key=lambda x: -x["annual_income"])

    # by_account — include display_name for friendly pie chart labels
    acct_totals: dict[str, float] = {}
    acct_display: dict[str, str] = {a["label"]: a.get("display_name") or a["label"]
                                     for a in holdings["accounts"]}
    for p in positions_flat:
        lbl = p.get("account_label", "unknown")
        acct_totals[lbl] = acct_totals.get(lbl, 0) + p["projected_annual_income"]
    by_account = sorted(
        [{"label": lbl, "display_name": acct_display.get(lbl, lbl),
          "annual_income": round(v, 2), "pct_of_total": round(v/total_annual*100, 2)}
         for lbl, v in acct_totals.items()], key=lambda x: -x["annual_income"])

    result = {
        "monthly":             monthly_out,
        "by_ticker":           by_ticker,
        "by_sector":           by_sector,
        "by_account":          by_account,
        "by_yield":            by_yield,
        "total_projected_12mo": round(sum(m["projected_income"] for m in monthly_out), 2),
    }
    await redis.set("dividend:forecast:cache", json.dumps(result), ex=3600)
    return result


@app.get("/api/dividends/history")
async def div_history(token: str = "", account_label: str = "", ticker: str = "", limit: int = 100):
    check_token(token)
    await _div_ensure_tables()
    if not DB_URL:
        return {"records": [], "total_received": 0}
    pool = await _get_db_pool()
    filters, vals = ["TRUE"], []
    if account_label:
        vals.append(account_label); filters.append(f"account_label = ${len(vals)}")
    if ticker:
        vals.append(ticker.upper()); filters.append(f"ticker = ${len(vals)}")
    vals.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM dividend_history WHERE {' AND '.join(filters)} ORDER BY pay_date DESC LIMIT ${len(vals)}",
        *vals,
    )
    records = [dict(r) for r in rows]
    for rec in records:
        for k, v in rec.items():
            if hasattr(v, "isoformat"):
                rec[k] = v.isoformat()
    total = sum(float(r.get("total_received", 0)) for r in records)
    return {"records": records, "total_received": round(total, 2)}


@app.post("/api/dividends/history")
async def div_history_add(body: _DivHistoryCreate, token: str = ""):
    check_token(token)
    await _div_ensure_tables()
    if not DB_URL:
        raise HTTPException(status_code=503, detail="No database configured")
    pool = await _get_db_pool()
    from datetime import date as _date
    await pool.execute("""
        INSERT INTO dividend_history
            (account_label, ticker, pay_date, amount_per_share, qty, total_received, broker, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (account_label, ticker, pay_date) DO UPDATE
            SET amount_per_share=EXCLUDED.amount_per_share,
                qty=EXCLUDED.qty,
                total_received=EXCLUDED.total_received
    """,
        body.account_label, body.ticker.upper(),
        _date.fromisoformat(body.pay_date),
        body.amount_per_share, body.qty, body.total_received,
        body.broker, body.source,
    )
    return {"ok": True}


@app.post("/api/dividends/refresh")
async def div_refresh(token: str = ""):
    check_token(token)
    redis = await get_redis()
    await redis.delete("dividend:forecast:cache", "dividend:holdings:cache")
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("DELETE FROM dividend_meta WHERE fetched_at < NOW()")
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/dividends/backfill")
async def div_backfill(token: str = ""):
    """
    Fetch 18 months of actual dividend payment history from yfinance for every
    ticker held in every broker account and save to dividend_history.
    Uses current qty as the held quantity for each account/ticker pair.
    """
    check_token(token)
    if not DB_URL:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Get all current holdings
    broker_data = await get_broker_positions()
    pool = await _get_db_pool()

    # Build {ticker: {account_label: qty}} map
    ticker_accounts: dict[str, dict[str, float]] = {}
    for acct in broker_data.get("accounts", []):
        lbl = acct["label"]
        for p in acct.get("positions", []):
            sym = (p.get("symbol") or "").upper().strip()
            if not sym:
                continue
            qty = float(p.get("qty") or p.get("quantity") or 0)
            if qty <= 0:
                continue
            if sym not in ticker_accounts:
                ticker_accounts[sym] = {}
            ticker_accounts[sym][lbl] = qty

    if not ticker_accounts:
        return {"ok": True, "saved": 0, "tickers": 0}

    import asyncio as _asyncio
    from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz
    import pandas as _pd

    cutoff = _pd.Timestamp.now(tz="America/New_York") - _pd.Timedelta(days=548)

    def _fetch_history_sync(ticker: str) -> list[tuple]:
        """Return list of (pay_date_str, amount_per_share) for last 18 months."""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            divs = t.dividends
            if divs is None or len(divs) == 0:
                return []
            # Ensure index is tz-aware for comparison
            if divs.index.tz is None:
                divs.index = divs.index.tz_localize("America/New_York")
            recent = divs[divs.index >= cutoff]
            results = []
            for dt_idx, amt in recent.items():
                try:
                    d = dt_idx.date() if hasattr(dt_idx, 'date') else _pd.Timestamp(dt_idx).date()
                    results.append((str(d), float(amt)))
                except Exception:
                    pass
            return results
        except Exception as e:
            log.warning("div_backfill.fetch_failed", ticker=ticker, error=str(e))
            return []

    loop = _asyncio.get_event_loop()
    sem = _asyncio.Semaphore(5)

    async def _fetch_one(ticker):
        async with sem:
            return ticker, await loop.run_in_executor(None, _fetch_history_sync, ticker)

    results = await _asyncio.gather(*[_fetch_one(t) for t in ticker_accounts], return_exceptions=True)

    total_saved = 0
    for res in results:
        if isinstance(res, Exception):
            continue
        ticker, payments = res
        if not payments:
            continue
        for pay_date_str, aps in payments:
            pay_date = _date.fromisoformat(pay_date_str)
            for acct_label, qty in ticker_accounts[ticker].items():
                try:
                    await pool.execute("""
                        INSERT INTO dividend_history
                            (account_label, ticker, pay_date, amount_per_share, qty,
                             total_received, source)
                        VALUES ($1, $2, $3, $4, $5, $6, 'backfill')
                        ON CONFLICT (account_label, ticker, pay_date) DO NOTHING
                    """,
                        acct_label, ticker, pay_date,
                        aps, qty, round(aps * qty, 4),
                    )
                    total_saved += 1
                except Exception as e:
                    log.warning("div_backfill.insert_failed",
                                ticker=ticker, acct=acct_label, error=str(e))

    log.info("div_backfill.done", tickers=len(ticker_accounts), saved=total_saved)
    return {"ok": True, "saved": total_saved, "tickers": len(ticker_accounts)}


_db_pool = None
async def _get_db_pool():
    global _db_pool
    if _db_pool:
        return _db_pool
    from urllib.parse import urlparse, unquote
    p = urlparse(DB_URL)
    _db_pool = await asyncpg.create_pool(
        host=p.hostname, port=p.port or 5432,
        user=p.username,
        password=unquote(p.password) if p.password else None,
        database=p.path.lstrip("/"),
        min_size=1, max_size=5,
    )
    return _db_pool


# ── Options Dashboard API ─────────────────────────────────────────────────────

def _days_between(d1, d2=None) -> int:
    """Return integer days between two dates (or d1 and today)."""
    if d2 is None:
        d2 = date.today()
    if d1 is None:
        return 0
    if isinstance(d1, str):
        d1 = date.fromisoformat(d1[:10])
    if isinstance(d2, str):
        d2 = date.fromisoformat(d2[:10])
    return (d1 - d2).days


@app.get("/api/options/positions")
async def get_option_positions(status: str = "active"):
    """Return all option positions with computed ATR levels and enriched fields."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT id, created_at, updated_at,
                  contract_symbol, underlying, option_type, strike, expiration_date,
                  account_label, account_name, broker, mode,
                  qty, entry_price, underlying_entry, entry_date,
                  atr_14, atr_calculated_at,
                  level_emergency, level_exit_alert, level_roll_1, level_roll_2, level_roll_3,
                  extra_roll_levels, alerts_fired, next_earnings_date,
                  delta, status, closed_at, close_reason, last_scan_at,
                  raw::text as raw_text
           FROM option_positions
           WHERE status = $1
           ORDER BY underlying, account_label""",
        status,
    )
    today = date.today()
    results = []
    for r in rows:
        exp_date   = r["expiration_date"]
        entry_date = r["entry_date"]
        dte        = (exp_date - today).days if exp_date else None
        dit        = (today - entry_date).days if entry_date else 0
        ded        = (r["next_earnings_date"] - today).days if r["next_earnings_date"] else None

        raw_obj = {}
        try:
            raw_obj = json.loads(r["raw_text"] or "{}")
        except Exception:
            pass

        extra_rolls = []
        try:
            extra_rolls = json.loads(r["extra_roll_levels"] or "[]")
        except Exception:
            pass

        alerts_fired = {}
        try:
            alerts_fired = json.loads(r["alerts_fired"] or "{}")
        except Exception:
            pass

        results.append({
            "id":               str(r["id"]),
            "contract_symbol":  r["contract_symbol"],
            "underlying":       r["underlying"],
            "option_type":      r["option_type"],
            "strike":           float(r["strike"]) if r["strike"] else None,
            "expiration_date":  exp_date.isoformat() if exp_date else None,
            "account_label":    r["account_label"],
            "account_name":     r["account_name"] or r["account_label"],
            "broker":           r["broker"],
            "mode":             r["mode"],
            "qty":              float(r["qty"]) if r["qty"] else 0,
            "entry_price":      float(r["entry_price"]) if r["entry_price"] else None,
            "underlying_entry": float(r["underlying_entry"]) if r["underlying_entry"] else None,
            "entry_date":       entry_date.isoformat() if entry_date else None,
            "atr_14":           float(r["atr_14"]) if r["atr_14"] else None,
            "level_emergency":  float(r["level_emergency"]) if r["level_emergency"] else None,
            "level_exit_alert": float(r["level_exit_alert"]) if r["level_exit_alert"] else None,
            "level_roll_1":     float(r["level_roll_1"]) if r["level_roll_1"] else None,
            "level_roll_2":     float(r["level_roll_2"]) if r["level_roll_2"] else None,
            "level_roll_3":     float(r["level_roll_3"]) if r["level_roll_3"] else None,
            "extra_roll_levels": extra_rolls,
            "alerts_fired":     alerts_fired,
            "next_earnings_date": r["next_earnings_date"].isoformat() if r["next_earnings_date"] else None,
            "days_in_trade":    dit,
            "days_to_exp":      dte,
            "days_to_earnings": ded,
            "delta":            float(r["delta"]) if r["delta"] is not None else None,
            "status":           r["status"],
            "last_scan_at":     r["last_scan_at"].isoformat() if r["last_scan_at"] else None,
            "has_chart":        bool(raw_obj.get("chart_b64")),
        })
    return results


@app.get("/api/options/positions/{position_id}/log")
async def get_option_position_log(position_id: str, limit: int = 100):
    """Return event log for a specific option position."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT ts, event_type, underlying_price, contract_price, atr_value,
                  distance_emergency, distance_exit_alert, distance_roll_1, notes
           FROM option_trade_log
           WHERE position_id = $1
           ORDER BY ts DESC
           LIMIT $2""",
        uuid.UUID(position_id), limit,
    )
    return [
        {
            "ts":                 r["ts"].isoformat(),
            "event_type":         r["event_type"],
            "underlying_price":   float(r["underlying_price"]) if r["underlying_price"] else None,
            "contract_price":     float(r["contract_price"]) if r["contract_price"] else None,
            "atr_value":          float(r["atr_value"]) if r["atr_value"] else None,
            "distance_emergency": float(r["distance_emergency"]) if r["distance_emergency"] else None,
            "distance_exit_alert":float(r["distance_exit_alert"]) if r["distance_exit_alert"] else None,
            "distance_roll_1":    float(r["distance_roll_1"]) if r["distance_roll_1"] else None,
            "notes":              r["notes"],
        }
        for r in rows
    ]


@app.get("/api/options/chart/{position_id}")
async def get_option_chart(position_id: str):
    """Return base64-encoded PNG chart for an option position."""
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        "SELECT raw::text FROM option_positions WHERE id=$1",
        uuid.UUID(position_id),
    )
    if not row:
        raise HTTPException(404, "Position not found")
    try:
        raw = json.loads(row["raw"] or "{}")
        chart_b64 = raw.get("chart_b64")
    except Exception:
        chart_b64 = None
    if not chart_b64:
        raise HTTPException(404, "Chart not yet generated")
    return {"chart_b64": chart_b64}


@app.patch("/api/options/positions/{position_id}")
async def patch_option_position(position_id: str, body: dict):
    """
    Manually correct strike, expiry, and/or option_type for a position.
    Sets expiry_locked=true so the scan won't overwrite the values.
    Body: { strike?: number, expiration_date?: "YYYY-MM-DD", option_type?: "call"|"put" }
    """
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        "SELECT id FROM option_positions WHERE id=$1",
        uuid.UUID(position_id),
    )
    if not row:
        raise HTTPException(404, "Position not found")

    sets, params = [], [uuid.UUID(position_id)]

    if "expiration_date" in body and body["expiration_date"]:
        from datetime import date as _date
        params.append(_date.fromisoformat(str(body["expiration_date"])))
        sets.append(f"expiration_date = ${len(params)}")
        sets.append("expiry_locked = TRUE")

    if "strike" in body and body["strike"] is not None:
        params.append(float(body["strike"]))
        sets.append(f"strike = ${len(params)}")

    if "option_type" in body and body["option_type"] in ("call", "put"):
        params.append(body["option_type"])
        sets.append(f"option_type = ${len(params)}")

    if not sets:
        raise HTTPException(400, "Nothing to update")

    sets.append("updated_at = NOW()")
    await pool.execute(
        f"UPDATE option_positions SET {', '.join(sets)} WHERE id=$1",
        *params,
    )
    return {"ok": True}


@app.post("/api/options/scan")
async def trigger_options_scan(token: str = ""):
    """Manually trigger an options position scan."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    redis = await get_redis()
    await redis.xadd(
        "system.commands",
        {"command": "trigger", "job": "options_scan", "issued_by": "webui"},
        maxlen=1_000,
    )
    return {"ok": True, "message": "Options scan triggered"}


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("/app/webui/static/index.html") as f:
        html = f.read()
    # Inject token and version meta tags
    html = html.replace(
        "<head>",
        f'<head>\n<meta name="ot-token" content="{WEBUI_TOKEN}">\n<meta name="ot-version" content="{APP_VERSION}">',
        1,
    )
    return html
