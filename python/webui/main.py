"""
OpenTrader Command Center — FastAPI backend
Central station for all agent containers.
Sections: Overview · Agents · Scheduler · Trades · Signals · Sentiment · Logs · System
"""
import asyncio
import base64
import csv
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import socket
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shared.crypto import encrypt_secret as _encrypt_secret_fn, decrypt_secret as _decrypt_secret_fn
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

WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "")
SECRET_KEY  = os.getenv("SECRET_KEY", "")
if not WEBUI_TOKEN:
    import secrets as _secrets
    WEBUI_TOKEN = _secrets.token_hex(32)
    log.warning("webui.WEBUI_TOKEN_not_set_using_random",
                note="Set WEBUI_TOKEN in .env for a stable token across restarts")
if not SECRET_KEY:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    log.warning("webui.SECRET_KEY_not_set_using_random",
                note="Set SECRET_KEY in .env — random key invalidates all sessions on restart")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """PBKDF2-SHA256 with random salt. Returns base64(salt + hash)."""
    salt = os.urandom(32)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return base64.b64encode(salt + key).decode()

def _verify_password(password: str, stored: str) -> bool:
    """Constant-time verify of a password against a stored PBKDF2 hash."""
    try:
        decoded = base64.b64decode(stored.encode())
        salt, key = decoded[:32], decoded[32:]
        new_key   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return hmac.compare_digest(key, new_key)
    except Exception:
        return False

def _make_jwt(user_id: str, username: str, exp_hours: int = 24) -> str:
    """Create an HMAC-SHA256 signed JWT (HS256) without external libraries."""
    header  = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": user_id, "usr": username,
        "iat": int(time.time()), "exp": int(time.time()) + exp_hours * 3600,
    }).encode()).rstrip(b"=").decode()
    msg = f"{header}.{payload}".encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"

def _verify_jwt(token: str) -> dict | None:
    """Verify signature and expiry. Returns payload dict or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        msg          = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(parts[2], expected_sig):
            return None
        padding = 4 - len(parts[1]) % 4
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * padding))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

def _encrypt_secret(value: str) -> str:
    return _encrypt_secret_fn(value, SECRET_KEY)

def _decrypt_secret(token: str) -> str:
    return _decrypt_secret_fn(token, SECRET_KEY)

# ── Auth DB helpers ───────────────────────────────────────────────────────────

async def _ensure_auth_tables():
    """Create users and user_secrets tables if they don't exist."""
    if not DB_URL:
        return
    pool = await _get_db_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            username      VARCHAR(64) UNIQUE NOT NULL,
            email         VARCHAR(255),
            password_hash TEXT        NOT NULL,
            is_admin      BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_secrets (
            id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key              VARCHAR(128) NOT NULL,
            encrypted_value  TEXT        NOT NULL,
            description      VARCHAR(255),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id, key)
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key         VARCHAR(128) NOT NULL,
            value_json  TEXT        NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, key)
        )
    """)

async def _count_users() -> int:
    try:
        pool = await _get_db_pool()
        return await pool.fetchval("SELECT COUNT(*) FROM users") or 0
    except Exception:
        return 0

async def _get_user(username: str) -> dict | None:
    try:
        pool = await _get_db_pool()
        row  = await pool.fetchrow(
            "SELECT id::text, username, email, password_hash, is_admin FROM users WHERE username=$1",
            username,
        )
        return dict(row) if row else None
    except Exception:
        return None

async def _load_user_secrets_to_env(user_id: str):
    """Decrypt and inject all stored secrets for this user into os.environ."""
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch(
            "SELECT key, encrypted_value FROM user_secrets WHERE user_id=$1::uuid",
            user_id,
        )
        for row in rows:
            try:
                os.environ[row["key"]] = _decrypt_secret(row["encrypted_value"])
            except Exception:
                pass
    except Exception:
        pass

# ── Auth middleware ───────────────────────────────────────────────────────────

_PUBLIC_PATHS = {"/login", "/setup", "/api/auth/login",
                 "/api/auth/logout", "/api/auth/setup", "/api/auth/check"}

# ── Request correlation middleware ────────────────────────────────────────────

@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """Stamp every request with X-Request-ID for end-to-end log tracing."""
    import uuid as _uuid
    req_id = request.headers.get("X-Request-ID") or str(_uuid.uuid4())
    try:
        import structlog
        structlog.contextvars.bind_contextvars(request_id=req_id)
    except Exception:
        pass
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    try:
        structlog.contextvars.unbind_contextvars("request_id")
    except Exception:
        pass
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Always allow public auth paths and static assets
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    # Check session cookie (JWT)
    session = request.cookies.get("ot_session", "")
    if session and _verify_jwt(session):
        return await call_next(request)

    # Fall back to ?token= query param (backwards compat for existing bookmarks + WS)
    token = request.query_params.get("token", "")
    if token and token == WEBUI_TOKEN:
        return await call_next(request)

    # Unauthenticated — decide response based on request type
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html:
        users = await _count_users()
        return RedirectResponse("/setup" if users == 0 else "/login")
    return JSONResponse({"error": "Unauthorized"}, status_code=401)

# ── Traffic monitor middleware ────────────────────────────────────────────────

_TRAFFIC_SKIP_PREFIXES = ("/static", "/login", "/setup")
_TRAFFIC_SKIP_EXACT    = {"/api/auth/login", "/api/auth/logout", "/api/auth/check",
                           "/api/auth/setup", "/api/system/traffic"}

@app.middleware("http")
async def traffic_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(_TRAFFIC_SKIP_PREFIXES) or path in _TRAFFIC_SKIP_EXACT or not path.startswith("/api/"):
        return await call_next(request)
    t0 = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - t0) * 1000.0
    asyncio.create_task(_record_traffic(request.method, path, response.status_code, duration_ms))
    return response


async def _record_traffic(method: str, path: str, status_code: int, duration_ms: float):
    if not DB_URL:
        return
    try:
        pool = await _get_db_pool()
        await pool.execute(
            "INSERT INTO api_traffic (method, path, status_code, duration_ms) VALUES ($1,$2,$3,$4)",
            method, path, status_code, duration_ms,
        )
    except Exception:
        pass


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def serve_login():
    try:
        with open("/app/webui/static/login.html") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Login page not found</h1>", status_code=500)

@app.get("/setup", response_class=HTMLResponse)
async def serve_setup():
    """Only accessible when no users exist yet."""
    if await _count_users() > 0:
        return RedirectResponse("/login")
    try:
        with open("/app/webui/static/setup.html") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Setup page not found</h1>", status_code=500)

@app.get("/api/auth/check")
async def auth_check():
    """Returns whether any users exist — used by login/setup pages."""
    return {"has_users": await _count_users() > 0}

@app.post("/api/auth/setup")
async def auth_setup(body: dict):
    """Create the first admin user. Only works when no users exist."""
    if await _count_users() > 0:
        raise HTTPException(status_code=403, detail="Setup already complete")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    email    = str(body.get("email", "")).strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    pool = await _get_db_pool()
    try:
        row = await pool.fetchrow(
            """INSERT INTO users (username, email, password_hash, is_admin)
               VALUES ($1, $2, $3, TRUE) RETURNING id::text, username""",
            username, email or None, _hash_password(password),
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Username already exists")
    log.info("auth.first_user_created", username=username)
    return {"ok": True, "username": row["username"]}

@app.post("/api/auth/login")
async def auth_login(request: Request, body: dict):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    user     = await _get_user(username)
    if not user or not _verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Load stored secrets into env so API keys are immediately available
    await _load_user_secrets_to_env(user["id"])

    token    = _make_jwt(user["id"], user["username"], exp_hours=168)
    is_https = request.headers.get("x-forwarded-proto", "http") == "https"

    response = JSONResponse({"ok": True, "username": user["username"]})
    response.set_cookie(
        "ot_session", token,
        httponly=True,
        secure=is_https,
        samesite="strict",
        max_age=604800,
        path="/",
    )
    log.info("auth.login", username=username)
    return response

@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("ot_session", path="/")
    return response

@app.get("/api/auth/me")
async def auth_me(request: Request):
    session = request.cookies.get("ot_session", "")
    payload = _verify_jwt(session)
    if not payload:
        raise HTTPException(status_code=401)
    return {"username": payload.get("usr"), "user_id": payload.get("sub")}

# ── User secrets (encrypted API key storage) ─────────────────────────────────

# Well-known secrets — shown in the profile UI with friendly descriptions
# Keys managed via Broker Configuration panel — hidden from API Keys & Secrets display
_BROKER_MANAGED_PREFIXES = ("TRADIER_", "ALPACA_", "WEBULL_")

KNOWN_SECRETS = [
    # ── Brokers ───────────────────────────────────────────────────────────────
    ("---", "Brokers"),
    ("ALPACA_API_KEY",             "Alpaca — Paper API Key"),
    ("ALPACA_API_SECRET",          "Alpaca — Paper API Secret"),
    ("ALPACA_LIVE_API_KEY",        "Alpaca — Live API Key"),
    ("ALPACA_LIVE_API_SECRET",     "Alpaca — Live API Secret"),
    ("TRADIER_SANDBOX_API_KEY",    "Tradier — Sandbox API Key"),
    ("TRADIER_PRODUCTION_API_KEY", "Tradier — Production API Key"),
    ("WEBULL_API_KEY",             "Webull — API Key"),
    ("WEBULL_SECRET_KEY",          "Webull — Secret Key"),
    # ── Market Data ───────────────────────────────────────────────────────────
    ("---", "Market Data"),
    ("MASSIVE_API_KEY",            "Massive — API Key"),
    ("MASSIVE_MCP_URL",            "Massive — MCP URL"),
    ("FRED_API_KEY",               "FRED — API Key"),
    ("ALPHA_VANTAGE_API_KEY",      "Alpha Vantage — API Key"),
    ("UNUSUAL_WHALES_API_KEY",     "Unusual Whales — API Key"),
    ("UNUSUAL_WHALES_MCP_URL",     "Unusual Whales — MCP URL"),
    ("EODDATA_API_KEY",            "EODData — API Key"),
    ("EODHD_API_KEY",              "EODHD — API Key"),
    ("FINNHUB_API_KEY",            "Finnhub — API Key"),
    ("FMP_API_KEY",                "FMP — API Key"),
    ("EODHD_MCP_URL",              "EODHD — MCP URL"),
    ("GOOGLE_BOOKS_API_KEY",       "Google Books — API Key"),
    # ── AI / LLM ──────────────────────────────────────────────────────────────
    ("---", "AI / LLM"),
    ("OPENROUTER_API_KEY",         "OpenRouter — API Key"),
    ("OPENROUTER_BASE_URL",        "OpenRouter — Base URL"),
    ("LLM_PREDICTOR_MODEL",        "LLM — Predictor Model"),
    ("LLM_REVIEW_MODEL",           "LLM — Review Model"),
    ("LLM_EOD_MODEL",              "LLM — EOD Report Model"),
    ("LLM_ORCHESTRATOR_MODEL",     "LLM — Orchestrator Model"),
    ("LLM_FALLBACK_MODEL",         "LLM — Fallback Model"),
    # ── Notifications ─────────────────────────────────────────────────────────
    ("---", "Notifications"),
    ("TELEGRAM_BOT_TOKEN",         "Telegram — Bot Token"),
    ("TELEGRAM_CHAT_ID",           "Telegram — Chat ID"),
    ("DISCORD_BOT_TOKEN",          "Discord — Bot Token"),
    ("DISCORD_CHANNEL_ID",         "Discord — Channel ID"),
    ("DISCORD_ALLOWED_GUILDS",     "Discord — Allowed Guilds"),
    ("DISCORD_INTENTS",            "Discord — Intents"),
    # ── AgentMail ─────────────────────────────────────────────────────────────
    ("---", "AgentMail"),
    ("AGENTMAIL_API_KEY",               "AgentMail — API Key"),
    ("AGENTMAIL_BASE_URL",              "AgentMail — Base URL"),
    ("AGENTMAIL_ORCHESTRATOR_INBOX",    "AgentMail — Orchestrator Inbox"),
    ("AGENTMAIL_REVIEW_INBOX",          "AgentMail — Review Inbox"),
    ("AGENTMAIL_EOD_INBOX",             "AgentMail — EOD Inbox"),
    ("AGENTMAIL_ALERTS_INBOX",          "AgentMail — Alerts Inbox"),
    ("REPORT_RECIPIENT_EMAIL",          "Report Recipient Email"),
    # ── OVTLYR ────────────────────────────────────────────────────────────────
    ("---", "OVTLYR"),
    ("OVTLYR_EMAIL",               "OVTLYR — Email"),
    ("OVTLYR_PASSWORD",            "OVTLYR — Password"),
    ("OVTLYR_BASE_URL",            "OVTLYR — Base URL"),
    # ── Alpaca MCP ────────────────────────────────────────────────────────────
    ("---", "Alpaca MCP"),
    ("ALPACA_SECRET_KEY",          "Alpaca MCP — Secret Key"),
    ("ALPACA_PAPER_TRADE",         "Alpaca MCP — Paper Trading"),
    ("ALPACA_MCP_URL",             "Alpaca MCP — MCP URL"),
    # ── SnapTrade / Portfolio Sync ────────────────────────────────────────────
    ("---", "SnapTrade / Portfolio Sync"),
    ("SNAPTRADE_CLIENT_ID",        "SnapTrade — Client ID"),
    ("SNAPTRADE_CONSUMER_KEY",     "SnapTrade — Consumer Key"),
    ("SNAPTRADE_USER_ID",          "SnapTrade — User ID"),
    ("SNAPTRADE_USER_SECRET",      "SnapTrade — User Secret"),
    ("P2P_EMAIL",                  "Paycheck2Portfolio — Email"),
    ("P2P_PASSWORD",               "Paycheck2Portfolio — Password"),
]

def _current_user_id(request: Request) -> str | None:
    payload = _verify_jwt(request.cookies.get("ot_session", ""))
    return payload.get("sub") if payload else None

async def _resolve_user_id(request: Request, token: str = "") -> str | None:
    """Return user_id from session cookie, or first user when token auth is used."""
    uid = _current_user_id(request)
    if uid:
        return uid
    if token and token == WEBUI_TOKEN:
        try:
            pool = await _get_db_pool()
            row = await pool.fetchrow("SELECT id FROM users ORDER BY created_at LIMIT 1")
            return str(row["id"]) if row else None
        except Exception:
            return None
    return None

@app.get("/api/user/secrets")
async def list_user_secrets(request: Request, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id and not token:
        raise HTTPException(status_code=401)

    stored: dict[str, bool] = {}
    if user_id:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                "SELECT key FROM user_secrets WHERE user_id=$1::uuid", user_id
            )
            stored = {r["key"]: True for r in rows}
        except Exception:
            pass

    result = []
    for key, desc in KNOWN_SECRETS:
        if key == "---":
            result.append({"separator": True, "label": desc})
            continue
        in_db  = stored.get(key, False)
        in_env = bool(os.environ.get(key))
        is_broker = key.startswith(_BROKER_MANAGED_PREFIXES)
        result.append({"key": key, "description": desc,
                       "is_set": in_db or in_env, "source": "db" if in_db else ("env" if in_env else ""),
                       "broker_managed": is_broker})
    return result

@app.post("/api/user/secrets")
async def save_user_secret(request: Request, body: dict, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    key   = str(body.get("key", "")).strip().upper()
    value = str(body.get("value", "")).strip()
    desc  = str(body.get("description", "")).strip() or None
    if not key or not value:
        raise HTTPException(status_code=400, detail="key and value required")
    encrypted = _encrypt_secret(value)
    pool = await _get_db_pool()
    await pool.execute("""
        INSERT INTO user_secrets (user_id, key, encrypted_value, description, updated_at)
        VALUES ($1::uuid, $2, $3, $4, NOW())
        ON CONFLICT (user_id, key) DO UPDATE
            SET encrypted_value=$3, description=COALESCE($4, user_secrets.description),
                updated_at=NOW()
    """, user_id, key, encrypted, desc)
    os.environ[key] = value
    log.info("auth.secret_saved", key=key)
    return {"ok": True, "key": key}

@app.post("/api/user/secrets/import-env")
async def import_secrets_from_env(request: Request, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    pool = await _get_db_pool()
    rows = await pool.fetch("SELECT key FROM user_secrets WHERE user_id=$1::uuid", user_id)
    already_in_db = {r["key"] for r in rows}
    imported, skipped = [], []
    for key, desc in KNOWN_SECRETS:
        if key == "---":
            continue
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        if key in already_in_db:
            skipped.append(key)
            continue
        encrypted = _encrypt_secret(value)
        await pool.execute("""
            INSERT INTO user_secrets (user_id, key, encrypted_value, description, updated_at)
            VALUES ($1::uuid, $2, $3, $4, NOW())
            ON CONFLICT (user_id, key) DO NOTHING
        """, user_id, key, encrypted, desc)
        imported.append(key)
    log.info("auth.secrets_imported_from_env", count=len(imported))
    return {"ok": True, "imported": imported, "skipped": skipped}

@app.delete("/api/user/secrets/{key}")
async def delete_user_secret(key: str, request: Request, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    key = key.upper()
    pool = await _get_db_pool()
    await pool.execute(
        "DELETE FROM user_secrets WHERE user_id=$1::uuid AND key=$2",
        user_id, key,
    )
    return {"ok": True, "key": key}

@app.get("/api/user/secrets/{key}/reveal")
async def reveal_user_secret(key: str, request: Request, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    key = key.upper()
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        "SELECT encrypted_value FROM user_secrets WHERE user_id=$1::uuid AND key=$2",
        user_id, key,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Secret not found")
    try:
        value = _decrypt_secret(row["encrypted_value"])
    except Exception:
        raise HTTPException(status_code=500, detail="Decryption failed")
    return {"key": key, "value": value}

async def _sync_secrets_to_env(user_id: str) -> None:
    """Write all user secrets to .env so non-webui agents pick them up on restart."""
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch(
            "SELECT key, encrypted_value FROM user_secrets WHERE user_id=$1::uuid", user_id
        )
        updates: dict = {}
        for row in rows:
            try:
                updates[row["key"]] = _decrypt_secret(row["encrypted_value"])
            except Exception:
                pass
        if updates:
            _write_env_file(updates)
    except Exception as e:
        log.warning("sync_secrets_to_env.failed", error=str(e))


class BatchRevealBody(BaseModel):
    keys: list = []


@app.post("/api/user/secrets/batch-reveal")
async def batch_reveal_secrets(request: Request, body: BatchRevealBody):
    """Return decrypted values for a list of keys from user_secrets (session-cookie auth)."""
    user_id = await _resolve_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    if not body.keys:
        return {}
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch(
            "SELECT key, encrypted_value FROM user_secrets WHERE user_id=$1::uuid AND key = ANY($2::text[])",
            user_id, [k.upper() for k in body.keys],
        )
        result = {k.upper(): os.getenv(k.upper(), "") for k in body.keys}
        for row in rows:
            try:
                result[row["key"]] = _decrypt_secret(row["encrypted_value"])
            except Exception:
                pass
        return result
    except Exception as e:
        log.error("batch_reveal.failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve secrets")


class BatchSaveBody(BaseModel):
    vars: dict = {}


@app.post("/api/user/secrets/batch")
async def batch_save_secrets(request: Request, body: BatchSaveBody):
    """Upsert multiple secrets into user_secrets and sync to .env for agents."""
    user_id = await _resolve_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="No user session found")
    if not body.vars:
        raise HTTPException(status_code=400, detail="No vars provided")
    pool = await _get_db_pool()
    updated = []
    for raw_key, value in body.vars.items():
        key = raw_key.upper()
        val = str(value).strip()
        if not val:
            continue
        encrypted = _encrypt_secret(val)
        await pool.execute("""
            INSERT INTO user_secrets (user_id, key, encrypted_value, updated_at)
            VALUES ($1::uuid, $2, $3, NOW())
            ON CONFLICT (user_id, key) DO UPDATE
                SET encrypted_value=$3, updated_at=NOW()
        """, user_id, key, encrypted)
        os.environ[key] = val
        updated.append(key)
    if updated:
        asyncio.create_task(_sync_secrets_to_env(user_id))
        if any(k.startswith(_BROKER_MANAGED_PREFIXES) for k in updated):
            asyncio.create_task(_restart_broker_gateway())
    return {"ok": True, "updated": updated}


@app.get("/api/user/preferences")
async def get_user_preferences(request: Request, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401)
    pool = await _get_db_pool()
    rows = await pool.fetch("SELECT key, value_json FROM user_preferences WHERE user_id=$1::uuid", user_id)
    import json as _json
    return {r["key"]: _json.loads(r["value_json"]) for r in rows}

@app.post("/api/user/preferences")
async def set_user_preference(request: Request, body: dict, token: str = ""):
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id:
        raise HTTPException(status_code=401)
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    import json as _json
    value_json = _json.dumps(body.get("value"))
    pool = await _get_db_pool()
    await pool.execute("""
        INSERT INTO user_preferences (user_id, key, value_json, updated_at)
        VALUES ($1::uuid, $2, $3, NOW())
        ON CONFLICT (user_id, key) DO UPDATE SET value_json=$3, updated_at=NOW()
    """, user_id, key, value_json)
    return {"ok": True}

@app.post("/api/user/change-password")
async def change_password(request: Request, body: dict, token: str = ""):
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401)
    current  = str(body.get("current_password", ""))
    new_pwd  = str(body.get("new_password", ""))
    if not current or not new_pwd:
        raise HTTPException(status_code=400, detail="current_password and new_password required")
    if len(new_pwd) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    pool = await _get_db_pool()
    row  = await pool.fetchrow(
        "SELECT password_hash FROM users WHERE id=$1::uuid", user_id
    )
    if not row or not _verify_password(current, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    await pool.execute(
        "UPDATE users SET password_hash=$2, updated_at=NOW() WHERE id=$1::uuid",
        user_id, _hash_password(new_pwd),
    )
    return {"ok": True}


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
    "scraper-ovtlyr", "scraper-wsb", "scraper-seekalpha",
    "scraper-etf-flows", "scraper-macro-regime", "scraper-news", "scraper-eodhd-news",
    "scraper-finnhub-insider",
    "aggregator", "review-agent", "broker-gateway", "directive-agent",
    # MCP servers, market-data gateway, and chat agent — health derived from Podman (no heartbeat)
    "market-data", "mcp-alpaca", "mcp-tradingview", "mcp-massive", "mcp-unusualwhales", "mcp-yahoo", "chat-agent",
]

# Containers that don't publish heartbeats — health is read from Podman status
PODMAN_HEALTH_ONLY = {"market-data", "mcp-alpaca", "mcp-tradingview", "mcp-massive", "mcp-unusualwhales", "mcp-yahoo", "chat-agent"}

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
    "scraper-etf-flows":        "ot-scraper-etf-flows",
    "scraper-macro-regime":     "ot-scraper-macro-regime",
    "scraper-news":             "ot-scraper-news",
    "scraper-eodhd-news":       "ot-scraper-eodhd-news",
    "scraper-finnhub-insider":  "ot-scraper-finnhub-insider",
    "aggregator":      "ot-aggregator",
    "review-agent":    "ot-review-agent",
    "broker-gateway":  "ot-broker-gateway",
    "directive-agent": "ot-directive-agent",
    "market-data":        "ot-market-data",
    "mcp-alpaca":         "ot-mcp-alpaca",
    "mcp-tradingview":    "ot-mcp-tradingview",
    "mcp-massive":        "ot-mcp-massive",
    "mcp-unusualwhales":  "ot-mcp-unusualwhales",
    "mcp-yahoo":          "ot-mcp-yahoo",
    "chat-agent":         "ot-chat-agent",
    "redis":           "ot-redis",
    "timescaledb":     "ot-timescaledb",
    "grafana":         "ot-grafana",
    "webui":           "ot-webui",
}


def check_token(token: str):
    # Empty token is allowed when the middleware already verified the session cookie.
    # Only reject an explicitly-provided token that doesn't match.
    if token and token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Helpers ──────────────────────────────────────────────────────────────────

_OCC_SYMBOL_RE = re.compile(r'^[A-Z]{1,6}[_ ]?\d{6}[CP]\d{8}$', re.IGNORECASE)
_OPTION_ASSET_CLASSES = {"option", "options", "us_option", "us_option_contract"}

def _is_equity_position(p: dict) -> bool:
    """Return True if a broker position is a long stock (equity), not an option contract."""
    raw = p.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if str(raw.get("instrument_type", "")).upper() == "OPTION":
        return False
    ac = str(raw.get("asset_class") or p.get("asset_class") or "").lower()
    if ac in _OPTION_ASSET_CLASSES or "option" in ac:
        return False
    sym = (p.get("symbol") or "").strip()
    if _OCC_SYMBOL_RE.match(sym):
        return False
    # US stock tickers are pure letters — any digit in the symbol means it's a derivative
    if any(c.isdigit() for c in sym):
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
    job_err_count = await redis.zcount("scheduler:job_errors", time.time() - 3600, "+inf")

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


# ── API — Equities: Buy-Borrow (SnapTrade) ────────────────────────────────────

@app.get("/api/equities/buy-borrow")
async def get_buy_borrow(request: Request, token: str = "",
                         ltv: float = 0.50, maint_ltv: float = 0.70,
                         apr: float = 0.065, cap_gains: float = 0.238,
                         draw: float = 0.0, refresh: int = 0):
    """Buy-Borrow-Die dashboard: collateral, borrowing capacity, LTV cushion,
    and tax-deferral estimates computed from SnapTrade-sourced holdings."""
    check_token(token) if token else None
    user_id = await _resolve_user_id(request, token)
    if not user_id and not token:
        raise HTTPException(status_code=401)
    if user_id:
        await _load_user_secrets_to_env(user_id)

    from shared import snaptrade_source as sts
    if not sts.configured_source():
        raise HTTPException(
            status_code=400,
            detail="No SnapTrade source configured. Add SnapTrade API keys or "
                   "Paycheck2Portfolio credentials under Config → Secrets.",
        )

    redis = await get_redis()
    cache_key = f"equities:buyborrow:portfolio:{user_id or 'token'}"
    portfolio, used_cache = None, False
    if not refresh:
        cached = await redis.get(cache_key)
        if cached:
            try:
                portfolio = json.loads(cached)
                used_cache = True
            except Exception:
                portfolio = None
    if portfolio is None:
        try:
            portfolio = await sts.fetch_portfolio()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Portfolio fetch failed: {e}")
        try:
            await redis.set(cache_key, json.dumps(portfolio), ex=300)
        except Exception:
            pass

    result = sts.compute_buy_borrow(
        portfolio, ltv=ltv, maint_ltv=maint_ltv, apr=apr,
        cap_gains_rate=cap_gains, draw=draw,
    )
    result["cached"] = used_cache
    return result


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
        log.error("agent.restart_failed", agent=agent, container=cname, error=str(e))
        raise HTTPException(status_code=500, detail="Container restart failed")


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
        pool = await _get_db_pool()
        async with pool.acquire() as conn:
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
    except Exception as e:
        log.warning("db_upsert_job_failed", error=str(e))


async def _db_delete_job(job_id: str):
    """Remove a job from TimescaleDB."""
    if not DB_URL:
        return
    try:
        pool = await _get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM scheduler_jobs WHERE id = $1", job_id)
    except Exception as e:
        log.warning("db_delete_job_failed", error=str(e))


async def _load_jobs_from_db_to_redis(redis):
    """On startup, restore all persisted jobs from DB into Redis."""
    if not DB_URL:
        return
    try:
        pool = await _get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM scheduler_jobs")
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
    await _ensure_auth_tables()
    # Remove retired secrets that are no longer used by the application
    _RETIRED_SECRETS = ["CLOUDFLARE_TUNNEL_TOKEN", "POLYGON_API_KEY"]
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute(
                "DELETE FROM user_secrets WHERE key = ANY($1::text[])", _RETIRED_SECRETS
            )
        except Exception:
            pass
    # Load the first (admin) user's secrets into env on startup
    if DB_URL:
        try:
            pool = await _get_db_pool()
            row  = await pool.fetchrow(
                "SELECT id::text FROM users ORDER BY created_at LIMIT 1"
            )
            if row:
                await _load_user_secrets_to_env(row["id"])
                await _sync_secrets_to_env(row["id"])
        except Exception:
            pass
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS signal_reflections (
                    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    ticker            TEXT        NOT NULL,
                    analysis_ts       TIMESTAMPTZ NOT NULL,
                    signal            TEXT        NOT NULL,
                    price_at_analysis NUMERIC(12,4),
                    price_5d_later    NUMERIC(12,4),
                    return_pct        NUMERIC(8,4),
                    alpha_vs_spy      NUMERIC(8,4),
                    reflection        TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await pool.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_reflections_ticker
                ON signal_reflections (ticker, created_at DESC)
            """)
        except Exception:
            pass

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
            # Add review column if missing (added after initial table creation)
            await pool.execute("""
                ALTER TABLE library_books ADD COLUMN IF NOT EXISTS review TEXT
            """)
            # Add 'read' to status check constraint if not already present
            await pool.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = 'library_books'
                          AND pg_get_constraintdef(c.oid) LIKE '%''read''%'
                    ) THEN
                        ALTER TABLE library_books
                            DROP CONSTRAINT IF EXISTS library_books_status_check;
                        ALTER TABLE library_books
                            ADD CONSTRAINT library_books_status_check
                            CHECK (status IN ('reading','read','purchased','reference'));
                    END IF;
                END $$
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
    # ATR template table
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS option_atr_templates (
                    id           BIGSERIAL PRIMARY KEY,
                    ticker       TEXT        NOT NULL,
                    anchor_price DOUBLE PRECISION NOT NULL,
                    atr_value    DOUBLE PRECISION NOT NULL,
                    trade_date   DATE        NOT NULL,
                    order_ids    TEXT,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await pool.execute("""
                CREATE INDEX IF NOT EXISTS idx_opt_atr_tpl_ticker
                ON option_atr_templates (ticker, trade_date DESC)
            """)
        except Exception:
            pass
    # Phase 2: spread columns on option_positions
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                ALTER TABLE option_positions
                    ADD COLUMN IF NOT EXISTS spread_group_id  UUID,
                    ADD COLUMN IF NOT EXISTS spread_role      TEXT,
                    ADD COLUMN IF NOT EXISTS spread_type      TEXT,
                    ADD COLUMN IF NOT EXISTS spread_meta      JSONB
            """)
            await pool.execute("""
                CREATE INDEX IF NOT EXISTS idx_op_spread_group
                    ON option_positions (spread_group_id)
                    WHERE spread_group_id IS NOT NULL
            """)
        except Exception:
            pass
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS ticker_classification (
                    ticker      TEXT        PRIMARY KEY,
                    sector      TEXT,
                    industry    TEXT,
                    market_type TEXT,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        except Exception:
            pass
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS report_log (
                    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    report_type TEXT        NOT NULL,
                    status      TEXT        NOT NULL,
                    subject     TEXT,
                    channels    TEXT[]      DEFAULT '{}',
                    recipient   TEXT,
                    body_html   TEXT,
                    body_text   TEXT,
                    meta        JSONB       DEFAULT '{}'
                )
            """)
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS report_log_ts_idx   ON report_log (ts DESC)"
            )
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS report_log_type_idx ON report_log (report_type, ts DESC)"
            )
        except Exception:
            pass
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS live_mode_ack (
                    id              INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    phrase_typed    TEXT        NOT NULL,
                    risk_sha256     TEXT        NOT NULL
                )
            """)
        except Exception:
            pass
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS report_config (
                    report_type      TEXT    PRIMARY KEY,
                    enabled          BOOLEAN NOT NULL DEFAULT true,
                    channels         TEXT[]  NOT NULL DEFAULT ARRAY['agentmail'],
                    recipient        TEXT    NOT NULL DEFAULT '',
                    schedule_days    TEXT    NOT NULL DEFAULT 'mon-fri',
                    schedule_hour    INTEGER NOT NULL DEFAULT 13,
                    schedule_minute  INTEGER NOT NULL DEFAULT 0,
                    include_stocks   BOOLEAN NOT NULL DEFAULT false,
                    include_options  BOOLEAN NOT NULL DEFAULT true,
                    include_earnings BOOLEAN NOT NULL DEFAULT false,
                    include_exdiv    BOOLEAN NOT NULL DEFAULT false,
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        except Exception:
            pass
    # Migrate report_config: add watch_tickers + alert_days_before if missing
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                ALTER TABLE report_config
                  ADD COLUMN IF NOT EXISTS watch_tickers     TEXT    NOT NULL DEFAULT '[]',
                  ADD COLUMN IF NOT EXISTS alert_days_before INTEGER NOT NULL DEFAULT 1
            """)
        except Exception:
            pass
    # execution_events — Code Insights telemetry store (30-day retention)
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS execution_events (
                    id            BIGSERIAL   PRIMARY KEY,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    agent         TEXT        NOT NULL,
                    event_name    TEXT        NOT NULL,
                    severity      TEXT        NOT NULL DEFAULT 'info',
                    duration_ms   DOUBLE PRECISION,
                    payload       JSONB       NOT NULL DEFAULT '{}',
                    traceback_str TEXT,
                    resolved      BOOLEAN     NOT NULL DEFAULT FALSE,
                    notes         TEXT
                )
            """)
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_events_ts     ON execution_events (ts DESC)"
            )
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_events_agent  ON execution_events (agent, ts DESC)"
            )
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_events_sev    ON execution_events (severity, ts DESC)"
            )
            # 30-day retention policy
            await pool.execute("""
                DELETE FROM execution_events WHERE ts < NOW() - INTERVAL '30 days'
            """)
        except Exception as e:
            log.warning("execution_events.table_init_failed", error=str(e))
    if DB_URL:
        try:
            pool = await _get_db_pool()
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS api_traffic (
                    id          BIGSERIAL PRIMARY KEY,
                    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    method      TEXT NOT NULL,
                    path        TEXT NOT NULL,
                    status_code INT NOT NULL,
                    duration_ms DOUBLE PRECISION NOT NULL
                )
            """)
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_traffic_ts ON api_traffic (ts DESC)"
            )
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_traffic_path ON api_traffic (path, ts DESC)"
            )
            await pool.execute("DELETE FROM api_traffic WHERE ts < NOW() - INTERVAL '7 days'")
        except Exception as e:
            log.warning("api_traffic.table_init_failed", error=str(e))

        try:
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS eodhd_news (
                    id           BIGSERIAL PRIMARY KEY,
                    ticker       TEXT        NOT NULL,
                    title        TEXT        NOT NULL,
                    url          TEXT,
                    published_at TIMESTAMPTZ,
                    source_name  TEXT,
                    polarity     REAL        DEFAULT 0,
                    pos_score    REAL        DEFAULT 0,
                    neg_score    REAL        DEFAULT 0,
                    neu_score    REAL        DEFAULT 0,
                    llm_summary  TEXT,
                    llm_keywords JSONB       DEFAULT '[]',
                    scraped_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(ticker, url)
                )
            """)
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_eodhd_news_ticker ON eodhd_news(ticker)"
            )
            await pool.execute(
                "CREATE INDEX IF NOT EXISTS idx_eodhd_news_published ON eodhd_news(published_at DESC)"
            )
        except Exception as e:
            log.warning("eodhd_news.table_init_failed", error=str(e))

    try:
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS av_ticker_sentiment (
                id                       BIGSERIAL PRIMARY KEY,
                ticker                   TEXT NOT NULL,
                title                    TEXT NOT NULL,
                url                      TEXT,
                time_published           TIMESTAMPTZ,
                source                   TEXT,
                overall_sentiment_label  TEXT,
                overall_sentiment_score  REAL DEFAULT 0,
                ticker_relevance_score   REAL DEFAULT 0,
                ticker_sentiment_score   REAL DEFAULT 0,
                ticker_sentiment_label   TEXT,
                summary                  TEXT,
                scraped_at               TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ticker, url)
            )
        """)
        await pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_av_ticker_sent_ticker ON av_ticker_sentiment(ticker)"
        )
    except Exception as e:
        log.warning("av_ticker_sentiment.table_init_failed", error=str(e))

    try:
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS insider_transactions (
                id               BIGSERIAL PRIMARY KEY,
                ticker           TEXT    NOT NULL,
                name             TEXT,
                share            BIGINT,
                change           BIGINT,
                filing_date      DATE,
                transaction_date DATE,
                transaction_code TEXT,
                transaction_price REAL,
                scraped_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ticker, name, transaction_date, transaction_code, share)
            )
        """)
        await pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_insider_tx_ticker ON insider_transactions(ticker)"
        )
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS insider_sentiment (
                id         BIGSERIAL PRIMARY KEY,
                ticker     TEXT NOT NULL,
                year       INT,
                month      INT,
                change     BIGINT,
                mspr       REAL,
                scraped_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ticker, year, month)
            )
        """)
        await pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_insider_sent_ticker ON insider_sentiment(ticker)"
        )
    except Exception as e:
        log.warning("insider_tables.init_failed", error=str(e))

    # Start price alert checker background loop
    asyncio.create_task(_price_alert_loop())
    # Pre-warm caches in background so first page load is fast
    asyncio.create_task(_warmup_caches())
    # Enrich ticker sector/industry from Yahoo Finance (delayed so DB is ready)
    asyncio.create_task(_enrich_ticker_classifications(delay=15))
    # Consume system.telemetry Redis stream and persist to execution_events
    asyncio.create_task(_telemetry_consumer())


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


@app.get("/api/jobs/{job_id}/state")
async def get_job_state(job_id: str, token: str = ""):
    """Read enabled state directly from the job key (bypasses the index set)."""
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        # Fall back to DB
        try:
            pool = await _get_db_pool()
            row = await pool.fetchrow(
                "SELECT enabled FROM scheduler_jobs WHERE id = $1", job_id
            )
            if row:
                return {"job_id": job_id, "enabled": bool(row["enabled"])}
        except Exception:
            pass
        return {"job_id": job_id, "enabled": True}  # default: on
    record = json.loads(raw)
    return {"job_id": job_id, "enabled": record.get("enabled", True)}


@app.post("/api/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, token: str = ""):
    check_token(token)
    redis = await get_redis()
    raw = await redis.get(f"{JOB_KEY_PREFIX}{job_id}")
    if raw:
        record = json.loads(raw)
    else:
        # Key missing (scheduler may have wiped the index) — try DB first
        record = None
        try:
            pool = await _get_db_pool()
            row = await pool.fetchrow("SELECT * FROM scheduler_jobs WHERE id = $1", job_id)
            if row:
                record = dict(row)
                for ts_field in ("created_at", "updated_at"):
                    if record.get(ts_field) and hasattr(record[ts_field], "isoformat"):
                        record[ts_field] = record[ts_field].isoformat()
                if isinstance(record.get("payload"), str):
                    try:
                        record["payload"] = json.loads(record["payload"])
                    except Exception:
                        record["payload"] = {}
        except Exception:
            pass
        if not record:
            record = {
                "id": job_id, "enabled": True,
                "created_at": now_et().isoformat(), "updated_at": now_et().isoformat(),
                "run_count": 0, "last_run": None, "last_status": None,
            }
    record["enabled"] = not record.get("enabled", True)
    record["updated_at"] = now_et().isoformat()
    await save_job(redis, record)
    return {"job_id": job_id, "enabled": record["enabled"]}


# ── API — Trades ──────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Recent trade fills from orders.events stream with unrealized P&L."""
    redis = await get_redis()
    entries = await redis.xrevrange(STREAMS["orders"], "+", "-", count=limit)
    trades = []
    for entry_id, fields in entries:
        try:
            ts_from_id = int(entry_id.split("-")[0]) if "-" in entry_id else 0
            ts = fields.get("ts_utc") or fields.get("ts") or ts_from_id or ""
            trades.append({
                "id":            entry_id,
                "ts":            ts,
                "ticker":        fields.get("ticker", ""),
                "asset_class":   fields.get("asset_class", ""),
                "direction":     fields.get("direction") or fields.get("side", ""),
                "qty":           fields.get("qty", ""),
                "price":         fields.get("price") or fields.get("fill_price", ""),
                "pnl":           fields.get("pnl", ""),
                "account":       fields.get("account_id") or fields.get("account_label", ""),
                "broker":        fields.get("broker", ""),
                "mode":          fields.get("mode", ""),
                "strategy":      fields.get("strategy", ""),
                "event_type":    fields.get("event_type", ""),
                "reject_reason": fields.get("reject_reason", ""),
            })
        except Exception:
            pass

    # Enrich fills that have no P&L with unrealized P&L using current prices
    fills_needing_pnl = [
        t for t in trades
        if t["event_type"] == "fill" and not t["pnl"] and t["ticker"] and t["price"]
    ]
    if fills_needing_pnl:
        tickers = list({t["ticker"] for t in fills_needing_pnl})
        current_prices: dict[str, float] = {}
        try:
            req_id = str(uuid.uuid4())
            await redis.xadd(STREAMS["broker_commands"], {
                "command":    "get_quotes",
                "request_id": req_id,
                "symbols":    ",".join(tickers),
                "issued_by":  "webui-trades",
            }, maxlen=10_000)
            reply_raw = await redis.blpop([f"broker:reply:{req_id}"], timeout=10)
            if reply_raw:
                result = json.loads(reply_raw[1])
                if not isinstance(result, list):
                    result = [result]
                for r in result:
                    # Gateway wraps as {"quotes": [{symbol, last, bid, ask, ...}, ...]}
                    quotes_list = r.get("data", {}).get("quotes", [])
                    if not isinstance(quotes_list, list):
                        quotes_list = [quotes_list]
                    for q in quotes_list:
                        if not isinstance(q, dict):
                            continue
                        sym = q.get("symbol", "").upper()
                        p = q.get("last") or q.get("ask") or q.get("bid")
                        if sym and p:
                            current_prices[sym] = float(p)
        except Exception:
            pass

        for t in fills_needing_pnl:
            cp = current_prices.get(t["ticker"].upper())
            if cp is None:
                continue
            try:
                ep  = float(t["price"])
                qty = float(t["qty"])
                mul = 1 if t["direction"] == "long" else -1
                t["pnl"] = round((cp - ep) * qty * mul, 2)
                t["current_price"] = cp
            except Exception:
                pass

    return trades


@app.get("/api/trades/options-stats")
async def get_options_trade_stats():
    """
    30-day options trade count and win rate from option_positions (Webull).
    Used by the dashboard trades smart chip.
    """
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT status, total_realized_pnl
        FROM option_positions
        WHERE entry_date >= CURRENT_DATE - INTERVAL '30 days'
          AND option_type IN ('call', 'put')
        """
    )
    total   = len(rows)
    closed  = [r for r in rows if r["status"] in ("closed", "rolled", "expired")]
    winners = [r for r in closed if r["total_realized_pnl"] and r["total_realized_pnl"] > 0]
    win_rate = round(len(winners) / len(closed) * 100, 1) if closed else 0.0
    return {
        "total":    total,
        "closed":   len(closed),
        "active":   total - len(closed),
        "winners":  len(winners),
        "win_rate": win_rate,
    }


@app.get("/api/trades/summary")
async def get_trade_summary():
    """P&L summary across all accounts."""
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
async def get_signals(limit: int = 50):
    redis = await get_redis()
    entries = await redis.xrevrange(STREAMS["signals"], "+", "-", count=limit)
    signals = []
    for entry_id, fields in entries:
        try:
            # Predictor publishes flat fields: ticker, direction, confidence,
            # asset_class, source, ttl_ms, metadata (JSON).
            # Older envelope format has a "payload" key — support both.
            if "ticker" in fields:
                meta = json.loads(fields.get("metadata", "{}"))
                ts_ms = int(entry_id.split("-")[0])
                signals.append({
                    "id":                 entry_id,
                    "ts":                 ts_ms,
                    "ticker":             fields.get("ticker", ""),
                    "asset_class":        fields.get("asset_class", ""),
                    "direction":          fields.get("direction", ""),
                    "confidence":         float(fields.get("confidence", 0)),
                    "source":             fields.get("source", "predictor"),
                    "entry":              float(fields.get("entry", 0) or 0) or None,
                    "stop":               float(fields.get("stop",  0) or 0) or None,
                    "target":             float(fields.get("target", 0) or 0) or None,
                    "ovtlyr_score":       meta.get("ovtlyr_score"),
                    "analyst_consensus":  meta.get("analyst_consensus", "none"),
                    "sentiment_label":    meta.get("sentiment_label", "neutral"),
                    "intel_summary":      meta.get("intel_summary", ""),
                    "ml_confidence":      meta.get("ml_confidence"),
                    "ml_val_accuracy":    meta.get("ml_val_accuracy"),
                    "ml_model_count":     meta.get("ml_model_count"),
                    "ml_composite_weight":meta.get("ml_composite_weight"),
                    "ml_rule_base":       meta.get("ml_rule_base"),
                    "llm_reason":         meta.get("llm_reason", ""),
                })
            else:
                payload = json.loads(fields.get("payload", "{}"))
                p = payload.get("payload", payload)
                signals.append({
                    "id":                entry_id,
                    "ts":                fields.get("ts_utc", ""),
                    "ticker":            p.get("ticker", ""),
                    "asset_class":       p.get("asset_class", ""),
                    "direction":         p.get("direction", ""),
                    "confidence":        p.get("confidence", ""),
                    "source":            p.get("source", ""),
                    "analyst_consensus": p.get("analyst_consensus", ""),
                    "sentiment_label":   p.get("sentiment", ""),
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
    import uuid as _uuid
    import json as _json

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
    if all_syms and DB_URL:
        try:
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                _rows = await conn.fetch(
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
                "cost_basis":       p.get("cost_basis", 0),
                "unrealized_pl":    p.get("unrealized_pl", 0),
                # unrealized_plpc: Alpaca provides as decimal (0.05 = 5%).
                # For brokers that don't, the client computes it from pl/cost_basis.
                "unrealized_plpc":  p.get("unrealized_plpc"),
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


def _sic_to_sector(sic_code) -> str:
    """Map a Polygon SIC code to a GICS-style sector name."""
    try:
        code = int(sic_code)
    except (TypeError, ValueError):
        return "Unknown"
    if   100  <= code <=  999:  return "Basic Materials"
    if  1000  <= code <= 1499:  return "Basic Materials"
    if  1500  <= code <= 1799:  return "Industrials"
    if  2000  <= code <= 2111:  return "Consumer Defensive"
    if  2200  <= code <= 2399:  return "Consumer Cyclical"
    if  2400  <= code <= 2499:  return "Industrials"
    if  2500  <= code <= 2599:  return "Consumer Cyclical"
    if  2600  <= code <= 2699:  return "Basic Materials"
    if  2700  <= code <= 2799:  return "Consumer Cyclical"
    if  2800  <= code <= 2829:  return "Basic Materials"
    if  2830  <= code <= 2836:  return "Healthcare"
    if  2837  <= code <= 2899:  return "Basic Materials"
    if  2900  <= code <= 2999:  return "Energy"
    if  3000  <= code <= 3299:  return "Basic Materials"
    if  3300  <= code <= 3399:  return "Basic Materials"
    if  3400  <= code <= 3499:  return "Industrials"
    if  3500  <= code <= 3599:  return "Industrials"
    if  3600  <= code <= 3699:  return "Technology"
    if  3700  <= code <= 3799:  return "Consumer Cyclical"
    if  3800  <= code <= 3840:  return "Industrials"
    if  3841  <= code <= 3851:  return "Healthcare"
    if  3852  <= code <= 3999:  return "Industrials"
    if  4000  <= code <= 4599:  return "Industrials"
    if  4600  <= code <= 4699:  return "Energy"
    if  4700  <= code <= 4799:  return "Industrials"
    if  4800  <= code <= 4899:  return "Communication Services"
    if  4900  <= code <= 4999:  return "Utilities"
    if  code == 5047:           return "Healthcare"
    if  code == 5122:           return "Healthcare"
    if  5000  <= code <= 5199:  return "Industrials"
    if  5200  <= code <= 5999:  return "Consumer Cyclical"
    if  6000  <= code <= 6299:  return "Financial Services"
    if  6300  <= code <= 6499:  return "Financial Services"
    if  6500  <= code <= 6599:  return "Real Estate"
    if  6700  <= code <= 6999:  return "Financial Services"
    if  7370  <= code <= 7379:  return "Technology"
    if  7000  <= code <= 7399:  return "Consumer Cyclical"
    if  7400  <= code <= 7999:  return "Consumer Cyclical"
    if  8000  <= code <= 8099:  return "Healthcare"
    if  8100  <= code <= 8299:  return "Industrials"
    if  8300  <= code <= 8799:  return "Industrials"
    return "Unknown"


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
    "GOOG":"Technology","META":"Technology",
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


async def _fetch_classification_yahoo(ticker: str, session) -> dict:
    """Fetch GICS sector + industry from Yahoo Finance quoteSummary API."""
    import aiohttp as _aiohttp
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=assetProfile"
    hdrs = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    try:
        async with session.get(url, headers=hdrs, timeout=_aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return {}
            d = await r.json(content_type=None)
            profile = ((d.get("quoteSummary") or {}).get("result") or [{}])[0]
            ap = profile.get("assetProfile") or {}
            return {
                "sector":      ap.get("sector")      or "",
                "industry":    ap.get("industry")    or "",
                "market_type": "Stock",
            }
    except Exception:
        return {}


async def _enrich_ticker_classifications(delay: int = 0):
    """
    Fetch sector + industry from Yahoo Finance for all active position tickers
    and persist to ticker_classification table + Redis sector/industry caches.
    Skips tickers updated within the last 30 days.
    """
    import aiohttp as _aiohttp
    if delay:
        await asyncio.sleep(delay)
    if not DB_URL:
        return
    pool   = await _get_db_pool()
    _redis = await get_redis()

    # Collect all unique underlying tickers from active positions
    tickers: set[str] = set()
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT underlying FROM option_positions WHERE status = 'active'"
        )
        tickers.update(r["underlying"] for r in rows if r["underlying"])
    except Exception:
        pass
    try:
        raw = await _redis.get("broker:position_tickers")
        if raw:
            tickers.update(json.loads(raw))
    except Exception:
        pass

    if not tickers:
        return

    # Skip tickers already classified within 30 days
    try:
        rows = await pool.fetch(
            "SELECT ticker FROM ticker_classification WHERE updated_at > NOW() - INTERVAL '30 days'"
        )
        fresh = {r["ticker"] for r in rows}
        tickers -= fresh
    except Exception:
        pass

    if not tickers:
        return

    log.info("ticker_classification.enriching", count=len(tickers))
    enriched = 0
    async with _aiohttp.ClientSession() as session:
        for ticker in sorted(tickers):
            cls = await _fetch_classification_yahoo(ticker, session)
            sector   = cls.get("sector",   "")
            industry = cls.get("industry", "")
            mtype    = cls.get("market_type", "")
            try:
                await pool.execute(
                    """INSERT INTO ticker_classification (ticker, sector, industry, market_type, updated_at)
                       VALUES ($1, $2, $3, $4, NOW())
                       ON CONFLICT (ticker) DO UPDATE
                       SET sector=EXCLUDED.sector, industry=EXCLUDED.industry,
                           market_type=EXCLUDED.market_type, updated_at=NOW()""",
                    ticker, sector or None, industry or None, mtype or None,
                )
                if sector:
                    await _redis.hset("ticker:sectors",    ticker, sector)
                if industry:
                    await _redis.hset("ticker:industries", ticker, industry)
                enriched += 1
            except Exception:
                pass
            await asyncio.sleep(0.25)   # stay polite with Yahoo's rate limits

    log.info("ticker_classification.done", enriched=enriched, total=len(tickers))


async def _fetch_sector_yahoo(ticker: str, session) -> str | None:
    """Fetch sector from Yahoo Finance chart API (free, no auth required)."""
    import aiohttp
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
async def get_position_sector_map():
    """
    Return { ticker: sector } for all current position tickers.
    Priority: Redis cache → OVTLYR signal data → DB → static map → Massive MCP → Yahoo Finance.
    Results cached in Redis hash ticker:sectors (30 day TTL per field).
    """
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
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT ON (ticker) ticker, sector FROM ovtlyr_signals "
                    "WHERE sector IS NOT NULL ORDER BY ticker, ts DESC"
                )
            for row in rows:
                if row["ticker"] not in result:
                    result[row["ticker"]] = row["sector"]
        except Exception:
            pass

    # 4. Static map fallback
    for sym in tickers:
        if sym not in result and sym in _SECTOR_STATIC:
            result[sym] = _SECTOR_STATIC[sym]

    # 4b. ticker_classification DB table (Yahoo-sourced GICS data)
    missing_cls = [sym for sym in tickers if sym not in result]
    if missing_cls and DB_URL:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                "SELECT ticker, sector FROM ticker_classification WHERE ticker = ANY($1) AND sector IS NOT NULL",
                missing_cls,
            )
            for row in rows:
                result[row["ticker"]] = row["sector"]
        except Exception:
            pass

    # 5. Polygon.io ticker details → SIC-mapped sector
    missing = [sym for sym in tickers if sym not in result]
    _poly_key = os.getenv("MASSIVE_API_KEY", "") or _read_env_file().get("MASSIVE_API_KEY", "")
    if missing and _poly_key:
        try:
            async with _aiohttp.ClientSession() as session:
                for sym in missing[:20]:
                    try:
                        url = f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={_poly_key}"
                        async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                sic = (d.get("results") or {}).get("sic_code")
                                sec = _sic_to_sector(sic) if sic else None
                                if sec and sec != "Unknown":
                                    result[sym] = sec
                    except Exception:
                        pass
        except Exception:
            pass

    # 6. Yahoo Finance chart API as last resort
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


# ── API — Ticker classification (sector + industry, Yahoo-sourced) ────────────

@app.get("/api/market/ticker-classifications")
async def get_ticker_classifications():
    """Return all stored ticker sector/industry classifications from DB."""
    if not DB_URL:
        return []
    pool = await _get_db_pool()
    rows = await pool.fetch(
        "SELECT ticker, sector, industry, market_type, updated_at "
        "FROM ticker_classification ORDER BY ticker"
    )
    return [
        {
            "ticker":      r["ticker"],
            "sector":      r["sector"],
            "industry":    r["industry"],
            "market_type": r["market_type"],
            "updated_at":  r["updated_at"].isoformat(),
        }
        for r in rows
    ]


@app.post("/api/market/ticker-classifications/refresh")
async def refresh_ticker_classifications(token: str = ""):
    """Trigger a background classification refresh for all active position tickers."""
    check_token(token)
    asyncio.create_task(_enrich_ticker_classifications())
    return {"ok": True, "message": "Enrichment started in background"}


# ── API — Market sector map (Finviz-style) ────────────────────────────────────

@app.get("/api/market/sector-map")
async def get_market_sector_map(index: str = "sp500"):
    """Return S&P 500 nested sector map via Polygon.io snapshot (MASSIVE_API_KEY)."""
    import aiohttp as _aiohttp

    # ── Sector / ticker universe per index ─────────────────────────────────────
    _NDX100_STOCKS: dict = {
        "Technology": [
            ("MSFT","Microsoft",3100,"Software"),("AAPL","Apple",3200,"Hardware"),
            ("NVDA","NVIDIA",2800,"Semiconductors"),("AVGO","Broadcom",900,"Semiconductors"),
            ("ORCL","Oracle",530,"Software"),("CRM","Salesforce",295,"Software"),
            ("AMD","AMD",220,"Semiconductors"),("NOW","ServiceNow",210,"Software"),
            ("ADBE","Adobe",195,"Software"),("QCOM","Qualcomm",185,"Semiconductors"),
            ("TXN","Texas Instr",165,"Semiconductors"),("AMAT","Applied Matls",160,"Semiconductors"),
            ("MU","Micron",120,"Semiconductors"),("KLAC","KLA Corp",95,"Semiconductors"),
            ("ADI","Analog Devices",90,"Semiconductors"),("CDNS","Cadence",88,"Software"),
            ("SNPS","Synopsys",85,"Software"),("MRVL","Marvell Tech",82,"Semiconductors"),
        ],
        "Communication Services": [
            ("META","Meta",1650,"Social & Search"),("GOOGL","Alphabet",2100,"Social & Search"),
            ("NFLX","Netflix",400,"Streaming"),("TMUS","T-Mobile",270,"Telecom"),
            ("CSCO","Cisco",220,"Networking"),
        ],
        "Consumer Discretionary": [
            ("AMZN","Amazon",2400,"E-Commerce"),("TSLA","Tesla",850,"Auto & EV"),
            ("BKNG","Booking",180,"Travel"),("MCD","McDonald's",235,"Restaurants"),
            ("SBUX","Starbucks",105,"Restaurants"),("CMG","Chipotle",92,"Restaurants"),
            ("ABNB","Airbnb",82,"Travel"),("MELI","MercadoLibre",75,"E-Commerce"),
        ],
        "Consumer Staples": [
            ("COST","Costco",440,"Warehouse Retail"),("PEP","PepsiCo",248,"Beverages"),
            ("MDLZ","Mondelez",92,"Food & Snacks"),("KDP","Keurig Dr Pepper",48,"Beverages"),
            ("MNST","Monster Bev",48,"Beverages"),
        ],
        "Health Care": [
            ("AMGN","Amgen",165,"Biotech"),("ISRG","Intuitive",225,"Med Devices"),
            ("REGN","Regeneron",90,"Biotech"),("GILD","Gilead",85,"Biotech"),
            ("IDXX","IDEXX Labs",40,"Life Sciences"),("DXCM","DexCom",35,"Med Devices"),
            ("MRNA","Moderna",30,"Biotech"),
        ],
        "Industrials": [
            ("HON","Honeywell",158,"Conglomerates"),("PCAR","PACCAR",60,"Machinery"),
            ("FAST","Fastenal",45,"Distribution"),("CTAS","Cintas",50,"Business Svcs"),
        ],
        "Financials": [
            ("PYPL","PayPal",65,"Payments"),("PAYX","Paychex",45,"Business Svcs"),
        ],
        "Software & Cloud": [
            ("INTU","Intuit",175,"Fintech Software"),("WDAY","Workday",65,"Cloud HCM"),
            ("CRWD","CrowdStrike",80,"Cybersecurity"),("PANW","Palo Alto",95,"Cybersecurity"),
            ("DDOG","Datadog",45,"Observability"),("TEAM","Atlassian",55,"Collab"),
            ("ZS","Zscaler",30,"Cybersecurity"),("FTNT","Fortinet",55,"Cybersecurity"),
            ("APP","AppLovin",70,"Ad Tech"),
        ],
    }

    _NDX100_ETFS = {
        "Technology": "QQQ", "Communication Services": "XLC",
        "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
        "Health Care": "XLV", "Industrials": "XLI",
        "Financials": "XLF", "Software & Cloud": "IGV",
    }

    _DOW30_STOCKS: dict = {
        "Technology": [
            ("AAPL","Apple",3200,"Hardware"),("MSFT","Microsoft",3100,"Software"),
            ("NVDA","NVIDIA",2800,"Semiconductors"),("CRM","Salesforce",295,"Software"),
            ("IBM","IBM",190,"IT Services"),("CSCO","Cisco",220,"Networking"),
        ],
        "Financials": [
            ("JPM","JPMorgan",780,"Banks"),("GS","Goldman Sachs",225,"Capital Markets"),
            ("V","Visa",640,"Payments"),("AXP","AmEx",220,"Payments"),
        ],
        "Health Care": [
            ("UNH","UnitedHealth",540,"Health Services"),("JNJ","J&J",395,"Pharma"),
            ("MRK","Merck",315,"Pharma"),("AMGN","Amgen",165,"Biotech"),
        ],
        "Industrials": [
            ("BA","Boeing",125,"Aerospace/Defense"),("CAT","Caterpillar",190,"Machinery"),
            ("HON","Honeywell",158,"Conglomerates"),("MMM","3M",62,"Conglomerates"),
        ],
        "Consumer Discretionary": [
            ("AMZN","Amazon",2400,"E-Commerce"),("HD","Home Depot",385,"Home Improvement"),
            ("MCD","McDonald's",235,"Restaurants"),("NKE","Nike",115,"Apparel"),
            ("DIS","Disney",195,"Entertainment"),
        ],
        "Consumer Staples": [
            ("WMT","Walmart",800,"Food Retail"),("KO","Coca-Cola",315,"Beverages"),
            ("PG","P&G",385,"Household Products"),
        ],
        "Energy": [
            ("CVX","Chevron",295,"Integrated Oil"),
        ],
        "Communication": [
            ("VZ","Verizon",165,"Telecom"),
        ],
        "Materials": [
            ("SHW","Sherwin-Williams",97,"Specialty Chems"),
            ("DOW","Dow Inc",42,"Commodity Chems"),
        ],
        "Insurance": [
            ("TRV","Travelers",62,"P&C Insurance"),
        ],
    }

    _DOW30_ETFS = {
        "Technology": "XLK","Financials":"XLF","Health Care":"XLV",
        "Industrials":"XLI","Consumer Discretionary":"XLY","Consumer Staples":"XLP",
        "Energy":"XLE","Communication":"XLC","Materials":"XLB","Insurance":"XLF",
    }

    _RUT2000_STOCKS: dict = {
        "Financials": [
            ("VIRT","Virtu Finl",4,"Market Making"),("CUBI","Customers Bancorp",2,"Banks"),
            ("TBBK","The Bancorp",3,"Banks"),("HOMB","Home Bancorp",2,"Banks"),
            ("IIPR","Innovative Ind REIT",4,"Healthcare REIT"),("STAG","STAG Ind REIT",7,"Industrial REIT"),
            ("PLMR","Palomar Holdings",4,"Insurance"),("KLIC","Kulicke & Soffa",3,"Capital Markets"),
            ("WD","Walker & Dunlop",3,"Real Estate Svcs"),("ARIS","Aris Water",2,"Specialty Finance"),
        ],
        "Health Care": [
            ("INSP","Inspire Medical",8,"Med Devices"),("AXNX","Axonics",3,"Med Devices"),
            ("PRVA","Privia Health",3,"Health Services"),("HIMS","Hims & Hers",4,"Telehealth"),
            ("ACAD","Acadia Pharma",4,"Biotech"),("SRPT","Sarepta Therap",8,"Biotech"),
            ("RXRX","Recursion Pharma",3,"Biotech"),("VCEL","Vericel",3,"Biotech"),
        ],
        "Technology": [
            ("RIOT","Riot Platforms",4,"Crypto Mining"),("MARA","Marathon Digital",5,"Crypto Mining"),
            ("CLBT","Cellebrite",5,"Cybersecurity"),("SPSC","SPS Commerce",5,"Supply Chain SW"),
            ("POWI","Power Integrations",4,"Semiconductors"),("DIOD","Diodes Inc",3,"Semiconductors"),
            ("CEVA","CEVA Inc",2,"Semiconductors"),("AMBA","Ambarella",3,"Semiconductors"),
        ],
        "Industrials": [
            ("KTOS","Kratos Defense",5,"Aerospace/Defense"),("AXON","Axon Enterprise",25,"Public Safety"),
            ("HTLF","Heartland BancCorp",3,"Transport"),("GNRC","Generac",15,"Electrical Equip"),
            ("POWL","Powell Industries",3,"Electrical Equip"),("GTES","Gates Industrial",4,"Machinery"),
        ],
        "Consumer Discretionary": [
            ("MODG","Acushnet",4,"Leisure"),("GOOS","Canada Goose",3,"Apparel"),
            ("OXM","Oxford Industries",3,"Apparel"),("BOOT","Boot Barn",7,"Apparel"),
            ("XPOF","Xponential Fitness",2,"Fitness"),("WINA","Winmark",2,"Franchise"),
        ],
        "Consumer Staples": [
            ("COKE","Coca-Cola Consol",12,"Beverages"),("FIZZ","National Bev",8,"Beverages"),
            ("CENTA","Central Garden",3,"Pet & Garden"),("JJSF","J&J Snack Foods",4,"Food"),
        ],
        "Energy": [
            ("CIVI","Civitas Resources",8,"E&P"),("MGY","Magnolia Oil",4,"E&P"),
            ("VTLE","Vital Energy",3,"E&P"),("NOG","Northern Oil",4,"E&P"),
        ],
        "Materials": [
            ("TROX","Tronox",3,"Specialty Chems"),("KMPR","Kemper",3,"Specialty Chems"),
            ("MERC","Mercer Intl",2,"Paper & Forest"),
        ],
        "Real Estate": [
            ("REXR","Rexford Ind REIT",10,"Industrial REIT"),("CUBE","CubeSmart",9,"Self-Storage"),
            ("IIPR","Innovative Ind REIT",4,"Healthcare REIT"),("IRT","Independence Realty",4,"Apartment REIT"),
        ],
    }

    _RUT2000_ETFS = {
        "Financials":"IWM","Health Care":"IWM","Technology":"IWM",
        "Industrials":"IWM","Consumer Discretionary":"IWM","Consumer Staples":"IWM",
        "Energy":"IWM","Materials":"IWM","Real Estate":"IWM",
    }

    _FUTURES_STOCKS: dict = {
        "Equity Index": [
            ("ES=F","S&P 500 Fut",1000,"Large Cap"),("NQ=F","Nasdaq 100 Fut",800,"Tech"),
            ("YM=F","Dow Jones Fut",300,"Blue Chip"),("RTY=F","Russell 2000 Fut",200,"Small Cap"),
            ("EMD=F","S&P MidCap 400",80,"Mid Cap"),
        ],
        "Energy": [
            ("CL=F","Crude Oil WTI",500,"Crude"),("BZ=F","Brent Crude",400,"Crude"),
            ("NG=F","Natural Gas",200,"Gas"),("RB=F","RBOB Gasoline",150,"Refined"),
            ("HO=F","Heating Oil",130,"Refined"),
        ],
        "Metals": [
            ("GC=F","Gold",600,"Precious"),("SI=F","Silver",150,"Precious"),
            ("HG=F","Copper",200,"Industrial"),("PL=F","Platinum",80,"Precious"),
            ("PA=F","Palladium",60,"Precious"),
        ],
        "Agriculture": [
            ("ZC=F","Corn",200,"Grain"),("ZS=F","Soybeans",180,"Grain"),
            ("ZW=F","Wheat",120,"Grain"),("KC=F","Coffee",90,"Soft"),
            ("SB=F","Sugar",70,"Soft"),("CT=F","Cotton",60,"Soft"),
        ],
        "Currencies": [
            ("DX-Y.NYB","Dollar Index",250,"Major"),("6E=F","Euro FX",400,"Major"),
            ("6J=F","Japanese Yen",300,"Major"),("6B=F","British Pound",200,"Major"),
            ("6C=F","Canadian Dollar",150,"Major"),("6A=F","Australian Dollar",120,"Major"),
        ],
        "Fixed Income": [
            ("ZN=F","10Y T-Note",600,"Treasury"),("ZB=F","30Y T-Bond",500,"Treasury"),
            ("ZF=F","5Y T-Note",300,"Treasury"),("ZT=F","2Y T-Note",200,"Treasury"),
            ("GE=F","Eurodollar",150,"Short Rate"),
        ],
    }

    _FUTURES_ETFS = {
        "Equity Index":"SPY","Energy":"XLE","Metals":"GLD",
        "Agriculture":"DBA","Currencies":"UUP","Fixed Income":"TLT",
    }

    _SP500_STOCKS: dict = {
        "Technology": [
            ("MSFT","Microsoft",3100,"Software"),("AAPL","Apple",3200,"Hardware"),
            ("NVDA","NVIDIA",2800,"Semiconductors"),("AVGO","Broadcom",900,"Semiconductors"),
            ("ORCL","Oracle",530,"Software"),("CRM","Salesforce",295,"Software"),
            ("AMD","AMD",220,"Semiconductors"),("NOW","ServiceNow",210,"Software"),
            ("ADBE","Adobe",195,"Software"),("ACN","Accenture",190,"Hardware"),
            ("QCOM","Qualcomm",185,"Semiconductors"),("TXN","Texas Instr",165,"Semiconductors"),
            ("AMAT","Applied Matls",160,"Semiconductors"),("MU","Micron",120,"Semiconductors"),
            ("INTC","Intel",95,"Hardware"),
        ],
        "Financials": [
            ("BRK-B","Berkshire",950,"Diversified"),("JPM","JPMorgan",780,"Banks"),
            ("V","Visa",640,"Payments"),("MA","Mastercard",520,"Payments"),
            ("BAC","Bank of Amer",335,"Banks"),("WFC","Wells Fargo",275,"Banks"),
            ("AXP","AmEx",220,"Payments"),("GS","Goldman Sachs",225,"Capital Markets"),
            ("MS","Morgan Stanley",205,"Capital Markets"),("PGR","Progressive",140,"Insurance"),
            ("BLK","BlackRock",155,"Asset Mgmt"),("SCHW","Schwab",135,"Asset Mgmt"),
            ("C","Citigroup",135,"Banks"),
        ],
        "Health Care": [
            ("LLY","Eli Lilly",850,"Pharma"),("UNH","UnitedHealth",540,"Health Services"),
            ("JNJ","J&J",395,"Pharma"),("ABBV","AbbVie",380,"Pharma"),
            ("MRK","Merck",315,"Pharma"),("ISRG","Intuitive",225,"Med Devices"),
            ("TMO","Thermo Fisher",215,"Life Sciences"),("ABT","Abbott",205,"Med Devices"),
            ("AMGN","Amgen",165,"Biotech"),("DHR","Danaher",175,"Life Sciences"),
            ("PFE","Pfizer",155,"Pharma"),("BMY","Bristol-Myers",140,"Pharma"),
        ],
        "Consumer Discretionary": [
            ("AMZN","Amazon",2400,"Retail"),("TSLA","Tesla",850,"Auto & EV"),
            ("HD","Home Depot",385,"Home Improvement"),("MCD","McDonald's",235,"Restaurants"),
            ("BKNG","Booking",180,"Travel"),("LOW","Lowe's",148,"Home Improvement"),
            ("TJX","TJX",145,"Retail"),("NKE","Nike",115,"Retail"),
            ("SBUX","Starbucks",105,"Restaurants"),("CMG","Chipotle",92,"Restaurants"),
            ("ABNB","Airbnb",82,"Travel"),
        ],
        "Industrials": [
            ("GE","GE",235,"Aerospace/Defense"),("CAT","Caterpillar",190,"Machinery"),
            ("ETN","Eaton",135,"Electrical Equip"),("RTX","RTX",175,"Aerospace/Defense"),
            ("HON","Honeywell",158,"Conglomerates"),("UNP","Union Pacific",152,"Transport"),
            ("DE","Deere",128,"Machinery"),("LMT","Lockheed",138,"Aerospace/Defense"),
            ("BA","Boeing",125,"Aerospace/Defense"),("UPS","UPS",88,"Transport"),
            ("GEV","GE Vernova",85,"Electrical Equip"),("PH","Parker Hannifin",82,"Machinery"),
            ("FDX","FedEx",67,"Transport"),
        ],
        "Communication Services": [
            ("META","Meta",1650,"Social & Search"),("GOOGL","Alphabet",2100,"Social & Search"),
            ("NFLX","Netflix",400,"Media & Entertainment"),("TMUS","T-Mobile",270,"Telecom"),
            ("DIS","Disney",195,"Media & Entertainment"),("CMCSA","Comcast",145,"Media & Entertainment"),
            ("VZ","Verizon",165,"Telecom"),("T","AT&T",140,"Telecom"),
            ("EA","Electronic Arts",32,"Media & Entertainment"),
        ],
        "Consumer Staples": [
            ("WMT","Walmart",800,"Food Retail"),("COST","Costco",440,"Food Retail"),
            ("PG","P&G",385,"Household Products"),("KO","Coca-Cola",315,"Beverages"),
            ("PEP","PepsiCo",248,"Beverages"),("PM","Philip Morris",235,"Tobacco"),
            ("MDLZ","Mondelez",92,"Food & Snacks"),("MO","Altria",88,"Tobacco"),
            ("CL","Colgate",72,"Household Products"),("GIS","General Mills",42,"Food & Snacks"),
        ],
        "Energy": [
            ("XOM","ExxonMobil",570,"Integrated Oil"),("CVX","Chevron",295,"Integrated Oil"),
            ("COP","ConocoPhillips",138,"E&P"),("EOG","EOG",68,"E&P"),
            ("SLB","SLB",66,"Oilfield Services"),("MPC","Marathon",62,"Refining"),
            ("PSX","Phillips 66",58,"Refining"),("OXY","Occidental",52,"E&P"),
            ("HES","Hess",46,"E&P"),("VLO","Valero",45,"Refining"),
        ],
        "Real Estate": [
            ("PLD","Prologis",108,"Industrial REIT"),("AMT","Amer Tower",92,"Tower REIT"),
            ("EQIX","Equinix",83,"Data Center REIT"),("WELL","Welltower",72,"Healthcare REIT"),
            ("SPG","Simon Property",62,"Retail REIT"),("PSA","Public Storage",56,"Self-Storage"),
            ("O","Realty Income",53,"Retail REIT"),("DLR","Digital Realty",52,"Data Center REIT"),
            ("CBRE","CBRE",37,"Real Estate Svcs"),
        ],
        "Utilities": [
            ("NEE","NextEra",158,"Electric"),("SO","Southern Co",97,"Electric"),
            ("DUK","Duke Energy",92,"Electric"),("SRE","Sempra",57,"Multi-Utility"),
            ("AEP","AEP",56,"Electric"),("D","Dominion",52,"Electric"),
            ("EXC","Exelon",39,"Electric"),("PCG","PG&E",39,"Electric"),
            ("XEL","Xcel Energy",36,"Electric"),
        ],
        "Materials": [
            ("LIN","Linde",235,"Industrial Gases"),("SHW","Sherwin-Williams",97,"Specialty Chems"),
            ("ECL","Ecolab",62,"Specialty Chems"),("APD","Air Products",60,"Industrial Gases"),
            ("FCX","Freeport",58,"Copper Mining"),("NEM","Newmont",57,"Gold Mining"),
            ("PPG","PPG",37,"Specialty Chems"),("NUE","Nucor",32,"Steel"),
            ("IFF","IFF",22,"Specialty Chems"),
        ],
    }

    _SP500_ETFS = {
        "Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
        "Consumer Discretionary": "XLY", "Industrials": "XLI",
        "Communication Services": "XLC", "Consumer Staples": "XLP",
        "Energy": "XLE", "Real Estate": "XLRE", "Utilities": "XLU", "Materials": "XLB",
    }

    _INDEX_MAP = {
        "sp500":   (_SP500_STOCKS,   _SP500_ETFS),
        "ndx100":  (_NDX100_STOCKS,  _NDX100_ETFS),
        "dow30":   (_DOW30_STOCKS,   _DOW30_ETFS),
        "rut2000": (_RUT2000_STOCKS, _RUT2000_ETFS),
        "futures": (_FUTURES_STOCKS, _FUTURES_ETFS),
    }
    SECTOR_STOCKS, SECTOR_ETFS = _INDEX_MAP.get(index, _INDEX_MAP["sp500"])

    _redis = await get_redis()
    cache_key = f"market:sector_map_{index}_v3"
    try:
        cached = await _redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    all_tickers = [t for stocks in SECTOR_STOCKS.values() for t, _, _, _ in stocks]
    changes: dict[str, tuple[float, float]] = {}  # ticker -> (change_pct, price)

    is_futures = index == "futures"  # futures tickers use =F suffix, incompatible with Polygon stock API

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if api_key and not is_futures:
        # Polygon.io snapshot: single async request for all tickers (stocks only)
        tickers_str = ",".join(all_tickers)
        url = (
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
            f"?tickers={tickers_str}&apiKey={api_key}"
        )
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for t in data.get("tickers", []):
                            sym   = t.get("ticker", "")
                            chg   = round(float(t.get("todaysChangePerc") or 0), 2)
                            price = round(float((t.get("day") or {}).get("c") or
                                                (t.get("lastTrade") or {}).get("p") or 0), 2)
                            changes[sym] = (chg, price)
        except Exception as e:
            log.warning("sector_map.polygon_error", error=str(e))

    # Yahoo Finance fallback via v8/finance/chart (per-ticker, concurrent).
    # Used for futures (=F tickers Polygon doesn't serve) and any stocks Polygon missed.
    missing = [t for t in all_tickers if t not in changes]
    if missing:
        sem = asyncio.Semaphore(10)

        async def _yf_chart(session, ticker):
            async with sem:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                    f"?interval=1d&range=2d"
                )
                try:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            d = await resp.json()
                            res = ((d.get("chart") or {}).get("result") or [None])[0]
                            if res:
                                meta  = res.get("meta", {})
                                raw_price = float(meta.get("regularMarketPrice") or 0)
                                prev      = float(meta.get("chartPreviousClose") or 0)
                                # Use unrounded values for % calc to avoid precision loss on small-priced futures (e.g. 6J=F)
                                chg   = round((raw_price / prev - 1) * 100, 2) if prev else 0.0
                                price = round(raw_price, 4)
                                return ticker, (chg, price)
                except Exception:
                    pass
                return ticker, None

        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with _aiohttp.ClientSession(headers=headers) as session:
                results = await asyncio.gather(*[_yf_chart(session, t) for t in missing])
            for tkr, val in results:
                if val:
                    changes[tkr] = val
        except Exception as e:
            log.warning("sector_map.yfinance_error", error=str(e))

    sectors = []
    for sname, stocks in SECTOR_STOCKS.items():
        enriched, total_mcap, weighted_chg = [], 0, 0.0
        for ticker, name, mcap, subsector in stocks:
            chg, price = changes.get(ticker, (0.0, 0.0))
            enriched.append({"ticker": ticker, "name": name, "mcap": mcap,
                              "subsector": subsector, "change": chg, "price": price})
            total_mcap  += mcap
            weighted_chg += chg * mcap
        avg_chg = round(weighted_chg / total_mcap, 2) if total_mcap else 0.0
        sectors.append({
            "name": sname, "etf": SECTOR_ETFS.get(sname, ""),
            "mcap": total_mcap, "change": avg_chg, "stocks": enriched,
        })

    result = {"sectors": sectors, "as_of": datetime.utcnow().isoformat()}
    try:
        await _redis.setex(cache_key, 300, json.dumps(result))
    except Exception:
        pass
    return result


# ── API — Sparklines for sector panel (Polygon.io) ────────────────────────────

@app.get("/api/market/sparklines")
async def get_market_sparklines(tickers: str = ""):
    """Return last 6 daily closes per ticker for sector hover sparklines."""
    import aiohttp as _aiohttp
    from datetime import date as _date, timedelta as _td

    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:30]
    if not ticker_list:
        return {}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {t: [] for t in ticker_list}

    _redis = await get_redis()
    results: dict = {}
    need_fetch: list = []

    for tkr in ticker_list:
        try:
            cached = await _redis.get(f"sparkline:{tkr}")
            if cached:
                results[tkr] = json.loads(cached)
                continue
        except Exception:
            pass
        need_fetch.append(tkr)

    if need_fetch:
        today   = _date.today().isoformat()
        from_dt = (_date.today() - _td(days=10)).isoformat()
        sem     = asyncio.Semaphore(10)

        async def _fetch(session, ticker):
            async with sem:
                url = (
                    f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
                    f"/{from_dt}/{today}?adjusted=true&sort=asc&limit=6&apiKey={api_key}"
                )
                try:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            d = await resp.json()
                            closes = [round(float(b["c"]), 2)
                                      for b in (d.get("results") or [])[-6:]]
                            return ticker, closes
                except Exception:
                    pass
                return ticker, []

        async with _aiohttp.ClientSession() as session:
            fetched = await asyncio.gather(*[_fetch(session, t) for t in need_fetch])

        for tkr, closes in fetched:
            results[tkr] = closes

        # Yahoo Finance fallback for tickers Polygon couldn't serve (e.g. futures =F format)
        yf_missing = [tkr for tkr, closes in fetched if not closes]
        if yf_missing:
            async def _yf_fetch(session, ticker):
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                    f"?interval=1d&range=10d"
                )
                try:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10),
                                           headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            d = await resp.json()
                            res = ((d.get("chart") or {}).get("result") or [None])[0]
                            if res:
                                closes = [round(float(c), 2) for c in
                                          (res.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                                          if c is not None][-6:]
                                return ticker, closes
                except Exception:
                    pass
                return ticker, []

            async with _aiohttp.ClientSession() as session:
                yf_fetched = await asyncio.gather(*[_yf_fetch(session, t) for t in yf_missing])
            for tkr, closes in yf_fetched:
                if closes:
                    results[tkr] = closes

        for tkr, closes in results.items():
            try:
                await _redis.setex(f"sparkline:{tkr}", 300, json.dumps(closes))
            except Exception:
                pass

    return results


# ── API — Market Groups (Finviz-style performance table) ─────────────────────

_MG_SECTORS = [
    ("XLK",  "Technology"),
    ("XLF",  "Financials"),
    ("XLE",  "Energy"),
    ("XLV",  "Health Care"),
    ("XLI",  "Industrials"),
    ("XLC",  "Communication Services"),
    ("XLY",  "Consumer Discretionary"),
    ("XLP",  "Consumer Staples"),
    ("XLRE", "Real Estate"),
    ("XLB",  "Materials"),
    ("XLU",  "Utilities"),
]

_MG_INDUSTRIES = [
    ("SOXX", "Semiconductors"),
    ("IGV",  "Software"),
    ("XBI",  "Biotech"),
    ("IBB",  "Biotech (iShares)"),
    ("KRE",  "Regional Banks"),
    ("XHB",  "Homebuilders"),
    ("XRT",  "Retail"),
    ("XOP",  "Oil & Gas E&P"),
    ("IYT",  "Transportation"),
    ("HACK", "Cybersecurity"),
    ("XME",  "Metals & Mining"),
    ("KIE",  "Insurance"),
    ("ARKK", "ARK Innovation"),
    ("SKYY", "Cloud Computing"),
    ("UUP",  "US Dollar"),
    ("GDX",  "Gold Miners"),
    ("CIBR", "Cybersecurity (iShares)"),
]

_MG_INDICES = [
    ("SPY",  "S&P 500"),
    ("QQQ",  "Nasdaq 100"),
    ("DIA",  "Dow Jones Industrial"),
    ("IWM",  "Russell 2000"),
    ("MDY",  "S&P MidCap 400"),
    ("VTI",  "Total US Market"),
    ("EFA",  "Developed Markets"),
    ("EEM",  "Emerging Markets"),
    ("GLD",  "Gold"),
    ("SLV",  "Silver"),
    ("TLT",  "20+ Year Treasuries"),
    ("HYG",  "High Yield Bonds"),
    ("LQD",  "Investment Grade Bonds"),
    ("VNQ",  "Real Estate (REITs)"),
    ("BIL",  "Short-Term T-Bills"),
]

_MG_GROUP_MAP = {
    "sectors":    _MG_SECTORS,
    "industries": _MG_INDUSTRIES,
    "indices":    _MG_INDICES,
}


@app.get("/api/market/groups")
async def get_market_groups(group: str = "sectors"):
    """Return ETF performance table (1D/1W/1M/3M/6M/YTD/1Y) via Polygon.io."""
    import aiohttp as _aiohttp
    import datetime as _dt

    group = group.lower()
    if group not in _MG_GROUP_MAP:
        group = "sectors"

    _redis = await get_redis()
    cache_key = f"market:groups_{group}"
    try:
        cached = await _redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    group_items = _MG_GROUP_MAP[group]
    tickers     = [t for t, _ in group_items]

    api_key  = os.getenv("MASSIVE_API_KEY", "")
    today    = _dt.date.today()
    today_s  = today.isoformat()
    from_s   = (today - _dt.timedelta(days=400)).isoformat()  # ~1Y + holiday buffer
    ytd_ms   = int(_dt.datetime(today.year, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)

    snap_1d:  dict[str, tuple[float, float, int]] = {}  # ticker -> (chg_pct, price, vol)
    aggs_map: dict[str, list]                     = {}  # ticker -> [{t, c}, ...]

    if api_key:
        # ── Snapshot for 1D change ────────────────────────────────────────────
        snap_url = (
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
            f"?tickers={','.join(tickers)}&apiKey={api_key}"
        )
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(snap_url, timeout=_aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for t in data.get("tickers", []):
                            sym   = t.get("ticker", "")
                            chg   = round(float(t.get("todaysChangePerc") or 0), 2)
                            day   = t.get("day") or {}
                            price = round(float(day.get("c") or
                                          (t.get("lastTrade") or {}).get("p") or 0), 2)
                            vol   = int(day.get("v") or 0)
                            snap_1d[sym] = (chg, price, vol)
        except Exception as e:
            log.warning("market_groups.snapshot_error", error=str(e))

        # ── Historical aggs for period returns ───────────────────────────────
        sem = asyncio.Semaphore(8)

        async def _fetch_agg(sess, tkr):
            async with sem:
                url = (
                    f"https://api.polygon.io/v2/aggs/ticker/{tkr}/range/1/day"
                    f"/{from_s}/{today_s}?adjusted=true&sort=asc&limit=500&apiKey={api_key}"
                )
                try:
                    async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            d    = await r.json()
                            bars = [{"t": b["t"], "c": float(b["c"])}
                                    for b in (d.get("results") or []) if "c" in b]
                            return tkr, bars
                except Exception as e:
                    log.warning("market_groups.agg_error", ticker=tkr, error=str(e))
                return tkr, []

        async with _aiohttp.ClientSession() as sess:
            fetched = await asyncio.gather(*[_fetch_agg(sess, t) for t in tickers])
        for tkr, bars in fetched:
            aggs_map[tkr] = bars

    def _pct(bars: list, n_back: int) -> float | None:
        if len(bars) < 2:
            return None
        cur  = bars[-1]["c"]
        idx  = max(0, len(bars) - 1 - n_back)
        past = bars[idx]["c"]
        return round((cur - past) / past * 100, 2) if past else None

    def _ytd(bars: list) -> float | None:
        if not bars:
            return None
        pre = [b for b in bars if b["t"] < ytd_ms]
        if not pre:
            return None
        past = pre[-1]["c"]
        cur  = bars[-1]["c"]
        return round((cur - past) / past * 100, 2) if past else None

    items = []
    for ticker, display_name in group_items:
        bars            = aggs_map.get(ticker, [])
        chg, price, vol = snap_1d.get(ticker, (0.0, 0.0, 0))

        if chg == 0.0 and len(bars) >= 2:
            c, p = bars[-1]["c"], bars[-2]["c"]
            if p:
                chg = round((c - p) / p * 100, 2)
        if price == 0.0 and bars:
            price = round(bars[-1]["c"], 2)

        items.append({
            "ticker":    ticker,
            "name":      display_name,
            "price":     price,
            "volume":    vol,
            "perf_1d":   chg,
            "perf_1w":   _pct(bars, 5),
            "perf_1m":   _pct(bars, 21),
            "perf_3m":   _pct(bars, 63),
            "perf_6m":   _pct(bars, 126),
            "perf_ytd":  _ytd(bars),
            "perf_1y":   _pct(bars, 252),
        })

    result = {"group": group, "items": items, "as_of": datetime.utcnow().isoformat()}
    try:
        await _redis.setex(cache_key, 1800, json.dumps(result))
    except Exception:
        pass
    return result


# ── Helper: expose SP500 universe without duplicating the data dict ───────────

async def _resolve_sp500_universe():
    """Return (_SP500_STOCKS, _SP500_ETFS) from module-level cache."""
    global _SP500_UNIVERSE_CACHE
    if _SP500_UNIVERSE_CACHE is not None:
        return _SP500_UNIVERSE_CACHE
    _SP500_STOCKS_LOCAL = {
        "Technology": [
            ("MSFT","Microsoft",3100,"Software"),("AAPL","Apple",3200,"Hardware"),
            ("NVDA","NVIDIA",2800,"Semiconductors"),("AVGO","Broadcom",900,"Semiconductors"),
            ("ORCL","Oracle",530,"Software"),("CRM","Salesforce",295,"Software"),
            ("AMD","AMD",220,"Semiconductors"),("NOW","ServiceNow",210,"Software"),
            ("ADBE","Adobe",195,"Software"),("ACN","Accenture",190,"Hardware"),
            ("QCOM","Qualcomm",185,"Semiconductors"),("TXN","Texas Instr",165,"Semiconductors"),
            ("AMAT","Applied Matls",160,"Semiconductors"),("MU","Micron",120,"Semiconductors"),
            ("INTC","Intel",95,"Hardware"),
        ],
        "Financials": [
            ("BRK-B","Berkshire",950,"Diversified"),("JPM","JPMorgan",780,"Banks"),
            ("V","Visa",640,"Payments"),("MA","Mastercard",520,"Payments"),
            ("BAC","Bank of Amer",335,"Banks"),("WFC","Wells Fargo",275,"Banks"),
            ("AXP","AmEx",220,"Payments"),("GS","Goldman Sachs",225,"Capital Markets"),
            ("MS","Morgan Stanley",205,"Capital Markets"),("PGR","Progressive",140,"Insurance"),
            ("BLK","BlackRock",155,"Asset Mgmt"),("SCHW","Schwab",135,"Asset Mgmt"),
            ("C","Citigroup",135,"Banks"),
        ],
        "Health Care": [
            ("LLY","Eli Lilly",850,"Pharma"),("UNH","UnitedHealth",540,"Health Services"),
            ("JNJ","J&J",395,"Pharma"),("ABBV","AbbVie",380,"Pharma"),
            ("MRK","Merck",315,"Pharma"),("ISRG","Intuitive",225,"Med Devices"),
            ("TMO","Thermo Fisher",215,"Life Sciences"),("ABT","Abbott",205,"Med Devices"),
            ("AMGN","Amgen",165,"Biotech"),("DHR","Danaher",175,"Life Sciences"),
            ("PFE","Pfizer",155,"Pharma"),("BMY","Bristol-Myers",140,"Pharma"),
        ],
        "Consumer Discretionary": [
            ("AMZN","Amazon",2400,"Retail"),("TSLA","Tesla",850,"Auto & EV"),
            ("HD","Home Depot",385,"Home Improvement"),("MCD","McDonald's",235,"Restaurants"),
            ("BKNG","Booking",180,"Travel"),("LOW","Lowe's",148,"Home Improvement"),
            ("TJX","TJX",145,"Retail"),("NKE","Nike",115,"Retail"),
            ("SBUX","Starbucks",105,"Restaurants"),("CMG","Chipotle",92,"Restaurants"),
            ("ABNB","Airbnb",82,"Travel"),
        ],
        "Industrials": [
            ("GE","GE",235,"Aerospace/Defense"),("CAT","Caterpillar",190,"Machinery"),
            ("ETN","Eaton",135,"Electrical Equip"),("RTX","RTX",140,"Aerospace/Defense"),
            ("HON","Honeywell",130,"Conglomerates"),("UNP","Union Pacific",145,"Rail"),
            ("LMT","Lockheed",120,"Aerospace/Defense"),("DE","Deere",115,"Machinery"),
            ("GEV","GE Vernova",105,"Electrical Equip"),("NOC","Northrop",100,"Aerospace/Defense"),
            ("ITW","Illinois Tool",105,"Machinery"),("FDX","FedEx",65,"Logistics"),
            ("EMR","Emerson",75,"Electrical Equip"),
        ],
        "Communication Services": [
            ("META","Meta",1400,"Social & Search"),("GOOGL","Alphabet",2200,"Social & Search"),
            ("NFLX","Netflix",390,"Media & Entertainment"),("DIS","Disney",200,"Media & Entertainment"),
            ("CMCSA","Comcast",155,"Media & Entertainment"),("T","AT&T",150,"Telecom"),
            ("VZ","Verizon",165,"Telecom"),("EA","EA",36,"Media & Entertainment"),
            ("TTWO","Take-Two",33,"Media & Entertainment"),
        ],
        "Consumer Staples": [
            ("WMT","Walmart",680,"Retail"),("PG","Procter & Gamble",380,"Household"),
            ("COST","Costco",380,"Retail"),("KO","Coca-Cola",285,"Beverages"),
            ("PEP","PepsiCo",220,"Beverages"),("PM","Philip Morris",195,"Tobacco"),
            ("MDLZ","Mondelez",85,"Food"),("CL","Colgate",65,"Household"),
            ("GIS","General Mills",35,"Food"),("MO","Altria",90,"Tobacco"),
        ],
        "Energy": [
            ("XOM","ExxonMobil",530,"Oil & Gas"),("CVX","Chevron",290,"Oil & Gas"),
            ("COP","ConocoPhillips",130,"Oil & Gas"),("EOG","EOG Resources",75,"Oil & Gas"),
            ("SLB","SLB",58,"Oilfield Services"),("MPC","Marathon Petro",60,"Refining"),
            ("PSX","Phillips 66",55,"Refining"),("VLO","Valero",50,"Refining"),
            ("KMI","Kinder Morgan",45,"Pipelines"),("OXY","Occidental",50,"Oil & Gas"),
        ],
        "Real Estate": [
            ("PLD","Prologis",115,"Industrial REITs"),("AMT","American Tower",90,"Infrastructure"),
            ("EQIX","Equinix",80,"Data Centers"),("WELL","Welltower",75,"Healthcare REITs"),
            ("SPG","Simon Property",65,"Retail REITs"),("O","Realty Income",55,"Retail REITs"),
            ("DLR","Digital Realty",50,"Data Centers"),("PSA","Public Storage",55,"Storage REITs"),
            ("CCI","Crown Castle",45,"Infrastructure"),
        ],
        "Utilities": [
            ("NEE","NextEra",165,"Electric"),("SO","Southern",90,"Electric"),
            ("DUK","Duke Energy",90,"Electric"),("SRE","Sempra",55,"Multi"),
            ("AEP","AEP",55,"Electric"),("CEG","Constellation",85,"Nuclear"),
            ("PCG","PG&E",50,"Electric"),("EXC","Exelon",45,"Electric"),
            ("XEL","Xcel Energy",35,"Electric"),
        ],
        "Materials": [
            ("LIN","Linde",225,"Chemicals"),("APD","Air Products",65,"Chemicals"),
            ("SHW","Sherwin-Williams",105,"Chemicals"),("ECL","Ecolab",65,"Chemicals"),
            ("NEM","Newmont",55,"Gold Mining"),("FCX","Freeport",55,"Copper"),
            ("NUE","Nucor",35,"Steel"),("DOW","Dow",45,"Chemicals"),
            ("VMC","Vulcan Materials",35,"Construction Materials"),
        ],
    }
    _SP500_ETFS_LOCAL = {
        "Technology":"XLK","Financials":"XLF","Health Care":"XLV",
        "Consumer Discretionary":"XLY","Industrials":"XLI",
        "Communication Services":"XLC","Consumer Staples":"XLP",
        "Energy":"XLE","Real Estate":"XLRE","Utilities":"XLU","Materials":"XLB",
    }
    _SP500_UNIVERSE_CACHE = (_SP500_STOCKS_LOCAL, _SP500_ETFS_LOCAL)
    return _SP500_UNIVERSE_CACHE

_SP500_UNIVERSE_CACHE = None   # module-level cache


# ── API — Sector Leaders (daily ranking + consecutive-day streak) ─────────────

@app.get("/api/market/sector-leaders")
async def get_sector_leaders(refresh: bool = False):
    """
    Daily ranked sector leaders with consecutive-day streak data.
    Fetches today's Polygon snapshot, stores per-sector rankings to DB,
    then computes streaks from history.  Redis-cached 15 min per calendar day.
    """
    import aiohttp as _aiohttp
    from datetime import date as _date
    from collections import defaultdict

    today    = _date.today()
    pool     = await _get_db_pool()
    api_key  = os.getenv("MASSIVE_API_KEY", "")
    cache_key = f"market:sector_leaders:{today.isoformat()}"
    _redis   = await get_redis()

    # Resolve the SP500 stock universe by calling the sector map helper to build _INDEX_MAP
    # We run a minimal call to populate the local data — simpler than duplicating the dict here
    _sp500_stocks, _sp500_etfs = await _resolve_sp500_universe()

    if not refresh:
        try:
            cached = await _redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    # ── Fetch today's data if not already stored ──────────────────────────────
    stored = await pool.fetchval(
        "SELECT COUNT(*) FROM sector_leader_history WHERE trade_date=$1", today
    )

    if stored == 0 or refresh:
        all_tickers = [t for stocks in _sp500_stocks.values() for t, _, _, _ in stocks]
        changes: dict[str, tuple[float, float, int]] = {}

        if api_key:
            snap_url = (
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
                f"?tickers={','.join(all_tickers)}&apiKey={api_key}"
            )
            try:
                async with _aiohttp.ClientSession() as sess:
                    async with sess.get(snap_url, timeout=_aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            data = await r.json()
                            for t in data.get("tickers", []):
                                sym  = t.get("ticker", "")
                                chg  = round(float(t.get("todaysChangePerc") or 0), 4)
                                day  = t.get("day") or {}
                                px   = round(float(day.get("c") or
                                             (t.get("lastTrade") or {}).get("p") or 0), 4)
                                vol  = int(day.get("v") or 0)
                                changes[sym] = (chg, px, vol)
            except Exception as e:
                log.warning("sector_leaders.polygon_error", error=str(e))

        if changes:
            await pool.execute(
                "DELETE FROM sector_leader_history WHERE trade_date=$1", today
            )
            rows_to_insert = []
            for sector, stocks in _sp500_stocks.items():
                ranked = sorted(
                    [(tkr, *changes[tkr]) for tkr, _, _, _ in stocks if tkr in changes],
                    key=lambda x: x[1], reverse=True,
                )
                for rank, (tkr, chg, px, vol) in enumerate(ranked, 1):
                    rows_to_insert.append((today, sector, tkr, rank, chg, px, vol))

            await pool.executemany(
                """INSERT INTO sector_leader_history
                   (trade_date, sector, ticker, rank, change_pct, price, volume)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (trade_date, sector, ticker) DO UPDATE
                   SET rank=$4, change_pct=$5, price=$6, volume=$7""",
                rows_to_insert,
            )

    # ── Load today's ranked data ───────────────────────────────────────────────
    today_rows = await pool.fetch(
        """SELECT sector, ticker, rank, change_pct, price, volume
           FROM sector_leader_history WHERE trade_date=$1
           ORDER BY sector, rank""",
        today,
    )

    # ── Compute consecutive-day streaks (rank ≤ 5) ───────────────────────────
    # Get all trading dates we have data for (most recent first)
    date_rows = await pool.fetch(
        "SELECT DISTINCT trade_date FROM sector_leader_history ORDER BY trade_date DESC LIMIT 60"
    )
    trading_dates = [r["trade_date"] for r in date_rows]

    # Which (ticker, sector) pairs were in top-5 on each trading date
    hist_rows = await pool.fetch(
        """SELECT trade_date, ticker, sector
           FROM sector_leader_history
           WHERE trade_date >= CURRENT_DATE - INTERVAL '60 days' AND rank <= 5
        """
    )
    top5_by_key: dict[tuple, set] = defaultdict(set)
    for r in hist_rows:
        top5_by_key[(r["ticker"], r["sector"])].add(r["trade_date"])

    streak_map: dict[tuple, int] = {}
    for key, date_set in top5_by_key.items():
        streak = 0
        for d in trading_dates:   # already ordered newest → oldest
            if d in date_set:
                streak += 1
            else:
                break
        streak_map[key] = streak

    # ── Build name + subsector lookup ─────────────────────────────────────────
    meta: dict[str, tuple[str, str]] = {}  # ticker -> (name, subsector)
    for sector, stocks in _sp500_stocks.items():
        for tkr, name, _, subsector in stocks:
            meta[tkr] = (name, subsector)

    # ── Assemble sector cards ─────────────────────────────────────────────────
    sector_buckets: dict[str, list] = defaultdict(list)
    for r in today_rows:
        name, subsector = meta.get(r["ticker"], (r["ticker"], ""))
        streak = streak_map.get((r["ticker"], r["sector"]), 0)
        sector_buckets[r["sector"]].append({
            "ticker":    r["ticker"],
            "name":      name,
            "subsector": subsector,
            "rank":      r["rank"],
            "change":    float(r["change_pct"] or 0),
            "price":     float(r["price"] or 0),
            "volume":    r["volume"] or 0,
            "streak":    streak,
        })

    sectors_out = []
    for sector, stocks in _sp500_stocks.items():
        bucket = sector_buckets.get(sector, [])
        avg_chg = round(sum(s["change"] for s in bucket) / len(bucket), 2) if bucket else 0
        sectors_out.append({
            "sector":     sector,
            "etf":        _sp500_etfs.get(sector, ""),
            "avg_change": avg_chg,
            "stocks":     bucket,
        })

    result = {
        "date":    today.isoformat(),
        "sectors": sectors_out,
        "as_of":   datetime.utcnow().isoformat(),
    }
    try:
        await _redis.setex(cache_key, 900, json.dumps(result))   # 15-min cache
    except Exception:
        pass
    return result


# ── API — Market bars (Massive MCP) ──────────────────────────────────────────

_market_bars_cache: dict = {}  # {cache_key: (expires_at, bars)}
_MARKET_BARS_TTL = 900         # 15-minute cache; avoids EODData rate-limit on simultaneous breadth requests
_eoddata_sem: asyncio.Semaphore | None = None  # serializes EODData requests to avoid rate-limits

@app.get("/api/market/bars")
async def get_market_bars(ticker: str = "SPY", days: int = 90):
    """
    Daily OHLCV bars for a ticker.
    Priority:  1) Polygon.io (plain ticker, then I:{ticker} index format)
               2) EODData api.eoddata.com (breadth indicators — needs EODDATA_API_KEY)
               3) Yahoo Finance chart API (plain ticker, then ^{ticker})
    Returns LightweightCharts-compatible format: time as Unix seconds.
    """
    import aiohttp as _aiohttp
    import calendar as _cal
    import time as _time
    from datetime import date as _date, timedelta

    sym       = ticker.upper()
    to_date   = _date.today().isoformat()
    from_date = (_date.today() - timedelta(days=days)).isoformat()
    lc_bars: list = []

    cache_key = f"{sym}:{days}"
    cached = _market_bars_cache.get(cache_key)
    if cached and _time.monotonic() < cached[0]:
        return {"ticker": sym, "bars": cached[1]}

    # ── Primary: Polygon.io (plain ticker, then index format) ────────────────
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if api_key:
        for poly_sym in [sym, f"I:{sym}"]:
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/{poly_sym}/range/1/day"
                f"/{from_date}/{to_date}?adjusted=true&sort=asc&limit=750&apiKey={api_key}"
            )
            try:
                async with _aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for b in data.get("results", []):
                                ts_ms = b.get("t", 0)
                                if not ts_ms:
                                    continue
                                ts = ts_ms // 1000
                                lc_bars.append({
                                    "time":   ts,
                                    "open":   float(b.get("o") or 0),
                                    "high":   float(b.get("h") or 0),
                                    "low":    float(b.get("l") or 0),
                                    "close":  float(b.get("c") or 0),
                                    "volume": int(b.get("v")   or 0),
                                })
                            if lc_bars:
                                break
            except Exception as e:
                log.warning("webui.market_bars.polygon_error", ticker=poly_sym, error=str(e))

    # ── Fallback 1: EODData api.eoddata.com (breadth indicators — needs EODDATA_API_KEY) ──
    # Exchange INDEX carries MMFI, MMTH, LOWN directly.
    # HIGN (52-week highs) has no direct match — mapped to MAHN on EODData.
    # Semaphore serializes concurrent requests to avoid EODData rate-limits.
    if not lc_bars:
        eod_key = os.getenv("EODDATA_API_KEY", "")
        if eod_key:
            global _eoddata_sem
            if _eoddata_sem is None:
                _eoddata_sem = asyncio.Semaphore(1)
            _EOD_ALIAS = {"HIGN": "MAHN"}
            eod_sym = _EOD_ALIAS.get(sym, sym)
            # EODData history starts ~2026-01-01; clamp so older requests still return data
            eod_from = max(from_date, "2026-01-01")
            async with _eoddata_sem:
                try:
                    async with _aiohttp.ClientSession() as session:
                        async with session.get(
                            f"https://api.eoddata.com/Quote/List/INDEX/{eod_sym}",
                            params={
                                "Interval":      "d",
                                "FromDateStamp": eod_from,
                                "ToDateStamp":   to_date,
                                "ApiKey":        eod_key,
                            },
                            timeout=_aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status == 200:
                                rows = await resp.json(content_type=None)
                                if isinstance(rows, list):
                                    for d in rows:
                                        try:
                                            dt = _date.fromisoformat(d["dateStamp"][:10])
                                            ts = int(_cal.timegm(dt.timetuple()))
                                            lc_bars.append({
                                                "time":   ts,
                                                "open":   float(d.get("open")   or 0),
                                                "high":   float(d.get("high")   or 0),
                                                "low":    float(d.get("low")    or 0),
                                                "close":  float(d.get("close")  or 0),
                                                "volume": int(d.get("volume")   or 0),
                                            })
                                        except (ValueError, KeyError):
                                            continue
                    # Pace EODData calls — free tier rate-limits burst requests
                    await asyncio.sleep(3)
                except Exception as e:
                    log.warning("webui.market_bars.eoddata_error", ticker=sym, error=str(e))

    # ── Fallback 2: Yahoo Finance chart API (breadth/vol indices) ─────────────
    if not lc_bars:
        period1 = int(_cal.timegm((_date.today() - timedelta(days=days)).timetuple()))
        period2 = int(_cal.timegm(_date.today().timetuple()))
        yf_candidates = [sym] if sym.startswith("^") else [sym, f"^{sym}"]
        headers = {"User-Agent": "Mozilla/5.0 (compatible; OpenTrader/1.0)"}
        for yf_sym in yf_candidates:
            yf_url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
                f"?interval=1d&period1={period1}&period2={period2}"
            )
            try:
                async with _aiohttp.ClientSession() as session:
                    async with session.get(yf_url, headers=headers,
                                           timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data  = await resp.json()
                            result = (data.get("chart", {}).get("result") or [None])[0]
                            if result:
                                tss    = result.get("timestamp", [])
                                q      = (result.get("indicators", {}).get("quote") or [{}])[0]
                                opens  = q.get("open",   [])
                                highs  = q.get("high",   [])
                                lows   = q.get("low",    [])
                                closes = q.get("close",  [])
                                vols   = q.get("volume", [])
                                for i, ts in enumerate(tss):
                                    o = opens[i]  if i < len(opens)  else None
                                    h = highs[i]  if i < len(highs)  else None
                                    l = lows[i]   if i < len(lows)   else None
                                    c = closes[i] if i < len(closes) else None
                                    if o is None or h is None or l is None or c is None:
                                        continue
                                    lc_bars.append({
                                        "time":   int(ts),
                                        "open":   float(o), "high": float(h),
                                        "low":    float(l), "close": float(c),
                                        "volume": int(vols[i] or 0) if i < len(vols) else 0,
                                    })
                            if lc_bars:
                                break
            except Exception as e:
                log.warning("webui.market_bars.yahoo_fallback_error", ticker=yf_sym, error=str(e))

    lc_bars.sort(key=lambda x: x["time"])
    if lc_bars:
        _market_bars_cache[cache_key] = (_time.monotonic() + _MARKET_BARS_TTL, lc_bars)
    return {"ticker": sym, "bars": lc_bars}


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


# ── Portfolio Groups ──────────────────────────────────────────────────────────

async def _pg_load_full(pool, group_id: str = None) -> list[dict]:
    """Load groups with their holdings and account assignments."""
    q = "SELECT * FROM portfolio_groups"
    args = []
    if group_id:
        q += " WHERE id = $1"
        args.append(group_id)
    q += " ORDER BY type DESC, name"   # parents first
    rows = await pool.fetch(q, *args)

    if not rows:
        return []

    ids = [str(r["id"]) for r in rows]
    holdings_rows = await pool.fetch(
        "SELECT * FROM portfolio_group_holdings WHERE group_id = ANY($1::uuid[]) ORDER BY sort_order, ticker",
        ids,
    )
    accounts_rows = await pool.fetch(
        "SELECT * FROM portfolio_group_accounts WHERE group_id = ANY($1::uuid[])",
        ids,
    )

    hold_map: dict = {}
    for h in holdings_rows:
        gid = str(h["group_id"])
        hold_map.setdefault(gid, []).append({
            "id": str(h["id"]), "ticker": h["ticker"],
            "alloc_pct": float(h["alloc_pct"]) if h["alloc_pct"] else None,
            "lot_size": h["lot_size"], "sort_order": h["sort_order"],
        })

    acct_map: dict = {}
    for a in accounts_rows:
        gid = str(a["group_id"])
        acct_map.setdefault(gid, []).append({
            "account_label": a["account_label"], "broker": a["broker"],
        })

    result = []
    for r in rows:
        gid = str(r["id"])
        holdings = hold_map.get(gid, [])
        # Compute effective allocation percentages
        if r["alloc_mode"] == "equal" and holdings:
            eq = round(100.0 / len(holdings), 4)
            for h in holdings:
                h["effective_pct"] = eq
        else:
            for h in holdings:
                h["effective_pct"] = h["alloc_pct"]
        result.append({
            "id":                 gid,
            "name":               r["name"],
            "type":               r["type"],
            "parent_id":          str(r["parent_id"]) if r["parent_id"] else None,
            "max_stocks":         r["max_stocks"],
            "alloc_mode":         r["alloc_mode"],
            "strategy_family_id": r["strategy_family_id"],
            "strategy_name":      r["strategy_name"],
            "color":              r["color"],
            "investment_amount":  float(r["investment_amount"]) if r["investment_amount"] is not None else None,
            "created_at":         r["created_at"].isoformat(),
            "updated_at":         r["updated_at"].isoformat(),
            "holdings":           holdings,
            "accounts":           acct_map.get(gid, []),
        })
    return result


async def _pg_load_with_subs(pool, group_id: str) -> dict | None:
    """Load a single group and, if it is a parent, attach its sub-portfolio dicts."""
    groups = await _pg_load_full(pool, group_id)
    if not groups:
        return None
    group = groups[0]
    if group["type"] == "parent":
        sub_id_rows = await pool.fetch(
            "SELECT id FROM portfolio_groups WHERE parent_id=$1::uuid AND type='sub'",
            group_id,
        )
        subs = []
        for row in sub_id_rows:
            sub_data = await _pg_load_full(pool, str(row["id"]))
            if sub_data:
                subs.append(sub_data[0])
        group["subs"] = subs
    else:
        group["subs"] = []
    return group


def _pg_flatten_holdings(group: dict) -> list[dict]:
    """
    Return a flat list of {ticker, target_pct, accounts} covering the group's
    direct holdings AND every sub-portfolio's holdings.

    Sub holdings are weighted by (sub.investment_amount / parent.investment_amount)
    so target_pct is expressed as a fraction of the parent's total capital.
    When investment_amount is not set the sub's effective_pct is used as-is.
    """
    parent_inv = float(group.get("investment_amount") or 0)
    flat: list[dict] = []

    for h in group.get("holdings") or []:
        flat.append({
            "ticker":     h["ticker"],
            "target_pct": float(h.get("effective_pct") or 0),
            "accounts":   group.get("accounts", []),
        })

    for sub in group.get("subs") or []:
        sub_inv = float(sub.get("investment_amount") or 0)
        weight  = (sub_inv / parent_inv) if (parent_inv > 0 and sub_inv > 0) else 1.0
        sub_accts = sub.get("accounts") or group.get("accounts", [])
        for h in sub.get("holdings") or []:
            eff = float(h.get("effective_pct") or 0)
            flat.append({
                "ticker":     h["ticker"],
                "target_pct": round(eff * weight, 4),
                "accounts":   sub_accts,
            })

    return flat


@app.get("/api/portfolio-groups")
async def list_portfolio_groups(token: str = ""):
    """List all portfolio groups with holdings and account assignments."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        return {"error": "DB unavailable", "groups": []}
    groups = await _pg_load_full(pool)
    # Nest subs under parents
    parent_map = {g["id"]: g for g in groups if g["type"] == "parent"}
    for g in groups:
        if g["type"] == "parent":
            g["subs"] = []
    for g in groups:
        if g["type"] == "sub" and g["parent_id"] and g["parent_id"] in parent_map:
            parent_map[g["parent_id"]]["subs"].append(g)
    return {"groups": [g for g in groups if g["type"] == "parent"]}


@app.post("/api/portfolio-groups")
async def create_portfolio_group(body: dict, token: str = ""):
    """Create a portfolio group (parent or sub)."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")

    grp_type   = str(body.get("type", "parent"))
    parent_id  = body.get("parent_id") or None
    max_stocks = int(body.get("max_stocks", 25 if grp_type == "parent" else 10))
    max_stocks = min(max_stocks, 25 if grp_type == "parent" else 10)

    if grp_type == "sub" and parent_id:
        sub_count = await pool.fetchval(
            "SELECT COUNT(*) FROM portfolio_groups WHERE parent_id=$1::uuid AND type='sub'",
            parent_id,
        )
        if sub_count >= 3:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Maximum 3 sub-portfolios per parent")

    row = await pool.fetchrow(
        """INSERT INTO portfolio_groups
               (name, type, parent_id, max_stocks, alloc_mode, strategy_family_id, strategy_name, color, investment_amount)
           VALUES ($1,$2,$3::uuid,$4,$5,$6,$7,$8,$9)
           RETURNING id""",
        str(body.get("name", "New Portfolio")),
        grp_type,
        parent_id,
        max_stocks,
        str(body.get("alloc_mode", "equal")),
        body.get("strategy_family_id") or None,
        body.get("strategy_name")      or None,
        str(body.get("color", "#60a5fa")),
        float(body["investment_amount"]) if body.get("investment_amount") else None,
    )
    groups = await _pg_load_full(pool, str(row["id"]))
    return {"group": groups[0] if groups else {}}


@app.patch("/api/portfolio-groups/{group_id}")
async def update_portfolio_group(group_id: str, body: dict, token: str = ""):
    """Rename or update a portfolio group's settings."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")
    fields, vals, idx = [], [], 1
    for field in ("name", "alloc_mode", "strategy_family_id", "strategy_name", "color", "investment_amount"):
        if field in body:
            fields.append(f"{field}=${idx}")
            vals.append(body[field])
            idx += 1
    if not fields:
        return {"ok": True}
    vals.append(group_id)
    await pool.execute(
        f"UPDATE portfolio_groups SET {', '.join(fields)}, updated_at=NOW() WHERE id=${idx}::uuid",
        *vals,
    )
    groups = await _pg_load_full(pool, group_id)
    return {"group": groups[0] if groups else {}}


@app.delete("/api/portfolio-groups/{group_id}")
async def delete_portfolio_group(group_id: str, token: str = ""):
    """Delete a portfolio group (cascades to holdings and accounts)."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")
    await pool.execute("DELETE FROM portfolio_groups WHERE id=$1::uuid", group_id)
    return {"deleted": True}


@app.put("/api/portfolio-groups/{group_id}/holdings")
async def replace_holdings(group_id: str, body: dict, token: str = ""):
    """Replace all holdings for a group.
    Body: {holdings: [{ticker, alloc_pct?, lot_size?}]}
    """
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")

    grp = await pool.fetchrow(
        "SELECT max_stocks, alloc_mode FROM portfolio_groups WHERE id=$1::uuid", group_id
    )
    if not grp:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Group not found")

    holdings = body.get("holdings", [])[:grp["max_stocks"]]

    # Validate custom allocations sum to 100
    if grp["alloc_mode"] == "custom" and holdings:
        total = sum(float(h.get("alloc_pct") or 0) for h in holdings)
        if holdings and abs(total - 100.0) > 0.5:
            from fastapi import HTTPException
            raise HTTPException(status_code=400,
                                detail=f"Custom allocations must sum to 100% (got {total:.1f}%)")

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM portfolio_group_holdings WHERE group_id=$1::uuid", group_id
        )
        for i, h in enumerate(holdings):
            ticker = str(h.get("ticker", "")).upper().strip()
            if not ticker:
                continue
            await conn.execute(
                """INSERT INTO portfolio_group_holdings
                       (group_id, ticker, alloc_pct, lot_size, sort_order)
                   VALUES ($1::uuid,$2,$3,$4,$5)
                   ON CONFLICT (group_id, ticker) DO UPDATE SET
                       alloc_pct=$3, lot_size=$4, sort_order=$5""",
                group_id, ticker,
                float(h["alloc_pct"]) if h.get("alloc_pct") else None,
                int(h.get("lot_size") or 1),
                i,
            )
    groups = await _pg_load_full(pool, group_id)
    return {"group": groups[0] if groups else {}}


@app.post("/api/portfolio-groups/{group_id}/accounts")
async def assign_accounts(group_id: str, body: dict, token: str = ""):
    """Assign broker accounts to a portfolio group.
    Body: {accounts: [{account_label, broker}]}
    """
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")
    accounts = body.get("accounts", [])
    await pool.execute(
        "DELETE FROM portfolio_group_accounts WHERE group_id=$1::uuid", group_id
    )
    for a in accounts:
        label = str(a.get("account_label", "")).strip()
        if label:
            await pool.execute(
                """INSERT INTO portfolio_group_accounts (group_id, account_label, broker)
                   VALUES ($1::uuid,$2,$3)
                   ON CONFLICT DO NOTHING""",
                group_id, label, str(a.get("broker", "")),
            )
    groups = await _pg_load_full(pool, group_id)
    return {"group": groups[0] if groups else {}}


@app.get("/api/portfolio-groups/strategies")
async def list_strategies_for_groups(token: str = ""):
    """Return available strategies for portfolio group assignment."""
    check_token(token)
    import json as _json
    strat_path = "/app/config/strategies.json"
    try:
        with open(strat_path) as f:
            strats = _json.load(f)
        return {
            "strategies": [
                {
                    "family_id": s.get("family_id", ""),
                    "name":      s.get("name", ""),
                    "asset":     s.get("asset", ""),
                    "status":    s.get("status", ""),
                }
                for s in strats
                if s.get("status") == "active"
            ]
        }
    except Exception as e:
        return {"strategies": [], "error": str(e)}


# ── Portfolio Groups: Cost Basis ─────────────────────────────────────────────

@app.get("/api/portfolio-groups/{group_id}/cost-basis")
async def pg_cost_basis(group_id: str, token: str = ""):
    """Return broker-reported cost basis and unrealized P&L for every holding in a group."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")

    group = await _pg_load_with_subs(pool, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    flat        = _pg_flatten_holdings(group)
    acct_labels = {a["account_label"] for h in flat for a in h["accounts"]}
    target_map  = {h["ticker"]: h["target_pct"] for h in flat}

    broker_data = await get_broker_positions()
    pos_by_ticker: dict = {}
    for acct in broker_data.get("accounts", []):
        if acct.get("label") not in acct_labels:
            continue
        for p in acct.get("positions", []):
            sym  = (p.get("symbol") or "").upper()
            qty  = float(p.get("qty") or 0)
            cb   = float(p.get("cost_basis") or 0)
            mv   = float(p.get("market_value") or 0)
            px   = float(p.get("current_price") or 0)
            if sym not in pos_by_ticker:
                pos_by_ticker[sym] = {"qty": 0, "cost_basis": 0, "market_value": 0, "current_price": px}
            pos_by_ticker[sym]["qty"]          += qty
            pos_by_ticker[sym]["cost_basis"]   += cb
            pos_by_ticker[sym]["market_value"] += mv
            if px:
                pos_by_ticker[sym]["current_price"] = px

    holdings_out = []
    for ticker, tgt_pct in target_map.items():
        p = pos_by_ticker.get(ticker, {})
        qty  = p.get("qty", 0)
        cb   = p.get("cost_basis", 0)
        mv   = p.get("market_value", 0)
        px   = p.get("current_price", 0)
        pnl  = round(mv - cb, 2)
        pnl_pct = round(pnl / cb * 100, 2) if cb else 0.0
        holdings_out.append({
            "ticker":           ticker,
            "qty":              qty,
            "cost_basis":       round(cb, 2),
            "market_value":     round(mv, 2),
            "unrealized_pnl":   pnl,
            "unrealized_pnl_pct": pnl_pct,
            "current_price":    round(px, 4),
            "alloc_pct":        tgt_pct,
        })

    total_cb = sum(h["cost_basis"]   for h in holdings_out)
    total_mv = sum(h["market_value"] for h in holdings_out)
    total_pnl = round(total_mv - total_cb, 2)
    return {
        "group_id": group_id,
        "group_name": group["name"],
        "holdings": holdings_out,
        "totals": {
            "cost_basis":         round(total_cb,  2),
            "market_value":       round(total_mv,  2),
            "unrealized_pnl":     total_pnl,
            "unrealized_pnl_pct": round(total_pnl / total_cb * 100, 2) if total_cb else 0.0,
        },
    }


# ── Portfolio Groups: Rebalancing ─────────────────────────────────────────────

async def _pg_rebalance_preview(group: dict) -> dict:
    """Shared logic for preview + execute: compute drift and order list.
    Handles parent groups by flattening sub-portfolio holdings with weighting.
    """
    import math as _math

    flat        = _pg_flatten_holdings(group)
    all_accts   = {a["account_label"] for h in flat for a in h["accounts"]}
    broker_data = await get_broker_positions()

    pos_by_ticker: dict = {}
    for acct in broker_data.get("accounts", []):
        if acct.get("label") not in all_accts:
            continue
        for p in acct.get("positions", []):
            sym  = (p.get("symbol") or "").upper()
            mv   = float(p.get("market_value") or 0)
            px   = float(p.get("current_price") or 0)
            if sym not in pos_by_ticker:
                pos_by_ticker[sym] = {"market_value": 0, "current_price": px}
            pos_by_ticker[sym]["market_value"] += mv
            if px:
                pos_by_ticker[sym]["current_price"] = px

    total_value = sum(p["market_value"] for p in pos_by_ticker.values())
    positions = []
    for h in flat:
        ticker   = h["ticker"]
        tgt_pct  = h["target_pct"]
        pos      = pos_by_ticker.get(ticker, {})
        act_val  = pos.get("market_value", 0.0)
        act_pct  = round(act_val / total_value * 100, 2) if total_value else 0.0
        tgt_val  = total_value * tgt_pct / 100.0
        delta    = tgt_val - act_val
        drift    = abs(delta / total_value * 100) if total_value else 0.0

        px = pos.get("current_price", 0.0)
        if not px and abs(delta) > 0.01:
            try:
                q  = await get_broker_quote(ticker)
                px = q.get("ask") or q.get("last") or 0.0
            except Exception:
                px = 0.0

        if px and drift >= 1.0:
            delta_qty = _math.floor(abs(delta) / px)
            action    = "buy" if delta > 0 else "sell"
        else:
            delta_qty = 0
            action    = "hold"

        positions.append({
            "ticker":        ticker,
            "target_pct":    round(tgt_pct, 4),
            "actual_pct":    act_pct,
            "target_value":  round(tgt_val, 2),
            "actual_value":  round(act_val, 2),
            "drift_pct":     round(drift, 2),
            "action":        action,
            "delta_usd":     round(delta, 2),
            "delta_qty":     delta_qty,
            "current_price": round(px, 4),
            "_accounts":     [a["account_label"] for a in h["accounts"]],
        })

    orders = [p for p in positions if p["action"] != "hold" and p["delta_qty"] >= 1]
    pub_positions = [{k: v for k, v in p.items() if k != "_accounts"} for p in positions]
    pub_orders    = [{k: v for k, v in p.items() if k != "_accounts"} for p in orders]
    return {
        "group_id":     group["id"],
        "group_name":   group["name"],
        "total_value":  round(total_value, 2),
        "positions":    pub_positions,
        "orders_count": len(pub_orders),
        "orders":       pub_orders,
    }


@app.get("/api/portfolio-groups/{group_id}/rebalance-preview")
async def pg_rebalance_preview(group_id: str, token: str = ""):
    """Preview what orders a rebalance would generate without executing them."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")
    group = await _pg_load_with_subs(pool, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return await _pg_rebalance_preview(group)


@app.post("/api/portfolio-groups/{group_id}/rebalance")
async def pg_rebalance_execute(group_id: str, token: str = ""):
    """Execute a rebalance: place buy/sell orders for each drifted holding."""
    import uuid as _uuid, json as _json
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")
    group = await _pg_load_with_subs(pool, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    preview = await _pg_rebalance_preview(group)
    orders  = preview["orders"]
    if not orders:
        return {"queued": 0, "orders": [], "message": "Portfolio is within 1% of target — no rebalance needed"}

    # Validate at least one account is assigned somewhere in the group
    all_accts = list({a for o in orders for a in o.get("_accounts", [])})
    if not all_accts:
        raise HTTPException(status_code=400, detail="No accounts assigned to this group")

    try:
        import redis.asyncio as _aioredis
        _REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        r = await _aioredis.from_url(_REDIS_URL, encoding="utf-8", decode_responses=True,
                                     socket_connect_timeout=5, socket_timeout=15)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    placed = []
    try:
        for o in orders:
            acct_labels = o.get("_accounts") or all_accts
            for acct in acct_labels:
                req_id = str(_uuid.uuid4())
                cmd = {
                    "command":       "place_order",
                    "request_id":    req_id,
                    "asset_class":   "equity",
                    "account_label": acct,
                    "symbol":        o["ticker"],
                    "side":          "buy" if o["action"] == "buy" else "sell_short",
                    "quantity":      str(o["delta_qty"]),
                    "order_type":    "market",
                    "duration":      "day",
                    "strategy_tag":  "Rebalance",
                    "tag":           f"reb-{group_id[:8]}",
                    "issued_by":     "webui-rebalance",
                }
                await r.xadd(STREAMS["broker_commands"], cmd, maxlen=10_000)
                placed.append({"ticker": o["ticker"], "action": o["action"],
                               "qty": o["delta_qty"], "request_id": req_id, "account": acct})
                await pool.execute(
                    """INSERT INTO portfolio_group_rebalance_log
                       (group_id, ticker, action, qty, price, delta_usd, request_id)
                       VALUES ($1::uuid,$2,$3,$4,$5,$6,$7)""",
                    group_id, o["ticker"], o["action"], o["delta_qty"],
                    o["current_price"], o["delta_usd"], req_id,
                )
    finally:
        await r.aclose()

    clean = [{k: v for k, v in p.items() if k != "_accounts"} for p in placed]
    return {"queued": len(clean), "orders": clean}


# ── Portfolio Groups: DCA ─────────────────────────────────────────────────────

def _dca_is_due_today(schedule: dict) -> bool:
    """Return True if this DCA schedule should run today (ET date)."""
    from scheduler.calendar import now_et
    today = now_et().date()
    freq  = schedule.get("frequency", "weekly")
    if freq == "daily":
        return True
    if freq == "weekly":
        dow = schedule.get("day_of_week")
        if dow is None:
            return today.weekday() == 0   # default Monday
        return today.weekday() == int(dow)
    if freq == "monthly":
        dom = schedule.get("day_of_month") or 1
        return today.day == int(dom)
    return False


async def _pg_dca_execute(group_id: str, amount_usd: float,
                           schedule_id: str | None, pool) -> dict:
    """Core DCA execution: buy proportional slices across group holdings."""
    import math as _math, uuid as _uuid
    group = await _pg_load_with_subs(pool, group_id)
    if not group:
        return {"executed": 0, "skipped": 0, "orders": [], "error": "Group not found"}

    flat = _pg_flatten_holdings(group)
    if not flat:
        return {"executed": 0, "skipped": 0, "orders": [], "error": "No holdings in group"}
    all_accts = list({a["account_label"] for h in flat for a in h["accounts"]})
    if not all_accts:
        return {"executed": 0, "skipped": 0, "orders": [], "error": "No accounts assigned"}

    broker_data = await get_broker_positions()
    price_map: dict = {}
    for acct in broker_data.get("accounts", []):
        for p in acct.get("positions", []):
            sym = (p.get("symbol") or "").upper()
            px  = float(p.get("current_price") or 0)
            if px and sym not in price_map:
                price_map[sym] = px

    try:
        import redis.asyncio as _aioredis
        _REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        r = await _aioredis.from_url(_REDIS_URL, encoding="utf-8", decode_responses=True,
                                     socket_connect_timeout=5, socket_timeout=15)
    except Exception as e:
        return {"executed": 0, "skipped": 0, "orders": [], "error": f"Redis: {e}"}

    placed, skipped = [], []
    try:
        for h in flat:
            ticker   = h["ticker"]
            eff_pct  = h["target_pct"]
            alloc    = amount_usd * eff_pct / 100.0
            acct_labels = [a["account_label"] for a in h["accounts"]] or all_accts

            px = price_map.get(ticker)
            if not px:
                try:
                    q  = await get_broker_quote(ticker)
                    px = q.get("ask") or q.get("last") or 0.0
                except Exception:
                    px = 0.0

            if not px or alloc < 0.01:
                skipped.append({"ticker": ticker, "reason": "no_price" if not px else "zero_alloc"})
                continue

            qty = _math.floor(alloc / px)
            if qty < 1:
                skipped.append({"ticker": ticker, "reason": "qty_too_small",
                                "alloc_usd": round(alloc, 2), "price": px})
                continue

            for acct in acct_labels:
                req_id = str(_uuid.uuid4())
                cmd = {
                    "command":       "place_order",
                    "request_id":    req_id,
                    "asset_class":   "equity",
                    "account_label": acct,
                    "symbol":        ticker,
                    "side":          "buy",
                    "quantity":      str(qty),
                    "order_type":    "market",
                    "duration":      "day",
                    "strategy_tag":  "DCA",
                    "tag":           f"dca-{ticker.lower()}",
                    "issued_by":     "webui-dca",
                }
                await r.xadd(STREAMS["broker_commands"], cmd, maxlen=10_000)
                placed.append({"ticker": ticker, "qty": qty, "price": px,
                               "alloc_usd": round(alloc, 2), "account": acct,
                               "request_id": req_id})
                await pool.execute(
                    """INSERT INTO portfolio_group_dca_log
                       (schedule_id, group_id, ticker, qty, price, amount_usd, request_id)
                       VALUES ($1,$2::uuid,$3,$4,$5,$6,$7)""",
                    schedule_id, group_id, ticker, qty, px, round(alloc, 2), req_id,
                )
    finally:
        await r.aclose()

    if schedule_id:
        try:
            await pool.execute(
                "UPDATE portfolio_group_dca_schedules SET last_run_at=NOW() WHERE id=$1",
                schedule_id,
            )
        except Exception:
            pass

    return {"executed": len(placed), "skipped": len(skipped), "orders": placed, "skipped_detail": skipped}


@app.get("/api/portfolio-groups/{group_id}/dca")
async def pg_dca_get(group_id: str, token: str = ""):
    """Return the DCA schedule for a portfolio group."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")
    row = await pool.fetchrow(
        "SELECT * FROM portfolio_group_dca_schedules WHERE group_id=$1::uuid", group_id
    )
    if not row:
        return {"schedule": None}
    return {"schedule": {
        "id":           str(row["id"]),
        "group_id":     str(row["group_id"]),
        "amount_usd":   float(row["amount_usd"]),
        "frequency":    row["frequency"],
        "day_of_week":  row["day_of_week"],
        "day_of_month": row["day_of_month"],
        "hour_et":      row["hour_et"],
        "minute_et":    row["minute_et"],
        "is_active":    row["is_active"],
        "last_run_at":  row["last_run_at"].isoformat() if row["last_run_at"] else None,
        "created_at":   row["created_at"].isoformat(),
    }}


@app.post("/api/portfolio-groups/{group_id}/dca")
async def pg_dca_upsert(group_id: str, body: dict, token: str = ""):
    """Create or update the DCA schedule for a portfolio group."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")

    amount    = float(body.get("amount_usd") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    freq      = body.get("frequency", "weekly")
    if freq not in ("daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="frequency must be daily, weekly, or monthly")
    dow       = body.get("day_of_week")
    dom       = body.get("day_of_month")
    hour_et   = int(body.get("hour_et", 10))
    minute_et = int(body.get("minute_et", 0))
    is_active = bool(body.get("is_active", True))

    await pool.execute(
        """INSERT INTO portfolio_group_dca_schedules
           (group_id, amount_usd, frequency, day_of_week, day_of_month,
            hour_et, minute_et, is_active)
           VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (group_id) DO UPDATE SET
               amount_usd=$2, frequency=$3, day_of_week=$4, day_of_month=$5,
               hour_et=$6, minute_et=$7, is_active=$8""",
        group_id, amount, freq, dow, dom, hour_et, minute_et, is_active,
    )
    row = await pool.fetchrow(
        "SELECT * FROM portfolio_group_dca_schedules WHERE group_id=$1::uuid", group_id
    )
    return {"schedule": {
        "id":           str(row["id"]),
        "group_id":     str(row["group_id"]),
        "amount_usd":   float(row["amount_usd"]),
        "frequency":    row["frequency"],
        "day_of_week":  row["day_of_week"],
        "day_of_month": row["day_of_month"],
        "hour_et":      row["hour_et"],
        "minute_et":    row["minute_et"],
        "is_active":    row["is_active"],
        "last_run_at":  row["last_run_at"].isoformat() if row["last_run_at"] else None,
        "created_at":   row["created_at"].isoformat(),
    }}


@app.delete("/api/portfolio-groups/{group_id}/dca")
async def pg_dca_delete(group_id: str, token: str = ""):
    """Delete the DCA schedule for a portfolio group."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")
    await pool.execute(
        "DELETE FROM portfolio_group_dca_schedules WHERE group_id=$1::uuid", group_id
    )
    return {"deleted": True}


@app.post("/api/portfolio-groups/{group_id}/dca/run")
async def pg_dca_run(group_id: str, body: dict = None, token: str = ""):
    """Manually trigger a DCA buy for a portfolio group."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")

    body = body or {}
    # Prefer body amount, fall back to stored schedule amount
    amount = float(body.get("amount_usd") or 0)
    schedule_id = None
    if not amount:
        row = await pool.fetchrow(
            "SELECT id, amount_usd FROM portfolio_group_dca_schedules WHERE group_id=$1::uuid",
            group_id,
        )
        if row:
            amount      = float(row["amount_usd"])
            schedule_id = str(row["id"])

    if not amount:
        raise HTTPException(status_code=400, detail="No amount_usd provided and no DCA schedule exists")

    result = await _pg_dca_execute(group_id, amount, schedule_id, pool)
    return result


@app.post("/api/portfolio-groups/dca/run-all")
async def pg_dca_run_all(token: str = ""):
    """Run all active DCA schedules that are due today. Called by the scheduler."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")

    rows = await pool.fetch(
        "SELECT * FROM portfolio_group_dca_schedules WHERE is_active=TRUE"
    )
    executed_groups = 0
    for row in rows:
        sched = dict(row)
        sched["group_id"] = str(sched["group_id"])
        sched["id"]       = str(sched["id"])
        if not _dca_is_due_today(sched):
            continue
        try:
            result = await _pg_dca_execute(
                sched["group_id"], float(sched["amount_usd"]), sched["id"], pool
            )
            if result.get("executed", 0) > 0:
                executed_groups += 1
        except Exception as e:
            log.warning("pg_dca_run_all.error", group_id=sched["group_id"], error=str(e))

    return {"executed_groups": executed_groups}


@app.get("/api/risk-clusters/latest")
async def get_risk_clusters_latest(token: str = ""):
    """Return the most recent cluster assignments for all tickers."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        return {"error": "DB unavailable", "tickers": [], "run_date": None}
    try:
        run_row = await pool.fetchrow(
            "SELECT run_date, n_tickers, n_clusters, silhouette FROM stock_cluster_runs ORDER BY run_date DESC LIMIT 1"
        )
        if not run_row:
            return {"run_date": None, "tickers": [], "tier_counts": {}}
        run_date = run_row["run_date"]
        rows = await pool.fetch(
            """SELECT ticker, cluster_id, risk_tier,
                      volatility, price_change, beta, pe_ratio, pb_ratio,
                      roe, roa, fcf_yield, earnings_yield
               FROM stock_risk_clusters
               WHERE run_date = $1
               ORDER BY risk_tier, ticker""",
            run_date,
        )
        tickers = [dict(r) for r in rows]
        tier_counts: dict = {}
        for r in tickers:
            tier_counts[r["risk_tier"]] = tier_counts.get(r["risk_tier"], 0) + 1
        return {
            "run_date":    run_date.isoformat(),
            "n_tickers":   run_row["n_tickers"],
            "n_clusters":  run_row["n_clusters"],
            "silhouette":  float(run_row["silhouette"]) if run_row["silhouette"] else None,
            "tier_counts": tier_counts,
            "tickers":     tickers,
        }
    except Exception as e:
        log.error("risk_clusters.latest_error", error=str(e))
        return {"error": str(e), "tickers": []}


@app.get("/api/risk-clusters/ticker/{ticker}")
async def get_risk_cluster_history(ticker: str, token: str = ""):
    """Return cluster assignment history for a single ticker."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        return {"error": "DB unavailable", "rows": []}
    try:
        rows = await pool.fetch(
            """SELECT run_date, cluster_id, risk_tier, volatility, price_change,
                      beta, pe_ratio, roe
               FROM stock_risk_clusters
               WHERE ticker = $1
               ORDER BY run_date DESC
               LIMIT 52""",
            ticker.upper(),
        )
        return {
            "ticker": ticker.upper(),
            "rows": [
                {**dict(r), "run_date": r["run_date"].isoformat()}
                for r in rows
            ],
        }
    except Exception as e:
        return {"error": str(e), "rows": []}


@app.post("/api/risk-clusters/run")
async def trigger_risk_clustering(token: str = ""):
    """Manually trigger a risk clustering run."""
    check_token(token)
    redis = await get_redis()
    await redis.xadd(
        "system.commands",
        {"command": "trigger", "job": "run_risk_clustering", "issued_by": "webui"},
        maxlen=500,
    )
    return {"triggered": True}


@app.get("/api/system/trading-mode")
async def get_trading_mode(token: str = ""):
    """Return the current global trading mode (live | paper_only)."""
    check_token(token)
    redis = await get_redis()
    mode = await redis.get("system:trading_mode") or "live"
    return {"mode": mode.lower()}


@app.post("/api/system/trading-mode")
async def set_trading_mode(body: dict, token: str = ""):
    """Set global trading mode. body: {mode: 'live' | 'paper_only'}"""
    check_token(token)
    mode = str(body.get("mode", "live")).lower()
    if mode not in ("live", "paper_only"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="mode must be 'live' or 'paper_only'")
    redis = await get_redis()
    await redis.set("system:trading_mode", mode)
    log.info("system.trading_mode_changed", mode=mode)
    return {"mode": mode, "ok": True}


@app.get("/api/system/traffic")
async def get_api_traffic(token: str = "", hours: int = 24):
    check_token(token)
    if not DB_URL:
        return {"recent": [], "top_paths": [], "summary": {}}
    try:
        pool = await _get_db_pool()
        recent = await pool.fetch("""
            SELECT ts, method, path, status_code,
                   ROUND(duration_ms::numeric, 1) AS duration_ms
            FROM api_traffic
            WHERE ts >= NOW() - INTERVAL '1 hour' * $1
            ORDER BY ts DESC LIMIT 200
        """, hours)
        top_paths = await pool.fetch("""
            SELECT path,
                   COUNT(*) AS count,
                   ROUND(AVG(duration_ms)::numeric, 1) AS avg_ms,
                   ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p95_ms,
                   COUNT(*) FILTER (WHERE status_code >= 400) AS error_count
            FROM api_traffic
            WHERE ts >= NOW() - INTERVAL '1 hour' * $1
            GROUP BY path
            ORDER BY count DESC LIMIT 20
        """, hours)
        summary = await pool.fetchrow("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status_code >= 400) AS errors,
                   ROUND(AVG(duration_ms)::numeric, 1) AS avg_ms,
                   ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p95_ms
            FROM api_traffic
            WHERE ts >= NOW() - INTERVAL '1 hour' * $1
        """, hours)
        return {
            "recent":    [dict(r) for r in recent],
            "top_paths": [dict(r) for r in top_paths],
            "summary":   dict(summary) if summary else {},
        }
    except Exception as e:
        return {"error": str(e), "recent": [], "top_paths": [], "summary": {}}


@app.get("/api/broker/latency")
async def get_broker_latency(token: str = "", hours: int = 24):
    check_token(token)
    if not DB_URL:
        return {"recent": [], "stats": [], "summary": {}}
    try:
        pool = await _get_db_pool()
        recent = await pool.fetch("""
            SELECT ts,
                   ROUND(duration_ms::numeric, 1) AS rtt_ms,
                   payload->>'broker'        AS broker,
                   payload->>'command'       AS command,
                   payload->>'account_label' AS account_label,
                   payload->>'status'        AS status
            FROM execution_events
            WHERE event_name = 'broker_latency'
              AND ts >= NOW() - INTERVAL '1 hour' * $1
            ORDER BY ts DESC LIMIT 200
        """, hours)
        stats = await pool.fetch("""
            SELECT payload->>'broker'  AS broker,
                   payload->>'command' AS command,
                   COUNT(*)            AS count,
                   ROUND(AVG(duration_ms)::numeric, 1)  AS avg_ms,
                   ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p50_ms,
                   ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p95_ms,
                   COUNT(*) FILTER (WHERE payload->>'status' = 'error') AS errors
            FROM execution_events
            WHERE event_name = 'broker_latency'
              AND ts >= NOW() - INTERVAL '1 hour' * $1
            GROUP BY payload->>'broker', payload->>'command'
            ORDER BY count DESC
        """, hours)
        summary = await pool.fetchrow("""
            SELECT COUNT(*) AS total,
                   ROUND(AVG(duration_ms)::numeric, 1)  AS avg_ms,
                   ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p50_ms,
                   ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p95_ms,
                   COUNT(*) FILTER (WHERE payload->>'status' = 'error') AS errors
            FROM execution_events
            WHERE event_name = 'broker_latency'
              AND ts >= NOW() - INTERVAL '1 hour' * $1
        """, hours)
        return {
            "recent":  [dict(r) for r in recent],
            "stats":   [dict(r) for r in stats],
            "summary": dict(summary) if summary else {},
        }
    except Exception as e:
        return {"error": str(e), "recent": [], "stats": [], "summary": {}}


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

    # SnapTrade (read-only aggregator; connects Fidelity et al. via SnapTrade or
    # the Paycheck2Portfolio fallback). Considered connected if either credential
    # set is present.
    st_client = ev("SNAPTRADE_CLIENT_ID")
    st_p2p    = ev("P2P_EMAIL")
    snaptrade_ok = (bool(st_client) and not _is_placeholder(st_client)) or \
                   (bool(st_p2p) and not _is_placeholder(st_p2p))

    try:
        redis = await get_redis()
        stored_mode = await redis.get("config:trade_mode")
        trade_mode  = stored_mode or ev("TRADE_MODE") or "sandbox"
    except Exception:
        trade_mode = ev("TRADE_MODE") or "sandbox"

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
            "snaptrade": {
                "connected": snaptrade_ok,
                "read_only": True,
                "accounts":  [],
                "env": {
                    "SNAPTRADE_CLIENT_ID":     _masked(ev("SNAPTRADE_CLIENT_ID")),
                    "SNAPTRADE_CONSUMER_KEY":  _masked(ev("SNAPTRADE_CONSUMER_KEY")),
                    "SNAPTRADE_USER_ID":       ev("SNAPTRADE_USER_ID"),
                    "SNAPTRADE_USER_SECRET":   _masked(ev("SNAPTRADE_USER_SECRET")),
                    "P2P_EMAIL":               ev("P2P_EMAIL"),
                    "P2P_PASSWORD":            _masked(ev("P2P_PASSWORD")),
                },
            },
        },
    }


# ── Positions cache — avoids 20-second broker-gateway wait on every page load ──
_positions_cache: dict = {"data": None, "ts": 0.0, "refreshing": False}
_POSITIONS_CACHE_TTL = 300  # serve cache for up to 5 minutes


async def _fetch_positions_from_gateway() -> dict:
    """Core broker-gateway fetch — no caching logic here."""
    import uuid as _uuid
    import json as _json

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
                    # OCC option symbols (e.g. AEHR260529C00080000) → underlying ticker
                    occ = re.match(r'^([A-Z]{1,6})\d{6}[CP]\d{8}$', sym)
                    if occ:
                        sym = occ.group(1)
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

        # ── Compute broker-normalized margin metrics ───────────────────────
        _eq    = float(bal.get("equity") or bal.get("total_equity") or bal.get("portfolio_value") or 0)
        _pv    = float(bal.get("portfolio_value") or _eq)
        _maint = float(bal.get("maintenance_margin") or 0)
        _init  = float(bal.get("initial_margin") or bal.get("current_requirement") or 0)
        _lmv   = float(bal.get("long_market_value") or 0)
        _dt_bp = float(
            bal.get("daytrading_buying_power") or
            ((bal.get("pdt") or {}).get("day_trade_buying_power")) or 0
        )
        _t_margin     = bal.get("margin") or {}
        _t_maint_call = float(_t_margin.get("maintenance_call") or 0)
        if _maint and _pv > 0:
            bal["maintenance_margin_pct"] = round(_maint / _pv * 100, 1)
        elif _t_maint_call > 0 and _eq > 0:
            bal["maintenance_margin_pct"] = round(_t_maint_call / _eq * 100, 1)
        if _init and (_pv or _eq) > 0:
            bal["initial_margin_pct"] = round(_init / (_pv or _eq) * 100, 1)
        if _lmv and _eq > 0:
            bal["leverage_ratio"] = round(_lmv / _eq, 2)
        if _dt_bp:
            bal["day_trade_bp"] = _dt_bp
        # ─────────────────────────────────────────────────────────────────

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


async def _warmup_caches():
    """Pre-warm broker positions, options prices, and SPY benchmark on startup."""
    import asyncio as _asyncio

    # 1. Broker positions — populate in-memory cache so equities pages load instantly
    try:
        await _refresh_positions_cache()
    except Exception:
        pass

    # 2. Options prices — pre-populate Redis for all active underlying tickers
    try:
        if not DB_URL:
            raise Exception("no db")
        pool = await _get_db_pool()
        rows = await pool.fetch(
            "SELECT DISTINCT underlying FROM option_positions WHERE status = 'active'"
        )
        tickers = [r["underlying"] for r in rows if r["underlying"]]
        if tickers:
            _redis_w = await get_redis()
            need_price = []
            for sym in tickers:
                if not await _redis_w.get(f"yf:price:{sym}"):
                    # Also skip if sentiment cache already has it
                    sent = await _redis_w.hget("sentiment:latest", sym)
                    if not sent:
                        need_price.append(sym)
            if need_price:
                _poly_key = os.getenv("MASSIVE_API_KEY", "")
                if _poly_key:
                    import aiohttp as _aiohttp
                    from datetime import timedelta as _td
                    _today = date.today()
                    _today_str = _today.isoformat()
                    _from_str  = (_today - _td(days=5)).isoformat()
                    async def _wp(session, sym):
                        url = (
                            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                            f"/{_from_str}/{_today_str}?adjusted=true&sort=desc&limit=1&apiKey={_poly_key}"
                        )
                        try:
                            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                                if resp.status == 200:
                                    d = await resp.json()
                                    bars = d.get("results") or []
                                    if bars:
                                        return sym, float(bars[0]["c"])
                        except Exception:
                            pass
                        return sym, None
                    async with _aiohttp.ClientSession() as sess:
                        results = await _asyncio.gather(*[_wp(sess, s) for s in need_price])
                    for sym, price in results:
                        if price is not None:
                            await _redis_w.setex(f"yf:price:{sym}", 900, str(price))
                            need_price.remove(sym)
    except Exception:
        pass

    # 3. SPY YTD benchmark — pre-populate so options performance loads instantly
    try:
        _redis_spy = await get_redis()
        if not await _redis_spy.get("yf:spy_ytd"):
            await get_options_performance()
    except Exception:
        pass


async def _refresh_positions_cache():
    """Background task: fetch positions and update the module-level cache."""
    global _positions_cache
    if _positions_cache["refreshing"]:
        return
    _positions_cache["refreshing"] = True
    try:
        data = await _fetch_positions_from_gateway()
        _positions_cache["data"] = data
        _positions_cache["ts"]   = time.monotonic()
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
    now = time.monotonic()
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
    _positions_cache["ts"]   = time.monotonic()
    _positions_cache["refreshing"] = False
    result = dict(data)
    result["cached"] = False
    result["cache_age_s"] = 0
    return result



@app.get("/api/broker/orders")
async def get_broker_orders(status: str = "open"):
    """Fetch open/all orders for all accounts via the broker gateway."""
    import uuid as _uuid
    import json as _json

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


@app.post("/api/broker/cancel-order")
async def cancel_broker_order(body: dict):
    """Cancel a specific open order by ID via the broker gateway."""
    check_token(body.get("token", ""))
    order_id = body.get("order_id", "")
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")
    import uuid as _uuid
    import json as _json
    try:
        import redis.asyncio as _aioredis
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=30,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")
    req_id = str(_uuid.uuid4())
    await redis.xadd(STREAMS["broker_commands"], {
        "command": "cancel_order", "request_id": req_id,
        "order_id": order_id,
        "account_label": body.get("account_label", ""),
        "issued_by": "webui",
    })
    result = await redis.blpop([f"broker:reply:{req_id}"], timeout=15)
    await redis.aclose()
    if not result:
        raise HTTPException(status_code=504, detail="Cancel order timeout")
    raw = _json.loads(result[1])
    r = raw[0] if isinstance(raw, list) else raw
    if r.get("status") != "ok":
        raise HTTPException(status_code=502, detail=r.get("error", "Cancel failed"))
    return {"status": "ok", "order_id": order_id}


@app.get("/api/broker/quote")
async def get_broker_quote(symbol: str, account_label: str = ""):
    """Fetch bid/ask/last for a symbol via the broker gateway."""
    import uuid as _uuid
    import json as _json

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


class ChartSnapshotBody(BaseModel):
    token:  str
    ticker: str
    date:   str   # YYYY-MM-DD
    image:  str   # base64-encoded PNG


@app.post("/api/options/trader/save-chart-snapshot")
async def save_chart_snapshot(body: ChartSnapshotBody):
    """Save a chart screenshot PNG captured at order placement time."""
    check_token(body.token)
    import base64 as _b64
    import re as _re
    ticker = _re.sub(r"[^A-Z0-9]", "", body.ticker.upper())[:10]
    date   = _re.sub(r"[^0-9\-]", "", body.date)[:10]
    if not ticker or not date:
        raise HTTPException(status_code=400, detail="Invalid ticker or date")
    snap_dir = Path("/app/webui/static/snapshots")
    snap_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{ticker}_{date}.png"
    path = snap_dir / filename
    try:
        path.write_bytes(_b64.b64decode(body.image))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image save failed: {e}")
    return {"status": "ok", "path": f"/static/snapshots/{filename}", "filename": filename}


class AtrTemplateBody(BaseModel):
    token:        str
    ticker:       str
    anchor_price: float
    atr_value:    float
    trade_date:   str    # YYYY-MM-DD
    order_ids:    str = ""


@app.post("/api/options/trader/save-atr-template")
async def save_atr_template(body: AtrTemplateBody):
    """Persist ATR levels from order placement for ongoing trade monitoring."""
    check_token(body.token)
    if not DB_URL:
        return {"status": "ok", "stored": False}
    import re as _re
    ticker = _re.sub(r"[^A-Z0-9]", "", body.ticker.upper())[:10]
    pool = await _get_db_pool()
    # Delete any existing template for same ticker+date then insert fresh
    await pool.execute(
        "DELETE FROM option_atr_templates WHERE ticker=$1 AND trade_date=$2::date",
        ticker, body.trade_date,
    )
    await pool.execute(
        """
        INSERT INTO option_atr_templates (ticker, anchor_price, atr_value, trade_date, order_ids)
        VALUES ($1, $2, $3, $4::date, $5)
        """,
        ticker, body.anchor_price, body.atr_value, body.trade_date, body.order_ids or None,
    )
    return {"status": "ok", "stored": True}


@app.get("/api/options/trader/atr-template/{ticker}")
async def get_atr_templates(ticker: str):
    """Return all saved ATR templates for a ticker (most recent first)."""
    if not DB_URL:
        return {"templates": []}
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT ticker, anchor_price, atr_value, trade_date::text, order_ids, created_at::text
        FROM option_atr_templates
        WHERE ticker = $1
        ORDER BY trade_date DESC
        LIMIT 10
        """,
        ticker.upper(),
    )
    return {"templates": [dict(r) for r in rows]}


class OptionOrderLeg(BaseModel):
    option_symbol: str   # full OCC/broker symbol, e.g. AAPL260530C00200000
    underlying:    str
    option_type:   str   # call | put
    strike:        float
    expiration:    str   # YYYY-MM-DD
    side:          str   # buy_to_open | sell_to_open | buy_to_close | sell_to_close
    quantity:      int
    price:         Optional[float] = None  # None → market order


class OptionOrderBody(BaseModel):
    token:         str
    account_label: str
    legs:          list[OptionOrderLeg]
    order_type:    str = "limit"   # market | limit
    duration:      str = "day"     # day | gtc


@app.post("/api/options/trader/place-order")
async def place_option_order(body: OptionOrderBody):
    """Submit one or more option order legs to the broker gateway."""
    check_token(body.token)
    if not body.legs:
        raise HTTPException(status_code=400, detail="At least one leg required")
    if body.order_type not in ("market", "limit"):
        raise HTTPException(status_code=400, detail="order_type must be market or limit")
    if body.duration not in ("day", "gtc"):
        raise HTTPException(status_code=400, detail="duration must be day or gtc")

    import uuid as _uuid
    import json as _json
    import redis.asyncio as _aioredis

    try:
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    results = []
    for leg in body.legs:
        req_id = str(_uuid.uuid4())
        cmd = {
            "command":       "place_order",
            "request_id":    req_id,
            "account_label": body.account_label,
            "asset_class":   "option",
            "symbol":        leg.underlying.upper(),
            "option_symbol": leg.option_symbol,
            "side":          leg.side,
            "quantity":      str(leg.quantity),
            "order_type":    body.order_type,
            "price":         str(leg.price) if leg.price is not None else "",
            "duration":      body.duration,
            "tag":           f"wt-{req_id[:36]}",
            "issued_by":     "webui",
        }
        await redis.xadd(STREAMS["broker_commands"], cmd)
        result = await redis.blpop([f"broker:reply:{req_id}"], timeout=15)
        if not result:
            await redis.aclose()
            raise HTTPException(status_code=504, detail=f"Order timeout for leg {leg.option_symbol}")

        raw = _json.loads(result[1])
        r   = raw[0] if isinstance(raw, list) else raw
        if r.get("status") != "ok":
            await redis.aclose()
            raise HTTPException(status_code=502, detail=r.get("error", "Order failed"))

        order_data   = r.get("data", {}) or {}
        inner_status = str(order_data.get("status", "")).lower()
        REJECTED     = {"rejected", "error", "canceled", "cancelled", "denied", "failed"}
        order_id     = str(order_data.get("id", order_data.get("orderId", "")))
        raw_errors   = order_data.get("errors") or {}
        if isinstance(raw_errors, dict):
            raw_errors = raw_errors.get("error") or {}
        broker_err   = str(
            raw_errors or order_data.get("error") or order_data.get("message") or ""
        ).strip()
        null_id      = not order_id or order_id in ("0", "None", "null")

        if inner_status in REJECTED or broker_err or null_id:
            await redis.aclose()
            raise HTTPException(status_code=422, detail=broker_err or f"Order {inner_status or 'rejected'} by broker")

        results.append({"leg": leg.option_symbol, "order_id": order_id, "status": inner_status or "pending"})

    await redis.aclose()
    return {"status": "ok", "orders": results}


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

    import uuid as _uuid
    import json as _json

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
        "ot-mcp-tradingview", "ot-mcp-alpaca",
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
    token: str = ""
    keys:  list


LIVE_MODE_CONFIRMATION_PHRASE = "I understand the risks and accept full responsibility"

def _get_risk_disclosure_hash() -> str:
    import hashlib
    path = os.path.join(os.path.dirname(__file__), "static", "RISK_DISCLOSURE.md")
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "__unavailable__"


class TradeModeBody(BaseModel):
    token: str
    mode:  str  # "sandbox" | "live"

class LiveAckBody(BaseModel):
    token:  str
    phrase: str


@app.get("/api/trade-mode")
async def get_trade_mode():
    redis = await get_redis()
    stored = await redis.get("config:trade_mode")
    mode   = stored or _read_env_file().get("TRADE_MODE", "sandbox") or "sandbox"
    return {"mode": mode}


@app.get("/api/live-mode/ack-status")
async def get_live_ack_status():
    current_hash = _get_risk_disclosure_hash()
    try:
        pool = await _get_db_pool()
        row  = await pool.fetchrow(
            "SELECT acknowledged_at, risk_sha256 FROM live_mode_ack WHERE id = 1"
        )
    except Exception:
        row = None
    if not row:
        return {"acked": False, "current": False, "acked_at": None}
    is_current = row["risk_sha256"] == current_hash
    return {
        "acked":    True,
        "current":  is_current,
        "acked_at": row["acknowledged_at"].isoformat() if row["acknowledged_at"] else None,
    }


@app.post("/api/live-mode/acknowledge")
async def post_live_acknowledge(body: LiveAckBody):
    check_token(body.token)
    if body.phrase != LIVE_MODE_CONFIRMATION_PHRASE:
        raise HTTPException(status_code=400, detail="phrase_mismatch")
    current_hash = _get_risk_disclosure_hash()
    try:
        pool = await _get_db_pool()
        await pool.execute(
            """INSERT INTO live_mode_ack (id, phrase_typed, risk_sha256)
               VALUES (1, $1, $2)
               ON CONFLICT (id) DO UPDATE SET
                   acknowledged_at = NOW(),
                   phrase_typed    = EXCLUDED.phrase_typed,
                   risk_sha256     = EXCLUDED.risk_sha256""",
            body.phrase, current_hash,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    acked_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return {"ok": True, "acked_at": acked_at}


@app.post("/api/trade-mode")
async def set_trade_mode(body: TradeModeBody):
    check_token(body.token)
    if body.mode not in ("sandbox", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'sandbox' or 'live'")
    if body.mode == "live":
        current_hash = _get_risk_disclosure_hash()
        try:
            pool = await _get_db_pool()
            row  = await pool.fetchrow(
                "SELECT risk_sha256 FROM live_mode_ack WHERE id = 1"
            )
        except Exception:
            row = None
        if not row or row["risk_sha256"] != current_hash:
            raise HTTPException(status_code=403, detail="acknowledgment_required")
    redis = await get_redis()
    await redis.set("config:trade_mode", body.mode)
    _write_env_file({"TRADE_MODE": body.mode})
    return {"mode": body.mode}


@app.post("/api/broker/env/reveal")
async def reveal_broker_env(request: Request, body: EnvReveal):
    """Return unmasked env values for the given keys (session-cookie or WEBUI_TOKEN auth)."""
    session = request.cookies.get("ot_session", "")
    payload = _verify_jwt(session)
    if not payload and body.token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    env = _read_env_file()
    # Merge DB-stored secrets so keys set via My Profile are visible after container restarts
    if payload:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                "SELECT key, encrypted_value FROM user_secrets WHERE user_id=$1::uuid AND key = ANY($2::text[])",
                payload["sub"], list(body.keys),
            )
            for row in rows:
                try:
                    env[row["key"]] = _decrypt_secret(row["encrypted_value"])
                except Exception:
                    pass
        except Exception:
            pass
    return {k: env.get(k) or os.getenv(k, "") for k in body.keys}


class EnvUpdate(BaseModel):
    token: str = ""
    vars:  dict


@app.post("/api/broker/env")
async def update_broker_env(request: Request, body: EnvUpdate):
    """Write broker credential env vars to the .env file (session-cookie or WEBUI_TOKEN auth)."""
    session = request.cookies.get("ot_session", "")
    if not _verify_jwt(session) and body.token != WEBUI_TOKEN:
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
        log.error("broker_env.update_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update configuration")


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
                    return {"ok": True, "message": "OVTLYR login page reachable — credentials saved (full login requires scraper container)"}
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

            elif service == "eodhd":
                api_key = ev("EODHD_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="EODHD_API_KEY is required")
                resp = await s.get(
                    "https://eodhd.com/api/real-time/AAPL.US",
                    params={"api_token": api_key, "fmt": "json"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid EODHD API key")
                if resp.status == 402:
                    raise HTTPException(status_code=400, detail="EODHD subscription does not cover this endpoint")
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    price = data.get("close") or data.get("previousClose", "")
                    return {"ok": True, "message": f"EODHD key valid — AAPL last price ${float(price):.2f}"}
                raise HTTPException(status_code=400, detail=f"EODHD returned HTTP {resp.status}")

            elif service == "alpha_vantage":
                api_key = ev("ALPHA_VANTAGE_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="ALPHA_VANTAGE_API_KEY is required")
                resp = await s.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "GLOBAL_QUOTE", "symbol": "AAPL", "apikey": api_key},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status != 200:
                    raise HTTPException(status_code=400, detail=f"Alpha Vantage returned HTTP {resp.status}")
                data = await resp.json()
                if "Error Message" in data:
                    raise HTTPException(status_code=400, detail=f"Alpha Vantage error: {data['Error Message']}")
                if "Information" in data:
                    raise HTTPException(status_code=400, detail="Alpha Vantage API limit reached or invalid key")
                quote = data.get("Global Quote", {})
                price = quote.get("05. price", "")
                if not price:
                    raise HTTPException(status_code=400, detail="Alpha Vantage returned no data — check API key")
                return {"ok": True, "message": f"Alpha Vantage key valid — AAPL last price ${float(price):.2f}"}

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
                    count = data.get("resultsCount", 0)
                    return {"ok": True, "message": f"Massive API key valid — {count} bar(s) returned for AAPL"}
                raise HTTPException(status_code=400, detail=f"Massive API returned HTTP {resp.status}")

            elif service == "fred":
                api_key = ev("FRED_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="FRED_API_KEY is required")
                resp = await s.get(
                    "https://api.stlouisfed.org/fred/series",
                    params={"series_id": "BAMLH0A0HYM2", "api_key": api_key, "file_type": "json"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 400:
                    body_text = await resp.text()
                    detail = "Invalid FRED API key" if "api_key" in body_text.lower() else f"FRED error: {body_text[:120]}"
                    raise HTTPException(status_code=400, detail=detail)
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    title = (data.get("seriess") or [{}])[0].get("title", "US HY OAS")
                    return {"ok": True, "message": f"FRED API key valid — series: {title}"}
                raise HTTPException(status_code=400, detail=f"FRED returned HTTP {resp.status}")

            elif service == "finnhub":
                api_key = ev("FINNHUB_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="FINNHUB_API_KEY is required")
                resp = await s.get(
                    "https://finnhub.io/api/v1/stock/profile2",
                    params={"symbol": "AAPL", "token": api_key},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401:
                    raise HTTPException(status_code=400, detail="Invalid Finnhub API key")
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("name", "Apple Inc.")
                    return {"ok": True, "message": f"Finnhub API key valid — test: {name}"}
                raise HTTPException(status_code=400, detail=f"Finnhub returned HTTP {resp.status}")

            elif service == "fmp":
                api_key = ev("FMP_API_KEY")
                if not api_key:
                    raise HTTPException(status_code=400, detail="FMP_API_KEY is required")
                resp = await s.get(
                    "https://financialmodelingprep.com/stable/profile",
                    params={"symbol": "AAPL", "apikey": api_key},
                    timeout=_aiohttp.ClientTimeout(total=10),
                )
                if resp.status == 401 or resp.status == 403:
                    raise HTTPException(status_code=400, detail="Invalid FMP API key")
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data and data[0].get("companyName"):
                        name = data[0]["companyName"]
                        mkt  = data[0].get("marketCap", 0)
                        return {"ok": True, "message": f"FMP API key valid — {name} mktCap ${mkt:,.0f}"}
                    if isinstance(data, dict) and "Error Message" in data:
                        raise HTTPException(status_code=400, detail=f"FMP error: {data['Error Message']}")
                    raise HTTPException(status_code=400, detail="FMP returned no data — check API key")
                raise HTTPException(status_code=400, detail=f"FMP returned HTTP {resp.status}")

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

            else:
                raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    except HTTPException:
        raise
    except Exception as e:
        log.error("config.test_connector_failed", service=getattr(body, "service", ""), error=str(e))
        raise HTTPException(status_code=500, detail="Connection test failed — check logs")


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
        log.error("agentmail.provision_failed", error=str(e))
        raise HTTPException(status_code=500, detail="AgentMail provisioning failed")

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
            results = []
            async with _aiohttp.ClientSession() as s:
                for label, key_id, secret, url in [
                    ("paper", ev("ALPACA_API_KEY"),      ev("ALPACA_API_SECRET"),      "https://paper-api.alpaca.markets/v2/account"),
                    ("live",  ev("ALPACA_LIVE_API_KEY"), ev("ALPACA_LIVE_API_SECRET"), "https://api.alpaca.markets/v2/account"),
                ]:
                    if not key_id or not secret or _is_placeholder(key_id) or _is_placeholder(secret):
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
            import base64 as _b64
            import hashlib as _hl
            import hmac as _hmac
            import uuid as _uuid
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

        elif broker == "snaptrade":
            from shared import snaptrade_source as sts
            # Surface .env / saved credential values to the connector (reads os.environ).
            for k in ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY", "SNAPTRADE_USER_ID",
                      "SNAPTRADE_USER_SECRET", "P2P_EMAIL", "P2P_PASSWORD",
                      "P2P_SUPABASE_URL", "P2P_SUPABASE_ANON"):
                v = ev(k)
                if v:
                    os.environ[k] = v
            src = sts.configured_source()
            if not src:
                return {"ok": False,
                        "message": "No SnapTrade or Paycheck2Portfolio credentials configured"}
            try:
                pf = await sts.fetch_portfolio()
            except Exception as e:
                return {"ok": False, "message": f"{src}: {str(e)[:160]}"}
            institutions = sorted({a.get("institution") for a in pf.get("accounts", [])
                                   if a.get("institution")})
            npos = len(pf.get("positions", []))
            inst_txt = ", ".join(institutions) if institutions else "no linked brokerages"
            return {"ok": True,
                    "message": f"{src} · {inst_txt} · {npos} positions synced"}

        else:
            return {"ok": False, "message": f"Unknown broker: {broker}"}

    except Exception as e:
        return {"ok": False, "message": f"Connection error: {str(e)[:100]}"}


@app.get("/api/broker/accounts")
async def get_broker_accounts():
    """Return all configured accounts from accounts.toml with their friendly display names.

    Reads the same source as the Broker dashboard so every configured account
    appears regardless of whether it has DB activity yet.  Display names come
    from {LABEL}_DISPLAY_NAME env vars (e.g. WEBULL_LIVE_2_DISPLAY_NAME).
    Only accounts whose account-ID env var resolves to a non-empty value are
    included (unconfigured slots are skipped).
    """
    import re as _re
    env = _read_env_file()

    def ev(key: str) -> str:
        return env.get(key) or os.getenv(key, "")

    def resolve(val: str) -> str:
        return _re.sub(r'\$\{(\w+)\}', lambda m: ev(m.group(1)) or "", val or "")

    accounts = []
    try:
        import toml as _toml
        raw = _toml.load(ACCOUNTS_CONFIG)
        for a in raw.get("accounts", []):
            if a.get("enabled") is False:
                continue
            label      = a.get("label", "")
            account_id = resolve(a.get("id", ""))
            if not account_id:
                continue
            dn_key       = label.upper().replace("-", "_") + "_DISPLAY_NAME"
            display_name = ev(dn_key) or label
            accounts.append({
                "account_id":   label,
                "label":        label,
                "display_name": display_name,
                "broker":       a.get("broker", ""),
                "mode":         a.get("mode", ""),
            })
    except Exception:
        pass

    return {"accounts": accounts}


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
    Signal source priority: ovtlyr:position_intel → scanner:ovtlyr:latest
    Price source priority:  sentiment:latest → last 2 bars from get_market_bars
    """
    check_token(token)
    import json as _json
    _redis = await get_redis()

    # Fetch OVTLYR signal — position_intel first (2h TTL), fall back to scanner cache
    result = {}
    for ticker in ("SPY", "QQQ"):
        raw = await _redis.hget("ovtlyr:position_intel", ticker)
        if not raw:
            raw = await _redis.hget("scanner:ovtlyr:latest", ticker)
        if raw:
            try:
                result[ticker] = _json.loads(raw)
            except Exception:
                pass

    # Enrich with daily price change — sentiment cache first, then bars fallback
    for ticker in ("SPY", "QQQ"):
        price_data = None

        # Primary: sentiment scraper cache
        raw = await _redis.hget("sentiment:latest", ticker)
        if raw:
            try:
                s = _json.loads(raw)
                close      = s.get("close")
                prev_close = s.get("prev_close")
                if close is not None and prev_close:
                    change     = round(float(close) - float(prev_close), 2)
                    change_pct = round(change / float(prev_close) * 100, 2)
                    price_data = {
                        "close":      round(float(close), 2),
                        "prev_close": round(float(prev_close), 2),
                        "change":     change,
                        "change_pct": change_pct,
                    }
            except Exception as e:
                log.warning("market_signals.price_enrich_error", ticker=ticker, error=str(e))

        # Fallback: derive change % from last 2 daily bars
        if price_data is None:
            try:
                bars_resp = await get_market_bars(ticker=ticker, days=5)
                bars = [b for b in (bars_resp.get("bars") or []) if b.get("close")]
                if len(bars) >= 2:
                    close      = round(float(bars[-1]["close"]), 2)
                    prev_close = round(float(bars[-2]["close"]), 2)
                    change     = round(close - prev_close, 2)
                    change_pct = round(change / prev_close * 100, 2)
                    price_data = {
                        "close":      close,
                        "prev_close": prev_close,
                        "change":     change,
                        "change_pct": change_pct,
                    }
                elif len(bars) == 1:
                    price_data = {"close": round(float(bars[-1]["close"]), 2)}
            except Exception as e:
                log.warning("market_signals.bars_fallback_error", ticker=ticker, error=str(e))

        if price_data:
            if ticker in result:
                result[ticker].update(price_data)
            else:
                result[ticker] = price_data

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
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
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
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
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
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
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
        except Exception as ex:
            log.warning("breadth.db_history_error", error=str(ex))

    return {"current": current, "history": history}


@app.get("/api/ovtlyr/ticker/{ticker}")
async def get_ovtlyr_ticker(ticker: str, token: str = ""):
    """Return OVTLYR intel for a single ticker (Redis → DB fallback)."""
    check_token(token)
    import json as _json
    sym = ticker.upper()
    _redis = await get_redis()

    # Redis: position_intel hash (per-ticker key)
    raw = await _redis.hget("ovtlyr:position_intel", sym)
    if raw:
        try:
            data = _json.loads(raw)
            data["ticker"] = sym
            data["source"] = "redis"
            return {"ticker": sym, "data": data}
        except Exception:
            pass

    # Redis: screener cache
    raw2 = await _redis.hget("scanner:ovtlyr:latest", sym)
    if raw2:
        try:
            data = _json.loads(raw2)
            data["ticker"] = sym
            data["source"] = "screener"
            return {"ticker": sym, "data": data}
        except Exception:
            pass

    # DB — merge both tables: ovtlyr_lists (signal/name/sector) + ovtlyr_intel (nine_score/oscillator/fear_greed)
    if DB_URL:
        try:
            pool = await _get_db_pool()
            async with pool.acquire() as conn:
                intel_row = await conn.fetchrow(
                    """
                    SELECT ticker, signal, signal_active, signal_date, nine_score,
                           oscillator, fear_greed, last_close, avg_vol_30d, raw, ts
                    FROM ovtlyr_intel WHERE ticker = $1 ORDER BY ts DESC LIMIT 1
                    """,
                    sym,
                )
                lists_row = await conn.fetchrow(
                    """
                    SELECT DISTINCT ON (ticker)
                           ticker, list_type, name, sector, signal, signal_date,
                           last_price, avg_vol_30d, ts
                    FROM ovtlyr_lists WHERE ticker = $1 ORDER BY ticker, ts DESC
                    """,
                    sym,
                )

            if intel_row or lists_row:
                # Start from lists data (has name/sector/list_type), overlay intel (has nine_score etc)
                data: dict = {}
                if lists_row:
                    data = {k: v for k, v in dict(lists_row).items()}
                    data["last_close"] = data.pop("last_price", None)
                if intel_row:
                    intel = {k: v for k, v in dict(intel_row).items()}
                    raw_json = intel.pop("raw", None)
                    # Overlay intel fields — prefer non-null intel values
                    for k, v in intel.items():
                        if v is not None:
                            data[k] = v
                    if raw_json and isinstance(raw_json, str):
                        try:
                            for k, v in _json.loads(raw_json).items():
                                if k not in data or data[k] is None:
                                    data[k] = v
                        except Exception:
                            pass
                # Normalise dates
                for field in ("signal_date", "ts"):
                    if data.get(field) and hasattr(data[field], "isoformat"):
                        data[field] = data[field].isoformat()
                data["source"] = "db"
                return {"ticker": sym, "data": data}
        except Exception as ex:
            log.warning("ovtlyr_ticker.db_error", ticker=sym, error=str(ex))

    return {"ticker": sym, "data": None}


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
    import os
    import glob as _glob
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
            # OCC option contract IDs look like AEHR250117C00003000 — extract underlying
            occ_match = re.match(r'^([A-Z]+)\d{6}[CP]\d+$', sym)
            chart_sym = occ_match.group(1) if occ_match else sym
            qty = float(p.get("qty") or p.get("quantity") or 0)
            positions.append({
                "symbol":       chart_sym,
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
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hmac
    import uuid as _uuid
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
        log.error("broker.accounts_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to fetch broker accounts")


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
    redis = await get_redis()
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
    redis = await get_redis()
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
    redis = await get_redis()
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

_LOG_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'   # timestamp
    r'\s+\[(\w+)\s*\]'                               # level
    r'\s+(\S+)'                                       # event
    r'(.*)?$'                                         # remainder (key=value pairs)
)

@app.get("/api/assignments/{assignment_id}/daily-log")
async def get_assignment_daily_log(assignment_id: str):
    """Return today's strategy execution log from market open for this assignment."""
    assignments = _read_assignments()
    a = next((x for x in assignments if x["id"] == assignment_id), None)
    if a is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    strategy_name = a.get("strategy_name", "")
    account_label = a.get("account_label", "")

    # Determine which container runs this strategy
    container = "ot-trader-equity"

    # Market open = 09:30 ET today as UTC unix timestamp
    today = date.today()
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    market_open_et = datetime(today.year, today.month, today.day, 9, 30, 0, tzinfo=et)
    since_unix = int(market_open_et.timestamp())

    raw = _podman_api(
        f"/v4.0.0/libpod/containers/{container}/logs"
        f"?stdout=true&stderr=true&since={since_unix}",
        timeout=10,
        raw=True,
    )
    raw_lines: list[str] = []
    if raw:
        raw_lines = _parse_docker_log_stream(raw)

    entries = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        m = _LOG_LINE_RE.match(line)
        if m:
            ts_str   = m.group(1)[11:]          # HH:MM:SS portion
            level    = m.group(2).lower().strip()
            event    = m.group(3)
            rest     = (m.group(4) or "").strip()
            entries.append({
                "ts":    ts_str,
                "level": level,
                "event": event,
                "rest":  rest,
                "raw":   line,
            })
        else:
            entries.append({"ts": "", "level": "info", "event": "", "rest": line, "raw": line})

    return {
        "date":          today.isoformat(),
        "account":       account_label,
        "strategy":      strategy_name,
        "container":     container,
        "market_open":   market_open_et.strftime("%H:%M ET"),
        "entries":       entries,
    }


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
    strategy:        str   = "ema_crossover"
    direction:       str   = "long"
    stop_pct:        float = 1.5
    tp_pct:          float = 3.0
    confidence:      float = 0.70
    max_pos:         float = 500.0
    # RSI strategy params
    rsi_period:  int   = 14
    oversold:    float = 30.0
    overbought:  float = 70.0
    # Volatility breakout params
    lookback:  int   = 20
    atr_period: int  = 14
    atr_mult:  float = 1.5
    stop_atr:  float = 2.0
    token:           str   = ""


def _bt_run_in_process(params: dict) -> dict:
    """Top-level wrapper so ProcessPoolExecutor can pickle it."""
    from webui.backtest_runner import run_backtest
    return run_backtest(params)


def _bt_validate_in_process(params: dict) -> dict:
    """Top-level wrapper for validation — required for ProcessPoolExecutor pickling."""
    from webui.backtest_validator import run_validation
    return run_validation(params)


def _bt_distribution_in_process(params: dict) -> dict:
    """Top-level wrapper for distribution backtest — required for ProcessPoolExecutor pickling."""
    from webui.backtest_runner import run_distribution_backtest
    step_days = int(params.pop("step_days", 21))
    return run_distribution_backtest(params, step_days=step_days)


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
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable)

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                leftMargin=12*mm, rightMargin=12*mm,
                                topMargin=12*mm, bottomMargin=12*mm)
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
    """Run a backtest without a saved strategy version (used by AI Engineer + Backtester page)."""
    check_token(body.token)
    params = {
        "ticker":          body.ticker,
        "period":          body.period,
        "strategy":        body.strategy,
        "stop_pct":        body.stop_pct,
        "tp_pct":          body.tp_pct,
        "confidence":      body.confidence,
        "direction":       body.direction,
        "max_pos":         body.max_pos,
        "initial_capital": body.initial_capital,
        "rsi_period":      body.rsi_period,
        "oversold":        body.oversold,
        "overbought":      body.overbought,
        "lookback":        body.lookback,
        "atr_period":      body.atr_period,
        "atr_mult":        body.atr_mult,
        "stop_atr":        body.stop_atr,
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
        log.error("backtest.error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)[:200])


class BacktestValidateBody(BaseModel):
    ticker:          str
    period:          str   = "2y"
    initial_capital: float = 10_000.0
    stop_pct:        float = 1.5
    tp_pct:          float = 3.0
    confidence:      float = 0.70
    direction:       str   = "long"
    max_pos:         float = 500.0
    n_splits:        int   = 0      # 0 = auto-size walk-forward windows
    n_perms:         int   = 1000   # Monte Carlo permutations
    n_bootstrap:     int   = 1000   # Bootstrap samples
    token:           str   = ""


class DistributionBacktestBody(BaseModel):
    ticker:          str
    period:          str   = "2y"
    initial_capital: float = 10_000.0
    stop_pct:        float = 1.5
    tp_pct:          float = 3.0
    confidence:      float = 0.70
    direction:       str   = "long"
    max_pos:         float = 500.0
    step_days:       int   = 21     # trading days between sampled start dates
    token:           str   = ""


@app.post("/api/backtest/distribution")
async def distribution_backtest(body: DistributionBacktestBody):
    """Run the strategy from every sampled start date and return a return distribution.

    Samples entry points every `step_days` trading days. Each run starts at that
    date and runs to the end of the history window, giving a full distribution of
    outcomes across all historical entry points.

    Returns per-run metrics plus a summary with percentiles (p10/p25/median/p75/p90),
    pct_positive, mean_sharpe, and mean_drawdown.

    Typical runtime: 10–60 s depending on period and step_days. Timeout: 300 s.
    """
    check_token(body.token)
    params = {
        "ticker":          body.ticker,
        "period":          body.period,
        "initial_capital": body.initial_capital,
        "stop_pct":        body.stop_pct,
        "tp_pct":          body.tp_pct,
        "confidence":      body.confidence,
        "direction":       body.direction,
        "max_pos":         body.max_pos,
        "step_days":       body.step_days,
    }
    loop = asyncio.get_event_loop()
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            results = await asyncio.wait_for(
                loop.run_in_executor(pool, _bt_distribution_in_process, params),
                timeout=300,
            )
        return results
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Distribution backtest timed out (300 s limit)")
    except Exception as e:
        log.error("backtest.error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/api/backtest/validate")
async def validate_backtest(body: BacktestValidateBody):
    """Statistical validation of the EMA crossover strategy on a given ticker.

    Runs three tests sequentially inside a single worker process:
      1. Walk-forward — equal-window consistency check across market regimes.
      2. Monte Carlo permutation — p-value test: is Sharpe better than random?
      3. Bootstrap CI — 90 % confidence interval on the Sharpe ratio.

    Also returns expanded base metrics: Sortino, Calmar, Recovery Factor,
    Profit/Loss ratio, Profit Factor, avg trade PnL, avg hold days.

    Typical runtime: 15–45 s depending on period and n_splits.
    Timeout: 300 s.
    """
    check_token(body.token)
    params = {
        "ticker":          body.ticker,
        "period":          body.period,
        "initial_capital": body.initial_capital,
        "stop_pct":        body.stop_pct,
        "tp_pct":          body.tp_pct,
        "confidence":      body.confidence,
        "direction":       body.direction,
        "max_pos":         body.max_pos,
        "n_splits":        body.n_splits,
        "n_perms":         body.n_perms,
        "n_bootstrap":     body.n_bootstrap,
    }
    loop = asyncio.get_event_loop()
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            results = await asyncio.wait_for(
                loop.run_in_executor(pool, _bt_validate_in_process, params),
                timeout=300,
            )
        return results
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Validation timed out (300 s limit)")
    except Exception as e:
        log.error("backtest.error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)[:200])


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


# ── SSL / TLS (Caddy) ─────────────────────────────────────────────────────────

CADDY_ADMIN_URL = os.getenv("CADDY_ADMIN_URL", "http://ot-caddy:2019")
_CADDY_CERT_ROOT = "/caddy-data/caddy/certificates"
_ACME_DIR        = "acme-v02.api.letsencrypt.org-directory"


def _parse_cert_file(cert_path: str) -> dict:
    """
    Parse a PEM certificate file using the openssl CLI.
    Returns expiry ISO string, days remaining, subject CN, and issuer O.
    Falls back to empty dict if openssl is unavailable or cert not found.
    """
    import subprocess
    import re
    from pathlib import Path
    from datetime import datetime, timezone

    if not Path(cert_path).exists():
        return {}
    try:
        r = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-noout",
             "-enddate", "-startdate", "-subject", "-issuer"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {}
        out = r.stdout
        def _field(key):
            m = re.search(rf"{key}=(.+)", out)
            return m.group(1).strip() if m else ""

        not_after  = _field("notAfter")
        not_before = _field("notBefore")
        subject    = _field("CN")
        issuer_o   = _field("O")  # first O= match (could be in subject or issuer)

        # Parse "Apr 17 12:00:00 2026 GMT"
        def _parse_dt(s):
            for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
                try:
                    return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            return None

        expiry = _parse_dt(not_after)
        issued = _parse_dt(not_before)
        now    = datetime.now(timezone.utc)
        days   = (expiry - now).days if expiry else None

        # Get issuer O specifically (second O= occurrence is usually issuer)
        issuer_matches = re.findall(r"O\s*=\s*([^,\n/]+)", out)
        issuer_name = issuer_matches[-1].strip() if issuer_matches else issuer_o

        return {
            "expiry":         expiry.isoformat() if expiry else None,
            "issued_at":      issued.isoformat() if issued else None,
            "days_remaining": days,
            "subject":        subject,
            "issuer":         issuer_name,
        }
    except Exception as e:
        log.warning("ssl.cert_parse_failed", error=str(e))
        return {}


@app.get("/api/ssl/status")
async def get_ssl_status(token: str = ""):
    """
    Return SSL/TLS status: Caddy health, certificate expiry, configured domain,
    and pipeline encryption posture for Redis/PostgreSQL/MCP.
    """
    check_token(token)

    import aiohttp
    domain      = os.getenv("CADDY_DOMAIN", "")
    caddy_up    = False
    cert_info   = {}

    # 1. Check Caddy admin API
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{CADDY_ADMIN_URL}/config/",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                caddy_up = (r.status == 200)
    except Exception:
        pass

    # 2. Read cert file from mounted Caddy data volume
    if domain and domain not in ("localhost", ""):
        cert_path = f"{_CADDY_CERT_ROOT}/{_ACME_DIR}/{domain}/{domain}.crt"
        cert_info = _parse_cert_file(cert_path)
        # Fallback: staging CA path
        if not cert_info:
            staging = "acme-staging-v02.api.letsencrypt.org-directory"
            cert_path_staging = f"{_CADDY_CERT_ROOT}/{staging}/{domain}/{domain}.crt"
            cert_info = _parse_cert_file(cert_path_staging)

    return {
        "caddy_running":   caddy_up,
        "domain":          domain,
        "cert_valid":      bool(cert_info.get("expiry")),
        "expiry":          cert_info.get("expiry"),
        "issued_at":       cert_info.get("issued_at"),
        "days_remaining":  cert_info.get("days_remaining"),
        "subject":         cert_info.get("subject", domain),
        "issuer":          cert_info.get("issuer", ""),
        "auto_renew":      True,           # Caddy always auto-renews
        # Pipeline encryption posture
        "pipeline": {
            "webui_https":   caddy_up and bool(cert_info.get("expiry")),
            "redis_tls":     False,        # internal network only — TLS not yet enabled
            "postgres_tls":  False,        # internal network only — TLS not yet enabled
            "mcp_tls":       False,        # internal Podman network (10.89.0.0/24)
        },
    }


@app.post("/api/ssl/configure")
async def configure_ssl(body: dict, token: str = ""):
    """
    Save CADDY_DOMAIN and ACME_EMAIL to .env and reload Caddy config.
    Caddy will automatically obtain a Let's Encrypt certificate on next request.
    """
    check_token(token)
    domain = str(body.get("domain", "")).strip().lower()
    email  = str(body.get("email", "")).strip()

    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")

    # Write domain + email to .env file
    env_path = os.getenv("ENV_FILE_PATH", "/app/.env")
    try:
        lines = []
        replaced_domain = replaced_email = False
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CADDY_DOMAIN="):
                        lines.append(f"CADDY_DOMAIN={domain}\n"); replaced_domain = True
                    elif line.startswith("ACME_EMAIL=") and email:
                        lines.append(f"ACME_EMAIL={email}\n"); replaced_email = True
                    else:
                        lines.append(line)
        if not replaced_domain:
            lines.append(f"CADDY_DOMAIN={domain}\n")
        if not replaced_email and email:
            lines.append(f"ACME_EMAIL={email}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write .env: {e}")

    # Update runtime env so status endpoint reflects new domain immediately
    os.environ["CADDY_DOMAIN"] = domain
    if email:
        os.environ["ACME_EMAIL"] = email

    # Signal Caddy to reload its config (picks up new CADDY_DOMAIN env var)
    reloaded = False
    try:
        import subprocess
        r = subprocess.run(
            ["podman", "exec", "ot-caddy", "caddy", "reload",
             "--config", "/etc/caddy/Caddyfile"],
            capture_output=True, timeout=10,
        )
        reloaded = (r.returncode == 0)
    except Exception:
        pass

    return {"ok": True, "domain": domain, "email": email, "caddy_reloaded": reloaded}


@app.post("/api/ssl/renew")
async def force_ssl_renew(token: str = ""):
    """
    Force immediate certificate renewal by reloading Caddy.
    Caddy will re-check and renew the cert if it is within 30 days of expiry.
    """
    check_token(token)
    try:
        import subprocess
        r = subprocess.run(
            ["podman", "exec", "ot-caddy", "caddy", "reload",
             "--config", "/etc/caddy/Caddyfile"],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            return {"ok": True, "message": "Caddy reloaded — renewal will complete in the background"}
        return {"ok": False, "message": r.stderr.decode().strip() or "Caddy reload failed"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


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
    # Authenticate before accepting — check JWT session cookie or ?token= param
    session = websocket.cookies.get("ot_session", "")
    token   = websocket.query_params.get("token", "")
    if not (_verify_jwt(session) or token == WEBUI_TOKEN):
        await websocket.close(code=1008)  # 1008 = Policy Violation
        return
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
            # Count active directives
            active_directives = 0
            try:
                raw_dir = await redis.get("trade:directives")
                if raw_dir:
                    active_directives = sum(
                        1 for d in json.loads(raw_dir)
                        if d.get("status") == "active"
                    )
            except Exception:
                pass
            await websocket.send_json({
                "type":              "update",
                "overview":          overview,
                "agents":            agents,
                "signals":           signals,
                "streams":           streams,
                "trades":            trades,
                "trade_mode":        trade_mode,
                "active_directives": active_directives,
                "app_version":       APP_VERSION,
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


async def _div_fetch_massive_meta(ticker: str) -> dict:
    """Fetch dividend metadata from massive.com: ex-date, pay-date, amount, frequency."""
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {}
    import aiohttp as _aiohttp
    from datetime import date as _date, timedelta as _td
    try:
        params = {
            "ticker": ticker,
            "ex_dividend_date.lte": _date.today().isoformat(),
            "limit": 14,
            "sort": "ex_dividend_date",
            "order": "desc",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        async with _aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(
                "https://api.massive.com/stocks/v1/dividends",
                params=params,
                timeout=_aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
    except Exception as e:
        log.warning("div_meta.massive_failed", ticker=ticker, error=str(e))
        return {}

    results = data.get("results", [])
    if not results:
        return {}

    r0 = results[0]
    cutoff_12m = (_date.today() - _td(days=365)).isoformat()
    recent_12m = [r for r in results if (r.get("ex_dividend_date") or "") >= cutoff_12m]
    n = len(recent_12m)
    if   n >= 40: frequency = 52
    elif n >= 10: frequency = 12
    elif n >= 3:  frequency = 4
    elif n >= 1:  frequency = 2
    else:         frequency = 1

    aps = float(r0.get("cash_amount") or 0) or None
    far = aps * frequency if aps else None

    return {
        "ex_date":               r0.get("ex_dividend_date"),
        "pay_date":              r0.get("pay_date"),
        "amount_per_share":      aps,
        "frequency":             frequency,
        "forward_annual_rate":   far,
        "forward_yield_pct":     None,
        "sector":                None,
        "industry":              None,
        "payout_ratio":          None,
        "five_yr_avg_yield_pct": None,
    }


def _div_fetch_dividendcom_sync(ticker: str) -> dict:
    """Attempt to scrape dividend metadata from dividend.com. Gracefully handles Cloudflare blocks."""
    import re
    import urllib.request
    url = f"https://www.dividend.com/stocks/{ticker.lower()}/"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("div_meta.dividendcom_failed", ticker=ticker, error=str(exc))
        return {}

    if "Just a moment" in body or "cf-browser-verification" in body or len(body) < 1000:
        return {}

    out: dict = {}
    m = re.search(r'[Ee]x.?[Dd]ividend\s+[Dd]ate[^<]{0,50}<[^>]+>([A-Z][a-z]{2}\s+\d+,?\s+\d{4})', body)
    if m:
        try:
            from datetime import datetime as _dt
            raw = m.group(1).replace(",", "")
            out["ex_date"] = _dt.strptime(raw, "%b %d %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.search(r'[Yy]ield[^<]{0,30}<[^>]+>([\d.]+)%', body)
    if m:
        out["forward_yield_pct"] = float(m.group(1))
    return out


async def _div_fetch_meta_tiered(ticker: str) -> dict:
    """Fetch dividend metadata: massive.com → dividend.com (two-tier, no yfinance)."""
    meta = await _div_fetch_massive_meta(ticker)
    if meta.get("ex_date") or meta.get("amount_per_share"):
        return meta

    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    meta = await loop.run_in_executor(None, _div_fetch_dividendcom_sync, ticker)
    if meta.get("ex_date") or meta.get("forward_yield_pct"):
        return meta

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
                return t, await _div_fetch_meta_tiered(t)
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


def _div_project_payments(positions_flat: list[dict], months: list[str],
                          last_pay: dict | None = None) -> dict:
    """
    Project dividend income for each calendar month.
    positions_flat: list of {symbol, qty, forward_annual_rate, amount_per_share,
                              frequency, ex_date, sector, account_label, projected_annual_income}
    months: ['2026-03', '2026-04', …] (13 items: 1 prior + current + 11 future)
    last_pay: {ticker: most_recent_pay_date} from dividend_history — used to
              anchor projection for tickers whose API ex_date is null.
    Returns {month_key: {symbol: income, …}, …}
    """
    from datetime import date as _date, timedelta as _td
    monthly: dict[str, dict] = {m: {} for m in months}
    pay_lag = 14  # typical ex→pay lag in days

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

        interval = max(1, int(365 / freq))

        # Anchor ex_date: prefer API ex_date, then derive from last known pay, then default
        ex_str = p.get("ex_date")
        ex: _date
        try:
            if ex_str:
                ex = _date.fromisoformat(str(ex_str))
            elif last_pay and sym in last_pay and last_pay[sym]:
                # Back-derive ex_date from the most recent actual payment
                ex = last_pay[sym] - _td(days=pay_lag)
            else:
                ex = _date.today() - _td(days=interval)
        except Exception:
            ex = _date.today() - _td(days=interval)

        today = _date.today()
        current_month_start = today.replace(day=1)

        # Step backward from the anchor until we are before the current-month window,
        # then advance forward to the first payment on or after the 1st of the current month.
        # This ensures the projection covers ALL payments in the current month — including
        # weeks that already paid earlier this month but may not be in local history yet.
        while ex + _td(days=pay_lag) >= current_month_start:
            ex -= _td(days=interval)
        while ex + _td(days=pay_lag) < current_month_start:
            ex += _td(days=interval)

        # Project payments across the window (freq + a few extra to cover the full window)
        for _ in range(freq + 4):
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
            if float(p.get("qty") or p.get("quantity") or p.get("shares") or 0) <= 0:
                continue
            sym = (p.get("symbol") or "").upper().strip()
            if sym and sym not in all_tickers:
                all_tickers.append(sym)

    # Enrich with dividend metadata (DB cache + yfinance) — used for ex/pay dates,
    # sector, industry, and yield% only. Income projection uses actual DB history below.
    meta = await _div_get_meta(all_tickers)

    # Pull per-(account, ticker) payment stats from dividend_history:
    #   recent_aps   — amount per share of the most recent recorded payment
    #   annual_count — number of payments in the last 12 months (actual frequency)
    # forward_annual_rate = recent_aps × annual_count (pure history, no yfinance rates)
    hist_stats: dict[tuple, dict] = {}  # (account_label, ticker) → stats
    if DB_URL:
        try:
            from datetime import date as _date, timedelta as _td
            pool = await _get_db_pool()
            _today = _date.today()
            _12mo_ago = _today - _td(days=365)
            _18mo_ago = _today - _td(days=548)
            # Most recent payment per (account, ticker)
            recent_rows = await pool.fetch("""
                SELECT DISTINCT ON (account_label, ticker)
                    account_label, ticker, amount_per_share
                FROM dividend_history
                WHERE pay_date >= $1
                ORDER BY account_label, ticker, pay_date DESC
            """, _18mo_ago)
            # Annual payment count per (account, ticker)
            count_rows = await pool.fetch("""
                SELECT account_label, ticker, COUNT(*)::int AS cnt
                FROM dividend_history
                WHERE pay_date >= $1
                GROUP BY account_label, ticker
            """, _12mo_ago)
            _count_map = {(r["account_label"], r["ticker"]): int(r["cnt"]) for r in count_rows}
            for r in recent_rows:
                key = (r["account_label"], r["ticker"])
                cnt = _count_map.get(key, 0)
                recent_aps = float(r["amount_per_share"] or 0)
                hist_stats[key] = {
                    "recent_aps":  recent_aps,
                    "annual_count": cnt,
                    "forward_annual_rate": round(recent_aps * cnt, 4) if cnt > 0 else 0.0,
                }
        except Exception:
            pass

    total_value = 0.0
    total_cost = 0.0
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
            if qty <= 0:
                continue
            cost = float(p.get("cost_basis") or p.get("cost") or 0)
            if not cost:
                # Webull (and some others) expose avg_entry_price per share, not total cost_basis
                avg_ep = float(p.get("avg_entry_price") or 0)
                if avg_ep > 0 and qty > 0:
                    cost = round(avg_ep * qty, 2)
            price= float(p.get("current_price") or p.get("last_price") or p.get("mark") or 0)
            mval = float(p.get("market_value") or (qty * price))
            if not price and qty > 0 and mval > 0:
                price = mval / qty
            total_value += mval
            total_cost += cost
            m = meta.get(sym, {})
            # Income projection: prefer actual DB history; fall back to yfinance only
            # when there are no recorded payments for this account + ticker.
            hs = hist_stats.get((lbl, sym))
            if hs and hs["forward_annual_rate"] > 0:
                far  = hs["forward_annual_rate"]
                aps  = hs["recent_aps"]
                freq = hs["annual_count"]
            else:
                far  = 0.0   # no history → not a confirmed payer for this account
                aps  = float(m.get("amount_per_share") or 0)
                freq = int(m.get("frequency") or 4)
            fyp  = float(m.get("forward_yield_pct") or 0)
            is_payer = far > 0
            ann_income = qty * far if is_payer else 0.0
            if is_payer:
                total_payers += 1
                total_annual  += ann_income
            positions_out.append({
                "symbol":                sym,
                "qty":                   qty,
                "cost_basis":            cost,
                "current_price":         price,
                "market_value":          mval,
                "unrealized_pnl":        round(mval - cost, 2) if cost > 0 else None,
                "unrealized_pnl_pct":    round((mval - cost) / cost * 100, 2) if cost > 0 else None,
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
                "yoc_pct":              round(ann_income / cost * 100, 2) if cost > 0 and ann_income > 0 else None,
            })
        bal = acct.get("balances", {})
        margin = bal.get("margin") or {}
        bp_raw = (bal.get("buying_power") or bal.get("cash_power")
                  or margin.get("stock_buying_power") or None)
        accounts_out.append({
            "label":         lbl,
            "display_name":  display_name,
            "mode":          acct.get("mode", ""),
            "buying_power":  float(bp_raw) if bp_raw not in (None, "", "None") else None,
            "positions":     positions_out,
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
            "total_cost_basis":             round(total_cost, 2),
            "total_unrealized_pnl":         round(total_value - total_cost, 2),
            "total_unrealized_pnl_pct":     round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0.0,
        },
    }


@app.get("/api/dividends/forecast")
async def div_forecast(token: str = ""):
    check_token(token)
    redis = await get_redis()
    _forecast_cache_key = f"dividend:forecast:{APP_VERSION}:cache"
    cached = await redis.get(_forecast_cache_key)
    if cached:
        return json.loads(cached)

    # Build from holdings
    holdings = await div_holdings(token=token)
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    current_month_key = today.strftime("%Y-%m")
    # Window: 1 prior month + current + 11 future = 13 months total.
    # i=-1 = prior month; i=0 = current; i=1..11 = future.
    months = [_date(today.year + (today.month + i - 1) // 12,
                    (today.month + i - 1) % 12 + 1, 1)
              for i in range(-1, 12)]
    month_keys   = [m.strftime("%Y-%m") for m in months]
    month_labels = [m.strftime("%b %Y") for m in months]

    # Flatten all positions
    positions_flat = []
    for acct in holdings["accounts"]:
        for p in acct["positions"]:
            if p["is_dividend_payer"]:
                positions_flat.append({**p, "account_label": acct["label"]})

    # Fetch 18 months of actual dividend history from DB, split by account + ticker.
    # This single query drives: past-month actuals, portfolio avg, and per-account
    # future breakdown so account filtering shows the correct share per broker.
    actual_by_month: dict[str, float] = {}         # month_key -> portfolio total
    actual_breakdown: dict[str, list] = {}         # month_key -> [{symbol,account_label,income}]
    acct_ticker_totals: dict[tuple, float] = {}    # (acct, ticker) -> total in completed months
    acct_first_month_raw: dict[str, str] = {}      # acct -> earliest past month_key with payments
    cutoff_18mo = today - _td(days=548)
    if DB_URL:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch("""
                SELECT to_char(pay_date, 'YYYY-MM') AS month_key,
                       account_label,
                       ticker,
                       SUM(total_received)::float AS total
                FROM dividend_history
                WHERE pay_date >= $1
                GROUP BY month_key, account_label, ticker
                ORDER BY month_key, total DESC
            """, cutoff_18mo)
            for r in rows:
                mk, acct, ticker = r["month_key"], r["account_label"], r["ticker"]
                total = float(r["total"])
                actual_by_month[mk] = actual_by_month.get(mk, 0) + total
                actual_breakdown.setdefault(mk, []).append(
                    {"symbol": ticker, "account_label": acct, "income": round(total, 2)}
                )
                if mk < current_month_key:
                    key = (acct, ticker)
                    acct_ticker_totals[key] = acct_ticker_totals.get(key, 0) + total
                    if acct not in acct_first_month_raw or mk < acct_first_month_raw[acct]:
                        acct_first_month_raw[acct] = mk
        except Exception:
            pass

    # History metadata and per-account monthly averages
    captured_count = sum(1 for mk in actual_by_month if mk < current_month_key)
    total_received_all = sum(acct_ticker_totals.values())

    cutoff_month_start = _date(cutoff_18mo.year, cutoff_18mo.month, 1)
    current_month_start = _date(today.year, today.month, 1)

    def _mo_elapsed(start_ym: str) -> int:
        """Calendar months from start_ym ('YYYY-MM') to current month (exclusive)."""
        y, m = int(start_ym[:4]), int(start_ym[5:7])
        return max(1, (today.year - y) * 12 + (today.month - m))

    # Per-account history-based monthly avg.
    # Denominator = calendar months from FIRST actual payment for that account → now.
    # Using the first payment month avoids the 18-month window diluting the avg when
    # positions were only recently added (e.g. 3 months of history ÷ 18 = 6× under-estimate).
    acct_completed_total: dict[str, float] = {}
    for (acct, ticker), total in acct_ticker_totals.items():
        acct_completed_total[acct] = acct_completed_total.get(acct, 0) + total
    per_account_monthly_avg = {
        acct: round(total / _mo_elapsed(acct_first_month_raw.get(acct, cutoff_month_start.strftime("%Y-%m"))), 2)
        for acct, total in acct_completed_total.items()
    }
    first_overall = min(acct_first_month_raw.values()) if acct_first_month_raw else None
    active_months = _mo_elapsed(first_overall) if first_overall else max(1, (
        current_month_start.year - cutoff_month_start.year) * 12 +
        (current_month_start.month - cutoff_month_start.month))
    history_monthly_total = round(sum(acct_completed_total.values()) / active_months, 2)

    # Holdings-based projection: recent_aps × annual_payments / 12 × qty (from div_holdings).
    # div_holdings now uses dividend_history exclusively — yfinance is NOT involved here.
    api_monthly_total = 0.0
    future_breakdown: list[dict] = []
    for p in positions_flat:
        ann = float(p.get("projected_annual_income") or 0)
        if ann <= 0:
            continue
        monthly = ann / 12
        api_monthly_total += monthly
        future_breakdown.append({
            "symbol":        p["symbol"],
            "account_label": p["account_label"],
            "income":        round(monthly, 2),
        })
    future_breakdown.sort(key=lambda x: -x["income"])

    # Build monthly output: actual for completed past months, API projection for the rest.
    # Current month uses the API projection so the frontend can show actual-received (green)
    # vs projected remaining (blue) using the two-tone bar logic.
    monthly_out = []
    for mk, lbl in zip(month_keys, month_labels):
        if mk in actual_by_month and mk < current_month_key:
            # Completed past month: real recorded income, per-(account, ticker) breakdown.
            income = actual_by_month[mk]
            breakdown = sorted(actual_breakdown.get(mk, []), key=lambda x: -x["income"])
            source = "actual"
        else:
            # Current or future month: current-holdings-based API projection.
            income = api_monthly_total
            breakdown = future_breakdown
            source = "projected"
        monthly_out.append({
            "month":            mk,
            "label":            lbl,
            "projected_income": round(income, 2),
            "breakdown":        breakdown,
            "data_source":      source,
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
        "monthly":                monthly_out,
        "by_ticker":              by_ticker,
        "by_sector":              by_sector,
        "by_account":             by_account,
        "by_yield":               by_yield,
        "total_projected_12mo":   round(sum(m["projected_income"] for m in monthly_out), 2),
        "avg_monthly_income":     round(api_monthly_total, 2),
        "history_monthly_avg":    history_monthly_total,
        "per_account_monthly_avg": per_account_monthly_avg,
        "captured_months_count":  captured_count,
        "active_months":          active_months,
        "total_received_history": round(total_received_all, 2),
    }
    await redis.set(_forecast_cache_key, json.dumps(result), ex=3600)
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


@app.get("/api/dividends/account-stats")
async def div_account_stats(token: str = ""):
    """Return per-account totals from dividend_history for the 18-month window."""
    check_token(token)
    await _div_ensure_tables()
    if not DB_URL:
        return {"accounts": [], "months_elapsed": 0}
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    cutoff = today - _td(days=548)
    cutoff_month_start = _date(cutoff.year, cutoff.month, 1)
    current_month_start = _date(today.year, today.month, 1)
    months_elapsed = max(1, (current_month_start.year - cutoff_month_start.year) * 12 +
                            (current_month_start.month - cutoff_month_start.month))
    current_month_key = today.strftime("%Y-%m")
    pool = await _get_db_pool()
    rows = await pool.fetch("""
        SELECT account_label,
               ticker,
               COUNT(*)::int                           AS payment_count,
               SUM(total_received)::float              AS total_received,
               SUM(qty)::float / NULLIF(COUNT(*),0)    AS avg_qty,
               MAX(total_received/NULLIF(qty,0))::float AS last_aps,
               MIN(pay_date)::text                     AS first_pay,
               MAX(pay_date)::text                     AS last_pay
        FROM dividend_history
        WHERE pay_date >= $1
          AND to_char(pay_date, 'YYYY-MM') < $2
        GROUP BY account_label, ticker
        ORDER BY account_label, total_received DESC
    """, cutoff, current_month_key)
    accounts: dict[str, dict] = {}
    for r in rows:
        lbl = r["account_label"]
        if lbl not in accounts:
            accounts[lbl] = {"label": lbl, "total_received": 0, "payment_count": 0,
                             "monthly_avg": 0, "tickers": []}
        total = float(r["total_received"] or 0)
        accounts[lbl]["total_received"] = round(accounts[lbl]["total_received"] + total, 2)
        accounts[lbl]["payment_count"] += int(r["payment_count"] or 0)
        accounts[lbl]["tickers"].append({
            "ticker": r["ticker"],
            "payment_count": int(r["payment_count"] or 0),
            "total_received": round(total, 2),
            "avg_qty": round(float(r["avg_qty"] or 0), 2),
            "monthly_avg": round(total / months_elapsed, 2),
            "first_pay": r["first_pay"],
            "last_pay": r["last_pay"],
        })
    for a in accounts.values():
        a["monthly_avg"] = round(a["total_received"] / months_elapsed, 2)
    return {"accounts": list(accounts.values()), "months_elapsed": months_elapsed, "cutoff": str(cutoff)}


@app.post("/api/dividends/refresh")
async def div_refresh(token: str = ""):
    check_token(token)
    redis = await get_redis()
    await redis.delete(f"dividend:forecast:{APP_VERSION}:cache", "dividend:holdings:cache")
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
    Fetch 18 months of actual dividend payment history from Polygon (Massive MCP) for every
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
    from datetime import date as _date, timedelta as _td

    cutoff_str = (_date.today() - _td(days=548)).isoformat()

    async def _fetch_history_polygon(ticker: str) -> list[tuple]:
        """Return list of (pay_date_str, amount_per_share) for last 18 months."""
        try:
            from shared.data_client import DataClient
            divs = await DataClient().dividends(ticker)
            results = []
            for d in (divs or []):
                pay = d.get("pay_date") or d.get("ex_date")
                amt = d.get("cash_amount")
                if pay and amt and str(pay) >= cutoff_str:
                    results.append((str(pay)[:10], float(amt)))
            return results
        except Exception as e:
            log.warning("div_backfill.polygon_failed", ticker=ticker, error=str(e))
            return []

    sem = _asyncio.Semaphore(5)

    async def _fetch_one(ticker):
        async with sem:
            return ticker, await _fetch_history_polygon(ticker)

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


def _divchannel_history_sync(ticker: str) -> list[dict]:
    """
    Fetch ex-dividend history for a ticker from dividendchannel.com (tickertech.net backend).
    Returns list of {"date": "YYYY-MM-DD", "amount": float} sorted descending (most recent first).
    Requires the Referer header to unlock the JS-rendered data.
    """
    import re
    import urllib.request
    url = (
        "https://www.tickertech.net/bnkinvest/cgi/"
        f"?n=2&ticker={ticker}&js=on&head=1&a=historical&w=dividends2&noform=1&footer=off"
    )
    try:
        req = urllib.request.Request(url, headers={"Referer": "https://www.dividendchannel.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("divchannel.fetch_failed", ticker=ticker, error=str(exc))
        return []
    # Dates are in center-aligned cells, amounts in right-aligned cells
    dates   = re.findall(r'document\.write\(\'<td align="center"[^\']*>(\d{2}/\d{2}/\d{2})', body)
    amounts = re.findall(r'document\.write\(\'<td align="right"[^\']*>(\d+\.\d+)', body)
    out = []
    for d_str, amt_str in zip(dates, amounts):
        try:
            m, dy, y2 = d_str.split("/")
            out.append({"date": f"{2000 + int(y2)}-{m}-{dy}", "amount": float(amt_str)})
        except Exception:
            pass
    return sorted(out, key=lambda x: x["date"], reverse=True)


@app.get("/api/dividends/upcoming")
async def div_upcoming(token: str = "", days: int = 7):
    """
    Ex-dividend events for held tickers within the next N days (default 7).
    Three-tier lookup:
      1. massive.com  — declared upcoming dates
      2. dividendchannel.com — project forward from recent history (for tickers
         massive.com returned no results for, e.g. weekly payers like HOOW)
      3. dividend_meta DB — yfinance ex_date cache (last-resort dedup guard)
    """
    check_token(token)

    api_key = os.getenv("MASSIVE_API_KEY", "")

    holdings = await div_holdings(token=token)
    ticker_qty: dict[str, float] = {}
    for acct in holdings.get("accounts", []):
        for p in acct.get("positions", []):
            sym = p.get("symbol", "").upper()
            qty = float(p.get("qty") or 0)
            if sym and qty > 0:
                ticker_qty[sym] = ticker_qty.get(sym, 0) + qty

    if not ticker_qty:
        return {"upcoming": [], "as_of": date.today().isoformat()}

    today    = date.today()
    end_date = today + timedelta(days=days)

    import aiohttp as _aiohttp

    # ── Tier 1: massive.com ─────────────────────────────────────────────────
    results: list     = []
    seen: set[tuple]  = set()
    massive_found: set[str] = set()  # tickers that returned ≥1 result

    if api_key:
        mas_sem = asyncio.Semaphore(5)

        async def _fetch_massive(session, sym):
            async with mas_sem:
                try:
                    params = {
                        "ticker": sym,
                        "ex_dividend_date.gte": today.isoformat(),
                        "ex_dividend_date.lte": end_date.isoformat(),
                        "limit": 5,
                        "sort": "ex_dividend_date.asc",
                    }
                    async with session.get(
                        "https://api.massive.com/stocks/v1/dividends",
                        params=params,
                        timeout=_aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            return sym, []
                        data = await resp.json()
                        out = []
                        for r in data.get("results", []):
                            ex_d      = r.get("ex_dividend_date")
                            cash      = float(r.get("cash_amount") or 0)
                            days_left = (date.fromisoformat(ex_d) - today).days if ex_d else None
                            out.append({
                                "ticker":            sym,
                                "ex_date":           ex_d,
                                "pay_date":          r.get("pay_date"),
                                "cash_amount":       cash,
                                "qty":               ticker_qty[sym],
                                "est_total":         round(cash * ticker_qty[sym], 2),
                                "days_until":        days_left,
                                "distribution_type": r.get("distribution_type") or "recurring",
                            })
                        return sym, out
                except Exception as exc:
                    log.warning("div_upcoming.massive_failed", ticker=sym, error=str(exc))
                    return sym, []

        headers = {"Authorization": f"Bearer {api_key}"}
        async with _aiohttp.ClientSession(headers=headers) as session:
            gathered = await asyncio.gather(
                *[_fetch_massive(session, sym) for sym in ticker_qty],
                return_exceptions=True,
            )

        for item in gathered:
            if isinstance(item, Exception):
                continue
            sym, rows = item
            for r in rows:
                massive_found.add(sym)
                key = (sym, r.get("ex_date") or "")
                if key not in seen:
                    seen.add(key)
                    results.append(r)

    # ── Tier 2: dividendchannel.com (project from history) ─────────────────
    # Only for tickers massive.com returned 0 results for. Handles weekly/monthly
    # payers (e.g. HOOW) whose upcoming dates haven't been declared on massive.com.
    no_coverage = [sym for sym in ticker_qty if sym not in massive_found]

    if no_coverage:
        loop   = asyncio.get_event_loop()
        dc_sem = asyncio.Semaphore(3)

        async def _project_divchannel(sym: str) -> list:
            async with dc_sem:
                history = await loop.run_in_executor(None, _divchannel_history_sync, sym)
                if len(history) < 2:
                    return []
                # Average interval from last 4 consecutive gaps
                intervals = [
                    (date.fromisoformat(history[i]["date"]) - date.fromisoformat(history[i + 1]["date"])).days
                    for i in range(min(4, len(history) - 1))
                ]
                avg_interval = round(sum(intervals) / len(intervals))
                next_ex      = date.fromisoformat(history[0]["date"]) + timedelta(days=avg_interval)
                if not (today <= next_ex <= end_date):
                    return []
                avg_amount = sum(h["amount"] for h in history[:4]) / min(4, len(history))
                qty        = ticker_qty[sym]
                return [{
                    "ticker":            sym,
                    "ex_date":           next_ex.isoformat(),
                    "pay_date":          (next_ex + timedelta(days=1)).isoformat(),
                    "cash_amount":       round(avg_amount, 4),
                    "qty":               qty,
                    "est_total":         round(avg_amount * qty, 2),
                    "days_until":        (next_ex - today).days,
                    "distribution_type": "recurring",
                    "source":            "divchannel_projected",
                }]

        dc_gathered = await asyncio.gather(
            *[_project_divchannel(sym) for sym in no_coverage],
            return_exceptions=True,
        )
        for item in dc_gathered:
            if isinstance(item, list):
                for r in item:
                    key = (r["ticker"], r.get("ex_date") or "")
                    if key not in seen:
                        seen.add(key)
                        results.append(r)

    # ── Tier 3: dividend_meta DB (yfinance cache, last-resort guard) ────────
    if DB_URL:
        try:
            pool     = await _get_db_pool()
            db_rows  = await pool.fetch(
                """SELECT ticker, ex_date, pay_date, amount_per_share
                   FROM dividend_meta
                   WHERE ticker = ANY($1)
                     AND ex_date >= $2 AND ex_date <= $3""",
                list(ticker_qty.keys()), today, end_date,
            )
            for row in db_rows:
                sym   = row["ticker"]
                ex_d  = str(row["ex_date"]) if row["ex_date"] else None
                key   = (sym, ex_d or "")
                if key in seen:
                    continue
                seen.add(key)
                cash      = float(row["amount_per_share"] or 0)
                qty       = ticker_qty.get(sym, 0)
                days_left = (row["ex_date"] - today).days if row["ex_date"] else None
                results.append({
                    "ticker":            sym,
                    "ex_date":           ex_d,
                    "pay_date":          str(row["pay_date"]) if row["pay_date"] else None,
                    "cash_amount":       cash,
                    "qty":               qty,
                    "est_total":         round(cash * qty, 2),
                    "days_until":        days_left,
                    "distribution_type": "recurring",
                    "source":            "yfinance_meta",
                })
        except Exception as exc:
            log.warning("div_upcoming.db_supplement_failed", error=str(exc))

    results.sort(key=lambda x: x.get("ex_date") or "")
    return {"upcoming": results, "as_of": today.isoformat()}


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


@app.get("/api/dividends/growth-streaks")
async def div_growth_streaks(token: str = ""):
    """Consecutive dividend growth years per ticker from dividend_history."""
    check_token(token)
    if not DB_URL:
        return {}
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch("""
            SELECT ticker,
                   EXTRACT(YEAR FROM pay_date)::int AS yr,
                   AVG(amount_per_share) AS avg_aps
            FROM dividend_history
            WHERE pay_date IS NOT NULL AND amount_per_share > 0
            GROUP BY ticker, EXTRACT(YEAR FROM pay_date)::int
            ORDER BY ticker, EXTRACT(YEAR FROM pay_date)::int
        """)
        from collections import defaultdict
        by_ticker: dict = defaultdict(list)
        for r in rows:
            by_ticker[r["ticker"]].append((int(r["yr"]), float(r["avg_aps"])))
        result = {}
        for ticker, year_data in by_ticker.items():
            year_data.sort()
            if len(year_data) < 2:
                result[ticker] = {"streak": 0, "years_tracked": len(year_data)}
                continue
            streak = 0
            for i in range(len(year_data) - 1, 0, -1):
                curr_yr, curr_aps = year_data[i]
                prev_yr, prev_aps = year_data[i - 1]
                if curr_yr == prev_yr + 1 and prev_aps > 0 and curr_aps > prev_aps * 1.001:
                    streak += 1
                else:
                    break
            result[ticker] = {"streak": streak, "years_tracked": len(year_data)}
        return result
    except Exception as e:
        log.warning("div.growth_streaks_error", error=str(e))
        return {}


async def _ensure_portfolio_targets_table():
    if not DB_URL:
        return
    pool = await _get_db_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_targets (
            ticker     TEXT        PRIMARY KEY,
            target_pct NUMERIC     NOT NULL CHECK (target_pct >= 0),
            notes      TEXT        DEFAULT '',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


@app.get("/api/dividends/targets")
async def get_portfolio_targets(token: str = ""):
    """User-defined allocation targets for goal portfolio tracking."""
    check_token(token)
    if not DB_URL:
        return []
    await _ensure_portfolio_targets_table()
    pool = await _get_db_pool()
    rows = await pool.fetch(
        "SELECT ticker, target_pct, notes FROM portfolio_targets ORDER BY target_pct DESC"
    )
    return [{"ticker": r["ticker"], "target_pct": float(r["target_pct"]), "notes": r["notes"] or ""} for r in rows]


class PortfolioTargetBody(BaseModel):
    ticker: str
    target_pct: float
    notes: str = ""


@app.post("/api/dividends/targets")
async def upsert_portfolio_target(body: PortfolioTargetBody, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(503, "No DB")
    await _ensure_portfolio_targets_table()
    pool = await _get_db_pool()
    await pool.execute("""
        INSERT INTO portfolio_targets (ticker, target_pct, notes, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (ticker) DO UPDATE SET
            target_pct = EXCLUDED.target_pct, notes = EXCLUDED.notes, updated_at = NOW()
    """, body.ticker.upper().strip(), max(0.0, body.target_pct), body.notes.strip())
    return {"ok": True}


@app.delete("/api/dividends/targets/{ticker}")
async def delete_portfolio_target(ticker: str, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(503, "No DB")
    await _ensure_portfolio_targets_table()
    pool = await _get_db_pool()
    await pool.execute("DELETE FROM portfolio_targets WHERE ticker = $1", ticker.upper())
    return {"ok": True}


async def _detect_market_regime() -> dict:
    """
    Classify current SPX market regime (bull/bear) via GradientBoosting on 6yr weekly SPX.
    Features: 4w/13w/26w/52w returns, 12w realized vol, RSI(14), cumulative return level.
    Label: bull if SPX > +5% over next 13 weeks.  Cached 6h in Redis.
    """
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get("market:ml_regime:latest")
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import accuracy_score
    except ImportError:
        return {"regime": "unknown", "confidence": 0.5, "error": "sklearn unavailable"}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"regime": "unknown", "confidence": 0.5, "error": "no api key"}

    try:
        import aiohttp as _ah

        # Fetch daily SPY bars (5yr) and aggregate to weekly closes
        from_dt = (date.today() - timedelta(days=365 * 5 + 10)).isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day"
            f"/{from_dt}/{date.today().isoformat()}?adjusted=true&sort=asc&limit=1500&apiKey={api_key}"
        )
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return {"regime": "unknown", "confidence": 0.5, "error": f"polygon {resp.status}"}
                data = await resp.json()

        daily_bars = data.get("results") or []
        if len(daily_bars) < 200:
            return {"regime": "unknown", "confidence": 0.5, "error": "insufficient data"}

        # Aggregate to weekly (ISO week boundary — last bar in each week = weekly close)
        from collections import OrderedDict as _OD
        week_map: dict = _OD()
        for b in daily_bars:
            ts_ms   = b.get("t", 0)
            wk_num  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-W%W")
            week_map[wk_num] = b   # last bar of the week wins

        bars = list(week_map.values())
        if len(bars) < 80:
            return {"regime": "unknown", "confidence": 0.5, "error": "insufficient weekly bars"}

        closes = np.array([float(b["c"]) for b in bars])
        n      = len(closes)

        def _wret(k):
            r = np.full(n, np.nan)
            r[k:] = closes[k:] / closes[:-k] - 1
            return r

        ret_4  = _wret(4)
        ret_13 = _wret(13)
        ret_26 = _wret(26)
        ret_52 = _wret(52)

        # 12-week realized volatility
        vol_12 = np.full(n, np.nan)
        for i in range(13, n):
            wk = closes[i - 12:i] / closes[i - 13:i - 1] - 1
            vol_12[i] = float(np.std(wk))

        # Weekly RSI(14) via Wilder smoothing
        delta  = np.diff(closes, prepend=closes[0])
        gain   = np.maximum(delta, 0.0)
        loss_v = np.maximum(-delta, 0.0)
        rsi    = np.full(n, np.nan)
        for i in range(14, n):
            ag = float(np.mean(gain[i - 14:i]))
            al = float(np.mean(loss_v[i - 14:i]))
            rsi[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

        cum_ret = closes / closes[0] - 1  # level feature

        # Label: 1=bull if +5% over next 13 weeks
        fwd = np.full(n, np.nan)
        fwd[:-13] = closes[13:] / closes[:-13] - 1
        labels = (fwd > 0.05).astype(int)

        X = np.column_stack([ret_4, ret_13, ret_26, ret_52, vol_12, rsi, cum_ret])

        has_label = ~np.isnan(fwd)
        has_label[-13:] = False
        valid = ~np.any(np.isnan(X), axis=1) & has_label

        X_cl = X[valid]
        y_cl = labels[valid]

        if len(X_cl) < 60:
            return {"regime": "unknown", "confidence": 0.5, "error": "too few samples"}

        n_tr   = int(len(X_cl) * 0.8)
        scaler = StandardScaler()
        X_tr_s  = scaler.fit_transform(X_cl[:n_tr])
        X_val_s = scaler.transform(X_cl[n_tr:])

        clf = GradientBoostingClassifier(n_estimators=80, learning_rate=0.05,
                                          max_depth=3, subsample=0.8, random_state=42)
        clf.fit(X_tr_s, y_cl[:n_tr])
        val_acc = float(accuracy_score(y_cl[n_tr:], clf.predict(X_val_s)))

        # Predict on most recent complete bar
        last_x = X[-1:].copy()
        if np.any(np.isnan(last_x)):
            for idx in range(n - 1, 0, -1):
                last_x = X[idx:idx + 1]
                if not np.any(np.isnan(last_x)):
                    break
        p_bull     = float(clf.predict_proba(scaler.transform(last_x))[0, 1])
        regime     = "bull" if p_bull >= 0.5 else "bear"
        confidence = p_bull if regime == "bull" else 1.0 - p_bull

        # History: last 52 weeks classified
        valid_idxs = np.where(valid)[0]
        history    = []
        for vi in valid_idxs[-52:]:
            xh = X[vi:vi + 1]
            if np.any(np.isnan(xh)):
                continue
            pb    = float(clf.predict_proba(scaler.transform(xh))[0, 1])
            ts    = bars[vi].get("t", 0)
            dt_s  = date.fromtimestamp(ts / 1000).isoformat() if ts else ""
            history.append({"date": dt_s, "p_bull": round(pb, 3),
                             "regime": "bull" if pb >= 0.5 else "bear"})

        result = {
            "regime":       regime,
            "confidence":   round(confidence, 3),
            "p_bull":       round(p_bull, 3),
            "val_accuracy": round(val_acc, 3),
            "history":      history,
            "features": {
                "ret_4w":  round(float(ret_4[-1]),  4),
                "ret_13w": round(float(ret_13[-1]), 4),
                "ret_26w": round(float(ret_26[-1]), 4),
                "ret_52w": round(float(ret_52[-1]), 4),
                "vol_12w": round(float(vol_12[-1]), 4),
                "rsi_14":  round(float(rsi[-1]),    2),
            },
        }
        if _r:
            await _r.setex("market:ml_regime:latest", 21600, json.dumps(result))
        return result

    except Exception as e:
        log.warning("market_regime.error", error=str(e))
        return {"regime": "unknown", "confidence": 0.5, "error": str(e)}


@app.get("/api/retirement/glidepath")
async def retirement_glidepath(
    current_age: int = 35,
    retirement_age: int = 65,
    style: str = "moderate",
    token: str = "",
):
    """
    Age-based equity/bond glidepath from current_age through retirement_age + 20 years.

    style:
      aggressive   — equity = 120 − age (min 15%, max 95%)
      moderate     — equity = 110 − age (min 15%, max 90%)
      conservative — equity = 100 − age (min 15%, max 80%)
    """
    check_token(token)
    current_age    = max(18, min(80, int(current_age)))
    retirement_age = max(current_age + 1, min(90, int(retirement_age)))
    style          = style.lower() if style.lower() in ("aggressive", "moderate", "conservative") else "moderate"
    base           = {"aggressive": 120, "moderate": 110, "conservative": 100}[style]
    max_eq         = {"aggressive": 95,  "moderate": 90,  "conservative": 80}[style]
    end_age        = min(100, retirement_age + 20)

    path = []
    for age in range(current_age, end_age + 1):
        equity_pct = max(15, min(max_eq, base - age))
        path.append({
            "age":        age,
            "year":       age - current_age,
            "equity_pct": equity_pct,
            "bond_pct":   100 - equity_pct,
            "retired":    age >= retirement_age,
        })

    return {
        "current_age":    current_age,
        "retirement_age": retirement_age,
        "style":          style,
        "path":           path,
    }


@app.get("/api/dividends/monte-carlo")
async def div_monte_carlo(
    token: str = "",
    years: int = 10,
    threshold: float = 0.0,
    regime_aware: bool = False,
):
    """
    Income simulation — 1,000 paths, returns p5/p25/p50/p75/p95 per year
    plus shortfall / ruin-probability metrics (Item 1 — Target Date Fund pattern).

    Parameters
    ----------
    years        : projection horizon (default 10, max 30)
    threshold    : annual income floor below which a path counts as "shortfall"
    regime_aware : when True, adjust mu/sigma based on ML market-regime classifier
    """
    check_token(token)
    if not DB_URL:
        return {"error": "no db"}
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy not available"}
    years = max(1, min(int(years), 30))
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch("""
            SELECT ticker,
                   EXTRACT(YEAR FROM pay_date)::int AS yr,
                   SUM(total_received)              AS ann_income
            FROM dividend_history
            WHERE pay_date IS NOT NULL AND total_received > 0
            GROUP BY ticker, EXTRACT(YEAR FROM pay_date)::int
            ORDER BY ticker, yr
        """)
        from collections import defaultdict
        by_ticker: dict = defaultdict(list)
        for r in rows:
            by_ticker[r["ticker"]].append((int(r["yr"]), float(r["ann_income"] or 0)))

        growth_rates: list = []
        current_annual = 0.0
        for _t, year_data in by_ticker.items():
            year_data.sort()
            amounts = [a for _, a in year_data]
            if len(amounts) >= 2:
                for i in range(1, len(amounts)):
                    if amounts[i - 1] > 0:
                        growth_rates.append(amounts[i] / amounts[i - 1] - 1)
            if amounts:
                current_annual += amounts[-1]

        if current_annual <= 0:
            return {"error": "no income history"}

        arr   = np.array(growth_rates) if growth_rates else np.array([0.05])
        mu    = float(np.mean(arr))
        sigma = float(np.std(arr)) if len(arr) > 1 else 0.12
        mu    = max(-0.20, min(0.25, mu))
        sigma = max(0.01, min(0.40, sigma))

        # ── Regime-aware adjustment ───────────────────────────────────────────
        regime_info: dict = {}
        if regime_aware:
            regime_info = await _detect_market_regime()
            rg = regime_info.get("regime", "unknown")
            if rg == "bull":
                mu    = 0.5 * mu + 0.5 * 0.10   # blend toward bull baseline
                sigma = sigma * 0.75             # lower volatility in bull
            elif rg == "bear":
                mu    = 0.5 * mu + 0.5 * (-0.12)  # blend toward bear baseline
                sigma = sigma * 1.40               # higher volatility in bear
            mu    = max(-0.30, min(0.35, mu))
            sigma = max(0.01, min(0.60, sigma))

        N_SIMS = 1_000
        rng    = np.random.default_rng(42)
        g      = rng.normal(mu, sigma, size=(N_SIMS, years))
        cum    = np.cumprod(1.0 + g, axis=1)
        sims   = current_annual * cum          # shape (N_SIMS, years)

        # ── Shortfall / ruin probability (Target Date Fund pattern) ───────────
        floor = float(threshold)

        # Year-by-year shortfall probability
        shortfall_by_year = [
            round(float(np.mean(sims[:, yr] < floor)) * 100, 2)
            for yr in range(years)
        ]

        # Ever-shortfall: path hits floor at least once across the horizon
        ever_shortfall_pct = round(
            float(np.mean(np.any(sims < floor, axis=1))) * 100, 2
        )

        # Terminal ruin (shortfall in the final year)
        terminal_shortfall_pct = round(
            float(np.mean(sims[:, -1] < floor)) * 100, 2
        )

        # Median years until first shortfall (only paths that ever shortfall)
        shortfall_year_idx = np.argmax(sims < floor, axis=1)  # first year below floor
        paths_that_fail    = shortfall_year_idx[np.any(sims < floor, axis=1)]
        median_years_to_shortfall = (
            round(float(np.median(paths_that_fail + 1)), 1)
            if len(paths_that_fail) > 0 else None
        )

        # Safe withdrawal rate: highest constant annual draw that keeps
        # shortfall < 5 % of paths (binary search over integer amounts)
        if current_annual > 0:
            lo, hi = 0.0, current_annual * 2.0
            for _ in range(20):
                mid  = (lo + hi) / 2.0
                fail = float(np.mean(np.any(sims < mid, axis=1)))
                if fail < 0.05:
                    lo = mid
                else:
                    hi = mid
            safe_floor_95 = round(lo, 2)
        else:
            safe_floor_95 = 0.0

        result: dict = {
            "years":          list(range(1, years + 1)),
            "current_annual": round(current_annual, 2),
            "mu":             round(mu, 4),
            "sigma":          round(sigma, 4),
            "n_sims":         N_SIMS,
            "floor":          floor,
            "regime_aware":   regime_aware,
            "regime":         regime_info.get("regime", "none") if regime_aware else "none",
            "regime_confidence": regime_info.get("confidence", 0.0) if regime_aware else 0.0,
            # percentile bands
            **{f"p{pct}": [round(float(v), 2) for v in np.percentile(sims, pct, axis=0)]
               for pct in [5, 25, 50, 75, 95]},
            # shortfall metrics
            "shortfall_by_year":       shortfall_by_year,
            "ever_shortfall_pct":      ever_shortfall_pct,
            "terminal_shortfall_pct":  terminal_shortfall_pct,
            "median_years_to_shortfall": median_years_to_shortfall,
            "safe_floor_95":           safe_floor_95,
        }
        return result
    except Exception as e:
        log.warning("div.monte_carlo_error", error=str(e))
        return {"error": str(e)}


@app.get("/api/dividends/sustainability")
async def div_sustainability(token: str = "", tickers: str = ""):
    """EPS TTM from Polygon for payout-ratio sustainability scoring. Cached 6h."""
    check_token(token)
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else []
    if not ticker_list:
        return {}
    api_key = os.environ.get("MASSIVE_API_KEY", "")
    if not api_key:
        return {}

    cache_key = "div:sustainability:" + ",".join(sorted(ticker_list))
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    sem = asyncio.Semaphore(5)

    async def _fetch_eps(sym: str) -> tuple:
        async with sem:
            try:
                import aiohttp as _aiohttp
                url = (
                    f"https://api.polygon.io/vX/reference/financials"
                    f"?ticker={sym}&timeframe=quarterly&limit=4"
                    f"&sort=period_of_report_date&order=desc&apiKey={api_key}"
                )
                async with _aiohttp.ClientSession() as sess:
                    async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status != 200:
                            return sym, None
                        data = await resp.json()
                results = (data.get("results") or [])[:4]
                eps_ttm = sum(
                    float(
                        (r.get("financials", {})
                         .get("income_statement", {})
                         .get("basic_earnings_per_share", {})
                         .get("value") or 0)
                    )
                    for r in results
                )
                return sym, round(eps_ttm, 4)
            except Exception:
                return sym, None

    pairs = await asyncio.gather(*[_fetch_eps(sym) for sym in ticker_list])
    result = {sym: {"eps_ttm": eps} for sym, eps in pairs}

    try:
        if _r:
            await _r.setex(cache_key, 21600, json.dumps(result))
    except Exception:
        pass
    return result


@app.get("/api/dividends/quality-scores")
async def div_quality_scores(token: str = ""):
    """Per-ticker dividend quality: CAGR, EWM growth, consistency, cut count. Cached 6h."""
    check_token(token)
    if not DB_URL:
        return {}
    cache_key = "div:quality_scores:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch("""
            SELECT ticker,
                   EXTRACT(YEAR FROM pay_date)::int AS yr,
                   SUM(total_received) AS ann_income
            FROM dividend_history
            WHERE pay_date IS NOT NULL AND total_received > 0
            GROUP BY ticker, EXTRACT(YEAR FROM pay_date)::int
            ORDER BY ticker, yr
        """)
        from collections import defaultdict
        by_ticker: dict = defaultdict(list)
        for r in rows:
            by_ticker[r["ticker"]].append((int(r["yr"]), float(r["ann_income"] or 0)))
        result = {}
        for ticker, year_data in by_ticker.items():
            year_data.sort()
            n = len(year_data)
            amounts = [a for _, a in year_data]
            cagr = None
            if n >= 2 and amounts[0] > 0:
                cagr = round((amounts[-1] / amounts[0]) ** (1 / (n - 1)) - 1, 4)
            growth_rates = [
                amounts[i] / amounts[i - 1] - 1
                for i in range(1, n) if amounts[i - 1] > 0
            ]
            ewm_growth = None
            if growth_rates:
                com = 0.5
                alpha = 1 / (1 + com)
                w_sum, v_sum = 0.0, 0.0
                for i, g in enumerate(growth_rates):
                    w = (1 - alpha) ** (len(growth_rates) - 1 - i)
                    w_sum += w
                    v_sum += w * g
                ewm_growth = round(v_sum / w_sum, 4) if w_sum else None
            first_yr, last_yr = year_data[0][0], year_data[-1][0]
            span = max(1, last_yr - first_yr + 1)
            consistency = round(n / span, 3)
            cuts = sum(1 for i in range(1, n) if amounts[i] < amounts[i - 1] * 0.99)
            result[ticker] = {
                "cagr":          cagr,
                "ewm_growth":    ewm_growth,
                "consistency":   consistency,
                "cuts":          cuts,
                "years_tracked": n,
                "current_annual": round(amounts[-1], 2),
            }
        try:
            if _r:
                await _r.setex(cache_key, 21600, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning("div.quality_scores_error", error=str(e))
        return {}


@app.get("/api/dividends/timing")
async def div_timing(token: str = ""):
    """Pre/post ex-dividend price patterns for held tickers via Polygon OHLCV. Cached 24h."""
    check_token(token)
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {}
    cache_key = "div:timing:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        holdings = await div_holdings(token=token)
        tickers = list({
            p.get("symbol", "").upper()
            for acct in holdings.get("accounts", [])
            for p in acct.get("positions", [])
            if p.get("is_dividend_payer")
        })[:20]
        if not tickers:
            return {}
        import aiohttp as _aiohttp
        sem = asyncio.Semaphore(4)

        async def _analyze(sym: str) -> tuple:
            async with sem:
                try:
                    async with _aiohttp.ClientSession() as sess:
                        url = (
                            f"https://api.polygon.io/v3/reference/dividends"
                            f"?ticker={sym}&limit=12&sort=ex_dividend_date&order=desc&apiKey={api_key}"
                        )
                        async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                            if resp.status != 200:
                                return sym, None
                            div_data = await resp.json()
                    ex_dates = [
                        r["ex_dividend_date"]
                        for r in (div_data.get("results") or [])
                        if r.get("ex_dividend_date")
                    ][:8]
                    if not ex_dates:
                        return sym, None
                    newest_ex = date.fromisoformat(ex_dates[0])
                    oldest_ex = date.fromisoformat(ex_dates[-1])
                    from_str = (oldest_ex - timedelta(days=40)).isoformat()
                    to_str   = (newest_ex + timedelta(days=35)).isoformat()
                    async with _aiohttp.ClientSession() as sess:
                        url = (
                            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                            f"/{from_str}/{to_str}?adjusted=true&sort=asc&limit=500&apiKey={api_key}"
                        )
                        async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                return sym, None
                            ohlcv = await resp.json()
                    bars = ohlcv.get("results") or []
                    if not bars:
                        return sym, None
                    price_map = {
                        date.fromtimestamp(b["t"] / 1000).isoformat(): b["c"]
                        for b in bars
                    }

                    def _nearest(target) -> float | None:
                        for d in range(0, 5):
                            for cand in [target + timedelta(days=d), target - timedelta(days=d)]:
                                v = price_map.get(cand.isoformat())
                                if v:
                                    return v
                        return None

                    pre_drifts, post_drifts = [], []
                    for ex_str in ex_dates:
                        ex_d = date.fromisoformat(ex_str)
                        ex_p   = _nearest(ex_d)
                        pre_p  = _nearest(ex_d - timedelta(days=21))
                        post_p = _nearest(ex_d + timedelta(days=14))
                        if ex_p and pre_p and pre_p > 0:
                            pre_drifts.append((ex_p - pre_p) / pre_p)
                        if post_p and ex_p and ex_p > 0:
                            post_drifts.append((post_p - ex_p) / ex_p)
                    if not pre_drifts:
                        return sym, None
                    return sym, {
                        "avg_pre_drift":  round(sum(pre_drifts) / len(pre_drifts), 4),
                        "avg_post_drift": round(sum(post_drifts) / len(post_drifts), 4) if post_drifts else None,
                        "ex_dates_used":  len(pre_drifts),
                        "last_ex_date":   ex_dates[0],
                    }
                except Exception as exc:
                    log.debug("div_timing.ticker_error", sym=sym, error=str(exc))
                    return sym, None

        pairs = await asyncio.gather(*[_analyze(sym) for sym in tickers])
        result = {sym: data for sym, data in pairs if data}
        try:
            if _r:
                await _r.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning("div.timing_error", error=str(e))
        return {}


@app.get("/api/dividends/drip-historical")
async def div_drip_historical(token: str = ""):
    """Historical DRIP: actual payments × real Polygon close prices → lot accumulation. Cached 4h."""
    check_token(token)
    if not DB_URL:
        return {}
    api_key = os.getenv("MASSIVE_API_KEY", "")
    cache_key = "div:drip_hist:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch("""
            SELECT ticker, pay_date, amount_per_share, qty, total_received
            FROM dividend_history
            WHERE pay_date IS NOT NULL AND total_received > 0 AND qty > 0
            ORDER BY ticker, pay_date ASC
        """)
        from collections import defaultdict
        by_ticker: dict = defaultdict(list)
        for r in rows:
            by_ticker[r["ticker"]].append({
                "pay_date": r["pay_date"].isoformat(),
                "aps":      float(r["amount_per_share"] or 0),
                "qty":      float(r["qty"]),
                "received": float(r["total_received"] or 0),
            })
        if not by_ticker:
            return {"tickers": {}, "total_drip_value": 0}
        holdings = await div_holdings(token=token)
        current_positions: dict = {}
        for acct in holdings.get("accounts", []):
            for p in acct.get("positions", []):
                sym = p.get("symbol", "").upper()
                if sym:
                    if sym not in current_positions:
                        current_positions[sym] = {"qty": 0, "cost": 0, "price": 0}
                    current_positions[sym]["qty"]   += float(p.get("qty") or 0)
                    current_positions[sym]["cost"]  += float(p.get("cost_basis") or 0)
                    current_positions[sym]["price"]  = float(p.get("current_price") or 0)
        import aiohttp as _aiohttp
        sem = asyncio.Semaphore(4)

        async def _fetch_ticker_prices(sym: str, payments: list) -> tuple:
            async with sem:
                try:
                    dates = [p["pay_date"] for p in payments]
                    from_str = (
                        date.fromisoformat(min(dates)) - timedelta(days=3)
                    ).isoformat()
                    to_str = (
                        date.fromisoformat(max(dates)) + timedelta(days=3)
                    ).isoformat()
                    url = (
                        f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                        f"/{from_str}/{to_str}?adjusted=true&sort=asc&limit=500&apiKey={api_key}"
                    )
                    async with _aiohttp.ClientSession() as sess:
                        async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                return sym, {}
                            data = await resp.json()
                    return sym, {
                        date.fromtimestamp(b["t"] / 1000).isoformat(): b["c"]
                        for b in (data.get("results") or [])
                    }
                except Exception:
                    return sym, {}

        if api_key:
            price_results = await asyncio.gather(*[
                _fetch_ticker_prices(sym, pays)
                for sym, pays in by_ticker.items()
            ])
            price_maps = dict(price_results)
        else:
            price_maps = {}

        def _nearest_price(pmap: dict, target_str: str) -> float | None:
            target = date.fromisoformat(target_str)
            for d in range(0, 5):
                for cand in [target + timedelta(days=d), target - timedelta(days=d)]:
                    v = pmap.get(cand.isoformat())
                    if v:
                        return v
            return None

        ticker_results = {}
        total_drip_value = 0.0
        for ticker, payments in by_ticker.items():
            pmap     = price_maps.get(ticker, {})
            pos      = current_positions.get(ticker, {})
            cur_price = pos.get("price", 0)
            drip_shares = 0.0
            drip_cost   = 0.0
            total_cash  = 0.0
            detail = []
            for pay in payments:
                received = pay["received"]
                total_cash += received
                price = _nearest_price(pmap, pay["pay_date"])
                if price and price > 0:
                    new_shares  = received / price
                    drip_shares += new_shares
                    drip_cost   += received
                    detail.append({
                        "date":          pay["pay_date"],
                        "received":      round(received, 2),
                        "price":         round(price, 2),
                        "shares_bought": round(new_shares, 4),
                    })
            drip_value = round(drip_shares * cur_price, 2) if cur_price else 0
            total_drip_value += drip_value
            ticker_results[ticker] = {
                "total_dividends_received": round(total_cash, 2),
                "drip_shares":  round(drip_shares, 4),
                "drip_cost":    round(drip_cost, 2),
                "drip_value":   drip_value,
                "current_price": round(cur_price, 2),
                "payments":     len(payments),
                "detail":       detail[-6:],
            }
        result = {
            "tickers": ticker_results,
            "total_drip_value": round(total_drip_value, 2),
            "mode": "historical" if api_key else "cash_only",
        }
        try:
            if _r:
                await _r.setex(cache_key, 14400, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning("div.drip_historical_error", error=str(e))
        return {"error": str(e)}


@app.get("/api/dividends/calendar")
async def div_calendar(token: str = ""):
    """12-month dividend calendar: projected pay/ex-dates per ticker. Cached 6h."""
    check_token(token)
    if not DB_URL:
        return {"months": {}, "as_of": date.today().isoformat()}
    cache_key = "div:calendar:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        holdings = await div_holdings(token=token)
        ticker_qty: dict = {}
        for acct in holdings.get("accounts", []):
            for p in acct.get("positions", []):
                sym = p.get("symbol", "").upper()
                qty = float(p.get("qty") or 0)
                if sym and qty > 0 and p.get("is_dividend_payer"):
                    ticker_qty[sym] = ticker_qty.get(sym, 0) + qty
        if not ticker_qty:
            return {"months": {}, "as_of": date.today().isoformat()}
        meta = await _div_get_meta(list(ticker_qty.keys()))
        today = date.today()
        freq_days = {"annual": 365, "semi-annual": 183, "quarterly": 91, "monthly": 30}
        months: dict = {}
        for ticker, qty in ticker_qty.items():
            m = meta.get(ticker, {})
            if not m:
                continue
            freq_str = (m.get("frequency") or "quarterly").lower()
            interval = freq_days.get(freq_str, 91)
            payments_per_year = round(365 / interval)
            aps = float(m.get("annual_dividend") or 0) / payments_per_year if m.get("annual_dividend") else 0
            pay_date = _div_parse_date(m.get("pay_date"))
            ex_date  = _div_parse_date(m.get("ex_date"))
            if not pay_date:
                continue
            proj_pay = pay_date
            proj_ex  = ex_date
            events_added = 0
            while events_added < 14:
                if proj_pay < today - timedelta(days=15):
                    proj_pay += timedelta(days=interval)
                    if proj_ex:
                        proj_ex += timedelta(days=interval)
                    continue
                if proj_pay > today + timedelta(days=375):
                    break
                mk = proj_pay.strftime("%Y-%m")
                if mk not in months:
                    months[mk] = []
                months[mk].append({
                    "ticker":    ticker,
                    "qty":       qty,
                    "aps":       round(aps, 4),
                    "est_total": round(aps * qty, 2),
                    "pay_date":  proj_pay.isoformat(),
                    "ex_date":   proj_ex.isoformat() if proj_ex else None,
                    "freq":      freq_str,
                })
                proj_pay += timedelta(days=interval)
                if proj_ex:
                    proj_ex += timedelta(days=interval)
                events_added += 1
        result = {"months": dict(sorted(months.items())), "as_of": today.isoformat()}
        try:
            if _r:
                await _r.setex(cache_key, 21600, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning("div.calendar_error", error=str(e))
        return {"months": {}, "as_of": date.today().isoformat()}


@app.get("/api/dividends/seasonality")
async def div_seasonality(token: str = ""):
    """Monthly income seasonality: per-month totals across years → mean/p25/p50/p75. Cached 6h."""
    check_token(token)
    if not DB_URL:
        return {}
    cache_key = "div:seasonality:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch("""
            SELECT EXTRACT(YEAR  FROM pay_date)::int AS yr,
                   EXTRACT(MONTH FROM pay_date)::int AS mo,
                   SUM(total_received)               AS total
            FROM dividend_history
            WHERE pay_date IS NOT NULL AND total_received > 0
            GROUP BY yr, mo
            ORDER BY yr, mo
        """)
        from collections import defaultdict
        by_month: dict = defaultdict(list)
        for r in rows:
            by_month[int(r["mo"])].append(float(r["total"] or 0))

        def _pct(arr, p):
            idx = (len(arr) - 1) * p / 100
            lo, hi = int(idx), min(int(idx) + 1, len(arr) - 1)
            return arr[lo] + (arr[hi] - arr[lo]) * (idx - lo)

        months_data = {}
        for mo in range(1, 13):
            vals = by_month.get(mo, [])
            if not vals:
                months_data[mo] = {"mean": 0, "p25": 0, "p50": 0, "p75": 0, "min": 0, "max": 0, "n": 0}
                continue
            sv = sorted(vals)
            months_data[mo] = {
                "mean": round(sum(sv) / len(sv), 2),
                "p25":  round(_pct(sv, 25), 2),
                "p50":  round(_pct(sv, 50), 2),
                "p75":  round(_pct(sv, 75), 2),
                "min":  round(sv[0], 2),
                "max":  round(sv[-1], 2),
                "n":    len(sv),
            }
        result = {"months": months_data}
        try:
            if _r:
                await _r.setex(cache_key, 21600, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning("div.seasonality_error", error=str(e))
        return {}


@app.get("/api/dividends/screener")
async def div_screener(token: str = ""):
    """Multi-factor dividend quality screener: percentile-ranked scores for held tickers. Cached 6h."""
    check_token(token)
    if not DB_URL:
        return []
    cache_key = "div:screener:v1"
    _r = None
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        holdings = await div_holdings(token=token)
        ticker_data: dict = {}
        for acct in holdings.get("accounts", []):
            for p in acct.get("positions", []):
                sym = p.get("symbol", "").upper()
                if not sym or not p.get("is_dividend_payer"):
                    continue
                if sym not in ticker_data:
                    ticker_data[sym] = {
                        "yield":      float(p.get("div_yield") or 0),
                        "ann_income": float(p.get("annual_income") or 0),
                        "qty":        0,
                        "mkt_value":  0,
                    }
                ticker_data[sym]["qty"]      += float(p.get("qty") or 0)
                ticker_data[sym]["mkt_value"] += float(p.get("market_value") or 0)
        if not ticker_data:
            return []
        quality, sustain = await asyncio.gather(
            div_quality_scores(token=token),
            div_sustainability(token=token, tickers=",".join(ticker_data.keys())),
        )
        rows = []
        for sym, td in ticker_data.items():
            qs  = quality.get(sym, {})
            sus = sustain.get(sym, {})
            eps = float(sus.get("eps_ttm") or 0)
            ann_div_per_share = td["ann_income"] / td["qty"] if td["qty"] > 0 else 0
            payout = round(ann_div_per_share / eps, 3) if eps > 0 else None
            rows.append({
                "ticker":        sym,
                "yield":         td["yield"],
                "cagr":          qs.get("cagr"),
                "ewm_growth":    qs.get("ewm_growth"),
                "consistency":   qs.get("consistency"),
                "cuts":          qs.get("cuts", 0),
                "years_tracked": qs.get("years_tracked", 0),
                "payout_ratio":  payout,
                "eps_ttm":       round(eps, 2) if eps else None,
                "ann_income":    td["ann_income"],
                "mkt_value":     td["mkt_value"],
            })

        def _rank(vals, higher_is_better=True):
            valid = sorted(v for v in vals if v is not None)
            if not valid:
                return [None] * len(vals)
            out = []
            for v in vals:
                if v is None:
                    out.append(None)
                    continue
                idx = valid.index(v)
                pct = idx / max(1, len(valid) - 1) * 100
                out.append(round(pct if higher_is_better else 100 - pct, 1))
            return out

        yield_rank   = _rank([r["yield"] for r in rows], True)
        cagr_rank    = _rank([r["cagr"] for r in rows], True)
        consist_rank = _rank([r["consistency"] for r in rows], True)
        cuts_rank    = _rank([r["cuts"] for r in rows], False)
        payout_rank  = _rank([r["payout_ratio"] for r in rows], False)
        for i, row in enumerate(rows):
            parts = [s for s in [yield_rank[i], cagr_rank[i], consist_rank[i], cuts_rank[i], payout_rank[i]] if s is not None]
            row.update({
                "yield_rank":   yield_rank[i],
                "cagr_rank":    cagr_rank[i],
                "consist_rank": consist_rank[i],
                "cuts_rank":    cuts_rank[i],
                "payout_rank":  payout_rank[i],
                "composite":    round(sum(parts) / len(parts), 1) if parts else None,
            })
        rows.sort(key=lambda r: -(r["composite"] or 0))
        try:
            if _r:
                await _r.setex(cache_key, 21600, json.dumps(rows))
        except Exception:
            pass
        return rows
    except Exception as e:
        log.warning("div.screener_error", error=str(e))
        return []


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
                  journal,
                  raw::text as raw_text
           FROM option_positions
           WHERE status = $1
           ORDER BY underlying, account_label""",
        status,
    )
    # Fetch current underlying prices — Redis sentiment cache first, Yahoo Finance MCP fallback
    live_prices: dict[str, float] = {}
    tickers = list({r["underlying"] for r in rows})
    try:
        _redis = await get_redis()
        for ticker in tickers:
            raw_p = await _redis.hget("sentiment:latest", ticker)
            if raw_p:
                d = json.loads(raw_p)
                p = float(d.get("close") or d.get("price") or 0)
                if p:
                    live_prices[ticker] = p
    except Exception:
        pass
    # Fill any missing prices: Redis cache → Polygon.io (Massive, 15-min TTL)
    missing = [t for t in tickers if t not in live_prices]
    if missing:
        try:
            _redis2 = await get_redis()
            still_missing = []
            for sym in missing:
                cached = await _redis2.get(f"yf:price:{sym}")
                if cached:
                    live_prices[sym] = float(cached)
                else:
                    still_missing.append(sym)
            if still_missing:
                # Primary: Polygon.io (MASSIVE_API_KEY) — all tickers in parallel
                _poly_key = os.getenv("MASSIVE_API_KEY", "")
                if _poly_key:
                    import aiohttp as _aiohttp
                    import asyncio as _asyncio
                    from datetime import timedelta
                    _today_str = date.today().isoformat()
                    _from_str  = (date.today() - timedelta(days=5)).isoformat()
                    async def _fetch_poly_price(session, sym):
                        url = (
                            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                            f"/{_from_str}/{_today_str}?adjusted=true&sort=desc&limit=1&apiKey={_poly_key}"
                        )
                        try:
                            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                                if resp.status == 200:
                                    d = await resp.json()
                                    bars = d.get("results") or []
                                    if bars:
                                        return sym, float(bars[0]["c"])
                        except Exception:
                            pass
                        return sym, None
                    async with _aiohttp.ClientSession() as _psess:
                        results = await _asyncio.gather(*[
                            _fetch_poly_price(_psess, sym) for sym in still_missing
                        ])
                    for sym, price in results:
                        if price is not None:
                            live_prices[sym] = price
                            await _redis2.setex(f"yf:price:{sym}", 900, str(price))
                            still_missing.remove(sym)
        except Exception:
            pass

    # Fetch latest predictor signal per underlying ticker
    live_signals: dict[str, dict] = {}
    try:
        _redis = await get_redis()
        sig_entries = await _redis.xrevrange(STREAMS["signals"], "+", "-", count=1000)
        for _eid, _fields in sig_entries:
            def _dec(v): return v.decode() if isinstance(v, bytes) else v
            ticker = _dec(_fields.get(b"ticker") or _fields.get("ticker", b""))
            if ticker and ticker not in live_signals:
                direction  = _dec(_fields.get(b"direction") or _fields.get("direction", b""))
                confidence = _dec(_fields.get(b"confidence") or _fields.get("confidence", b""))
                live_signals[ticker] = {
                    "direction":  direction,
                    "confidence": float(confidence) if confidence else None,
                    "source":     "predictor",
                }
    except Exception:
        pass
    # Fill missing signals from OVTLYR position intel / screener (higher authority than Yahoo)
    missing_after_predictor = [t for t in tickers if t not in live_signals]
    if missing_after_predictor:
        try:
            def _nine_to_conf(n):
                return round(0.55 + (int(n) / 9.0) * 0.40, 2) if n is not None else None
            _SIGNAL_MAP = {"buy": ("long", 0.90), "sell": ("short", 0.80)}
            ovt_intel_raw  = await _redis.hgetall("ovtlyr:position_intel")
            ovt_screen_raw = await _redis.hgetall("scanner:ovtlyr:latest")
            for sym in missing_after_predictor:
                raw = ovt_intel_raw.get(sym) or ovt_screen_raw.get(sym)
                if not raw:
                    continue
                try:
                    d = json.loads(raw) if isinstance(raw, str) else raw
                    sig_str = (d.get("signal") or d.get("direction") or "").lower()
                    mapped  = _SIGNAL_MAP.get(sig_str)
                    if not mapped:
                        if sig_str in ("long",):  mapped = ("long", 0.80)
                        elif sig_str in ("short",): mapped = ("short", 0.75)
                    if mapped:
                        nine    = d.get("nine_score")
                        conf    = _nine_to_conf(nine) if nine is not None else mapped[1]
                        live_signals[sym] = {
                            "direction":  mapped[0],
                            "confidence": conf,
                            "source":     "ovtlyr",
                        }
                except Exception:
                    pass
        except Exception:
            pass

    # Fill remaining missing signals from Benzinga analyst consensus (Massive MCP, Redis-cached 4-hr TTL)
    _REC_MAP = {
        "strong buy": ("long", 0.95), "buy": ("long", 0.75),
        "outperform": ("long", 0.70), "overweight": ("long", 0.70),
        "hold": None, "neutral": None, "market perform": None,
        "underperform": ("short", 0.60), "underweight": ("short", 0.60),
        "sell": ("short", 0.80), "strong sell": ("short", 0.95),
    }
    missing_sig = [t for t in tickers if t not in live_signals]
    if missing_sig:
        try:
            from shared.data_client import DataClient
            _redis3 = await get_redis()
            still_missing_sig = []
            for sym in missing_sig:
                cached = await _redis3.get(f"consensus:{sym}")
                if cached:
                    try:
                        live_signals[sym] = json.loads(cached)
                    except Exception:
                        still_missing_sig.append(sym)
                else:
                    still_missing_sig.append(sym)
            if still_missing_sig:
                _sem = asyncio.Semaphore(5)
                async def _fetch_one_consensus(sym):
                    async with _sem:
                        try:
                            d = await DataClient().analyst(sym) or {}
                            rating = (d.get("consensus_rating") or "").lower()
                            mapped = _REC_MAP.get(rating)
                            if mapped:
                                return sym, {"direction": mapped[0], "confidence": mapped[1], "source": "consensus"}
                        except Exception:
                            pass
                        return sym, None
                rec_results = await asyncio.gather(*[_fetch_one_consensus(s) for s in still_missing_sig])
                for sym, sig in rec_results:
                    if sig:
                        await _redis3.setex(f"consensus:{sym}", 14400, json.dumps(sig))
                        live_signals[sym] = sig
        except Exception:
            pass

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
            "underlying_price":  live_prices.get(r["underlying"]),
            "signal":            live_signals.get(r["underlying"]),
            "status":           r["status"],
            "last_scan_at":     r["last_scan_at"].isoformat() if r["last_scan_at"] else None,
            "has_chart":        bool(raw_obj.get("chart_b64")),
            "journal":          r["journal"],
        })
    return results


class OptionsReportBody(BaseModel):
    html:  str
    count: int = 0

@app.post("/api/options/report/email")
async def email_options_report(body: OptionsReportBody, token: str = ""):
    """Send the options positions HTML report via AgentMail."""
    check_token(token)
    import aiohttp as _aiohttp
    env = _read_env_file()
    def ev(k): return env.get(k) or os.getenv(k, "")

    api_key   = ev("AGENTMAIL_API_KEY")
    base_url  = (ev("AGENTMAIL_BASE_URL") or "https://api.agentmail.to").rstrip("/")
    recipient = ev("REPORT_RECIPIENT_EMAIL")
    inbox_raw = ev("AGENTMAIL_ALERTS_INBOX") or "alerts"
    inbox_id  = inbox_raw if "@" in inbox_raw else f"{inbox_raw}@agentmail.to"

    if not api_key:
        raise HTTPException(status_code=400, detail="AGENTMAIL_API_KEY is not configured — set it in Configuration → AgentMail")
    if not recipient:
        raise HTTPException(status_code=400, detail="REPORT_RECIPIENT_EMAIL is not configured — set it in Configuration → User Settings → Report Delivery")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    date_str = datetime.now().strftime("%Y-%m-%d")
    n        = body.count
    subject  = f"OpenTrader Daily Option Report {date_str} ({n} position{'s' if n != 1 else ''})"
    plain    = f"OpenTrader Daily Option Report {date_str} — {n} positions. Open in an HTML-capable email client."

    try:
        async with _aiohttp.ClientSession() as s:
            async with s.post(
                f"{base_url}/v0/inboxes/{inbox_id}/messages/send",
                headers=headers,
                json={"to": [recipient], "subject": subject, "text": plain, "html": body.html},
                timeout=_aiohttp.ClientTimeout(total=30),
            ) as r_send:
                send_status = r_send.status
                if send_status not in (200, 201):
                    err = await r_send.text()
                    raise HTTPException(status_code=502,
                        detail=f"AgentMail send failed ({send_status}): {err[:300]}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AgentMail connection error: {e}")

    return {"ok": True, "message": f"Report sent to {recipient}"}


async def _get_sgov_alert() -> dict | None:
    """
    Fetch SGOV's next ex-dividend date via Massive MCP and return an alert dict if
    action is needed today, or None.  Alert dict keys:
      action  — "sell" (day before ex-div) or "buy" (on ex-div date)
      ex_date — YYYY-MM-DD string
      accounts — list of IRA account labels
    """
    try:
        # Collect IRA account labels from env vars
        ira_labels = []
        for n in range(1, 6):
            if os.getenv(f"WEBULL_LIVE_ACCOUNT_{n}_IRA", "false").lower() == "true":
                ira_labels.append(f"webull-live-{n}")
        if not ira_labels:
            ira_labels = ["webull-live-2", "webull-live-3", "webull-live-4"]

        from shared.data_client import DataClient
        from datetime import date as _date, timedelta as _td
        divs = await DataClient().dividends("SGOV")
        if not divs:
            return None
        today_et = now_et().date()
        today_str = today_et.isoformat()
        upcoming = [d for d in (divs or []) if d.get("ex_date") and d["ex_date"] >= today_str]
        if not upcoming:
            return None
        ex_date = _date.fromisoformat(upcoming[-1]["ex_date"])

        if ex_date == today_et + _td(days=1):
            return {"action": "sell", "ex_date": ex_date.isoformat(), "accounts": ira_labels}
        if ex_date == today_et:
            return {"action": "buy", "ex_date": ex_date.isoformat(), "accounts": ira_labels}
        return None
    except Exception as e:
        log.warning("sgov_alert_fetch_failed", error=str(e))
        return None


async def _fetch_watchlist_alerts(watch_tickers: list, alert_days_before: int) -> list[dict]:
    """Return [{ticker, event, date, days_until}] for watched tickers within the alert window."""
    from datetime import date as _date
    alerts: list[dict] = []
    if not watch_tickers:
        return alerts
    today   = _date.today()
    cutoff  = max(1, int(alert_days_before))

    tickers_earn  = [w["ticker"] for w in watch_tickers if w.get("earnings")]
    tickers_exdiv = [w["ticker"] for w in watch_tickers if w.get("exdiv")]

    # Ex-div from dividend_meta
    if tickers_exdiv:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                "SELECT ticker, ex_date FROM dividend_meta WHERE ticker = ANY($1)", tickers_exdiv
            )
            for r in rows:
                if not r["ex_date"]:
                    continue
                days = (r["ex_date"] - today).days
                if 0 <= days <= cutoff:
                    alerts.append({"ticker": r["ticker"], "event": "Ex-Dividend",
                                   "date": str(r["ex_date"]), "days_until": days})
        except Exception:
            pass

    # Earnings: option_positions DB first, then DataClient fallback
    if tickers_earn:
        found: dict[str, str] = {}
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                """SELECT DISTINCT ON (underlying) underlying, next_earnings_date
                   FROM option_positions
                   WHERE underlying = ANY($1) AND next_earnings_date IS NOT NULL
                   ORDER BY underlying, next_earnings_date""",
                tickers_earn,
            )
            for r in rows:
                found[r["underlying"]] = r["next_earnings_date"].isoformat()
        except Exception:
            pass

        for ticker in [t for t in tickers_earn if t not in found]:
            try:
                from shared.data_client import DataClient
                dc = DataClient()
                d  = await dc.earnings(ticker) or {}
                entries = d.get("upcoming") or d.get("earnings") or []
                if entries:
                    dt_str = entries[0].get("date") or entries[0].get("report_date") or ""
                    if dt_str:
                        found[ticker] = dt_str
            except Exception:
                pass

        for ticker, dt_str in found.items():
            try:
                from datetime import date as _d2
                dt   = _d2.fromisoformat(str(dt_str)[:10])
                days = (dt - today).days
                if 0 <= days <= cutoff:
                    alerts.append({"ticker": ticker, "event": "Earnings",
                                   "date": str(dt), "days_until": days})
            except Exception:
                pass

    alerts.sort(key=lambda a: (a["days_until"], a["ticker"]))
    return alerts


def _build_daily_report_html(
    positions:        list[dict],
    sgov_alert:       dict | None = None,
    equity_positions: list[dict] | None = None,
    exdiv_map:        dict | None = None,   # ticker → ex_date string
    config:           dict | None = None,
    watchlist_alerts: list[dict] | None = None,
) -> str:
    """Build the HTML daily report. Sections are controlled by config flags."""
    from datetime import date as _date
    cfg            = config or {}
    include_opts   = cfg.get("include_options",  True)
    include_stocks = cfg.get("include_stocks",   False)
    include_earn   = cfg.get("include_earnings", False)
    include_exdiv  = cfg.get("include_exdiv",    False)
    exdiv          = exdiv_map or {}

    today    = _date.today()
    date_str = today.isoformat()
    report_title = f"OpenTrader Daily Report {date_str}"

    def fmt(v):
        return f"${float(v):.2f}" if v is not None else "—"

    def _exdiv_cell(ticker):
        d = exdiv.get(ticker)
        if not d:
            return "—"
        try:
            from datetime import date as _d2
            days = (_d2.fromisoformat(str(d)) - today).days
            style = "color:#e67e22;font-weight:bold" if 0 <= days <= 5 else ""
            label = f"{d} ({days}d)" if days >= 0 else str(d)
            return f'<span style="{style}">{label}</span>'
        except Exception:
            return str(d)

    # ── Options section ──────────────────────────────────────────────────────
    opts_section = ""
    if include_opts and positions:
        sorted_pos = sorted(positions, key=lambda p: (
            0 if (p.get("mode") or "") == "live" else 1,
            p.get("account_name") or p.get("account_label") or "",
            p.get("expiration_date") or "",
        ))
        _OPT_COLS = 13 + (1 if include_earn else 0) + (1 if include_exdiv else 0)
        rows_html = ""
        _prev_opt_mode = None
        for p in sorted_pos:
            _opt_mode = p.get("mode") or ""
            _opt_group = "live" if _opt_mode == "live" else "paper"
            if _opt_group != _prev_opt_mode:
                _lbl = "Live Accounts" if _opt_group == "live" else "Paper / Sandbox"
                _bg  = "#e8f4fd" if _opt_group == "live" else "#f5f5f5"
                _clr = "#1a5276" if _opt_group == "live" else "#666"
                rows_html += (
                    f'<tr><td colspan="{_OPT_COLS}" style="background:{_bg};font-weight:bold;'
                    f'color:{_clr};padding:6px 10px;font-size:11px;letter-spacing:.08em;'
                    f'text-transform:uppercase">{_lbl}</td></tr>'
                )
                _prev_opt_mode = _opt_group
            dte = p.get("days_to_exp")
            dte_str   = f"{dte}d" if dte is not None else "—"
            dte_style = "color:#c0392b;font-weight:bold" if dte is not None and dte <= 7 else ""
            earn_date = p.get("next_earnings_date") or "—"
            price     = p.get("underlying_price")
            price_str = f"${float(price):.2f}" if price else "—"
            sym       = p.get("underlying", "")
            sig = p.get("signal") or {}
            direction  = (sig.get("direction") or "").lower()
            confidence = sig.get("confidence")
            conflict   = sig.get("conflict", False)
            if direction == "long":
                cp = f" {round(confidence * 100)}%" if confidence else ""
                sig_html = f'<span style="color:#1a7a3a;font-weight:bold">▲ BUY{cp}</span>'
            elif direction == "short":
                cp = f" {round(confidence * 100)}%" if confidence else ""
                sig_html = f'<span style="color:#c0392b;font-weight:bold">▼ SELL{cp}</span>'
            else:
                sig_html = "—"
            if conflict:
                pred_dir = sig.get("predictor_direction", "")
                pred_src = sig.get("predictor_source", "predictor")
                pred_label = "BUY" if pred_dir == "long" else "SELL" if pred_dir == "short" else pred_dir.upper()
                sig_html += (
                    f' <span style="display:inline-block;background:#fff3cd;color:#856404;'
                    f'font-size:10px;padding:1px 5px;border-radius:3px;font-weight:600;'
                    f'border:1px solid #ffc107" title="{pred_src}: {pred_label} — OVTLYR overrides">'
                    f'&#9888; {pred_src}: {pred_label}</span>'
                )
            earn_col  = f"<td>{earn_date}</td>"  if include_earn  else ""
            exdiv_col = f"<td>{_exdiv_cell(sym)}</td>" if include_exdiv else ""
            rows_html += f"""<tr>
              <td>{sym}</td>
              <td>{p.get("account_name", p.get("account_label",""))}</td>
              <td>{fmt(p.get("strike"))}</td>
              <td>{price_str}</td>
              <td>{p.get("expiration_date","—")}</td>
              <td style="{dte_style}">{dte_str}</td>
              <td>{sig_html}</td>
              {earn_col}{exdiv_col}
              <td style="color:#c0392b;font-weight:bold">{fmt(p.get("level_emergency"))}</td>
              <td style="color:#b8860b;font-weight:bold">{fmt(p.get("level_exit_alert"))}</td>
              <td>{fmt(p.get("level_roll_1"))}</td>
              <td>{fmt(p.get("level_roll_2"))}</td>
              <td>{fmt(p.get("level_roll_3"))}</td>
            </tr>"""

        conflicts = [p["underlying"] for p in sorted_pos if (p.get("signal") or {}).get("conflict")]
        conflict_banner = ""
        if conflicts:
            tickers_str = ", ".join(conflicts)
            conflict_banner = (
                f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;'
                f'padding:10px 14px;margin-bottom:16px;color:#856404;font-size:12px">'
                f'<strong>&#9888; Signal Conflict</strong> &mdash; OVTLYR overrides predictor for: '
                f'<strong>{tickers_str}</strong>.</div>'
            )

        sgov_banner = ""
        if sgov_alert:
            accts_str   = ", ".join(sgov_alert.get("accounts", []))
            ex_date_str = sgov_alert.get("ex_date", "")
            if sgov_alert.get("action") == "sell":
                sgov_banner = (
                    f'<div style="background:#fff3cd;border:2px solid #e67e22;border-radius:6px;'
                    f'padding:12px 16px;margin-bottom:14px;color:#7d4e00;font-size:13px">'
                    f'<strong>&#9888; SGOV ACTION &mdash; SELL TODAY</strong><br>'
                    f'Ex-dividend date is tomorrow (<strong>{ex_date_str}</strong>). '
                    f'Sell SGOV in IRA accounts (<strong>{accts_str}</strong>) today.</div>'
                )
            elif sgov_alert.get("action") == "buy":
                sgov_banner = (
                    f'<div style="background:#d4edda;border:2px solid #28a745;border-radius:6px;'
                    f'padding:12px 16px;margin-bottom:14px;color:#155724;font-size:13px">'
                    f'<strong>&#10003; SGOV ACTION &mdash; BUY TODAY</strong><br>'
                    f'Today is the ex-dividend date (<strong>{ex_date_str}</strong>). '
                    f'Buy SGOV in IRA accounts (<strong>{accts_str}</strong>) today.</div>'
                )

        earn_th  = "<th>Earnings</th>"  if include_earn  else ""
        exdiv_th = "<th>Ex-Div</th>"   if include_exdiv else ""
        opts_section = f"""
{sgov_banner}{conflict_banner}
<h3 style="margin:0 0 8px;color:#222;font-size:14px">Options Positions &nbsp;<span style="font-weight:normal;color:#777;font-size:11px">({len(sorted_pos)})</span></h3>
<table>
  <thead><tr>
    <th>Ticker</th><th>Account</th><th>Strike</th><th>Price</th>
    <th>Expiration</th><th>DTE</th><th>Signal</th>
    {earn_th}{exdiv_th}
    <th class="emg">Emergency</th><th class="sl">Stop Loss</th>
    <th class="r1">Roll 1</th><th class="r2">Roll 2</th><th class="r3">Roll 3</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""

    # ── Equity section ───────────────────────────────────────────────────────
    equity_section = ""
    if include_stocks and equity_positions:
        eq_sorted = sorted(equity_positions, key=lambda p: (
            0 if (p.get("account_mode") or p.get("mode") or "") == "live" else 1,
            p.get("account_label") or "",
            p.get("symbol") or "",
        ))
        earn_th  = "<th>Earnings</th>" if include_earn  else ""
        exdiv_th = "<th>Ex-Div</th>"   if include_exdiv else ""
        _EQ_COLS = 7 + (1 if include_earn else 0) + (1 if include_exdiv else 0)
        eq_rows = ""
        _prev_eq_mode = None
        for p in eq_sorted:
            sym    = (p.get("symbol") or "").upper()
            acct   = p.get("display_name") or p.get("account_label") or ""
            qty    = p.get("qty") or p.get("quantity") or p.get("shares") or 0
            cost   = p.get("cost_basis") or p.get("average_cost") or p.get("avg_cost") or 0
            price  = p.get("market_price") or p.get("last_price") or p.get("current_price") or 0
            pnl    = p.get("unrealized_pnl") or p.get("unrealized_pl") or p.get("gain_loss") or 0
            m      = p.get("account_mode") or p.get("mode") or ""
            eq_group = "live" if m == "live" else "paper"
            if eq_group != _prev_eq_mode:
                _lbl = "Live Accounts" if eq_group == "live" else "Paper / Sandbox"
                _bg  = "#e8f4fd" if eq_group == "live" else "#f5f5f5"
                _clr = "#1a5276" if eq_group == "live" else "#666"
                eq_rows += (
                    f'<tr><td colspan="{_EQ_COLS}" style="background:{_bg};font-weight:bold;'
                    f'color:{_clr};padding:6px 10px;font-size:11px;letter-spacing:.08em;'
                    f'text-transform:uppercase">{_lbl}</td></tr>'
                )
                _prev_eq_mode = eq_group
            try:
                pnl_f   = float(pnl)
                pnl_pct = (pnl_f / (float(cost) * float(qty))) * 100 if cost and qty else 0
                pnl_str = f'<span style="color:{"#1a7a3a" if pnl_f >= 0 else "#c0392b"};font-weight:bold">${pnl_f:,.2f} ({pnl_pct:+.1f}%)</span>'
            except Exception:
                pnl_str = "—"
            # OVTLYR signal
            eq_sig = p.get("signal") or {}
            eq_dir = (eq_sig.get("direction") or "").lower()
            eq_conf = eq_sig.get("confidence")
            if eq_dir == "long":
                cp = f" {round(eq_conf * 100)}%" if eq_conf else ""
                eq_sig_html = f'<span style="color:#1a7a3a;font-weight:bold">&#9650; BUY{cp}</span>'
            elif eq_dir == "short":
                cp = f" {round(eq_conf * 100)}%" if eq_conf else ""
                eq_sig_html = f'<span style="color:#c0392b;font-weight:bold">&#9660; SELL{cp}</span>'
            else:
                eq_sig_html = "—"
            earn_col  = f"<td>{p.get('next_earnings_date') or '—'}</td>" if include_earn  else ""
            exdiv_col = f"<td>{_exdiv_cell(sym)}</td>"                    if include_exdiv else ""
            eq_rows += f"""<tr>
              <td><strong>{sym}</strong></td>
              <td>{acct}</td>
              <td>{qty}</td>
              <td>{fmt(cost)}</td>
              <td>{fmt(price)}</td>
              <td>{pnl_str}</td>
              <td>{eq_sig_html}</td>
              {earn_col}{exdiv_col}
            </tr>"""

        equity_section = f"""
<h3 style="margin:24px 0 8px;color:#222;font-size:14px">Stock Positions &nbsp;<span style="font-weight:normal;color:#777;font-size:11px">({len(eq_sorted)})</span></h3>
<table>
  <thead><tr>
    <th>Ticker</th><th>Account</th><th>Qty</th><th>Avg Cost</th><th>Price</th>
    <th>Unrealized P&amp;L</th><th>Signal</th>
    {earn_th}{exdiv_th}
  </tr></thead>
  <tbody>{eq_rows}</tbody>
</table>"""

    n_opts = len(positions) if include_opts else 0
    n_eq   = len(equity_positions) if (include_stocks and equity_positions) else 0
    meta_parts = []
    if n_opts: meta_parts.append(f"{n_opts} option position{'s' if n_opts!=1 else ''}")
    if n_eq:   meta_parts.append(f"{n_eq} stock position{'s' if n_eq!=1 else ''}")
    meta_str = " &middot; ".join(meta_parts) or "No positions"

    # ── Watchlist alerts section ─────────────────────────────────────────────
    watch_section = ""
    if watchlist_alerts:
        def _when_label(days):
            if days == 0:   return "Today"
            if days == 1:   return "Tomorrow"
            return f"{days}d"
        def _when_style(days):
            if days == 0:   return "color:#c0392b;font-weight:bold"
            if days == 1:   return "color:#e67e22;font-weight:bold"
            return ""
        rows_html = "".join(
            f'<tr>'
            f'<td><strong>{a["ticker"]}</strong></td>'
            f'<td>{a["event"]}</td>'
            f'<td>{a["date"]}</td>'
            f'<td style="{_when_style(a["days_until"])}">{_when_label(a["days_until"])}</td>'
            f'</tr>'
            for a in watchlist_alerts
        )
        watch_section = f"""
<div style="background:#fff8e1;border:1px solid #f9a825;border-radius:6px;padding:12px 16px;margin-bottom:18px">
  <h3 style="margin:0 0 8px;color:#e65100;font-size:13px">&#9888; Watchlist Alerts ({len(watchlist_alerts)})</h3>
  <table>
    <thead><tr><th>Ticker</th><th>Event</th><th>Date</th><th>When</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{report_title}</title>
<style>
  body{{font-family:Arial,sans-serif;font-size:13px;color:#222;margin:24px}}
  h2{{margin:0 0 4px;color:#111}}h3{{color:#111}}
  .meta{{color:#777;font-size:11px;margin-bottom:18px}}
  table{{border-collapse:collapse;width:100%;margin-bottom:8px}}
  th{{background:#f2f2f2;border:1px solid #ccc;padding:8px 10px;text-align:left;font-size:11px;
      text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}}
  td{{border:1px solid #ddd;padding:7px 10px;white-space:nowrap}}
  tr:nth-child(even) td{{background:#fafafa}}
  th.emg{{color:#c0392b}} th.sl{{color:#b8860b}}
  th.r1{{color:#2980b9}} th.r2{{color:#5499c7}} th.r3{{color:#7fb3d3}}
</style></head><body>
<h2>{report_title}</h2>
<div class="meta">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} ET &nbsp;&middot;&nbsp;
  {meta_str} &nbsp;&middot;&nbsp; Signals: OVTLYR</div>
{watch_section}
{opts_section}
{equity_section}
</body></html>"""


def _build_options_report_html(positions: list[dict], sgov_alert: dict | None = None) -> str:
    """Backward-compat wrapper around _build_daily_report_html."""
    return _build_daily_report_html(positions, sgov_alert=sgov_alert)


@app.post("/api/options/report/email/auto")
async def email_options_report_auto(token: str = "", force: bool = False):
    """Generate and email the options report from DB — called by the scheduler."""
    check_token(token)
    # Idempotency guard: only send once per calendar day unless force=True
    _idem_redis = await get_redis()
    _today      = datetime.now(TZ).date().isoformat()
    _sent_key   = f"report:daily:sent:{_today}"
    if not force and await _idem_redis.get(_sent_key):
        return {"ok": True, "message": f"Report already sent today ({_today}) — skipped"}
    positions = await get_option_positions(status="active")
    if not positions:
        try:
            pool = await _get_db_pool()
            await pool.execute(
                """INSERT INTO report_log (report_type, status, subject, channels, meta)
                   VALUES ('options_1pm', 'skipped', 'No active positions', '{}', '{}')"""
            )
        except Exception:
            pass
        return {"ok": True, "message": "No active positions — report skipped"}

    # OVTLYR is the authoritative signal source for the report.
    # Override whatever the predictor says with OVTLYR's signal and flag any conflict.
    try:
        _redis = await get_redis()
        def _nine_to_conf(n):
            return round(0.55 + (int(n) / 9.0) * 0.40, 2) if n is not None else None
        _SIGNAL_MAP = {"buy": ("long", 0.90), "sell": ("short", 0.80)}
        ovt_intel_raw  = await _redis.hgetall("ovtlyr:position_intel")
        ovt_screen_raw = await _redis.hgetall("scanner:ovtlyr:latest")
        _pool = await _get_db_pool()
        for pos in positions:
            sym = pos["underlying"]
            raw = ovt_intel_raw.get(sym) or ovt_screen_raw.get(sym)
            if not raw and _pool:
                try:
                    row = await _pool.fetchrow(
                        """SELECT signal, signal_active, nine_score
                           FROM ovtlyr_intel WHERE ticker=$1
                           ORDER BY created_at DESC LIMIT 1""",
                        sym,
                    )
                    if row and row["signal"]:
                        raw = {"signal": row["signal"], "signal_active": row["signal_active"],
                               "nine_score": row["nine_score"]}
                except Exception:
                    pass
            if not raw:
                continue
            try:
                d = json.loads(raw) if isinstance(raw, str) else raw
                # active_signal = Current Active Signal (position monitoring / exit trigger)
                # signal        = Current Signal (entry/transition trigger)
                # Report uses active_signal — it tells us when to get out of a trade.
                sig_str = (d.get("active_signal") or d.get("signal") or d.get("direction") or "").lower()
                mapped = _SIGNAL_MAP.get(sig_str)
                if not mapped:
                    if sig_str in ("long",):    mapped = ("long", 0.80)
                    elif sig_str in ("short",): mapped = ("short", 0.75)
                if mapped:
                    nine = d.get("nine_score")
                    conf = _nine_to_conf(nine) if nine is not None else mapped[1]
                    existing = pos.get("signal") or {}
                    ovtlyr_sig: dict = {
                        "direction":  mapped[0],
                        "confidence": conf,
                        "source":     "ovtlyr",
                    }
                    if existing and existing.get("source") != "ovtlyr" and existing.get("direction") and existing.get("direction") != mapped[0]:
                        ovtlyr_sig["conflict"] = True
                        ovtlyr_sig["predictor_direction"] = existing.get("direction")
                        ovtlyr_sig["predictor_source"]    = existing.get("source", "predictor")
                    pos["signal"] = ovtlyr_sig
            except Exception:
                pass
    except Exception:
        pass

    sgov_alert = await _get_sgov_alert()
    cfg = await _get_daily_report_config()

    # Optionally fetch equity positions
    equity_positions: list[dict] | None = None
    if cfg.get("include_stocks"):
        try:
            broker_data = await get_broker_positions()
            equity_positions = []
            for acct in broker_data.get("accounts", []):
                acct_mode  = acct.get("mode", "")
                acct_label = acct.get("display_name") or acct.get("label") or ""
                for p in acct.get("positions", []):
                    if _is_equity_position(p) and float(p.get("qty") or p.get("quantity") or p.get("shares") or 0) > 0:
                        p.setdefault("account_label", acct_label)
                        p["account_mode"] = acct_mode
                        equity_positions.append(p)
        except Exception:
            pass

    # Enrich equity positions with OVTLYR signals
    if equity_positions:
        try:
            _eq_redis = await get_redis()
            _eq_intel = await _eq_redis.hgetall("ovtlyr:position_intel")
            _eq_screen = await _eq_redis.hgetall("scanner:ovtlyr:latest")
            _eq_pool = await _get_db_pool()
            _SIGNAL_MAP_EQ = {"buy": ("long", 0.90), "sell": ("short", 0.80)}
            def _nine_to_conf_eq(n):
                return round(0.55 + (int(n) / 9.0) * 0.40, 2) if n is not None else None
            for ep in equity_positions:
                sym = (ep.get("symbol") or "").upper()
                if not sym:
                    continue
                raw = _eq_intel.get(sym) or _eq_screen.get(sym)
                if not raw and _eq_pool:
                    try:
                        row = await _eq_pool.fetchrow(
                            """SELECT signal, signal_active, nine_score
                               FROM ovtlyr_intel WHERE ticker=$1
                               ORDER BY created_at DESC LIMIT 1""",
                            sym,
                        )
                        if row and row["signal"]:
                            raw = {"signal": row["signal"], "signal_active": row["signal_active"],
                                   "nine_score": row["nine_score"]}
                    except Exception:
                        pass
                if not raw:
                    continue
                try:
                    d = json.loads(raw) if isinstance(raw, str) else raw
                    sig_str = (d.get("active_signal") or d.get("signal") or d.get("direction") or "").lower()
                    mapped = _SIGNAL_MAP_EQ.get(sig_str)
                    if not mapped:
                        if sig_str in ("long",):   mapped = ("long",  0.80)
                        elif sig_str in ("short",): mapped = ("short", 0.75)
                    if mapped:
                        nine = d.get("nine_score")
                        conf = _nine_to_conf_eq(nine) if nine is not None else mapped[1]
                        ep["signal"] = {"direction": mapped[0], "confidence": conf, "source": "ovtlyr"}
                except Exception:
                    pass
        except Exception:
            pass

    # Optionally fetch ex-div dates for all relevant tickers
    exdiv_map: dict | None = None
    if cfg.get("include_exdiv"):
        try:
            tickers = list({p.get("underlying", "") for p in positions} |
                           {(p.get("symbol") or "").upper() for p in (equity_positions or [])})
            tickers = [t for t in tickers if t]
            if tickers:
                pool_d = await _get_db_pool()
                rows_d = await pool_d.fetch(
                    "SELECT ticker, ex_date FROM dividend_meta WHERE ticker = ANY($1)", tickers
                )
                exdiv_map = {r["ticker"]: str(r["ex_date"]) for r in rows_d if r["ex_date"]}
        except Exception:
            pass

    # Watchlist alerts
    watchlist_alerts: list[dict] = []
    _watch = cfg.get("watch_tickers") or []
    if _watch:
        try:
            watchlist_alerts = await _fetch_watchlist_alerts(_watch, cfg.get("alert_days_before", 1))
        except Exception:
            pass

    html = _build_daily_report_html(
        positions, sgov_alert=sgov_alert,
        equity_positions=equity_positions, exdiv_map=exdiv_map, config=cfg,
        watchlist_alerts=watchlist_alerts or None,
    )
    channels   = list(cfg.get("channels") or ["agentmail"])
    recipient  = cfg.get("recipient") or ""
    body = OptionsReportBody(html=html, count=len(positions))
    result = await email_options_report(body, token=token)
    # Mark sent for today so duplicate scheduler firings are suppressed
    try:
        await _idem_redis.set(_sent_key, "1", ex=86400)
    except Exception:
        pass
    # Log to report_log
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        n_opts   = len(positions) if cfg.get("include_options", True) else 0
        n_eq     = len(equity_positions) if equity_positions else 0
        parts    = []
        if n_opts: parts.append(f"{n_opts} option")
        if n_eq:   parts.append(f"{n_eq} stock")
        pos_summary = ", ".join(parts) or "no positions"
        subject  = f"OpenTrader Daily Report {date_str} ({pos_summary})"
        pool = await _get_db_pool()
        await pool.execute(
            """INSERT INTO report_log
               (report_type, status, subject, channels, recipient, body_html, meta)
               VALUES ('options_1pm', 'sent', $1, $2, $3, $4, $5)""",
            subject, channels, recipient, html,
            json.dumps({"position_count": len(positions), "sgov_alert": bool(sgov_alert),
                        "equity_count": n_eq}),
        )
    except Exception:
        pass
    return result


# ── Reporting API ─────────────────────────────────────────────────────────────

@app.post("/api/reports/log")
async def log_report_entry(body: dict, token: str = ""):
    """Internal endpoint: review-agent calls this to write an EOD entry to report_log."""
    check_token(token)
    pool = await _get_db_pool()
    await pool.execute(
        """INSERT INTO report_log
           (report_type, status, subject, channels, body_text, meta)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        body.get("report_type", "eod"),
        body.get("status", "sent"),
        body.get("subject"),
        body.get("channels", []),
        body.get("body_text"),
        json.dumps(body.get("meta") or {}),
    )
    return {"ok": True}


@app.get("/api/reports/history")
async def get_reports_history(
    limit: int = 50, offset: int = 0, report_type: str = "", token: str = ""
):
    """Paginated report history from report_log + review_log backfill."""
    check_token(token)
    pool = await _get_db_pool()

    where = "WHERE r.report_type = $3" if report_type else ""
    params = [limit, offset] + ([report_type] if report_type else [])
    rows = await pool.fetch(
        f"""SELECT id::text, ts, report_type, status, subject, channels, recipient,
                   meta, (body_html IS NOT NULL) AS has_html, (body_text IS NOT NULL) AS has_text
            FROM report_log r
            {where}
            ORDER BY ts DESC
            LIMIT $1 OFFSET $2""",
        *params,
    )
    entries = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            pass
        entries.append({
            "id":          r["id"],
            "ts":          r["ts"].isoformat(),
            "report_type": r["report_type"],
            "status":      r["status"],
            "subject":     r["subject"],
            "channels":    r["channels"] or [],
            "recipient":   r["recipient"],
            "meta":        meta,
            "has_html":    r["has_html"],
            "has_text":    r["has_text"],
        })

    # Backfill from review_log for EOD reports not yet in report_log
    if not report_type or report_type == "eod":
        existing_dates = {e["ts"][:10] for e in entries if e["report_type"] == "eod"}
        legacy = await pool.fetch(
            """SELECT id::text, ts, trade_count, findings
               FROM review_log
               ORDER BY ts DESC LIMIT 90"""
        )
        for r in legacy:
            d = r["ts"].strftime("%Y-%m-%d")
            if d in existing_dates:
                continue
            entries.append({
                "id":          r["id"],
                "ts":          r["ts"].isoformat(),
                "report_type": "eod",
                "status":      "sent",
                "subject":     f"OpenTrader EOD Report — {d}",
                "channels":    [],
                "recipient":   None,
                "meta":        {"trade_count": r["trade_count"]},
                "has_html":    False,
                "has_text":    bool(r["findings"]),
                "_legacy":     True,
            })
        entries.sort(key=lambda x: x["ts"], reverse=True)

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM report_log" + (" WHERE report_type=$1" if report_type else ""),
        *([report_type] if report_type else []),
    )
    return {"entries": entries[:limit], "total": int(total)}


@app.get("/api/reports/entry/{entry_id}")
async def get_report_entry(entry_id: str, legacy: bool = False, token: str = ""):
    """Return full body of a single report entry."""
    check_token(token)
    pool = await _get_db_pool()
    if legacy:
        row = await pool.fetchrow(
            "SELECT id::text, ts, findings AS body_text, trade_count FROM review_log WHERE id=$1",
            uuid.UUID(entry_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return {"body_text": row["body_text"], "body_html": None,
                "ts": row["ts"].isoformat(), "meta": {"trade_count": row["trade_count"]}}
    row = await pool.fetchrow(
        "SELECT body_html, body_text, ts, meta FROM report_log WHERE id=$1",
        uuid.UUID(entry_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    meta = {}
    try:
        meta = json.loads(row["meta"]) if row["meta"] else {}
    except Exception:
        pass
    return {"body_html": row["body_html"], "body_text": row["body_text"],
            "ts": row["ts"].isoformat(), "meta": meta}


@app.get("/api/reports/preview/options")
async def preview_options_report(token: str = ""):
    """Build the daily report HTML without sending it."""
    check_token(token)
    positions  = await get_option_positions(status="active")
    sgov_alert = await _get_sgov_alert()
    cfg        = await _get_daily_report_config()

    equity_positions: list[dict] | None = None
    if cfg.get("include_stocks"):
        try:
            broker_data = await get_broker_positions()
            equity_positions = []
            for acct in broker_data.get("accounts", []):
                acct_mode  = acct.get("mode", "")
                acct_label = acct.get("display_name") or acct.get("label") or ""
                for p in acct.get("positions", []):
                    if _is_equity_position(p) and float(p.get("qty") or p.get("quantity") or p.get("shares") or 0) > 0:
                        p.setdefault("account_label", acct_label)
                        p["account_mode"] = acct_mode
                        equity_positions.append(p)
        except Exception:
            pass

    # Enrich equity positions with OVTLYR signals
    if equity_positions:
        try:
            _eq_redis  = await get_redis()
            _eq_intel  = await _eq_redis.hgetall("ovtlyr:position_intel")
            _eq_screen = await _eq_redis.hgetall("scanner:ovtlyr:latest")
            _eq_pool   = await _get_db_pool()
            _SIGNAL_MAP_EQ2 = {"buy": ("long", 0.90), "sell": ("short", 0.80)}
            def _nine_conf(n):
                return round(0.55 + (int(n) / 9.0) * 0.40, 2) if n is not None else None
            for ep in equity_positions:
                sym = (ep.get("symbol") or "").upper()
                if not sym:
                    continue
                raw = _eq_intel.get(sym) or _eq_screen.get(sym)
                if not raw and _eq_pool:
                    try:
                        row = await _eq_pool.fetchrow(
                            """SELECT signal, signal_active, nine_score
                               FROM ovtlyr_intel WHERE ticker=$1
                               ORDER BY created_at DESC LIMIT 1""",
                            sym,
                        )
                        if row and row["signal"]:
                            raw = {"signal": row["signal"], "signal_active": row["signal_active"],
                                   "nine_score": row["nine_score"]}
                    except Exception:
                        pass
                if not raw:
                    continue
                try:
                    d = json.loads(raw) if isinstance(raw, str) else raw
                    sig_str = (d.get("active_signal") or d.get("signal") or d.get("direction") or "").lower()
                    mapped = _SIGNAL_MAP_EQ2.get(sig_str)
                    if not mapped:
                        if sig_str in ("long",):   mapped = ("long",  0.80)
                        elif sig_str in ("short",): mapped = ("short", 0.75)
                    if mapped:
                        nine = d.get("nine_score")
                        conf = _nine_conf(nine) if nine is not None else mapped[1]
                        ep["signal"] = {"direction": mapped[0], "confidence": conf, "source": "ovtlyr"}
                except Exception:
                    pass
        except Exception:
            pass

    exdiv_map: dict | None = None
    if cfg.get("include_exdiv"):
        try:
            tickers = list({p.get("underlying", "") for p in positions} |
                           {(p.get("symbol") or "").upper() for p in (equity_positions or [])})
            tickers = [t for t in tickers if t]
            if tickers:
                pool_d = await _get_db_pool()
                rows_d = await pool_d.fetch(
                    "SELECT ticker, ex_date FROM dividend_meta WHERE ticker = ANY($1)", tickers
                )
                exdiv_map = {r["ticker"]: str(r["ex_date"]) for r in rows_d if r["ex_date"]}
        except Exception:
            pass

    # Watchlist alerts
    _watch2 = cfg.get("watch_tickers") or []
    _walerts: list[dict] = []
    if _watch2:
        try:
            _walerts = await _fetch_watchlist_alerts(_watch2, cfg.get("alert_days_before", 1))
        except Exception:
            pass

    if not positions and not equity_positions and not _walerts:
        return {"html": None, "position_count": 0,
                "generated_at": datetime.now().isoformat(),
                "message": "No active positions"}

    html = _build_daily_report_html(
        positions, sgov_alert=sgov_alert,
        equity_positions=equity_positions, exdiv_map=exdiv_map, config=cfg,
        watchlist_alerts=_walerts or None,
    )
    return {"html": html, "position_count": len(positions),
            "equity_count": len(equity_positions) if equity_positions else 0,
            "generated_at": datetime.now().isoformat(), "sgov_alert": bool(sgov_alert)}


@app.get("/api/reports/preview/eod")
async def preview_eod_report(token: str = ""):
    """Build today's EOD report text without sending it."""
    check_token(token)
    pool = await _get_db_pool()
    date_str = now_et().date().isoformat()

    trades = []
    option_closures = []
    try:
        rows = await pool.fetch(
            """SELECT id, ticker, direction, qty, entry_price, exit_price,
                      pnl, strategy, status, signal_src, ts
               FROM trades WHERE ts::date = $1 ORDER BY ts DESC""",
            datetime.fromisoformat(date_str).date(),
        )
        trades = [dict(r) for r in rows]
    except Exception:
        pass
    try:
        rows = await pool.fetch(
            """SELECT p.underlying, l.contract_symbol, l.contract_price,
                      l.realized_pnl, p.account_label, p.broker
               FROM option_trade_log l
               JOIN option_positions p ON l.position_id = p.id
               WHERE l.event_type = 'closed' AND l.ts::date = $1
               ORDER BY l.ts DESC""",
            datetime.fromisoformat(date_str).date(),
        )
        option_closures = [dict(r) for r in rows]
    except Exception:
        pass

    # Build stats and template report (no LLM — instant)
    rejects  = [t for t in trades if t.get("status") == "reject"]
    active   = [t for t in trades if t.get("status") != "reject"]
    closed   = [t for t in active if t.get("pnl") is not None]
    wins     = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses   = [t for t in closed if (t.get("pnl") or 0) < 0]
    equity_pnl = sum(float(t.get("pnl") or 0) for t in closed)
    opt_with_pnl = [o for o in option_closures if o.get("realized_pnl") is not None]
    opt_wins     = [o for o in opt_with_pnl if float(o["realized_pnl"]) > 0]
    opt_losses   = [o for o in opt_with_pnl if float(o["realized_pnl"]) <= 0]
    opt_pnl      = sum(float(o["realized_pnl"]) for o in opt_with_pnl)

    stats = {
        "total_trades": len(active), "rejected": len(rejects),
        "filled": len(active), "longs": sum(1 for t in active if t.get("direction") == "long"),
        "shorts": sum(1 for t in active if t.get("direction") == "short"),
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins)/len(closed)*100, 1) if closed else 0.0,
        "total_pnl": round(equity_pnl, 2),
        "avg_pnl": round(equity_pnl/len(closed), 2) if closed else 0.0,
        "opt_closed": len(opt_with_pnl), "opt_wins": len(opt_wins),
        "opt_losses": len(opt_losses),
        "opt_win_rate": round(len(opt_wins)/len(opt_with_pnl)*100, 1) if opt_with_pnl else 0.0,
        "opt_pnl": round(opt_pnl, 2),
        "combined_pnl": round(equity_pnl + opt_pnl, 2),
    }
    opt_lines = ""
    if opt_with_pnl:
        rows_txt = "\n".join(
            f"  {o['underlying']:6s}  {o['contract_symbol']}  "
            f"price=${float(o['contract_price']):.2f}  P&L=${int(o['realized_pnl']):+d}"
            for o in opt_with_pnl if o.get("realized_pnl") is not None
        ) or "  None."
        opt_lines = (
            f"\nOPTIONS CLOSURES\n"
            f"  Closed: {len(opt_with_pnl)}  Wins/Losses: {len(opt_wins)}/{len(opt_losses)}"
            f"  P&L: ${opt_pnl:+.2f}\n\n{rows_txt}\n"
        )
    text = (
        f"OpenTrader EOD Report — {date_str} (PREVIEW)\n{'='*40}\n\n"
        f"EQUITY TRADING SUMMARY\n"
        f"  Total: {stats['total_trades']}  Rejected: {stats['rejected']}"
        f"  Filled: {stats['filled']}\n"
        f"  Longs: {stats['longs']}  Shorts: {stats['shorts']}\n\n"
        f"EQUITY PERFORMANCE\n"
        f"  Closed: {stats['closed']}  Wins: {stats['wins']}  Losses: {stats['losses']}\n"
        f"  Win rate: {stats['win_rate']}%  P&L: ${stats['total_pnl']:+.2f}\n"
        f"{opt_lines}"
    )
    return {"text": text, "stats": stats, "generated_at": datetime.now().isoformat()}


@app.post("/api/reports/trigger/options")
async def trigger_options_report(token: str = ""):
    """Manually fire the 1pm options report."""
    check_token(token)
    return await email_options_report_auto(token=token, force=True)


@app.post("/api/reports/trigger/eod")
async def trigger_eod_report(token: str = ""):
    """Manually fire the EOD report by publishing to the scheduler command stream."""
    check_token(token)
    redis = await get_redis()
    date_str = now_et().date().isoformat()
    await redis.xadd(
        "system.commands",
        {
            "command":   "trigger_job",
            "job":       "eod_report",
            "date":      date_str,
            "channels":  json.dumps(["agentmail", "telegram", "discord"]),
            "issued_by": "reports_ui",
        },
        maxlen=500,
    )
    return {"ok": True, "triggered_at": datetime.now().isoformat(),
            "message": "EOD report triggered — check Agents logs for delivery status"}


async def _get_daily_report_config() -> dict:
    """Read daily report config from DB, falling back to Redis then defaults."""
    env = _read_env_file()
    defaults: dict = {
        "enabled":          True,
        "channels":         ["agentmail"],
        "recipient":        env.get("REPORT_RECIPIENT_EMAIL") or os.getenv("REPORT_RECIPIENT_EMAIL", ""),
        "schedule_days":    "mon-fri",
        "schedule_hour":    13,
        "schedule_minute":  0,
        "include_stocks":   False,
        "include_options":  True,
        "include_earnings": False,
        "include_exdiv":    False,
        "watch_tickers":    [],
        "alert_days_before": 1,
    }
    if DB_URL:
        try:
            pool = await _get_db_pool()
            row  = await pool.fetchrow(
                "SELECT * FROM report_config WHERE report_type = 'daily_report'"
            )
            if row:
                d = dict(row)
                d.pop("report_type", None)
                d.pop("updated_at", None)
                if d.get("channels") is not None:
                    d["channels"] = list(d["channels"])
                if d.get("watch_tickers") is not None:
                    try:
                        d["watch_tickers"] = json.loads(d["watch_tickers"])
                    except Exception:
                        d["watch_tickers"] = []
                return {**defaults, **d}
        except Exception:
            pass
    # Fall back to legacy Redis key
    try:
        redis = await get_redis()
        raw   = await redis.get("report_config:options_1pm")
        if raw:
            return {**defaults, **json.loads(raw)}
    except Exception:
        pass
    return defaults


@app.get("/api/reports/config")
async def get_reports_config(token: str = ""):
    """Return per-report enabled state and channel config."""
    check_token(token)
    daily_cfg = await _get_daily_report_config()

    env = _read_env_file()
    def ev(k): return env.get(k) or os.getenv(k, "")
    redis = await get_redis()
    raw_eod = await redis.get("report_config:eod")
    eod_defaults = {
        "enabled":  True,
        "channels": ["agentmail"]
                    + (["telegram"] if ev("TELEGRAM_BOT_TOKEN") else [])
                    + (["discord"]  if ev("DISCORD_WEBHOOK_URL") else []),
    }
    def _parse(raw, defaults):
        try:
            return {**defaults, **json.loads(raw)} if raw else defaults
        except Exception:
            return defaults

    return {
        "daily_report": daily_cfg,
        "options_1pm":  daily_cfg,   # backward-compat alias
        "eod":          _parse(raw_eod, eod_defaults),
    }


@app.post("/api/reports/config")
async def save_reports_config(body: dict, token: str = ""):
    """Persist per-report config to DB + sync Redis scheduler key."""
    check_token(token)
    redis = await get_redis()

    # Accept both 'daily_report' and legacy 'options_1pm' keys
    daily_cfg = body.get("daily_report") or body.get("options_1pm")
    if daily_cfg:
        if DB_URL:
            try:
                pool = await _get_db_pool()
                await pool.execute(
                    """INSERT INTO report_config
                         (report_type, enabled, channels, recipient,
                          schedule_days, schedule_hour, schedule_minute,
                          include_stocks, include_options, include_earnings, include_exdiv,
                          watch_tickers, alert_days_before,
                          updated_at)
                       VALUES ('daily_report', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                       ON CONFLICT (report_type) DO UPDATE SET
                         enabled=$1, channels=$2, recipient=$3,
                         schedule_days=$4, schedule_hour=$5, schedule_minute=$6,
                         include_stocks=$7, include_options=$8, include_earnings=$9,
                         include_exdiv=$10, watch_tickers=$11, alert_days_before=$12,
                         updated_at=NOW()""",
                    bool(daily_cfg.get("enabled", True)),
                    list(daily_cfg.get("channels", ["agentmail"])),
                    daily_cfg.get("recipient", ""),
                    daily_cfg.get("schedule_days", "mon-fri"),
                    int(daily_cfg.get("schedule_hour", 13)),
                    int(daily_cfg.get("schedule_minute", 0)),
                    bool(daily_cfg.get("include_stocks", False)),
                    bool(daily_cfg.get("include_options", True)),
                    bool(daily_cfg.get("include_earnings", False)),
                    bool(daily_cfg.get("include_exdiv", False)),
                    json.dumps(daily_cfg.get("watch_tickers") or []),
                    int(daily_cfg.get("alert_days_before") or 1),
                )
            except Exception:
                pass
        # Sync Redis so scheduler picks up schedule/enabled changes
        sched_rec = {
            "id":          "options_report",
            "enabled":     bool(daily_cfg.get("enabled", True)),
            "cron_hour":   int(daily_cfg.get("schedule_hour", 13)),
            "cron_minute": int(daily_cfg.get("schedule_minute", 0)),
            "cron_days":   daily_cfg.get("schedule_days", "mon-fri"),
        }
        await redis.set("scheduler:job:options_report", json.dumps(sched_rec))
        await redis.publish("scheduler:reload", "options_report")
        # Keep legacy key for older code paths
        await redis.set("report_config:options_1pm", json.dumps(daily_cfg))

    if "eod" in body:
        cfg_eod = body["eod"]
        if DB_URL:
            try:
                pool = await _get_db_pool()
                await pool.execute(
                    """INSERT INTO report_config (report_type, enabled, channels, updated_at)
                       VALUES ('eod', $1, $2, NOW())
                       ON CONFLICT (report_type) DO UPDATE SET
                         enabled=$1, channels=$2, updated_at=NOW()""",
                    bool(cfg_eod.get("enabled", True)),
                    list(cfg_eod.get("channels", ["agentmail"])),
                )
            except Exception:
                pass
        await redis.set("report_config:eod", json.dumps(cfg_eod))

    return {"ok": True}


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


@app.get("/api/options/positions/{position_id}/greeks-history")
async def get_option_greeks_history(position_id: str, days: int = 30, token: str = ""):
    """Return Greeks time series for a position from greeks_history hypertable."""
    check_token(token)
    pool = await _get_db_pool()
    if not pool:
        return {"error": "DB unavailable", "rows": []}
    try:
        rows = await pool.fetch(
            """
            SELECT ts, underlying_price, contract_price,
                   delta, gamma, theta, vega, rho, iv, dte
            FROM greeks_history
            WHERE position_id = $1::uuid
              AND ts >= NOW() - ($2 || ' days')::INTERVAL
            ORDER BY ts ASC
            """,
            position_id, str(days),
        )
        return {
            "position_id": position_id,
            "days":        days,
            "count":       len(rows),
            "rows": [
                {
                    "ts":               r["ts"].isoformat(),
                    "underlying_price": float(r["underlying_price"]) if r["underlying_price"] is not None else None,
                    "contract_price":   float(r["contract_price"])   if r["contract_price"]   is not None else None,
                    "delta":            float(r["delta"])  if r["delta"]  is not None else None,
                    "gamma":            float(r["gamma"])  if r["gamma"]  is not None else None,
                    "theta":            float(r["theta"])  if r["theta"]  is not None else None,
                    "vega":             float(r["vega"])   if r["vega"]   is not None else None,
                    "rho":              float(r["rho"])    if r["rho"]    is not None else None,
                    "iv":               float(r["iv"])     if r["iv"]     is not None else None,
                    "dte":              r["dte"],
                }
                for r in rows
            ],
        }
    except Exception as e:
        log.error("options.greeks_history_error", error=str(e))
        return {"error": str(e), "rows": []}


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


@app.post("/api/options/positions/{position_id}/close")
async def manual_close_option_position(position_id: str, body: dict, token: str = ""):
    """
    Manually record an option position closure or roll.

    Writes a 'closed' event to option_trade_log immediately (so the EOD
    report captures it the same day), then marks option_positions as
    closed/rolled/expired.

    Body:
        contract_price      float   closing contract price
        close_reason        str     'closed' | 'rolled' | 'expired'
        notes               str?    optional free-text
        new_contract_symbol str?    optional, for roll documentation
    """
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")

    pool   = await _get_db_pool()
    pos_id = uuid.UUID(position_id)

    pos = await pool.fetchrow(
        """SELECT id, underlying, contract_symbol, entry_price, qty, status
           FROM option_positions WHERE id=$1""",
        pos_id,
    )
    if not pos:
        raise HTTPException(404, "Position not found")
    if pos["status"] != "active":
        raise HTTPException(400, f"Position already {pos['status']}")

    close_reason = body.get("close_reason", "closed")
    if close_reason not in ("closed", "rolled", "expired"):
        raise HTTPException(400, "close_reason must be closed, rolled, or expired")

    contract_price = float(body.get("contract_price") or 0)
    notes          = str(body.get("notes") or "").strip()
    new_sym        = str(body.get("new_contract_symbol") or "").strip()
    if new_sym:
        notes = f"Rolled into {new_sym}. {notes}".strip(". ").strip()
    if not notes:
        notes = f"Manual {close_reason}"

    # Short option convention: profit = premium collected − cost to close
    entry_price  = float(pos["entry_price"]) if pos["entry_price"] else 0.0
    qty          = abs(float(pos["qty"])) if pos["qty"] else 1.0
    realized_pnl = round((entry_price - contract_price) * qty * 100, 2)

    new_status = "rolled" if close_reason == "rolled" else close_reason

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO option_trade_log
                   (position_id, contract_symbol, underlying, event_type,
                    contract_price, realized_pnl, notes)
                   VALUES ($1,$2,$3,'closed',$4::NUMERIC,$5::NUMERIC,$6)""",
                pos_id, pos["contract_symbol"], pos["underlying"],
                contract_price, realized_pnl, notes,
            )
            await conn.execute(
                """UPDATE option_positions
                   SET status=$2, closed_at=NOW(), close_reason=$3,
                       total_realized_pnl=$4, updated_at=NOW()
                   WHERE id=$1""",
                pos_id, new_status, close_reason, realized_pnl,
            )

    # Update Redis accumulator so the circuit-breaker limit check stays current
    try:
        from shared.risk_controls import record_trade_pnl as _rec_pnl
        await _rec_pnl(await get_redis(), realized_pnl)
    except Exception:
        pass

    log.info("options.manual_close",
             contract=pos["contract_symbol"], underlying=pos["underlying"],
             reason=close_reason, price=contract_price, pnl=realized_pnl)

    return {
        "ok":             True,
        "position_id":    position_id,
        "status":         new_status,
        "contract_price": contract_price,
        "realized_pnl":   realized_pnl,
    }


def _ev_label(event_type: str) -> str:
    return {
        "imported":        "Open",
        "alert_roll_1":    "Roll 1",
        "alert_roll_2":    "Roll 2",
        "alert_roll_3":    "Roll 3",
        "alert_roll_extra":"Roll",
        "alert_emergency": "Emergency Exit",
        "alert_exit":      "Exit Alert",
        "closed":          "Closed",
        "expired":         "Expired",
    }.get(event_type, event_type)


@app.get("/api/options/log/summary")
async def get_options_log_summary():
    """Top-level P&L summary with per-position event snapshots for the tree view."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT
            p.id,
            p.underlying,
            p.account_label,
            p.account_name,
            p.broker,
            p.entry_price,
            p.qty,
            p.entry_date,
            p.closed_at,
            p.status,
            p.total_realized_pnl,
            p.option_type,
            p.strike,
            p.expiration_date,
            p.contract_symbol,
            p.close_reason,
            COALESCE(
                (p.closed_at::date - p.entry_date),
                (CURRENT_DATE - p.entry_date)
            ) AS days_in_trade
        FROM option_positions p
        WHERE p.status IN ('closed','rolled','expired','active')
          AND p.option_type IN ('call','put')
        ORDER BY p.broker, p.account_label, p.underlying, p.entry_date ASC
        LIMIT 500
        """
    )

    # Fetch all key events for these positions in one query
    pos_ids = [r["id"] for r in rows]
    events_by_pos: dict = {}
    if pos_ids:
        ev_rows = await pool.fetch(
            """
            SELECT position_id, ts, event_type, underlying_price, contract_price,
                   atr_value, distance_emergency, distance_exit_alert, distance_roll_1,
                   realized_pnl, risk_level, notes
            FROM option_trade_log
            WHERE position_id = ANY($1::uuid[])
              AND event_type NOT IN ('scan')
            ORDER BY ts ASC
            """,
            pos_ids,
        )
        for ev in ev_rows:
            pid = str(ev["position_id"])
            if pid not in events_by_pos:
                events_by_pos[pid] = []
            # Determine risk level from distances if not stored
            rl = ev["risk_level"]
            if not rl:
                de = float(ev["distance_emergency"]) if ev["distance_emergency"] else None
                if de is not None and de <= 0:
                    rl = "emergency"
                elif ev["distance_exit_alert"] and float(ev["distance_exit_alert"]) <= 0:
                    rl = "high"
                elif ev["distance_roll_1"] and float(ev["distance_roll_1"]) <= 0:
                    rl = "moderate"
                else:
                    rl = "low"
            events_by_pos[pid].append({
                "ts":               ev["ts"].strftime("%Y-%m-%d"),
                "event_type":       ev["event_type"],
                "underlying_price": float(ev["underlying_price"]) if ev["underlying_price"] else None,
                "contract_price":   float(ev["contract_price"]) if ev["contract_price"] is not None else None,
                "realized_pnl":     float(ev["realized_pnl"]) if ev["realized_pnl"] is not None else None,
                "risk_level":       rl,
                "notes":            ev["notes"],
            })

    total_pnl = 0.0
    winning = 0
    losing  = 0
    trades  = []
    ticker_pnl: dict = {}
    # Tickers that still have at least one active position are excluded from the
    # top/bottom panels so only fully-historical tickers appear there.
    active_tickers: set = {r["underlying"] for r in rows if r["status"] == "active"}

    for r in rows:
        pnl = float(r["total_realized_pnl"]) if r["total_realized_pnl"] is not None else None
        pid = str(r["id"])
        ep  = float(r["entry_price"]) if r["entry_price"] else None
        qty = float(r["qty"]) if r["qty"] else None
        cost_basis = round(ep * abs(qty) * 100, 2) if ep and qty else None

        # Compute P&L from events if not stored on position
        pos_events = events_by_pos.get(pid, [])
        if pnl is None:
            ev_pnl = sum(e["realized_pnl"] for e in pos_events if e["realized_pnl"] is not None)
            if ev_pnl != 0:
                pnl = ev_pnl
        # For active positions compute unrealised P&L from last event price
        last_contract_price = None
        for e in reversed(pos_events):
            if e["contract_price"] is not None:
                last_contract_price = e["contract_price"]
                break
        if last_contract_price is None and len(pos_events) == 0:
            # Fallback to entry price
            last_contract_price = ep

        # Cost basis chain: entry → each roll/close event price
        milestones = []
        # Entry milestone
        if ep:
            milestones.append({
                "label": "Open",
                "date":  r["entry_date"].isoformat() if r["entry_date"] else None,
                "contract_price": ep,
                "cost_basis": cost_basis,
                "realized_pnl": None,
                "risk_level": "low",
                "event_type": "imported",
            })
        for e in pos_events:
            et = e["event_type"]
            is_key = et in ("imported", "closed", "expired",
                            "alert_roll_1", "alert_roll_2", "alert_roll_3", "alert_roll_extra",
                            "alert_emergency", "alert_exit")
            if is_key:
                cp = e["contract_price"]
                rpnl = e["realized_pnl"]
                if rpnl is None and cp and ep and qty:
                    # Short option convention: profit = premium collected − cost to close
                    rpnl = round((ep - cp) * abs(qty) * 100, 2)
                # Running ROC at this milestone: P&L so far as % of cost basis
                running_roc = round(rpnl / cost_basis * 100, 1) if (rpnl is not None and cost_basis) else None
                milestones.append({
                    "label":          _ev_label(et),
                    "date":           e["ts"],
                    "contract_price": cp,
                    "cost_basis":     cost_basis,
                    "realized_pnl":   rpnl,
                    "running_roc":    running_roc,
                    "risk_level":     e["risk_level"],
                    "event_type":     et,
                })

        roc_pct = round(pnl / cost_basis * 100, 1) if (pnl is not None and cost_basis) else None
        trades.append({
            "id":             pid,
            "underlying":     r["underlying"],
            "account_label":  r["account_label"],
            "account_name":   r["account_name"] or r["account_label"],
            "broker":         r["broker"],
            "status":         r["status"],
            "option_type":    r["option_type"],
            "strike":         float(r["strike"]) if r["strike"] else None,
            "expiration_date":r["expiration_date"].isoformat() if r["expiration_date"] else None,
            "contract_symbol":r["contract_symbol"],
            "close_reason":   r["close_reason"],
            "entry_price":    ep,
            "qty":            qty,
            "cost_basis":     cost_basis,
            "entry_date":     r["entry_date"].isoformat() if r["entry_date"] else None,
            "closed_at":      r["closed_at"].isoformat() if r["closed_at"] else None,
            "days_in_trade":  int(r["days_in_trade"]) if r["days_in_trade"] is not None else None,
            "realized_pnl":   round(pnl, 2) if pnl is not None else None,
            "roc_pct":        roc_pct,
            "milestones":     milestones,
        })

        if pnl is not None:
            total_pnl += pnl
            if pnl > 0:
                winning += 1
            elif pnl < 0:
                losing += 1
            # Only fully-historical tickers (no remaining active leg) count
            if r["status"] not in ("active",) and r["underlying"] not in active_tickers:
                sym = r["underlying"]
                if sym not in ticker_pnl:
                    ticker_pnl[sym] = {"underlying": sym, "total_pnl": 0.0, "count": 0}
                ticker_pnl[sym]["total_pnl"] += pnl
                ticker_pnl[sym]["count"] += 1

    # Roll-chain risk reduction: credits banked from all prior closed legs on same underlying/account/type
    # reduce the net risk on each subsequent position. No status='rolled' required — close+reopen counts.
    _chain_groups: dict = {}
    for t in trades:
        k = (t["broker"], t["account_label"], t["underlying"], t["option_type"])
        if k not in _chain_groups:
            _chain_groups[k] = []
        _chain_groups[k].append(t)
    for grp in _chain_groups.values():
        grp.sort(key=lambda x: x["entry_date"] or "")
        cum_credits = 0.0
        for pos in grp:
            cb = pos["cost_basis"]
            if cb:
                net_risk = cb - cum_credits
                pos["gross_risk"]         = cb
                pos["cumulative_credits"] = round(cum_credits, 2)
                pos["net_risk"]           = round(net_risk, 2)
                pos["risk_offset_pct"]    = round(cum_credits / cb * 100, 1)
                pos["house_money"]        = net_risk <= 0
            if pos["status"] in ("closed", "rolled", "expired") and pos.get("realized_pnl") is not None:
                cum_credits += pos["realized_pnl"]

    # Top 5 / bottom 5 — only include tickers with strictly positive / negative net P&L
    all_ticker_list = list(ticker_pnl.values())
    top_tickers    = sorted([t for t in all_ticker_list if t["total_pnl"] > 0],  key=lambda x: x["total_pnl"], reverse=True)[:5]
    bottom_tickers = sorted([t for t in all_ticker_list if t["total_pnl"] < 0],  key=lambda x: x["total_pnl"])[:5]
    for t in top_tickers + bottom_tickers:
        t["total_pnl"] = round(t["total_pnl"], 2)

    closed_count = winning + losing
    closed_trades   = [t for t in trades if t["days_in_trade"] is not None and t["status"] != "active"]
    avg_dit         = round(sum(t["days_in_trade"] for t in closed_trades) / len(closed_trades), 1) if closed_trades else None
    # Capital efficiency = realized P&L / cost basis of closed trades only.
    # Excluding active positions ensures the denominator reflects capital that has
    # cycled through a full open→close journey, giving a meaningful ROC metric.
    total_cost_basis = sum(t["cost_basis"] for t in trades
                          if t["cost_basis"] and t["status"] != "active")
    cap_eff         = round(total_pnl / total_cost_basis * 100, 1) if total_cost_basis else None
    active_count = sum(1 for t in trades if t["status"] == "active")
    return {
        "total_positions":    len(trades),
        "active_count":       active_count,
        "closed_count":       closed_count,
        "winning_trades":     winning,
        "losing_trades":      losing,
        "win_rate":           round(winning / closed_count * 100, 1) if closed_count else 0.0,
        "total_pnl":          round(total_pnl, 2),
        "avg_days_in_trade":  avg_dit,
        "capital_efficiency": cap_eff,
        "top_tickers":        top_tickers,
        "bottom_tickers":     bottom_tickers,
        "trades":             trades,
    }


@app.get("/api/options/log/accounts")
async def get_options_log_by_account():
    """P&L broken down by account."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT
            account_label,
            MAX(account_name)  AS account_name,
            MAX(broker)        AS broker,
            COUNT(*)           AS total_positions,
            COUNT(*) FILTER (WHERE status IN ('closed','rolled','expired')) AS closed_count,
            SUM(total_realized_pnl)  AS total_pnl,
            COUNT(*) FILTER (WHERE total_realized_pnl > 0) AS winning,
            COUNT(*) FILTER (WHERE total_realized_pnl < 0) AS losing,
            SUM(COALESCE(entry_price,0) * ABS(COALESCE(qty,0)) * 100)
                FILTER (WHERE status IN ('closed','rolled','expired'))  AS total_cost_basis,
            ROUND(AVG(
                CASE WHEN status IN ('closed','rolled','expired')
                     THEN COALESCE(closed_at::date, CURRENT_DATE) - entry_date
                END
            ), 1) AS avg_days_in_trade
        FROM option_positions
        WHERE option_type IN ('call','put')
        GROUP BY account_label
        ORDER BY total_pnl DESC NULLS LAST
        """
    )
    result = []
    for r in rows:
        total_pnl        = round(float(r["total_pnl"]), 2) if r["total_pnl"] else 0.0
        total_cost_basis = round(float(r["total_cost_basis"]), 2) if r["total_cost_basis"] else 0.0
        capital_eff      = round(total_pnl / total_cost_basis * 100, 1) if total_cost_basis else None
        closed           = int(r["closed_count"])
        avg_risk         = round(total_cost_basis / closed, 2) if closed > 0 else None
        result.append({
            "account_label":      r["account_label"],
            "account_name":       r["account_name"] or r["account_label"],
            "broker":             r["broker"],
            "total_positions":    int(r["total_positions"]),
            "closed_count":       closed,
            "total_pnl":          total_pnl,
            "total_cost_basis":   total_cost_basis,
            "capital_efficiency": capital_eff,
            "avg_risk_per_trade": avg_risk,
            "avg_days_in_trade":  float(r["avg_days_in_trade"]) if r["avg_days_in_trade"] else None,
            "winning":            int(r["winning"]),
            "losing":             int(r["losing"]),
            "win_rate":           round(int(r["winning"]) / closed * 100, 1) if closed > 0 else 0.0,
        })
    return result


@app.get("/api/options/log/ticker/{ticker}")
async def get_options_log_ticker(ticker: str):
    """Full trade history for a specific ticker with per-event P&L and risk levels."""
    pool = await _get_db_pool()
    # All positions for this ticker
    positions = await pool.fetch(
        """
        SELECT
            p.id, p.underlying, p.contract_symbol, p.option_type, p.strike,
            p.expiration_date, p.account_label, p.account_name, p.broker,
            p.entry_price, p.qty, p.entry_date, p.status,
            p.closed_at, p.close_reason, p.total_realized_pnl,
            p.ai_analysis, p.ai_analyzed_at,
            p.level_emergency, p.level_exit_alert, p.level_roll_1,
            COALESCE(
                (p.closed_at::date - p.entry_date),
                (CURRENT_DATE - p.entry_date)
            ) AS days_in_trade
        FROM option_positions p
        WHERE p.underlying = $1
          AND p.option_type IN ('call','put')
        ORDER BY p.entry_date DESC
        """,
        ticker.upper(),
    )

    result = []
    for pos in positions:
        pos_id = pos["id"]
        # Get all log events for this position
        events = await pool.fetch(
            """
            SELECT ts, event_type, underlying_price, contract_price, atr_value,
                   distance_emergency, distance_exit_alert, distance_roll_1,
                   qty, realized_pnl, pnl_pct, risk_level, notes
            FROM option_trade_log
            WHERE position_id = $1
            ORDER BY ts ASC
            """,
            pos_id,
        )

        event_list = []
        prev_ts = None
        for ev in events:
            days_since = None
            if prev_ts:
                days_since = (ev["ts"].date() - prev_ts.date()).days
            prev_ts = ev["ts"]

            # Determine risk level if not stored
            rl = ev["risk_level"]
            if not rl:
                de = float(ev["distance_emergency"]) if ev["distance_emergency"] else None
                if de is not None and de <= 0:
                    rl = "emergency"
                elif ev["distance_exit_alert"] and float(ev["distance_exit_alert"]) <= 0:
                    rl = "high"
                elif ev["distance_roll_1"] and float(ev["distance_roll_1"]) <= 0:
                    rl = "moderate"
                else:
                    rl = "low"

            # Compute realized_pnl for close/roll events if not already stored
            rpnl = float(ev["realized_pnl"]) if ev["realized_pnl"] is not None else None
            if rpnl is None and ev["event_type"] in ("closed", "expired", "alert_roll_1", "alert_roll_2", "alert_roll_3"):
                ep = float(pos["entry_price"]) if pos["entry_price"] is not None else None
                cp = float(ev["contract_price"]) if ev["contract_price"] is not None else None
                q  = abs(float(pos["qty"])) if pos["qty"] else 1.0
                if ep is not None and cp is not None:
                    rpnl = round((ep - cp) * q * 100, 2)  # short option: profit = credit - debit
                    # Persist computed P&L back so it's not recomputed every request
                    try:
                        await pool.execute(
                            """UPDATE option_trade_log SET realized_pnl=$1
                               WHERE position_id=$2 AND ts=$3 AND realized_pnl IS NULL""",
                            rpnl, pos_id, ev["ts"],
                        )
                    except Exception:
                        pass

            event_list.append({
                "ts":                ev["ts"].isoformat(),
                "event_type":        ev["event_type"],
                "underlying_price":  float(ev["underlying_price"]) if ev["underlying_price"] else None,
                "contract_price":    float(ev["contract_price"]) if ev["contract_price"] else None,
                "atr_value":         float(ev["atr_value"]) if ev["atr_value"] else None,
                "distance_emergency":float(ev["distance_emergency"]) if ev["distance_emergency"] else None,
                "risk_level":        rl,
                "realized_pnl":      rpnl,
                "pnl_pct":           float(ev["pnl_pct"]) if ev["pnl_pct"] is not None else None,
                "days_since_prev":   days_since,
                "notes":             ev["notes"],
            })

        result.append({
            "id":               str(pos_id),
            "underlying":       pos["underlying"],
            "contract_symbol":  pos["contract_symbol"],
            "option_type":      pos["option_type"],
            "strike":           float(pos["strike"]) if pos["strike"] else None,
            "expiration_date":  pos["expiration_date"].isoformat() if pos["expiration_date"] else None,
            "account_label":    pos["account_label"],
            "account_name":     pos["account_name"] or pos["account_label"],
            "broker":           pos["broker"],
            "entry_price":      float(pos["entry_price"]) if pos["entry_price"] else None,
            "qty":              float(pos["qty"]) if pos["qty"] else None,
            "entry_date":       pos["entry_date"].isoformat() if pos["entry_date"] else None,
            "status":           pos["status"],
            "closed_at":        pos["closed_at"].isoformat() if pos["closed_at"] else None,
            "close_reason":     pos["close_reason"],
            "days_in_trade":    int(pos["days_in_trade"]) if pos["days_in_trade"] is not None else None,
            "total_realized_pnl": float(pos["total_realized_pnl"]) if pos["total_realized_pnl"] is not None else None,
            "ai_analysis":      pos["ai_analysis"],
            "ai_analyzed_at":   pos["ai_analyzed_at"].isoformat() if pos["ai_analyzed_at"] else None,
            "events":           event_list,
        })

        # Persist total_realized_pnl if not stored but computable from events
        if pos["total_realized_pnl"] is None and result and result[-1]["total_realized_pnl"] is None:
            ev_sum = sum(e["realized_pnl"] for e in event_list if e.get("realized_pnl") is not None)
            if ev_sum != 0:
                try:
                    await pool.execute(
                        """UPDATE option_positions SET total_realized_pnl=$1
                           WHERE id=$2 AND total_realized_pnl IS NULL""",
                        ev_sum, pos_id,
                    )
                    result[-1]["total_realized_pnl"] = round(ev_sum, 2)
                except Exception:
                    pass

    # Roll-chain risk reduction per account/type group — accumulate credits from all prior closed legs
    _tk_chain_groups: dict = {}
    for pos in result:
        k = (pos["account_label"], pos["option_type"])
        if k not in _tk_chain_groups:
            _tk_chain_groups[k] = []
        _tk_chain_groups[k].append(pos)
    for grp in _tk_chain_groups.values():
        grp.sort(key=lambda x: x["entry_date"] or "")
        cum_credits = 0.0
        for pos in grp:
            ep  = pos["entry_price"]
            qty = pos["qty"]
            cb  = round(ep * abs(qty) * 100, 2) if ep and qty else None
            if cb:
                net_risk = cb - cum_credits
                pos["gross_risk"]         = cb
                pos["cumulative_credits"] = round(cum_credits, 2)
                pos["net_risk"]           = round(net_risk, 2)
                pos["risk_offset_pct"]    = round(cum_credits / cb * 100, 1)
                pos["house_money"]        = net_risk <= 0
            if pos["status"] in ("closed", "rolled", "expired") and pos.get("total_realized_pnl") is not None:
                cum_credits += pos["total_realized_pnl"]

    # Ticker-level summary
    total_pnl = sum(
        p["total_realized_pnl"] for p in result
        if p["total_realized_pnl"] is not None
    )
    return {
        "ticker":          ticker.upper(),
        "positions":       result,
        "total_pnl":       round(total_pnl, 2),
        "position_count":  len(result),
    }


@app.post("/api/options/log/analyze/{position_id}")
async def analyze_option_position(position_id: str, token: str = ""):
    """Generate AI post-close analysis for a completed option position."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    pool = await _get_db_pool()
    pos = await pool.fetchrow(
        """SELECT p.*, p.ai_analysis, p.ai_analyzed_at
           FROM option_positions p WHERE p.id=$1""",
        uuid.UUID(position_id),
    )
    if not pos:
        raise HTTPException(404, "Position not found")

    # Gather events
    events = await pool.fetch(
        """SELECT ts, event_type, underlying_price, contract_price, atr_value,
                  distance_emergency, notes
           FROM option_trade_log WHERE position_id=$1 ORDER BY ts ASC""",
        uuid.UUID(position_id),
    )

    # Build context for LLM
    event_lines = []
    for ev in events:
        cp   = f"${float(ev['contract_price']):.2f}" if ev["contract_price"] else "N/A"
        up   = f"${float(ev['underlying_price']):.2f}" if ev["underlying_price"] else "N/A"
        event_lines.append(
            f"  [{ev['ts'].strftime('%Y-%m-%d')}] {ev['event_type']} | underlying={up} contract={cp}"
        )
    events_str = "\n".join(event_lines) or "  (no events)"

    # Calc rough P&L
    ep = float(pos["entry_price"]) if pos["entry_price"] else 0
    qty = abs(float(pos["qty"])) if pos["qty"] else 1
    stored_pnl = float(pos["total_realized_pnl"]) if pos["total_realized_pnl"] else None
    pnl_str = f"${stored_pnl:+.2f}" if stored_pnl is not None else "unknown"

    prompt = f"""You are an options trading coach. Analyze this completed option trade and provide concise, actionable improvement suggestions.

Position: {pos['contract_symbol']}
Underlying: {pos['underlying']}
Type: {pos['option_type']}
Strike: {pos['strike']}
Expiration: {pos['expiration_date']}
Account: {pos['account_label']} ({pos['broker']})
Entry Date: {pos['entry_date']}
Entry Price (premium): ${ep:.2f}
Qty: {qty} contracts
Status: {pos['status']}
Close Reason: {pos['close_reason'] or 'N/A'}
Realized P&L: {pnl_str}

Trade Events:
{events_str}

Based on this trade history, provide:
1. What worked well in this trade
2. What could have been done better (entry timing, strike selection, exit management)
3. Specific actionable suggestions for the next similar trade on {pos['underlying']}
4. Risk management observations (ATR levels, position sizing)

Keep your analysis concise (4-6 sentences per section). Focus on practical improvements."""

    # Use the existing AI infrastructure
    analysis_text = None
    try:
        _redis = await get_redis()
        # Try Claude via the AI broker if available
        import anthropic as _anthropic
        _client = _anthropic.Anthropic()
        msg = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis_text = msg.content[0].text if msg.content else None
    except Exception as exc:
        analysis_text = f"Analysis unavailable: {exc}"

    if analysis_text:
        await pool.execute(
            """UPDATE option_positions
               SET ai_analysis=$1, ai_analyzed_at=NOW()
               WHERE id=$2""",
            analysis_text, uuid.UUID(position_id),
        )

    return {"position_id": position_id, "analysis": analysis_text}


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


# ── Portfolio Optimization ────────────────────────────────────────────────────

@app.post("/api/portfolio/optimize")
async def portfolio_optimize(body: dict):
    """
    Run portfolio optimization (MVO / Risk Parity / Inv-Vol / Max-Div).

    Body fields:
      tickers        list[str]   required, 2–30 symbols
      method         str         one of: max_sharpe, min_variance, risk_parity, equal_vol, max_div
      total_capital  float       dollars to allocate (default 10000)
      lookback_days  int         trading days of history (default 252)
      risk_free_rate float       annualized (default 0.045)
      max_weight     float       max single-asset weight 0–1 (default 1.0 = uncapped)
    """
    # Auth is handled by the HTTP middleware (query ?token= or session cookie).
    tickers = body.get("tickers", [])
    if not tickers or len(tickers) < 2:
        raise HTTPException(400, "Need at least 2 tickers")

    from .portfolio_optimizer import optimize

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: optimize(
                tickers       = tickers,
                method        = body.get("method",         "max_sharpe"),
                total_capital = float(body.get("total_capital", 10_000)),
                lookback_days = int(body.get("lookback_days",   252)),
                risk_free_rate= float(body.get("risk_free_rate", 0.045)),
                max_weight    = float(body.get("max_weight",     1.0)),
            ),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("portfolio_optimize.error", error=str(e))
        raise HTTPException(500, f"Optimization failed: {e}")

    return result


@app.get("/api/portfolio/signals-tickers")
async def portfolio_signals_tickers(token: str = ""):
    """Return unique tickers from the 50 most recent predictor signals."""
    check_token(token)
    redis   = await get_redis()
    entries = await redis.xrevrange(STREAMS["signals"], "+", "-", count=50)
    seen, tickers = set(), []
    for _eid, fields in entries:
        t = (fields.get("ticker") or "").upper().strip()
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)
    return {"tickers": tickers}


# ── Portfolio NAV history ─────────────────────────────────────────────────────

@app.post("/api/portfolio/snapshot")
async def capture_portfolio_snapshot(token: str = ""):
    """Capture current broker account balances as an EOD NAV snapshot."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    import datetime as _dt
    try:
        broker_data = await _fetch_positions_from_gateway()
    except Exception as e:
        raise HTTPException(503, detail=f"Broker gateway unavailable: {e}")
    pool  = await _get_db_pool()
    today = _dt.date.today()
    saved = 0
    for acct in broker_data.get("accounts", []):
        bal   = acct.get("balances", {})
        label = acct.get("label", "")
        mode  = acct.get("mode", "live")
        broker = acct.get("broker", "")
        try:
            total_nav    = float(
                bal.get("portfolio_value")   # Alpaca
                or bal.get("net_value")       # Webull
                or bal.get("total_equity")    # Tradier (equity=0 on margin accounts)
                or bal.get("account_value")   # Tradier alt
                or bal.get("equity")          # Alpaca secondary
                or bal.get("total_value")     # generic
                or 0
            )
            cash         = float(bal.get("cash") or bal.get("buying_power") or 0)
            equity_value = float(bal.get("long_market_value") or bal.get("market_value") or 0)
            day_pnl      = float(bal.get("unrealized_pl") or bal.get("day_pl") or bal.get("pnl") or 0)
        except Exception:
            continue
        if total_nav <= 0:
            continue
        await pool.execute(
            """INSERT INTO portfolio_snapshots
               (snapshot_date, account_label, broker, mode, total_nav, cash, equity_value, day_pnl)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (snapshot_date, account_label)
               DO UPDATE SET total_nav=$5, cash=$6, equity_value=$7, day_pnl=$8""",
            today, label, broker, mode, total_nav, cash, equity_value, day_pnl,
        )
        saved += 1
    return {"ok": True, "saved": saved, "date": today.isoformat()}



@app.get("/api/portfolio/nav-history")
async def get_portfolio_nav_history(
    days: int = 90,
    mode: str = "live",
    account: str = "",
):
    """Return daily NAV history aggregated across all accounts (or a single account)."""
    pool = await _get_db_pool()
    if account:
        rows = await pool.fetch(
            """SELECT snapshot_date, account_label, total_nav, cash, equity_value, day_pnl
               FROM portfolio_snapshots
               WHERE mode = $1 AND account_label = $2
                 AND snapshot_date >= CURRENT_DATE - ($3 || ' days')::INTERVAL
               ORDER BY snapshot_date ASC""",
            mode, account, str(days),
        )
        series = [
            {
                "date":          r["snapshot_date"].isoformat(),
                "account_label": r["account_label"],
                "total_nav":     float(r["total_nav"]),
                "cash":          float(r["cash"] or 0),
                "equity_value":  float(r["equity_value"] or 0),
                "day_pnl":       float(r["day_pnl"] or 0),
            }
            for r in rows
        ]
    else:
        rows = await pool.fetch(
            """SELECT snapshot_date,
                      SUM(total_nav)    AS total_nav,
                      SUM(cash)         AS cash,
                      SUM(equity_value) AS equity_value,
                      SUM(day_pnl)      AS day_pnl
               FROM portfolio_snapshots
               WHERE mode = $1
                 AND snapshot_date >= CURRENT_DATE - ($2 || ' days')::INTERVAL
               GROUP BY snapshot_date
               ORDER BY snapshot_date ASC""",
            mode, str(days),
        )
        series = [
            {
                "date":         r["snapshot_date"].isoformat(),
                "total_nav":    float(r["total_nav"]),
                "cash":         float(r["cash"] or 0),
                "equity_value": float(r["equity_value"] or 0),
                "day_pnl":      float(r["day_pnl"] or 0),
            }
            for r in rows
        ]

    # Compute running drawdown
    peak = 0.0
    for pt in series:
        nav = pt["total_nav"]
        if nav > peak:
            peak = nav
        pt["drawdown_pct"] = round((peak - nav) / peak * 100, 2) if peak > 0 else 0.0

    return {"series": series, "count": len(series)}


# ── Feature 1: Intraday portfolio NAV snapshot + pruning ─────────────────────

@app.post("/api/portfolio/intraday-snapshot")
async def capture_intraday_snapshot(token: str = ""):
    """Capture current broker NAV as an intraday snapshot (called every 30m during market hours)."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    try:
        broker_data = await _fetch_positions_from_gateway()
    except Exception as e:
        raise HTTPException(503, detail=f"Broker gateway unavailable: {e}")
    pool  = await _get_db_pool()
    saved = 0
    for acct in broker_data.get("accounts", []):
        bal   = acct.get("balances", {})
        label = acct.get("label", "")
        mode  = acct.get("mode", "live")
        broker = acct.get("broker", "")
        try:
            total_nav = float(
                bal.get("portfolio_value") or bal.get("net_value") or
                bal.get("total_equity") or bal.get("account_value") or
                bal.get("equity") or bal.get("total_value") or 0
            )
            cash         = float(bal.get("cash") or bal.get("buying_power") or 0)
            equity_value = float(bal.get("long_market_value") or bal.get("market_value") or 0)
            day_pnl      = float(bal.get("unrealized_pl") or bal.get("day_pl") or bal.get("pnl") or 0)
        except Exception:
            continue
        if total_nav <= 0:
            continue
        await pool.execute(
            """INSERT INTO portfolio_intraday_snapshots
               (account_label, broker, mode, total_nav, cash, equity_value, day_pnl, bucket)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'raw')""",
            label, broker, mode, total_nav, cash, equity_value, day_pnl,
        )
        saved += 1
    return {"ok": True, "saved": saved}


@app.post("/api/portfolio/prune-history")
async def prune_portfolio_history(token: str = ""):
    """Compress and prune intraday NAV snapshots using tiered retention."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    pool = await _get_db_pool()
    deleted = 0

    # Keep full resolution for last 24h — delete raw rows older than 24h that don't fall on 15-min boundaries
    r1 = await pool.execute("""
        DELETE FROM portfolio_intraday_snapshots
        WHERE bucket = 'raw'
          AND ts < NOW() - INTERVAL '24 hours'
          AND EXTRACT(MINUTE FROM ts)::int NOT IN (0,15,30,45)
    """)
    deleted += int(r1.split()[-1]) if r1 else 0

    # Keep 15-min resolution for last 7 days — delete 15min rows older than 7 days that aren't on the hour
    r2 = await pool.execute("""
        DELETE FROM portfolio_intraday_snapshots
        WHERE bucket IN ('raw','15min')
          AND ts < NOW() - INTERVAL '7 days'
          AND EXTRACT(MINUTE FROM ts)::int != 0
    """)
    deleted += int(r2.split()[-1]) if r2 else 0

    # Delete all intraday rows older than 30 days (daily portfolio_snapshots handle longer history)
    r3 = await pool.execute("""
        DELETE FROM portfolio_intraday_snapshots
        WHERE ts < NOW() - INTERVAL '30 days'
    """)
    deleted += int(r3.split()[-1]) if r3 else 0

    return {"ok": True, "deleted": deleted}


@app.get("/api/portfolio/intraday-nav")
async def get_intraday_nav(
    hours: int = 8,
    mode: str  = "live",
    account: str = "",
):
    """Return intraday NAV series for the last N hours (up to 30 days)."""
    hours = min(max(hours, 1), 720)
    pool  = await _get_db_pool()
    where = "mode = $1 AND ts >= NOW() - ($2 || ' hours')::INTERVAL"
    args  = [mode, str(hours)]
    if account:
        where += " AND account_label = $3"
        args.append(account)
    rows = await pool.fetch(
        f"""SELECT ts, account_label, total_nav, cash, equity_value, day_pnl
            FROM portfolio_intraday_snapshots
            WHERE {where}
            ORDER BY ts ASC""",
        *args,
    )
    series = [
        {
            "ts":            r["ts"].isoformat(),
            "account_label": r["account_label"],
            "total_nav":     float(r["total_nav"]),
            "cash":          float(r["cash"] or 0),
            "equity_value":  float(r["equity_value"] or 0),
            "day_pnl":       float(r["day_pnl"] or 0),
        }
        for r in rows
    ]
    # If per-account=False, aggregate across accounts by timestamp
    if not account and series:
        from collections import defaultdict
        by_ts: dict = defaultdict(lambda: {"total_nav": 0, "cash": 0, "equity_value": 0, "day_pnl": 0})
        for pt in series:
            by_ts[pt["ts"]]["total_nav"]     += pt["total_nav"]
            by_ts[pt["ts"]]["cash"]          += pt["cash"]
            by_ts[pt["ts"]]["equity_value"]  += pt["equity_value"]
            by_ts[pt["ts"]]["day_pnl"]       += pt["day_pnl"]
        series = [{"ts": ts, **vals} for ts, vals in sorted(by_ts.items())]
    return {"series": series, "count": len(series)}


# ── Feature 3: ETF capital flow ───────────────────────────────────────────────

@app.get("/api/market/etf-flows")
async def get_etf_flows(category: str = "", refresh: bool = False):
    """Return latest ETF capital flow snapshot (Redis-cached, refreshed by scraper)."""
    _redis = await get_redis()
    if not refresh:
        cached = await _redis.get("etf_flows:latest")
        if cached:
            rows = json.loads(cached)
            if category:
                rows = [r for r in rows if r.get("category") == category]
            return {"flows": rows, "count": len(rows), "source": "cache"}

    # Fallback: query DB for latest rows per ticker
    pool = await _get_db_pool()
    db_rows = await pool.fetch(
        """SELECT DISTINCT ON (ticker)
               ticker, name, category, price, volume, dollar_volume,
               avg_volume_30d, flow_ratio, change_pct, ts
           FROM etf_flow_snapshots
           ORDER BY ticker, ts DESC"""
    )
    flows = [
        {
            "ticker":        r["ticker"],
            "name":          r["name"],
            "category":      r["category"],
            "price":         float(r["price"] or 0),
            "volume":        int(r["volume"] or 0),
            "dollar_volume": float(r["dollar_volume"] or 0),
            "avg_volume_30d": int(r["avg_volume_30d"] or 0),
            "flow_ratio":    float(r["flow_ratio"] or 1),
            "change_pct":    float(r["change_pct"] or 0),
            "ts":            r["ts"].isoformat(),
        }
        for r in db_rows
    ]
    if category:
        flows = [f for f in flows if f.get("category") == category]
    return {"flows": flows, "count": len(flows), "source": "db"}


@app.get("/api/market/etf-flows/history")
async def get_etf_flow_history(ticker: str, days: int = 30):
    """Return flow_ratio history for a single ETF."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT ts, flow_ratio, change_pct, price, volume
           FROM etf_flow_snapshots
           WHERE ticker = $1 AND ts >= NOW() - ($2 || ' days')::INTERVAL
           ORDER BY ts ASC""",
        ticker.upper(), str(days),
    )
    return {
        "ticker": ticker.upper(),
        "series": [
            {"ts": r["ts"].isoformat(), "flow_ratio": float(r["flow_ratio"] or 1),
             "change_pct": float(r["change_pct"] or 0), "price": float(r["price"] or 0)}
            for r in rows
        ],
    }


# ── Feature 4: Macro regime ───────────────────────────────────────────────────

@app.get("/api/market/macro-regime")
async def get_macro_regime(history: bool = False):
    """Return latest macro regime snapshot."""
    _redis = await get_redis()
    cached = await _redis.get("macro_regime:latest")
    if cached and not history:
        return {"regime": json.loads(cached), "source": "cache"}

    pool = await _get_db_pool()
    if history:
        rows = await pool.fetch(
            """SELECT ts, regime, bull_signals, bear_signals, regime_score, spy_trend,
                      vix_level, dxy_trend, tlt_trend, breadth_pct
               FROM macro_regime_snapshots
               WHERE ts >= NOW() - INTERVAL '30 days'
               ORDER BY ts ASC"""
        )
        return {
            "history": [
                {
                    "ts":           r["ts"].isoformat(),
                    "regime":       r["regime"],
                    "bull_signals": r["bull_signals"],
                    "bear_signals": r["bear_signals"],
                    "regime_score": float(r["regime_score"] or 0),
                    "spy_trend":    r["spy_trend"],
                    "vix_level":    float(r["vix_level"] or 0) if r["vix_level"] else None,
                    "dxy_trend":    r["dxy_trend"],
                    "tlt_trend":    r["tlt_trend"],
                    "breadth_pct":  float(r["breadth_pct"] or 0) if r["breadth_pct"] else None,
                }
                for r in rows
            ]
        }

    row = await pool.fetchrow(
        """SELECT ts, regime, bull_signals, bear_signals, total_signals, regime_score,
                  spy_trend, vix_level, dxy_trend, tlt_trend, breadth_pct, raw
           FROM macro_regime_snapshots
           ORDER BY ts DESC LIMIT 1"""
    )
    if not row:
        return {"regime": None}
    return {
        "regime": {
            "ts":            row["ts"].isoformat(),
            "regime":        row["regime"],
            "bull_signals":  row["bull_signals"],
            "bear_signals":  row["bear_signals"],
            "total_signals": row["total_signals"],
            "regime_score":  float(row["regime_score"] or 0),
            "spy_trend":     row["spy_trend"],
            "vix_level":     float(row["vix_level"] or 0) if row["vix_level"] else None,
            "dxy_trend":     row["dxy_trend"],
            "tlt_trend":     row["tlt_trend"],
            "breadth_pct":   float(row["breadth_pct"] or 0) if row["breadth_pct"] else None,
            "raw":           row["raw"] or {},
        },
        "source": "db",
    }


@app.get("/api/market/ml-regime")
async def get_ml_regime(token: str = "", bust: bool = False):
    """
    ML-based SPX bull/bear market regime classifier.
    Trains GradientBoosting on 6yr weekly SPX with 7 features (4w/13w/26w/52w returns,
    12w realized vol, RSI-14, cumulative level). Label: +5% in next 13 weeks = bull.
    Cached 6h. Use bust=true to force re-training.
    """
    check_token(token)
    if bust:
        try:
            _r = await get_redis()
            await _r.delete("market:ml_regime:latest")
        except Exception:
            pass
    return await _detect_market_regime()


@app.get("/api/market/macro-news")
async def get_macro_news(limit: int = 15):
    """Fetch broad market/macro news via Polygon (SPY, QQQ). Cached 15 min."""
    _redis = await get_redis()
    cached = await _redis.get("macro_news:latest")
    if cached:
        return {"articles": json.loads(cached)[:limit], "source": "cache"}

    from shared.data_client import DataClient
    seen_urls: set = set()
    articles: list = []

    for sym in ("SPY", "QQQ"):
        try:
            news = await DataClient().news(sym, limit=15)
            for item in (news or []):
                url   = item.get("article_url", "")
                title = item.get("title", "")
                if not title or url in seen_urls:
                    continue
                seen_urls.add(url or title)
                articles.append({
                    "title":   title,
                    "summary": item.get("description", ""),
                    "url":     url,
                    "source":  sym,
                    "pub_ts":  item.get("published_utc", ""),
                })
        except Exception:
            pass

    articles.sort(key=lambda x: x.get("pub_ts", ""), reverse=True)
    articles = articles[:30]
    if articles:
        await _redis.setex("macro_news:latest", 900, json.dumps(articles))
    return {"articles": articles[:limit], "source": "live"}


# ── Feature 5: News sentiment ─────────────────────────────────────────────────

@app.get("/api/market/news-sentiment")
async def get_news_sentiment(category: str = "", ticker: str = "", limit: int = 20, days: int = 1):
    """Return latest news sentiment articles. days=1 uses Redis cache; days>1 queries DB directly."""
    days = max(1, min(days, 30))
    limit = max(1, min(limit, 5000))
    _redis = await get_redis()
    if days == 1 and not ticker:
        cached = await _redis.get("news_sentiment:latest")
        if cached:
            by_cat = json.loads(cached)
            if category and category in by_cat:
                return {"articles": by_cat[category][:limit], "source": "cache"}
            elif not category:
                merged = []
                for arts in by_cat.values():
                    merged.extend(arts)
                merged.sort(key=lambda x: x.get("ts", ""), reverse=True)
                return {"articles": merged[:limit], "source": "cache"}

    pool  = await _get_db_pool()
    where = f"ts >= NOW() - INTERVAL '{days} days'"
    args: list = []
    if category:
        where += f" AND category = ${len(args)+1}"
        args.append(category)
    if ticker:
        where += f" AND ticker = ${len(args)+1}"
        args.append(ticker.upper())
    rows = await pool.fetch(
        f"""SELECT ts, category, ticker, title, source, url, overall_score, relevance_score, topics
            FROM news_sentiment_snapshots
            WHERE {where}
            ORDER BY ts DESC LIMIT ${ len(args)+1 }""",
        *args, limit,
    )
    return {
        "articles": [
            {
                "ts":              r["ts"].isoformat(),
                "category":        r["category"],
                "ticker":          r["ticker"],
                "title":           r["title"],
                "source":          r["source"],
                "url":             r["url"],
                "overall_score":   float(r["overall_score"] or 0),
                "relevance_score": float(r["relevance_score"] or 0),
                "topics":          r["topics"] or [],
            }
            for r in rows
        ],
        "source": "db",
    }


@app.get("/api/sentiment/eodhd-news")
async def get_eodhd_news(ticker: str = "", hours: int = 48, limit: int = 20):
    """Return EODHD per-ticker news articles with native + LLM sentiment."""
    limit = min(limit, 100)
    _redis = await get_redis()

    # Fast path: Redis cache for single-ticker recent requests
    if ticker:
        ticker = ticker.upper()
        cached = await _redis.get(f"eodhd_news:{ticker}")
        if cached:
            items = json.loads(cached)
            return {"ticker": ticker, "articles": items[:limit], "source": "cache"}

    pool = await _get_db_pool()
    where_parts = [f"published_at >= NOW() - INTERVAL '{hours} hours'"]
    args: list = []
    if ticker:
        args.append(ticker)
        where_parts.append(f"ticker = ${len(args)}")

    where = " AND ".join(where_parts)
    args.append(limit)
    rows = await pool.fetch(
        f"""SELECT ticker, title, url, published_at, source_name,
                   polarity, pos_score, neg_score, neu_score,
                   llm_summary, llm_keywords, scraped_at
            FROM eodhd_news
            WHERE {where}
            ORDER BY published_at DESC
            LIMIT ${len(args)}""",
        *args,
    )
    articles = [
        {
            "ticker":       r["ticker"],
            "title":        r["title"],
            "url":          r["url"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "source":       r["source_name"],
            "polarity":     float(r["polarity"] or 0),
            "pos":          float(r["pos_score"] or 0),
            "neg":          float(r["neg_score"] or 0),
            "neu":          float(r["neu_score"] or 0),
            "llm_summary":  r["llm_summary"],
            "llm_keywords": r["llm_keywords"] or [],
            "scraped_at":   r["scraped_at"].isoformat() if r["scraped_at"] else None,
        }
        for r in rows
    ]
    return {"ticker": ticker or None, "articles": articles, "source": "db"}


# ── Alpha Vantage per-ticker structured sentiment ─────────────────────────────

@app.get("/api/sentiment/av-ticker")
async def get_av_ticker_sentiment(ticker: str = "", hours: int = 72, limit: int = 20):
    """Return Alpha Vantage per-ticker structured sentiment rows (relevance + sentiment score)."""
    limit = min(limit, 100)
    _redis = await get_redis()

    if ticker:
        ticker = ticker.upper()
        cache_key = f"av_sentiment:{ticker}"
        cached = await _redis.get(cache_key)
        if cached:
            return {"ticker": ticker, "articles": json.loads(cached)[:limit], "source": "cache"}

    pool = await _get_db_pool()
    where_parts = [f"scraped_at >= NOW() - INTERVAL '{hours} hours'"]
    args: list = []
    if ticker:
        args.append(ticker)
        where_parts.append(f"ticker = ${len(args)}")
    where = " AND ".join(where_parts)
    args.append(limit)
    rows = await pool.fetch(
        f"""SELECT ticker, title, url, time_published, source,
                   overall_sentiment_label, overall_sentiment_score,
                   ticker_relevance_score, ticker_sentiment_score, ticker_sentiment_label,
                   summary, scraped_at
            FROM av_ticker_sentiment
            WHERE {where}
            ORDER BY time_published DESC NULLS LAST
            LIMIT ${len(args)}""",
        *args,
    )
    articles = [
        {
            "ticker":                  r["ticker"],
            "title":                   r["title"],
            "url":                     r["url"],
            "time_published":          r["time_published"].isoformat() if r["time_published"] else None,
            "source":                  r["source"],
            "overall_sentiment_label": r["overall_sentiment_label"],
            "overall_sentiment_score": float(r["overall_sentiment_score"] or 0),
            "relevance_score":         float(r["ticker_relevance_score"] or 0),
            "ticker_sentiment_score":  float(r["ticker_sentiment_score"] or 0),
            "ticker_sentiment_label":  r["ticker_sentiment_label"],
            "summary":                 r["summary"],
        }
        for r in rows
    ]
    return {"ticker": ticker or None, "articles": articles, "source": "db"}


# ── Market Data Gateway ───────────────────────────────────────────────────────

@app.get("/api/market-data/health")
async def get_market_data_health():
    """Proxy the market-data gateway /health endpoint."""
    import aiohttp as _aiohttp
    gw = os.getenv("MARKET_DATA_URL", "http://ot-market-data:8090")
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(f"{gw}/health", timeout=_aiohttp.ClientTimeout(total=5)) as r:
                return await r.json()
    except Exception as e:
        return {"error": str(e)}


# ── Finnhub insider transactions + sentiment ──────────────────────────────────

@app.get("/api/market/insider/{ticker}")
async def get_insider_data(ticker: str):
    """Return insider transactions and monthly MSPR sentiment for a ticker."""
    ticker = ticker.upper()
    _redis = await get_redis()

    cached = await _redis.get(f"insider:{ticker}")
    if cached:
        data = json.loads(cached)
        return {"ticker": ticker, **data, "source": "cache"}

    pool = await _get_db_pool()
    tx_rows = await pool.fetch(
        """SELECT name, share, change, filing_date, transaction_date,
                  transaction_code, transaction_price
           FROM insider_transactions
           WHERE ticker = $1
           ORDER BY transaction_date DESC NULLS LAST
           LIMIT 30""",
        ticker,
    )
    sent_rows = await pool.fetch(
        """SELECT year, month, change, mspr
           FROM insider_sentiment
           WHERE ticker = $1
           ORDER BY year DESC, month DESC
           LIMIT 12""",
        ticker,
    )

    _CODES = {
        "P": "Purchase", "S": "Sale", "A": "Award", "D": "Disposition",
        "F": "Tax Withholding", "G": "Gift", "M": "Option Exercise",
        "X": "Exercise & Sale", "C": "Conversion",
    }

    transactions = [
        {
            "name":              r["name"],
            "share":             r["share"],
            "change":            r["change"],
            "filing_date":       r["filing_date"].isoformat() if r["filing_date"] else None,
            "transaction_date":  r["transaction_date"].isoformat() if r["transaction_date"] else None,
            "transaction_code":  r["transaction_code"],
            "transaction_label": _CODES.get(r["transaction_code"] or "", "Other"),
            "transaction_price": float(r["transaction_price"] or 0) if r["transaction_price"] else None,
        }
        for r in tx_rows
    ]
    sentiment = [
        {
            "year":   r["year"],
            "month":  r["month"],
            "change": r["change"],
            "mspr":   float(r["mspr"] or 0),
        }
        for r in sent_rows
    ]

    # Net shares bought (purchases minus sales across all transactions)
    net = sum(
        (r["change"] or 0) if r["transaction_code"] == "P"
        else -(abs(r["change"] or 0)) if r["transaction_code"] == "S"
        else 0
        for r in tx_rows
    )

    return {
        "ticker":       ticker,
        "transactions": transactions,
        "sentiment":    sentiment,
        "net_shares":   net,
        "source":       "db",
    }


# ── Feature 6: Per-symbol technical analysis snapshots ───────────────────────

@app.get("/api/market/stock-analysis/{ticker}")
async def get_stock_analysis(ticker: str, generate: bool = False, token: str = ""):
    """Return latest stock analysis snapshot for a ticker. With generate=true+token, re-run analysis."""
    ticker = ticker.upper()
    pool   = await _get_db_pool()

    if generate and token == WEBUI_TOKEN:
        # Generate a fresh snapshot using available data
        snapshot = await _generate_stock_analysis(ticker, pool)
        if snapshot:
            await pool.execute(
                """INSERT INTO stock_analysis_snapshots
                   (ticker, signal, confidence, price, rsi, atr, support, resistance,
                    ma_50, ma_200, trend, bullish_factors, bearish_factors, raw, summary)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                ticker, snapshot["signal"], snapshot["confidence"], snapshot["price"],
                snapshot.get("rsi"), snapshot.get("atr"), snapshot.get("support"),
                snapshot.get("resistance"), snapshot.get("ma_50"), snapshot.get("ma_200"),
                snapshot.get("trend"), json.dumps(snapshot.get("bullish_factors", [])),
                json.dumps(snapshot.get("bearish_factors", [])), json.dumps(snapshot.get("raw", {})),
                snapshot.get("summary"),
            )
            return {"ticker": ticker, "analysis": snapshot, "fresh": True}

    row = await pool.fetchrow(
        """SELECT ts, signal, confidence, price, rsi, atr, support, resistance,
                  ma_50, ma_200, trend, bullish_factors, bearish_factors, summary
           FROM stock_analysis_snapshots
           WHERE ticker = $1
           ORDER BY ts DESC LIMIT 1""",
        ticker,
    )
    if not row:
        return {"ticker": ticker, "analysis": None}
    return {
        "ticker": ticker,
        "analysis": {
            "ts":              row["ts"].isoformat(),
            "signal":          row["signal"],
            "confidence":      float(row["confidence"] or 0),
            "price":           float(row["price"] or 0),
            "rsi":             float(row["rsi"] or 0) if row["rsi"] else None,
            "atr":             float(row["atr"] or 0) if row["atr"] else None,
            "support":         float(row["support"] or 0) if row["support"] else None,
            "resistance":      float(row["resistance"] or 0) if row["resistance"] else None,
            "ma_50":           float(row["ma_50"] or 0) if row["ma_50"] else None,
            "ma_200":          float(row["ma_200"] or 0) if row["ma_200"] else None,
            "trend":           row["trend"],
            "bullish_factors": json.loads(row["bullish_factors"]) if row["bullish_factors"] else [],
            "bearish_factors": json.loads(row["bearish_factors"]) if row["bearish_factors"] else [],
            "summary":         row["summary"],
        },
    }


async def _generate_stock_analysis(ticker: str, pool) -> dict | None:
    """Generate a stock analysis snapshot from predictor signals + OVTLYR data."""
    try:
        _redis = await get_redis()

        # Get latest predictor signal for this ticker
        sig_rows = await pool.fetch(
            """SELECT direction, confidence, payload AS raw
               FROM signals WHERE ticker = $1
               ORDER BY ts DESC LIMIT 3""",
            ticker,
        )

        # OVTLYR intel from Redis
        ovtlyr_raw = await _redis.hget("scanner:ovtlyr:latest", ticker)
        ovtlyr     = json.loads(ovtlyr_raw) if ovtlyr_raw else {}

        # Sentiment score
        sent_raw = await _redis.hget("sentiment:latest", ticker)
        sent     = json.loads(sent_raw) if sent_raw else {}

        # Determine signal direction
        signal     = "HOLD"
        confidence = 0.5
        bullish_factors: list[str] = []
        bearish_factors: list[str] = []
        price = 0.0
        rsi   = None

        if sig_rows:
            latest_sig = sig_rows[0]
            direction  = latest_sig["direction"]
            confidence = float(latest_sig["confidence"] or 0.5)
            _raw       = latest_sig["raw"] or {}
            raw        = json.loads(_raw) if isinstance(_raw, str) else (_raw or {})
            price      = float(raw.get("price", raw.get("entry_price", 0)))
            rsi        = raw.get("rsi")
            if direction == "long":
                signal = "Buy" if confidence > 0.75 else "Overweight"
            elif direction == "short":
                signal = "Sell" if confidence > 0.75 else "Underweight"
            else:
                signal = "Hold"

        if ovtlyr:
            ov_signal = ovtlyr.get("signal", "")
            nine_score = int(ovtlyr.get("nine_score", 0))
            if ov_signal.lower() in ("buy", "long") or nine_score >= 7:
                bullish_factors.append(f"OVTLYR nine_score={nine_score}")
            elif ov_signal.lower() in ("sell", "short") or nine_score <= 3:
                bearish_factors.append(f"OVTLYR nine_score={nine_score}")

        if sent:
            score = float(sent.get("score", 0))
            if score > 0.2:
                bullish_factors.append(f"Sentiment score={score:.2f}")
            elif score < -0.2:
                bearish_factors.append(f"Sentiment score={score:.2f}")

        # Fetch real technical indicators from Massive/yfinance
        tech = await _fetch_technical_indicators(ticker)
        if tech.get("price"):
            price = tech["price"]
        if tech.get("rsi") is not None:
            rsi = tech["rsi"]

        # Enrich factors with technical context
        if tech.get("rsi") is not None:
            r = tech["rsi"]
            if r < 30:
                bullish_factors.append(f"RSI oversold ({r:.1f})")
            elif r > 70:
                bearish_factors.append(f"RSI overbought ({r:.1f})")
        if tech.get("ma_50") and tech.get("ma_200") and tech.get("price"):
            p, m50, m200 = tech["price"], tech["ma_50"], tech["ma_200"]
            if p > m50 > m200:
                bullish_factors.append(f"Price above MA50 & MA200 ({m50:.2f} / {m200:.2f})")
            elif p < m50 < m200:
                bearish_factors.append(f"Price below MA50 & MA200 ({m50:.2f} / {m200:.2f})")

        # Fetch fundamental data (yfinance) — non-blocking, best-effort
        fund = await _fetch_fundamentals(ticker)
        if fund.get("pe_ratio"):
            if fund["pe_ratio"] > 35:
                bearish_factors.append(f"High P/E ({fund['pe_ratio']:.1f}x)")
            elif fund["pe_ratio"] < 15:
                bullish_factors.append(f"Low P/E ({fund['pe_ratio']:.1f}x)")
        if fund.get("net_insider_shares"):
            net = fund["net_insider_shares"]
            if net > 0:
                bullish_factors.append(f"Net insider buys +{net:,} shares (90d)")
            elif net < 0:
                bearish_factors.append(f"Net insider sells {net:,} shares (90d)")
        if fund.get("revenue_growth") and fund["revenue_growth"] > 20:
            bullish_factors.append(f"Revenue growth {fund['revenue_growth']:.0f}% YoY")
        elif fund.get("revenue_growth") and fund["revenue_growth"] < -5:
            bearish_factors.append(f"Revenue declining {fund['revenue_growth']:.0f}% YoY")

        # Past outcome reflections for LLM context
        past_reflections: list[str] = []
        try:
            ref_rows = await pool.fetch(
                """SELECT signal, return_pct, alpha_vs_spy, reflection
                   FROM signal_reflections WHERE ticker = $1
                   ORDER BY created_at DESC LIMIT 3""",
                ticker,
            )
            for rr in ref_rows:
                past_reflections.append(
                    f"{rr['signal']} → {float(rr['return_pct'] or 0):+.1f}% "
                    f"(α {float(rr['alpha_vs_spy'] or 0):+.1f}%): {rr['reflection']}"
                )
        except Exception:
            pass

        trend = tech.get("trend") or ("uptrend" if signal in ("Buy", "Overweight") else ("downtrend" if signal in ("Sell", "Underweight") else "sideways"))
        summary = await _generate_stock_summary(
            ticker, signal, confidence, price, rsi, trend,
            bullish_factors, bearish_factors, fund=fund,
            past_reflections=past_reflections,
        )

        return {
            "signal":          signal,
            "confidence":      round(confidence, 3),
            "price":           price,
            "rsi":             float(rsi) if rsi else None,
            "atr":             tech.get("atr"),
            "support":         tech.get("support"),
            "resistance":      tech.get("resistance"),
            "ma_50":           tech.get("ma_50"),
            "ma_200":          tech.get("ma_200"),
            "trend":           trend,
            "bullish_factors": bullish_factors,
            "bearish_factors": bearish_factors,
            "summary":         summary,
            "fundamentals":    fund,
            "raw":             {
                "ovtlyr":    ovtlyr,
                "sentiment": sent,
                "sig_count": len(sig_rows),
            },
        }
    except Exception as e:
        log.warning("stock_analysis.generate_error", ticker=ticker, error=str(e))
        return None


async def _fetch_technical_indicators(ticker: str) -> dict:
    """Fetch daily bars and compute RSI-14, MA50, MA200, ATR-14, support, resistance, price."""
    import aiohttp as _aiohttp
    from datetime import date as _date, timedelta

    sym       = ticker.upper()
    to_date   = _date.today().isoformat()
    from_date = (_date.today() - timedelta(days=280)).isoformat()  # ~200 trading days

    closes: list[float] = []
    highs:  list[float] = []
    lows:   list[float] = []

    env     = _read_env_file()
    api_key = env.get("MASSIVE_API_KEY") or os.getenv("MASSIVE_API_KEY", "")

    if api_key:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
            f"/{from_date}/{to_date}?adjusted=true&sort=asc&limit=300&apiKey={api_key}"
        )
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for b in data.get("results", []):
                            closes.append(float(b.get("c") or 0))
                            highs.append(float(b.get("h") or 0))
                            lows.append(float(b.get("l") or 0))
        except Exception as e:
            log.warning("stock_analysis.bars_error", ticker=sym, error=str(e))

    if len(closes) < 20:
        return {}

    price = closes[-1]

    # RSI-14
    rsi = None
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-14:]]
        losses = [abs(min(d, 0)) for d in deltas[-14:]]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs  = avg_gain / avg_loss
            rsi = round(100 - (100 / (1 + rs)), 2)

    # Moving averages
    ma_50  = round(sum(closes[-50:])  / min(len(closes), 50),  2) if len(closes) >= 20 else None
    ma_200 = round(sum(closes[-200:]) / min(len(closes), 200), 2) if len(closes) >= 20 else None

    # ATR-14 (using last 14 bars with negative indices)
    atr = None
    if len(closes) >= 15 and len(highs) >= 15 and len(lows) >= 15:
        true_ranges = []
        for i in range(-14, 0):
            tr = max(
                highs[i]  - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            true_ranges.append(tr)
        atr = round(sum(true_ranges) / 14, 4)

    # Support: avg of 3 lowest lows in last 60 bars
    # Resistance: avg of 3 highest highs in last 60 bars
    recent_lows  = sorted(lows[-60:])[:3]  if len(lows)  >= 20 else []
    recent_highs = sorted(highs[-60:])[-3:] if len(highs) >= 20 else []
    support    = round(sum(recent_lows)  / len(recent_lows),  2) if recent_lows  else None
    resistance = round(sum(recent_highs) / len(recent_highs), 2) if recent_highs else None

    # Trend from MAs
    if ma_50 and ma_200:
        if price > ma_50 > ma_200:
            trend = "uptrend"
        elif price < ma_50 < ma_200:
            trend = "downtrend"
        elif price > ma_50:
            trend = "uptrend"
        else:
            trend = "sideways"
    else:
        trend = None

    return {
        "price":      round(price, 4),
        "rsi":        rsi,
        "ma_50":      ma_50,
        "ma_200":     ma_200,
        "atr":        atr,
        "support":    support,
        "resistance": resistance,
        "trend":      trend,
    }


async def _fetch_fundamentals(ticker: str) -> dict:
    """Fetch fundamental data + short interest via the Market Data Gateway."""
    from shared.data_client import DataClient
    dc  = DataClient()
    sym = ticker.upper()
    result: dict = {
        "market_cap":         None,
        "analyst_target":     None,
        "recommendation":     None,
        "short_interest":     None,
        "days_to_cover":      None,
    }

    try:
        d = await dc.fundamentals(sym) or {}
        result["market_cap"] = d.get("market_cap")
    except Exception:
        pass

    try:
        d = await dc.analyst(sym) or {}
        if not d.get("error"):
            result["analyst_target"] = d.get("consensus_price_target")
            result["recommendation"] = d.get("consensus_rating")
    except Exception:
        pass

    try:
        d = await dc.short_interest(sym) or {}
        rows = d if isinstance(d, list) else d.get("results") or []
        if rows:
            result["short_interest"] = rows[0].get("short_interest")
            result["days_to_cover"]  = rows[0].get("days_to_cover")
    except Exception:
        pass

    return result


async def _generate_stock_summary(
    ticker: str, signal: str, confidence: float, price: float,
    rsi, trend: str, bullish_factors: list, bearish_factors: list,
    fund: dict | None = None,
    past_reflections: list | None = None,
) -> str:
    """Call OpenRouter to produce a concise natural-language market snapshot. Falls back to a template."""
    bull_str = "; ".join(bullish_factors[:3]) or "none identified"
    bear_str = "; ".join(bearish_factors[:3]) or "none identified"
    rsi_str  = f"{float(rsi):.1f}" if rsi else "N/A"

    fallback = (
        f"{ticker} shows a {signal} signal ({confidence*100:.0f}% confidence) with a {trend} bias. "
        + (f"Bullish: {bull_str}. " if bullish_factors else "")
        + (f"Bearish: {bear_str}." if bearish_factors else "")
    ).strip()

    env = _read_env_file()
    openrouter_key = env.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
    if not openrouter_key or openrouter_key.startswith("your_"):
        return fallback

    model = env.get("LLM_ANALYST_MODEL") or os.getenv("LLM_ANALYST_MODEL", "") or \
            env.get("LLM_PREDICTOR_MODEL") or os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-haiku-4-5")

    # Build fundamentals line for prompt
    fund_line = ""
    if fund:
        parts = []
        if fund.get("pe_ratio"):    parts.append(f"P/E {fund['pe_ratio']:.1f}")
        if fund.get("pb_ratio"):    parts.append(f"P/B {fund['pb_ratio']:.2f}")
        if fund.get("roe"):         parts.append(f"ROE {fund['roe']:.0f}%")
        if fund.get("profit_margin"): parts.append(f"Margin {fund['profit_margin']:.0f}%")
        if fund.get("revenue_growth"): parts.append(f"RevGrowth {fund['revenue_growth']:.0f}%")
        if fund.get("debt_to_equity"): parts.append(f"D/E {fund['debt_to_equity']:.1f}")
        if fund.get("analyst_target"): parts.append(f"Target ${fund['analyst_target']:.2f}")
        if fund.get("recommendation"): parts.append(f"Rating {fund['recommendation']}")
        if parts:
            fund_line = f"Fundamentals: {', '.join(parts)}\n"

    # Past outcomes context
    past_line = ""
    if past_reflections:
        past_line = "Past outcomes:\n" + "\n".join(f"  {p}" for p in past_reflections) + "\n"

    prompt = (
        "Write one concise market snapshot paragraph for a trading dashboard.\n"
        "Rules: under 75 words, specific and grounded in the supplied metrics only, "
        "mention the strongest support and strongest risk, no bullet points, "
        "no AI disclaimers, no uncertainty hedges.\n\n"
        f"Symbol: {ticker}\n"
        f"Signal: {signal}\n"
        f"Confidence: {confidence*100:.0f}%\n"
        f"Trend: {trend}\n"
        f"Price: {'${:.2f}'.format(price) if price else 'N/A'}\n"
        f"RSI: {rsi_str}\n"
        f"Bullish factors: {bull_str}\n"
        f"Bearish factors: {bear_str}\n"
        + fund_line
        + past_line
    )

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 150, "temperature": 0.4},
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")
            if content and content.strip():
                content = content.strip()[:600]
                # Feature 7: Bear-case challenge for bullish signals
                if signal in ("Buy", "Overweight"):
                    bear_prompt = (
                        f"Given this analysis of {ticker}:\n{content}\n\n"
                        "Name the 2 strongest bear-case risks in one sentence each. "
                        "Format exactly: 'Bear risks: [risk1]; [risk2]'. No other text."
                    )
                    try:
                        async with _aiohttp.ClientSession() as _bs:
                            async with _bs.post(
                                "https://openrouter.ai/api/v1/chat/completions",
                                headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                                json={"model": model, "messages": [{"role": "user", "content": bear_prompt}],
                                      "max_tokens": 80, "temperature": 0.3},
                                timeout=_aiohttp.ClientTimeout(total=15),
                            ) as _br:
                                _bd = await _br.json()
                        _bc = ((_bd.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                        if _bc and _bc.strip():
                            content = content + "\n" + _bc.strip()[:250]
                    except Exception as _be:
                        log.warning("stock_analysis.bear_challenge_error", ticker=ticker, error=str(_be))
                return content
    except Exception as e:
        log.warning("stock_analysis.llm_error", ticker=ticker, error=str(e))

    return fallback


@app.post("/api/market/stock-analysis/{ticker}/reflect")
async def reflect_stock_signal(ticker: str, token: str = ""):
    """Compute 5-day return + alpha vs SPY, call LLM for reflection, save to signal_reflections."""
    check_token(token)
    import asyncio as _asyncio
    ticker = ticker.upper()
    pool   = await _get_db_pool()

    row = await pool.fetchrow(
        """SELECT ts, signal, price, summary
           FROM stock_analysis_snapshots WHERE ticker = $1
           ORDER BY ts DESC LIMIT 1""",
        ticker,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No analysis snapshot found for ticker")

    analysis_ts = row["ts"]
    signal_at   = row["signal"]
    price_at    = float(row["price"] or 0)
    summary_at  = row["summary"] or ""

    async def _fetch_prices_polygon(sym: str) -> dict:
        import aiohttp as _aiohttp
        api_key = os.getenv("MASSIVE_API_KEY", "")
        if not api_key:
            return {}
        from_d = (date.today() - timedelta(days=14)).isoformat()
        to_d   = date.today().isoformat()
        url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
               f"/{from_d}/{to_d}?adjusted=true&sort=asc&limit=15&apiKey={api_key}")
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results:
                            return {
                                "current":  float(results[-1].get("c", 0)),
                                "five_ago": float(results[-5].get("c", 0)) if len(results) >= 5 else float(results[0].get("c", 0)),
                            }
        except Exception:
            pass
        return {}

    prices_sym, prices_spy = await _asyncio.gather(
        _fetch_prices_polygon(ticker),
        _fetch_prices_polygon("SPY"),
    )

    current_price = prices_sym.get("current", price_at)
    return_pct    = ((current_price - price_at) / price_at * 100) if price_at else 0.0
    spy_curr      = prices_spy.get("current", 0)
    spy_ago       = prices_spy.get("five_ago", spy_curr)
    spy_ret       = ((spy_curr - spy_ago) / spy_ago * 100) if spy_ago else 0.0
    alpha_vs_spy  = return_pct - spy_ret

    reflection_text = f"Signal {signal_at} at ${price_at:.2f}; current price ${current_price:.2f} ({return_pct:+.1f}%), alpha vs SPY {alpha_vs_spy:+.1f}%."
    env = _read_env_file()
    openrouter_key = env.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
    if openrouter_key and not openrouter_key.startswith("your_"):
        model = env.get("LLM_ANALYST_MODEL") or os.getenv("LLM_ANALYST_MODEL", "") or \
                env.get("LLM_PREDICTOR_MODEL") or os.getenv("LLM_PREDICTOR_MODEL", "anthropic/claude-haiku-4-5")
        ref_prompt = (
            f"You issued a {signal_at} signal on {ticker} at ${price_at:.2f}. "
            f"Current price is ${current_price:.2f} ({return_pct:+.1f}%), "
            f"alpha vs SPY: {alpha_vs_spy:+.1f}%. "
            f"Original analysis: {summary_at[:300]}\n\n"
            "Write 2-4 sentences reflecting on what the signal got right or wrong "
            "and one concrete lesson for next time. Be specific, no disclaimers."
        )
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": ref_prompt}],
                          "max_tokens": 180, "temperature": 0.4},
                    timeout=_aiohttp.ClientTimeout(total=20),
                ) as resp:
                    rd = await resp.json()
            rc = ((rd.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            if rc and rc.strip():
                reflection_text = rc.strip()[:600]
        except Exception as e:
            log.warning("reflect.llm_error", ticker=ticker, error=str(e))

    await pool.execute(
        """INSERT INTO signal_reflections
               (ticker, analysis_ts, signal, price_at_analysis, price_5d_later,
                return_pct, alpha_vs_spy, reflection)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        ticker, analysis_ts, signal_at, price_at, current_price,
        round(return_pct, 4), round(alpha_vs_spy, 4), reflection_text,
    )
    return {
        "ticker":       ticker,
        "signal":       signal_at,
        "price_at":     price_at,
        "price_now":    current_price,
        "return_pct":   round(return_pct, 2),
        "alpha_vs_spy": round(alpha_vs_spy, 2),
        "reflection":   reflection_text,
    }


@app.get("/api/market/stock-analysis/{ticker}/reflections")
async def get_stock_reflections(ticker: str, limit: int = 5):
    """Return last N signal reflections for a ticker."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT id, ticker, analysis_ts, signal, price_at_analysis, price_5d_later,
                  return_pct, alpha_vs_spy, reflection, created_at
           FROM signal_reflections
           WHERE ticker = $1 ORDER BY created_at DESC LIMIT $2""",
        ticker.upper(), limit,
    )
    return {
        "ticker":      ticker.upper(),
        "reflections": [
            {
                "id":             str(r["id"]),
                "analysis_ts":    r["analysis_ts"].isoformat(),
                "signal":         r["signal"],
                "price_at":       float(r["price_at_analysis"] or 0),
                "price_5d_later": float(r["price_5d_later"] or 0),
                "return_pct":     float(r["return_pct"] or 0),
                "alpha_vs_spy":   float(r["alpha_vs_spy"] or 0),
                "reflection":     r["reflection"],
                "created_at":     r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/api/market/stock-analysis")
async def list_stock_analyses(limit: int = 20):
    """Return most recent analysis snapshot per ticker (last 24h)."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT DISTINCT ON (ticker)
               ticker, ts, signal, confidence, price, rsi, trend
           FROM stock_analysis_snapshots
           WHERE ts >= NOW() - INTERVAL '24 hours'
           ORDER BY ticker, ts DESC
           LIMIT $1""",
        limit,
    )
    return {
        "analyses": [
            {
                "ticker":     r["ticker"],
                "ts":         r["ts"].isoformat(),
                "signal":     r["signal"],
                "confidence": float(r["confidence"] or 0),
                "price":      float(r["price"] or 0),
                "rsi":        float(r["rsi"] or 0) if r["rsi"] else None,
                "trend":      r["trend"],
            }
            for r in rows
        ]
    }


# ── Feature 7: Trending symbols ───────────────────────────────────────────────

@app.get("/api/market/trending")
async def get_trending_symbols():
    """Return cached trending symbols list (top 20)."""
    _redis = await get_redis()
    cached = await _redis.get("trending:symbols")
    if cached:
        return {"symbols": json.loads(cached), "source": "cache"}
    # Compute on-demand if cache is cold
    result = await _compute_trending(_redis, await _get_db_pool())
    return {"symbols": result, "source": "computed"}


@app.post("/api/market/trending/refresh")
async def refresh_trending_symbols(token: str = ""):
    """Recompute and cache trending symbols."""
    if token != WEBUI_TOKEN:
        raise HTTPException(403, "Forbidden")
    _redis = await get_redis()
    pool   = await _get_db_pool()
    result = await _compute_trending(_redis, pool)
    return {"ok": True, "count": len(result)}


async def _compute_trending(_redis, pool) -> list[dict]:
    """
    Score symbols by:
      - Signal frequency in last 24h (weight 3)
      - Presence in OVTLYR bull/bear lists (weight 2)
      - Active position ticker (weight 1)
      - Sentiment score magnitude (weight 1)
    Returns top-20 ranked list.
    """
    scores: dict[str, float] = {}
    meta:   dict[str, dict]  = {}

    # Signal frequency (last 24h)
    try:
        rows = await pool.fetch(
            """SELECT ticker, COUNT(*) as cnt, AVG(confidence) as avg_conf
               FROM signals
               WHERE ts >= NOW() - INTERVAL '24 hours'
               GROUP BY ticker""",
        )
        for r in rows:
            t = r["ticker"]
            scores[t] = scores.get(t, 0) + float(r["cnt"]) * 3
            meta.setdefault(t, {})["signal_count"] = int(r["cnt"])
            meta[t]["avg_confidence"] = round(float(r["avg_conf"] or 0), 3)
    except Exception:
        pass

    # OVTLYR lists
    for lst in ("bull", "bear", "market_leaders", "alpha_picks"):
        try:
            raw = await _redis.get(f"ovtlyr:list:{lst}")
            if raw:
                for item in json.loads(raw):
                    t = item.get("ticker", "")
                    if t:
                        scores[t]  = scores.get(t, 0) + 2
                        meta.setdefault(t, {})["ovtlyr_list"] = lst
        except Exception:
            pass

    # Active position tickers
    try:
        pos_raw = await _redis.get("broker:position_tickers")
        if pos_raw:
            for t in json.loads(pos_raw):
                scores[t] = scores.get(t, 0) + 1
                meta.setdefault(t, {})["in_portfolio"] = True
    except Exception:
        pass

    # Sentiment magnitude — normalize F&G (0-100) to [-1, +1] range before scoring
    try:
        sent_all = await _redis.hgetall("sentiment:latest")
        for t, val_raw in (sent_all or {}).items():
            val = json.loads(val_raw)
            raw_score = float(val.get("score", 0))
            # Yahoo F&G scores are 0-100; VADER scores are -1 to +1
            normalized = (raw_score / 50.0 - 1.0) if abs(raw_score) > 1 else raw_score
            score_mag  = abs(normalized)
            if score_mag > 0.1:
                scores[t] = scores.get(t, 0) + score_mag
                meta.setdefault(t, {})["sentiment_score"] = round(normalized, 3)
    except Exception:
        pass

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:20]
    result = [
        {
            "ticker": t,
            "score":  round(s, 2),
            **meta.get(t, {}),
        }
        for t, s in ranked
        if len(t) <= 5 and t.isalpha()  # basic ticker sanity check
    ]

    await _redis.set("trending:symbols", json.dumps(result), ex=300)  # 5-min cache
    return result


# ── Options portfolio Greeks ──────────────────────────────────────────────────

@app.get("/api/options/portfolio-greeks")
async def get_portfolio_greeks(mode: str = "live"):
    """Aggregate delta, theta, vega, gamma across all active option positions."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT account_label, underlying, option_type, qty,
                  delta, theta, vega, gamma, rho, volga, vanna, charm, pop,
                  expiration_date, strike
           FROM option_positions
           WHERE status = 'active' AND mode = $1""",
        mode,
    )
    _GREEKS = ("delta", "theta", "vega", "gamma", "rho", "volga", "vanna", "charm", "pop")
    totals = {g: 0.0 for g in _GREEKS}
    per_account: dict = {}
    per_underlying: dict = {}
    for r in rows:
        qty = float(r["qty"] or 0)
        mult = qty * 100   # 1 contract = 100 shares
        for g in _GREEKS:
            val = r[g]
            if val is None:
                continue
            v = float(val) * mult
            totals[g] += v
            acct = r["account_label"]
            under = r["underlying"]
            per_account.setdefault(acct, {g2: 0.0 for g2 in _GREEKS})
            per_underlying.setdefault(under, {g2: 0.0 for g2 in _GREEKS})
            per_account[acct][g] += v
            per_underlying[under][g] += v

    # Round totals
    totals = {k: round(v, 2) for k, v in totals.items()}
    per_account   = {k: {g: round(v, 2) for g, v in vals.items()} for k, vals in per_account.items()}
    per_underlying = {k: {g: round(v, 2) for g, v in vals.items()} for k, vals in per_underlying.items()}

    return {
        "totals":         totals,
        "per_account":    per_account,
        "per_underlying": per_underlying,
        "position_count": len(rows),
    }


# ── Implied Volatility solver (Newton-Raphson) ────────────────────────────────

@app.get("/api/options/implied-vol")
async def options_implied_vol(
    spot: float, strike: float, T: float, market_price: float,
    option_type: str = "call", r: float = 0.045, token: str = "",
):
    """Newton-Raphson implied volatility solver. T is years to expiry."""
    check_token(token)
    import math as _m

    def _bs(S, K, t, sig, ri, opt):
        if t <= 0 or sig <= 0 or S <= 0 or K <= 0:
            return max(0.0, S - K) if opt == "call" else max(0.0, K - S)
        try:
            st = _m.sqrt(t)
            d1 = (_m.log(S / K) + (ri + 0.5 * sig ** 2) * t) / (sig * st)
            d2 = d1 - sig * st
            nc = lambda x: (1 + _m.erf(x / _m.sqrt(2))) / 2
            er = _m.exp(-ri * t)
            return S * nc(d1) - K * er * nc(d2) if opt == "call" else K * er * nc(-d2) - S * nc(-d1)
        except Exception:
            return None

    def _vega(S, K, t, sig, ri):
        if t <= 0 or sig <= 0 or S <= 0 or K <= 0:
            return 0.0
        try:
            d1 = (_m.log(S / K) + (ri + 0.5 * sig ** 2) * t) / (sig * _m.sqrt(t))
            return S * _m.exp(-0.5 * d1 ** 2) / _m.sqrt(2 * _m.pi) * _m.sqrt(t)
        except Exception:
            return 0.0

    sigma = 0.20
    converged = False
    iters = 0
    for iters in range(100):
        price = _bs(spot, strike, T, sigma, r, option_type)
        if price is None:
            break
        diff = price - market_price
        if abs(diff) < 1e-6:
            converged = True
            break
        vega = _vega(spot, strike, T, sigma, r)
        if abs(vega) < 1e-8:
            break
        sigma = max(0.001, min(5.0, sigma - diff / vega))

    return {
        "implied_vol":     round(sigma, 6),
        "implied_vol_pct": round(sigma * 100, 3),
        "iterations":      iters + 1,
        "converged":       converged,
    }


# ── Portfolio VaR / Expected Shortfall (delta-gamma approx) ───────────────────

@app.get("/api/options/portfolio-var")
async def options_portfolio_var(
    mode: str = "live", confidence: float = 0.95, token: str = "",
):
    """
    Delta-gamma portfolio VaR across 200 spot scenarios (±25%).
    Each position's P&L: qty × 100 × (Δ·ΔS + ½Γ·ΔS²).
    """
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT underlying, option_type, qty, strike, underlying_entry,
                  delta, gamma, theta
           FROM option_positions
           WHERE status = 'active' AND mode = $1""",
        mode,
    )
    if not rows:
        return {"var_95": None, "es_95": None, "max_loss": None,
                "pop": None, "theta_daily": None, "position_count": 0}

    import math as _m
    N = 200
    moves = [-0.25 + i * 0.50 / (N - 1) for i in range(N)]
    pnl   = [0.0] * N

    for row in rows:
        delta = float(row["delta"] or 0)
        gamma = float(row["gamma"] or 0)
        qty   = float(row["qty"]   or 0)
        spot0 = float(row["underlying_entry"] or 0)
        if spot0 <= 0:
            continue
        for i, mv in enumerate(moves):
            ds    = mv * spot0
            pnl[i] += qty * 100 * (delta * ds + 0.5 * gamma * ds * ds)

    sorted_pnl = sorted(pnl)
    idx  = max(0, int(_m.ceil((1.0 - confidence) * N)) - 1)
    var  = -sorted_pnl[idx]
    tail = [-p for p in sorted_pnl[: idx + 1] if p < 0]
    es   = sum(tail) / len(tail) if tail else 0.0
    theta_daily = sum(float(r["theta"] or 0) * float(r["qty"] or 0) * 100 for r in rows)

    return {
        "var_95":         round(var, 2),
        "es_95":          round(es, 2),
        "max_loss":       round(-min(sorted_pnl), 2),
        "pop":            round(sum(1 for p in pnl if p > 0) / N, 3),
        "theta_daily":    round(theta_daily, 2),
        "position_count": len(rows),
        "confidence":     confidence,
    }


# ── Unusual options flow (volume delta + importance score) ───────────────────

@app.get("/api/options/unusual-flow")
async def get_unusual_flow():
    """
    Return unusual options flow hits from the most recent options_monitor scan.
    Each hit: underlying, contract, strike, expiry, type, price, vol_delta,
              notional, direction, dir_confidence, change_pct, delta, score.
    Written to Redis key 'options:flow:latest' by options_monitor every scan cycle.
    """
    try:
        redis = await get_redis()
        raw = await redis.get("options:flow:latest")
        if not raw:
            return {"hits": [], "ts": None, "count": 0, "source": "none"}
        data = json.loads(raw)
        return {**data, "source": "redis"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Options expiry calendar ───────────────────────────────────────────────────

@app.get("/api/options/expiry-calendar")
async def get_options_expiry_calendar(mode: str = "live"):
    """Return active option positions grouped by expiration date with DTE and Greeks."""
    import datetime as _dt
    pool  = await _get_db_pool()
    today = _dt.date.today()
    rows  = await pool.fetch(
        """SELECT id, contract_symbol, underlying, option_type, strike,
                  expiration_date, account_label, account_name, qty,
                  entry_price, delta, theta, vega, gamma, status
           FROM option_positions
           WHERE status = 'active' AND mode = $1
           ORDER BY expiration_date ASC NULLS LAST, underlying ASC""",
        mode,
    )
    by_date: dict = {}
    for r in rows:
        exp = r["expiration_date"]
        key = exp.isoformat() if exp else "unknown"
        dte = (exp - today).days if exp else None
        if key not in by_date:
            by_date[key] = {
                "expiration_date": key,
                "dte":             dte,
                "urgency":         "critical" if dte is not None and dte <= 3
                                   else "warning" if dte is not None and dte <= 7
                                   else "caution" if dte is not None and dte <= 14
                                   else "ok",
                "positions":       [],
                "total_delta":     0.0,
                "total_theta":     0.0,
                "total_vega":      0.0,
                "position_count":  0,
            }
        qty  = float(r["qty"] or 0)
        mult = qty * 100
        entry = by_date[key]
        entry["position_count"] += 1
        if r["delta"] is not None:
            entry["total_delta"] += float(r["delta"]) * mult
        if r["theta"] is not None:
            entry["total_theta"] += float(r["theta"]) * mult
        if r["vega"] is not None:
            entry["total_vega"]  += float(r["vega"])  * mult
        entry["positions"].append({
            "id":              str(r["id"]),
            "contract_symbol": r["contract_symbol"],
            "underlying":      r["underlying"],
            "option_type":     r["option_type"],
            "strike":          float(r["strike"]) if r["strike"] else None,
            "account_label":   r["account_label"],
            "account_name":    r["account_name"],
            "qty":             float(r["qty"]),
            "entry_price":     float(r["entry_price"]) if r["entry_price"] else None,
            "delta":           float(r["delta"]) if r["delta"] else None,
            "theta":           float(r["theta"]) if r["theta"] else None,
        })

    # Round aggregated values
    for entry in by_date.values():
        for k in ("total_delta", "total_theta", "total_vega"):
            entry[k] = round(entry[k], 2)

    calendar = sorted(by_date.values(), key=lambda x: x["expiration_date"])
    return {"calendar": calendar, "today": today.isoformat()}


# ── Chain Analytics: Max Pain / GEX / Zero Gamma / IV Smile / OI Heatmap ────

@app.get("/api/options/chain-analytics")
async def options_chain_analytics(ticker: str, expiry: str = "", token: str = ""):
    """
    Full-chain analytics for a given underlying and expiry date.
    Computes: Max Pain, GEX per strike, Zero Gamma level, IV Smile, OI distribution.
    Fetches from Polygon v3/snapshot/options. Cached 30 min.
    """
    check_token(token)
    sym     = ticker.upper()
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "No Polygon API key configured"}

    cache_key = f"options:chain_analytics:{sym}:{expiry}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    try:
        import aiohttp as _ah
        from datetime import date as _date

        # ── Fetch underlying spot price ────────────────────────────────────────
        # Try: prev-close agg → last trade → zero fallback
        spot = 0.0
        try:
            from datetime import date as _date, timedelta as _td
            today_s = _date.today().isoformat()
            from_s  = (_date.today() - _td(days=5)).isoformat()
            agg_url = (
                f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                f"/{from_s}/{today_s}?adjusted=true&sort=desc&limit=1&apiKey={api_key}"
            )
            async with _ah.ClientSession() as s:
                async with s.get(agg_url, timeout=_ah.ClientTimeout(total=6)) as r:
                    if r.status == 200:
                        dj = await r.json()
                        bars = dj.get("results") or []
                        if bars:
                            spot = float(bars[0].get("c") or 0)
        except Exception:
            pass
        # last-trade fallback
        if not spot:
            try:
                async with _ah.ClientSession() as s:
                    async with s.get(
                        f"https://api.polygon.io/v2/last/trade/{sym}?apiKey={api_key}",
                        timeout=_ah.ClientTimeout(total=6),
                    ) as r:
                        if r.status == 200:
                            dj = await r.json()
                            spot = float((dj.get("results") or {}).get("p") or 0)
            except Exception:
                pass

        # ── Fetch full options chain ───────────────────────────────────────────
        # Pull calls + puts for the requested expiry (or nearest if blank)
        async def _fetch_contracts(contract_type: str) -> list[dict]:
            contracts: list[dict] = []
            url = (
                f"https://api.polygon.io/v3/snapshot/options/{sym}"
                f"?contract_type={contract_type}&limit=250"
            )
            if expiry:
                url += f"&expiration_date={expiry}"
            url += f"&apiKey={api_key}"
            async with _ah.ClientSession() as s:
                while url:
                    async with s.get(url, timeout=_ah.ClientTimeout(total=12)) as r:
                        if r.status != 200:
                            break
                        data = await r.json()
                    contracts.extend(data.get("results") or [])
                    next_url = data.get("next_url")
                    url = (next_url + f"&apiKey={api_key}") if next_url else None
            return contracts

        calls_raw, puts_raw = await asyncio.gather(
            _fetch_contracts("call"),
            _fetch_contracts("put"),
        )

        if not calls_raw and not puts_raw:
            return {"error": f"No chain data for {sym}"}

        # ── Determine expiry used (nearest if not specified) ───────────────────
        all_expiries: set[str] = set()
        for c in calls_raw + puts_raw:
            e = (c.get("details") or {}).get("expiration_date", "")
            if e:
                all_expiries.add(e)
        if not expiry and all_expiries:
            expiry = min(e for e in all_expiries if e >= _date.today().isoformat())

        # Filter to chosen expiry
        def _exp(c): return (c.get("details") or {}).get("expiration_date", "")
        if expiry:
            calls_raw = [c for c in calls_raw if _exp(c) == expiry]
            puts_raw  = [c for c in puts_raw  if _exp(c) == expiry]

        def _parse(contracts: list[dict], side: str) -> dict[float, dict]:
            out: dict[float, dict] = {}
            for c in contracts:
                det   = c.get("details") or {}
                k     = float(det.get("strike_price") or 0)
                if not k:
                    continue
                g     = c.get("greeks") or {}
                gamma = abs(float(g.get("gamma") or 0))
                iv    = float(c.get("implied_volatility") or 0)
                oi    = int(c.get("open_interest") or 0)
                out[k] = {
                    "strike": k,
                    f"{side}_gamma": gamma,
                    f"{side}_iv":    round(iv * 100, 2),   # percent
                    f"{side}_oi":    oi,
                }
            return out

        call_data = _parse(calls_raw, "call")
        put_data  = _parse(puts_raw,  "put")

        # ── Build per-strike merged rows ───────────────────────────────────────
        all_strikes = sorted(set(call_data) | set(put_data))
        rows: list[dict] = []
        for k in all_strikes:
            cd = call_data.get(k, {})
            pd = put_data.get(k,  {})
            c_gamma = cd.get("call_gamma", 0)
            p_gamma = pd.get("put_gamma",  0)
            c_oi    = cd.get("call_oi",    0)
            p_oi    = pd.get("put_oi",     0)
            # GEX: dealers short options → call GEX positive, put GEX negative
            c_gex   = round(c_oi * c_gamma * 100 * (spot or 1), 0)
            p_gex   = round(-p_oi * p_gamma * 100 * (spot or 1), 0)
            rows.append({
                "strike":   k,
                "call_oi":  c_oi,
                "put_oi":   p_oi,
                "call_iv":  cd.get("call_iv", 0),
                "put_iv":   pd.get("put_iv",  0),
                "call_gex": c_gex,
                "put_gex":  p_gex,
                "net_gex":  c_gex + p_gex,
            })

        # ── Max Pain ───────────────────────────────────────────────────────────
        # Strike where total $ value of ITM options is minimised (MM max profit)
        max_pain_strike = None
        min_pain = float("inf")
        for candidate in all_strikes:
            pain = 0.0
            for k in all_strikes:
                cd = call_data.get(k, {}); pd = put_data.get(k, {})
                if k < candidate:   # ITM call
                    pain += (candidate - k) * cd.get("call_oi", 0) * 100
                if k > candidate:   # ITM put
                    pain += (k - candidate) * pd.get("put_oi",  0) * 100
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = candidate

        # ── Net GEX + Zero Gamma ──────────────────────────────────────────────
        net_gex_total = sum(r["net_gex"] for r in rows)

        # Zero gamma = strike where cumulative GEX (sorted asc) crosses zero
        zero_gamma = None
        cum = 0.0
        prev_cum = 0.0
        prev_strike = None
        for r in rows:
            cum += r["net_gex"]
            if prev_strike is not None and prev_cum * cum < 0:
                # Linear interpolation of the crossover strike
                frac = abs(prev_cum) / (abs(prev_cum) + abs(cum))
                zero_gamma = round(prev_strike + frac * (r["strike"] - prev_strike), 2)
                break
            prev_cum    = cum
            prev_strike = r["strike"]

        # ── PCR (Put/Call Ratio) ───────────────────────────────────────────────
        total_call_oi = sum(r.get("call_oi", 0) for r in rows)
        total_put_oi  = sum(r.get("put_oi", 0) for r in rows)
        pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None
        if pcr is None:
            pcr_label = "—"
        elif pcr > 1.2:
            pcr_label = "Bearish"
        elif pcr < 0.7:
            pcr_label = "Bullish"
        else:
            pcr_label = "Neutral"

        # ── ATM IV + IV Percentile (vs 252-day realized vol history) ──────────
        atm_iv        = None   # ATM implied vol in percent
        iv_percentile = None   # % of hist HV days below current ATM IV
        iv_rank       = None   # (ATM_IV - min_HV) / (max_HV - min_HV) × 100
        iv_label      = "—"

        if spot > 0 and all_strikes:
            atm_strike = min(all_strikes, key=lambda k: abs(k - spot))
            atm_row    = next((r for r in rows if r["strike"] == atm_strike), None)
            if atm_row:
                ivs = [v for v in [atm_row.get("call_iv", 0), atm_row.get("put_iv", 0)] if v > 0]
                if ivs:
                    atm_iv = round(sum(ivs) / len(ivs), 2)   # percent

        if atm_iv is not None:
            try:
                import math as _math
                from datetime import timedelta as _td2
                hv_url = (
                    f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
                    f"/{(_date.today() - _td2(days=400)).isoformat()}"
                    f"/{_date.today().isoformat()}"
                    f"?adjusted=true&sort=asc&limit=400&apiKey={api_key}"
                )
                async with _ah.ClientSession() as _s:
                    async with _s.get(hv_url, timeout=_ah.ClientTimeout(total=8)) as _r:
                        hv_bars = (await _r.json()).get("results") or [] if _r.status == 200 else []

                if len(hv_bars) >= 30:
                    closes   = [b["c"] for b in hv_bars]
                    log_rets = [_math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                    period   = 21
                    hv_series = []
                    for i in range(period - 1, len(log_rets)):
                        window = log_rets[i - period + 1: i + 1]
                        mu     = sum(window) / period
                        var    = sum((x - mu) ** 2 for x in window) / (period - 1)
                        hv_series.append(_math.sqrt(var) * _math.sqrt(252) * 100)

                    if hv_series:
                        min_hv = min(hv_series)
                        max_hv = max(hv_series)
                        iv_percentile = round(
                            sum(1 for h in hv_series if h < atm_iv) / len(hv_series) * 100, 1
                        )
                        iv_rank = round((atm_iv - min_hv) / (max_hv - min_hv) * 100, 1) \
                            if max_hv > min_hv else 50.0
                        iv_label = "Low" if iv_percentile < 25 else ("High" if iv_percentile > 75 else "Normal")
            except Exception:
                pass

        # ── Price sensitivity heatmap (B-S call price: spot × vol grid) ──────────
        vol_heatmap = None
        if spot > 0 and max_pain_strike:
            try:
                import math as _mh
                _K = float(max_pain_strike)
                _T = max(
                    (
                        __import__("datetime").date.fromisoformat(expiry) -
                        __import__("datetime").date.today()
                    ).days, 1
                ) / 365.0 if expiry else 30 / 365.0

                def _bs_hm(S, K, T, sig, r=0.045, opt="call"):
                    if T <= 0 or sig <= 0 or S <= 0 or K <= 0:
                        return round(max(0.0, S - K if opt == "call" else K - S), 2)
                    try:
                        st = _mh.sqrt(T)
                        d1 = (_mh.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * st)
                        d2 = d1 - sig * st
                        nc = lambda x: (1.0 + _mh.erf(x / _mh.sqrt(2.0))) / 2.0
                        er = _mh.exp(-r * T)
                        if opt == "call":
                            return round(max(0.0, S * nc(d1) - K * er * nc(d2)), 2)
                        return round(max(0.0, K * er * nc(-d2) - S * nc(-d1)), 2)
                    except Exception:
                        return None

                _N_spot, _N_vol = 12, 10
                spot_axis = [round(spot * (0.70 + i * 0.60 / (_N_spot - 1)), 2) for i in range(_N_spot)]
                vol_axis  = [round(5 + i * 145 / (_N_vol - 1), 1) for i in range(_N_vol)]
                call_grid, put_grid = [], []
                for v_pct in vol_axis:
                    sig = v_pct / 100.0
                    call_grid.append([_bs_hm(s, _K, _T, sig, opt="call") for s in spot_axis])
                    put_grid.append( [_bs_hm(s, _K, _T, sig, opt="put")  for s in spot_axis])
                vol_heatmap = {
                    "spot_axis":   spot_axis,
                    "vol_axis":    vol_axis,
                    "call_prices": call_grid,
                    "put_prices":  put_grid,
                    "atm_strike":  _K,
                    "T_years":     round(_T, 4),
                }
            except Exception:
                pass

        result = {
            "ticker":           sym,
            "expiry":           expiry,
            "spot":             spot,
            "max_pain":         max_pain_strike,
            "zero_gamma":       zero_gamma,
            "net_gex":          round(net_gex_total, 0),
            "gex_regime":       "positive" if net_gex_total >= 0 else "negative",
            "pcr":              pcr,
            "pcr_label":        pcr_label,
            "atm_iv":           atm_iv,
            "iv_percentile":    iv_percentile,
            "iv_rank":          iv_rank,
            "iv_label":         iv_label,
            "vol_heatmap":      vol_heatmap,
            "strikes":          rows,
            "available_expiries": sorted(all_expiries),
        }
        try:
            if _r:
                await _r.setex(cache_key, 1800, json.dumps(result))
        except Exception:
            pass
        return result

    except Exception as e:
        log.warning("options.chain_analytics_error", ticker=sym, error=str(e))
        return {"error": str(e)}


# ── Daily P&L / loss limit ────────────────────────────────────────────────────

@app.get("/api/trading/daily-pnl")
async def get_daily_pnl(token: str = ""):
    """Return today's cumulative P&L and loss limit status.

    P&L is computed from the DB (option_trade_log + trades) so the number
    reflects actual closed positions today rather than the Redis accumulator,
    which is never written to by the traders.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    redis = await get_redis()
    from shared.risk_controls import get_risk_controls, CIRCUIT_BROKEN_KEY, CIRCUIT_REASON_KEY
    controls       = await get_risk_controls(redis)
    max_loss       = float(controls.get("max_daily_loss_usd") or 0)
    circuit_broken = await redis.get(CIRCUIT_BROKEN_KEY) in ("1", b"1")
    circuit_reason = await redis.get(CIRCUIT_REASON_KEY) or ""

    # Use Eastern time so the trading-day boundary matches US market hours
    _et = ZoneInfo("America/New_York")
    today = _dt.now(_et).date()
    pool  = await _get_db_pool()

    # Options P&L: genuinely closed positions today (ET).
    # Exclude "not_in_scan" scanner closures that happen after 4:30 PM ET —
    # these are artifacts from Webull dropping positions in post-market scans.
    # Real closes during market hours (before 4:30 PM ET) are kept regardless.
    opt_pnl = 0.0
    try:
        row = await pool.fetchrow(
            """SELECT COALESCE(SUM(otl.realized_pnl), 0) AS total
               FROM option_trade_log otl
               JOIN option_positions op ON op.id = otl.position_id
               WHERE otl.event_type = 'closed'
                 AND otl.realized_pnl IS NOT NULL
                 AND (otl.ts AT TIME ZONE 'America/New_York')::date = $1
                 AND (
                   otl.notes IS NULL
                   OR otl.notes NOT LIKE 'Position closed%no longer in broker scan%'
                   OR (otl.ts AT TIME ZONE 'America/New_York')::time < '16:30'
                 )""",
            today,
        )
        opt_pnl = float(row["total"]) if row else 0.0
    except Exception:
        pass

    # Equity P&L: trades with a recorded P&L today (ET)
    eq_pnl = 0.0
    try:
        row = await pool.fetchrow(
            """SELECT COALESCE(SUM(pnl), 0) AS total
               FROM trades
               WHERE pnl IS NOT NULL
                 AND (ts AT TIME ZONE 'America/New_York')::date = $1""",
            today,
        )
        eq_pnl = float(row["total"]) if row else 0.0
    except Exception:
        pass

    current_pnl = round(opt_pnl + eq_pnl, 2)
    loss_pct    = abs(min(current_pnl, 0.0)) / max_loss * 100 if max_loss > 0 else 0.0

    return {
        "current_pnl":    current_pnl,
        "max_daily_loss": max_loss,
        "loss_pct_used":  round(loss_pct, 1),
        "limit_enabled":  max_loss > 0,
        "circuit_broken": circuit_broken,
        "circuit_reason": circuit_reason,
    }


@app.get("/api/trading/daily-loss-history")
async def get_daily_loss_history(days: int = 14, token: str = ""):
    """Return per-day P&L history from daily_loss_log for sparkline + trend."""
    check_token(token)
    pool  = await _get_db_pool()
    rows  = await pool.fetch(
        """SELECT log_date, SUM(realized_pnl) AS pnl, SUM(trade_count) AS trades
           FROM daily_loss_log
           WHERE log_date >= CURRENT_DATE - ($1 || ' days')::INTERVAL
           GROUP BY log_date
           ORDER BY log_date ASC""",
        str(days),
    )
    return {
        "days": [
            {
                "date":   r["log_date"].isoformat(),
                "pnl":    float(r["pnl"] or 0),
                "trades": int(r["trades"] or 0),
            }
            for r in rows
        ]
    }


@app.get("/api/review/recommendations")
async def get_review_recommendations(limit: int = 5, token: str = ""):
    """Return latest discipline review recommendations from review_log."""
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT id, ts, trade_count, findings, recommendations, applied
           FROM review_log
           ORDER BY ts DESC LIMIT $1""",
        limit,
    )
    return {
        "reviews": [
            {
                "id":              str(r["id"]),
                "ts":              r["ts"].isoformat(),
                "trade_count":     r["trade_count"],
                "findings":        r["findings"],
                "recommendations": r["recommendations"] or [],
                "applied":         r["applied"],
            }
            for r in rows
        ]
    }


@app.get("/api/market/etf-flows/anomalies")
async def get_etf_flow_anomalies(threshold: float = 2.0, token: str = ""):
    """
    Return ETFs with flow_ratio z-score above threshold (anomalous inflows/outflows).
    Uses 30-day rolling mean + stddev per ticker. threshold=2.0 means >2 sigma.
    """
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """WITH stats AS (
               SELECT ticker, name, category,
                      AVG(flow_ratio) AS mean_ratio,
                      STDDEV(flow_ratio) AS std_ratio
               FROM etf_flow_snapshots
               WHERE ts >= NOW() - INTERVAL '30 days'
               GROUP BY ticker, name, category
               HAVING STDDEV(flow_ratio) > 0
           ),
           latest AS (
               SELECT DISTINCT ON (ticker)
                   ticker, flow_ratio, change_pct, price, ts
               FROM etf_flow_snapshots
               ORDER BY ticker, ts DESC
           )
           SELECT l.ticker, s.name, s.category,
                  l.flow_ratio, l.change_pct, l.price,
                  s.mean_ratio, s.std_ratio,
                  (l.flow_ratio - s.mean_ratio) / s.std_ratio AS z_score,
                  l.ts
           FROM latest l
           JOIN stats s USING (ticker)
           WHERE ABS((l.flow_ratio - s.mean_ratio) / s.std_ratio) >= $1
           ORDER BY ABS((l.flow_ratio - s.mean_ratio) / s.std_ratio) DESC
           LIMIT 20""",
        threshold,
    )
    return {
        "anomalies": [
            {
                "ticker":      r["ticker"],
                "name":        r["name"],
                "category":    r["category"],
                "flow_ratio":  float(r["flow_ratio"] or 1),
                "change_pct":  float(r["change_pct"] or 0),
                "price":       float(r["price"] or 0),
                "mean_ratio":  round(float(r["mean_ratio"] or 1), 3),
                "std_ratio":   round(float(r["std_ratio"] or 0), 3),
                "z_score":     round(float(r["z_score"] or 0), 2),
                "ts":          r["ts"].isoformat(),
                "direction":   "inflow" if float(r["flow_ratio"] or 1) > float(r["mean_ratio"] or 1) else "outflow",
            }
            for r in rows
        ],
        "threshold": threshold,
    }


@app.get("/api/options/performance")
async def get_options_performance(mode: str = "live"):
    """Return YTD trading performance metrics + SPY benchmark comparison."""
    from datetime import date
    pool = await _get_db_pool()
    year_start = date(date.today().year, 1, 1)

    # YTD closed trades from option_trade_log
    rows = await pool.fetch("""
        SELECT realized_pnl, pnl_pct
        FROM option_trade_log
        WHERE event_type = 'closed'
          AND realized_pnl IS NOT NULL
          AND ts >= $1
    """, year_start)

    total_trades = len(rows)
    wins  = [r for r in rows if r["realized_pnl"] > 0]
    losses = [r for r in rows if r["realized_pnl"] <= 0]
    win_rate   = len(wins) / total_trades if total_trades else 0.0
    # Use pnl_pct if available, else fall back to realized_pnl for avg calculations
    win_pcts   = [float(r["pnl_pct"]) for r in wins   if r["pnl_pct"] is not None]
    loss_pcts  = [float(r["pnl_pct"]) for r in losses if r["pnl_pct"] is not None]
    win_pnls   = [float(r["realized_pnl"]) for r in wins   if r["realized_pnl"] is not None]
    loss_pnls  = [float(r["realized_pnl"]) for r in losses if r["realized_pnl"] is not None]
    avg_win    = sum(win_pcts)  / len(win_pcts)  if win_pcts  else None
    avg_loss   = sum(loss_pcts) / len(loss_pcts) if loss_pcts else None
    avg_win_usd  = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0.0
    avg_loss_usd = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    proven_edge = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if avg_win is not None and avg_loss is not None else None
    # Fall back to USD-based edge when pct values are unavailable
    proven_edge_usd = None
    proven_edge_unit = "pct"
    if proven_edge is not None:
        proven_edge_unit = "pct"
    elif win_pnls or loss_pnls:
        proven_edge_usd = (win_rate * avg_win_usd) + ((1 - win_rate) * avg_loss_usd)
        proven_edge_unit = "usd"
    total_pnl  = sum(float(r["realized_pnl"]) for r in rows)

    # Portfolio NAV for YTD return
    nav_rows = await pool.fetch("""
        SELECT snapshot_date, SUM(total_nav) AS total_nav
        FROM portfolio_snapshots
        WHERE mode = $1
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """, mode)

    ytd_return_pct = 0.0
    ann_return_pct = 0.0
    start_nav = end_nav = None
    if nav_rows:
        # Find first snapshot at or after year start, and latest
        ytd_rows = [r for r in nav_rows if r["snapshot_date"] >= year_start]
        if ytd_rows:
            start_nav = float(ytd_rows[0]["total_nav"])
            end_nav   = float(ytd_rows[-1]["total_nav"])
            if start_nav and start_nav != 0:
                ytd_return_pct = (end_nav - start_nav) / start_nav * 100
                days_elapsed = (ytd_rows[-1]["snapshot_date"] - ytd_rows[0]["snapshot_date"]).days
                if days_elapsed > 0:
                    ann_return_pct = ((1 + ytd_return_pct / 100) ** (365 / days_elapsed) - 1) * 100

    # SPY YTD via Polygon.io or yfinance (Redis-cached, 1-hr TTL)
    spy_ytd = spy_ann = market_corr = None
    try:
        _redis_perf = await get_redis()
        _spy_cache = await _redis_perf.get("yf:spy_ytd")
        if _spy_cache:
            _spy_cached = json.loads(_spy_cache)
            spy_ytd = _spy_cached.get("spy_ytd")
            spy_ann = _spy_cached.get("spy_ann")
        else:
            import aiohttp as _aiohttp
            spy_start_str = year_start.isoformat()
            spy_end_str   = date.today().isoformat()
            spy_close_first = spy_close_last = None
            api_key = os.getenv("MASSIVE_API_KEY", "")
            if api_key:
                url = (
                    f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day"
                    f"/{spy_start_str}/{spy_end_str}?adjusted=true&sort=asc&limit=365&apiKey={api_key}"
                )
                async with _aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            spy_data = await resp.json()
                            bars = spy_data.get("results", [])
                            if len(bars) >= 2:
                                spy_close_first = float(bars[0]["c"])
                                spy_close_last  = float(bars[-1]["c"])
            if spy_close_first and spy_close_last and spy_close_first != 0:
                spy_ytd = (spy_close_last - spy_close_first) / spy_close_first * 100
                days_ytd = (date.today() - year_start).days
                if days_ytd > 0:
                    spy_ann = ((1 + spy_ytd / 100) ** (365 / days_ytd) - 1) * 100
            if spy_ytd is not None:
                await _redis_perf.setex("yf:spy_ytd", 3600, json.dumps({"spy_ytd": spy_ytd, "spy_ann": spy_ann}))
    except Exception:
        pass

    current_alpha = (ytd_return_pct - spy_ytd) if spy_ytd is not None else None
    ann_alpha     = (ann_return_pct - spy_ann)  if spy_ann  is not None else None

    return {
        "total_trades_ytd":  total_trades,
        "total_pnl_ytd":     round(total_pnl, 2),
        "ytd_return_pct":    round(ytd_return_pct, 2),
        "ann_return_pct":    round(ann_return_pct, 2),
        "avg_win_pct":       round(avg_win, 2) if avg_win is not None else None,
        "avg_loss_pct":      round(avg_loss, 2) if avg_loss is not None else None,
        "avg_win_usd":       round(avg_win_usd, 2),
        "avg_loss_usd":      round(avg_loss_usd, 2),
        "win_rate":          round(win_rate * 100, 1),
        "proven_edge":       round(proven_edge, 2) if proven_edge is not None else None,
        "proven_edge_usd":   round(proven_edge_usd, 2) if proven_edge_usd is not None else None,
        "proven_edge_unit":  proven_edge_unit,
        "spy_ytd_pct":       round(spy_ytd, 2) if spy_ytd is not None else None,
        "spy_ann_pct":       round(spy_ann, 2) if spy_ann is not None else None,
        "current_alpha":     round(current_alpha, 2) if current_alpha is not None else None,
        "ann_alpha":         round(ann_alpha, 2) if ann_alpha is not None else None,
        "market_corr":       market_corr,
        "start_nav":         round(start_nav, 2) if start_nav else None,
        "end_nav":           round(end_nav, 2) if end_nav else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Trade Journal — option_positions.journal + trades.notes
# ══════════════════════════════════════════════════════════════════════════════

@app.patch("/api/options/positions/{position_id}/journal")
async def patch_option_journal(position_id: str, body: dict):
    pool = await _get_db_pool()
    await pool.execute(
        "UPDATE option_positions SET journal=$1, updated_at=NOW() WHERE id=$2",
        body.get("journal") or None,
        uuid.UUID(position_id),
    )
    return {"ok": True}


@app.patch("/api/trades/{trade_id}/notes")
async def patch_trade_notes(trade_id: str, body: dict):
    pool = await _get_db_pool()
    await pool.execute(
        "UPDATE trades SET notes=$1 WHERE id=$2",
        body.get("notes") or None,
        uuid.UUID(trade_id),
    )
    return {"ok": True}


# ── Equity Position Journal (notes + commission per account+ticker) ────────────

@app.get("/api/equity/journal/{account_id}")
async def get_equity_journal_all(account_id: str):
    """Return all journal entries for an account as {ticker: {notes, trade_cost}}."""
    pool = await _get_db_pool()
    rows = await pool.fetch(
        "SELECT ticker, notes, trade_cost FROM equity_journal WHERE account_id=$1",
        account_id,
    )
    return {
        r["ticker"]: {
            "notes":      r["notes"],
            "trade_cost": float(r["trade_cost"]) if r["trade_cost"] is not None else None,
        }
        for r in rows
    }


@app.get("/api/equity/journal/{account_id}/{ticker}")
async def get_equity_journal(account_id: str, ticker: str):
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        "SELECT notes, trade_cost, updated_at FROM equity_journal WHERE account_id=$1 AND ticker=$2",
        account_id, ticker.upper(),
    )
    if not row:
        return {"notes": None, "trade_cost": None}
    return {
        "notes":      row["notes"],
        "trade_cost": float(row["trade_cost"]) if row["trade_cost"] is not None else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@app.patch("/api/equity/journal/{account_id}/{ticker}")
async def patch_equity_journal(account_id: str, ticker: str, body: dict):
    pool = await _get_db_pool()
    notes      = body.get("notes") or None
    trade_cost = body.get("trade_cost")
    if trade_cost is not None:
        try:
            trade_cost = float(trade_cost)
        except (TypeError, ValueError):
            trade_cost = None
    await pool.execute(
        """INSERT INTO equity_journal (account_id, ticker, notes, trade_cost, updated_at)
           VALUES ($1, $2, $3, $4, NOW())
           ON CONFLICT (account_id, ticker)
           DO UPDATE SET notes=$3, trade_cost=$4, updated_at=NOW()""",
        account_id, ticker.upper(), notes, trade_cost,
    )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# Price Alerts
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/alerts")
async def get_alerts(status: str = "active", token: str = ""):
    pool = await _get_db_pool()
    if status == "all":
        rows = await pool.fetch(
            "SELECT * FROM price_alerts ORDER BY created_at DESC LIMIT 200"
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM price_alerts WHERE status=$1 ORDER BY created_at DESC",
            status,
        )
    return [dict(r) for r in rows]


@app.post("/api/alerts")
async def create_alert(body: dict, token: str = ""):
    ticker = str(body.get("ticker", "")).upper().strip()
    condition = str(body.get("condition", "")).lower()
    target_price = body.get("target_price")
    note = body.get("note") or None
    if not ticker or condition not in ("above", "below") or target_price is None:
        raise HTTPException(400, "ticker, condition (above/below), and target_price required")
    pool = await _get_db_pool()
    row = await pool.fetchrow(
        """INSERT INTO price_alerts (ticker, condition, target_price, note)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        ticker, condition, float(target_price), note,
    )
    return {"ok": True, "id": str(row["id"])}


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, token: str = ""):
    pool = await _get_db_pool()
    await pool.execute(
        "UPDATE price_alerts SET status='dismissed' WHERE id=$1",
        uuid.UUID(alert_id),
    )
    return {"ok": True}


@app.post("/api/alerts/{alert_id}/reactivate")
async def reactivate_alert(alert_id: str, token: str = ""):
    pool = await _get_db_pool()
    await pool.execute(
        "UPDATE price_alerts SET status='active', triggered_at=NULL WHERE id=$1",
        uuid.UUID(alert_id),
    )
    return {"ok": True}


async def _check_price_for_alert(ticker: str, pool, notifier) -> float | None:
    """Fetch latest price via Polygon or yfinance. Returns close price or None."""
    import aiohttp as _aiohttp
    api_key = os.getenv("MASSIVE_API_KEY", "")
    today = __import__("datetime").date.today()
    from_dt = (today - __import__("datetime").timedelta(days=5)).isoformat()
    to_dt = today.isoformat()
    if api_key:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
            f"/{from_dt}/{to_dt}?adjusted=true&sort=desc&limit=1&apiKey={api_key}"
        )
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results:
                            return float(results[0]["c"])
        except Exception:
            pass
    return None


async def _price_alert_loop():
    """Background loop: checks active price alerts every 5 minutes."""
    from notifier.agentmail import Notifier
    notifier = Notifier("alerts")
    await asyncio.sleep(30)  # let startup finish
    while True:
        try:
            if not DB_URL:
                await asyncio.sleep(300)
                continue
            pool = await _get_db_pool()
            alerts = await pool.fetch(
                "SELECT * FROM price_alerts WHERE status='active' ORDER BY ticker"
            )
            # Group by ticker to avoid redundant price fetches
            by_ticker: dict = {}
            for a in alerts:
                by_ticker.setdefault(a["ticker"], []).append(a)

            for ticker, ticker_alerts in by_ticker.items():
                price = await _check_price_for_alert(ticker, pool, notifier)
                if price is None:
                    continue
                for a in ticker_alerts:
                    target = float(a["target_price"])
                    hit = (a["condition"] == "above" and price >= target) or \
                          (a["condition"] == "below" and price <= target)
                    await pool.execute(
                        "UPDATE price_alerts SET last_price=$1, last_checked=NOW() WHERE id=$2",
                        price, a["id"],
                    )
                    if hit:
                        note_part = f" — {a['note']}" if a.get("note") else ""
                        msg = (
                            f"*Price Alert Triggered* 🔔\n"
                            f"{ticker} is now ${price:.2f} "
                            f"({'≥' if a['condition']=='above' else '≤'} ${target:.2f}){note_part}"
                        )
                        await notifier.telegram(msg)
                        await pool.execute(
                            "UPDATE price_alerts SET status='triggered', triggered_at=NOW() WHERE id=$1",
                            a["id"],
                        )
                        log.info("webui.price_alert.triggered",
                                 ticker=ticker, condition=a["condition"],
                                 target=target, price=price)
        except Exception as e:
            log.warning("webui.price_alert_loop.error", error=str(e))
        await asyncio.sleep(300)  # 5 minutes


# ── Options Trader dashboard helpers ─────────────────────────────────────────

@app.get("/api/options/trader/buys")
async def get_trader_ovtlyr_buys(token: str = ""):
    """OVTLYR tickers with active buy signals, sorted by nine_score desc."""
    check_token(token)
    import json as _json
    _redis = await get_redis()

    results: list[dict] = []
    seen: set[str] = set()

    # Bull list
    raw = await _redis.get("ovtlyr:list:bull")
    if raw:
        try:
            for e in _json.loads(raw):
                t = e.get("ticker", "")
                if not t or t in seen:
                    continue
                seen.add(t)
                results.append({
                    "ticker": t,
                    "name": e.get("name", ""),
                    "nine_score": e.get("nine_score"),
                    "signal": e.get("signal", "buy"),
                    "signal_date": e.get("signal_date", ""),
                    "last_price": e.get("last_price"),
                    "source": "bull",
                })
        except Exception:
            pass

    # Screener cache (scanner:ovtlyr:latest)
    screener_raw = await _redis.hgetall("scanner:ovtlyr:latest")
    for ticker, raw_val in screener_raw.items():
        if ticker in seen:
            continue
        try:
            d = _json.loads(raw_val)
        except Exception:
            continue
        direction = (d.get("direction") or d.get("signal") or "").lower()
        if direction not in ("buy", "long", "bull"):
            continue
        seen.add(ticker)
        results.append({
            "ticker": ticker,
            "name": d.get("name", ""),
            "nine_score": d.get("nine_score"),
            "signal": direction,
            "signal_date": d.get("signal_date", ""),
            "last_price": d.get("last_close"),
            "source": "screener",
        })

    results.sort(key=lambda r: (-(r.get("nine_score") or 0), r["ticker"]))
    return {"buys": results, "count": len(results)}


def _tradier_keys() -> tuple[str, str]:
    """Return (api_key, base_url) for the best available Tradier key."""
    env = _read_env_file()
    prod_key = env.get("TRADIER_PRODUCTION_API_KEY") or os.getenv("TRADIER_PRODUCTION_API_KEY", "")
    sand_key = env.get("TRADIER_SANDBOX_API_KEY")    or os.getenv("TRADIER_SANDBOX_API_KEY", "")
    if prod_key and not _is_placeholder(prod_key):
        return prod_key, "https://api.tradier.com/v1"
    if sand_key and not _is_placeholder(sand_key):
        return sand_key, "https://sandbox.tradier.com/v1"
    return "", ""


def _chain_contract(c: dict, otype: str, price: float) -> dict:
    """Normalise one Tradier option contract dict into our chain schema."""
    strike    = float(c.get("strike") or 0)
    bid       = float(c.get("bid") or 0)
    ask       = float(c.get("ask") or 0)
    last      = float(c.get("last") or 0)
    mid       = round((bid + ask) / 2, 2) if bid and ask else last
    intrinsic = round(max(0.0, price - strike) if otype == "call" else max(0.0, strike - price), 2)
    extrinsic = round(max(0.0, mid - intrinsic), 2)
    greeks    = c.get("greeks") or {}

    def _g(key): return round(float(greeks[key]), 6) if greeks.get(key) is not None else None

    iv = greeks.get("mid_iv") or greeks.get("smv_vol")
    return {
        "contract":   c.get("symbol", ""),
        "strike":     strike,
        "expiration": c.get("expiration_date", ""),
        "bid":        bid,
        "ask":        ask,
        "mid":        mid,
        "last":       last,
        "intrinsic":  intrinsic,
        "extrinsic":  extrinsic,
        "iv":         round(float(iv), 4) if iv is not None else None,
        "delta":      _g("delta"),
        "gamma":      _g("gamma"),
        "theta":      _g("theta"),
        "vega":       _g("vega"),
        "volume":     int(c.get("volume") or 0),
        "oi":         int(c.get("open_interest") or 0),
        "itm":        (otype == "call" and price > strike) or (otype == "put" and price < strike),
        "has_position": False,
    }


@app.get("/api/options/trader/chain")
async def get_options_chain_data(ticker: str, account_label: str = "", token: str = ""):
    """
    Options chain routed through the broker gateway for the selected account.
    Uses the account's broker (Tradier / Webull / Alpaca) for live data.
    Falls back to Tradier market API then Yahoo Finance when no account is selected
    or the gateway call fails.
    """
    check_token(token)
    import uuid as _uuid
    import json as _json
    import asyncio as _asyncio

    sym = ticker.upper()

    # ── Route through broker gateway when an account is selected ─────────────
    async def _gateway_chain(acct_label: str) -> dict | None:
        try:
            import redis.asyncio as _aioredis
            _REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
            _r = await _aioredis.from_url(
                _REDIS_URL, encoding="utf-8", decode_responses=True,
                socket_connect_timeout=5, socket_timeout=35,
            )

            req_id = str(_uuid.uuid4())
            cmd: dict = {
                "command":       "get_option_chain",
                "request_id":    req_id,
                "symbol":        sym,
                "issued_by":     "webui",
            }
            if acct_label:
                cmd["account_label"] = acct_label

            await _r.xadd(STREAMS["broker_commands"], cmd)
            result = await _r.blpop([f"broker:reply:{req_id}"], timeout=30)
            await _r.aclose()

            if not result:
                return None
            raw = _json.loads(result[1])
            results_list = raw if isinstance(raw, list) else [raw]
            for r in results_list:
                if r.get("status") == "ok":
                    d = r.get("data", {})
                    # Treat empty chain as a miss so Tradier fallback can run
                    if not d.get("expirations") and not d.get("calls"):
                        log.warning("options_trader.chain.gateway_empty",
                                    ticker=sym, broker=r.get("broker"))
                        continue
                    d["source"] = r.get("broker", "broker")
                    return d
            errs = [r.get("error", "") for r in results_list]
            log.warning("options_trader.chain.gateway_error", ticker=sym, errors=errs)
            return None
        except Exception as e:
            log.warning("options_trader.chain.gateway_failed", ticker=sym, error=str(e))
            return None

    # ── Tradier direct fallback ───────────────────────────────────────────────
    async def _tradier_fallback() -> dict | None:
        api_key, base_url = _tradier_keys()
        if not api_key:
            return None
        import aiohttp as _aiohttp
        hdrs = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        from datetime import date as _dt_date, timedelta as _dt_td
        req_timeout = _aiohttp.ClientTimeout(total=15)
        try:
            async with _aiohttp.ClientSession(headers=hdrs) as s:
                price = 0.0
                try:
                    async with s.get(f"{base_url}/markets/quotes",
                                     params={"symbols": sym, "greeks": "false"},
                                     timeout=req_timeout) as r:
                        if r.status == 200:
                            d = await r.json(content_type=None)
                            q = d.get("quotes", {}).get("quote", {})
                            if isinstance(q, dict):
                                price = float(q.get("last") or q.get("prevclose") or 0)
                except Exception:
                    pass

                expirations: list[str] = []
                async with s.get(f"{base_url}/markets/options/expirations",
                                 params={"symbol": sym, "includeAllRoots": "false"},
                                 timeout=req_timeout) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        raw = d.get("expirations") or {}
                        if raw and raw != "null":
                            dates = raw.get("date", [])
                            expirations = dates if isinstance(dates, list) else [dates]

                if not expirations:
                    return None

                # Filter to ~18 months, cap at 60 to avoid excessive parallelism
                cutoff = (_dt_date.today() + _dt_td(days=548)).isoformat()
                expirations = [e for e in expirations if e <= cutoff][:60]

                async def _fetch_exp(exp: str):
                    try:
                        async with s.get(f"{base_url}/markets/options/chains",
                                         params={"symbol": sym, "expiration": exp, "greeks": "true"},
                                         timeout=req_timeout) as r:
                            if r.status != 200:
                                return []
                            d = await r.json(content_type=None)
                            opts = d.get("options") or {}
                            if not opts or opts == "null":
                                return []
                            raw = opts.get("option", [])
                            return raw if isinstance(raw, list) else [raw]
                    except Exception:
                        return []

                # Limit concurrency to avoid rate-limiting
                _sem = _asyncio.Semaphore(15)
                async def _fetch_exp_sem(exp):
                    async with _sem:
                        return await _fetch_exp(exp)

                chains = await _asyncio.gather(*[_fetch_exp_sem(e) for e in expirations])
                all_calls, all_puts = [], []
                for contracts in chains:
                    for c in contracts:
                        if not isinstance(c, dict):
                            continue
                        otype = (c.get("option_type") or "").lower()
                        rec = _chain_contract(c, otype, price)
                        (all_calls if otype == "call" else all_puts).append(rec)

                return {"ticker": sym, "price": round(price, 2),
                        "expirations": expirations,
                        "calls": all_calls, "puts": all_puts, "source": "tradier"}
        except Exception as e:
            log.warning("options_trader.chain.tradier_fallback_error", ticker=sym, error=str(e))
            return None

    # ── Select source ─────────────────────────────────────────────────────────
    data: dict | None = None

    if account_label:
        data = await _gateway_chain(account_label)

    if data is None:
        # No account selected or gateway failed: try Tradier market API
        data = await _tradier_fallback()

    # ── Mark open positions ───────────────────────────────────────────────────
    if data is None:
        data = {"ticker": sym, "price": 0.0, "expirations": [], "calls": [], "puts": [], "source": "none"}
    data["open_expiries"] = []
    if DB_URL:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                "SELECT strike, expiration_date, option_type FROM option_positions "
                "WHERE underlying=$1 AND status='active'",
                sym,
            )
            # Build lookup by (strike, expiration_date, option_type) — broker symbols differ across sources
            pos_keys: set[tuple] = set()
            for r in rows:
                exp_str = r["expiration_date"].isoformat() if r["expiration_date"] else ""
                pos_keys.add((float(r["strike"]), exp_str, (r["option_type"] or "").lower()))
            data["open_expiries"] = list({k[1] for k in pos_keys})
            for c in data.get("calls", []):
                key = (float(c.get("strike", 0)), c.get("expiration", ""), "call")
                c["has_position"] = key in pos_keys
            for c in data.get("puts", []):
                key = (float(c.get("strike", 0)), c.get("expiration", ""), "put")
                c["has_position"] = key in pos_keys
        except Exception:
            pass

    return data


@app.get("/api/options/trader/ticker-meta")
async def get_trader_ticker_meta(ticker: str, token: str = ""):
    """Earnings date, ex-dividend date and current price for chart markers."""
    check_token(token)
    from shared.data_client import DataClient
    dc  = DataClient()
    sym = ticker.upper()
    result = {"ticker": sym, "price": None, "ex_dividend_date": None,
              "earnings_date": None, "company_name": ""}

    try:
        q = await dc.quote(sym) or {}
        price = q.get("last") or q.get("close") or q.get("prev_close")
        if price:
            result["price"] = round(float(price), 2)
    except Exception:
        pass

    try:
        d = await dc.fundamentals(sym) or {}
        result["company_name"] = d.get("name", "")
    except Exception:
        pass

    try:
        divs = await dc.dividends(sym) or []
        divs = divs if isinstance(divs, list) else divs.get("results") or []
        today_str = date.today().isoformat()
        upcoming = [d for d in divs if d.get("ex_date") and d["ex_date"] >= today_str]
        if upcoming:
            result["ex_dividend_date"] = upcoming[-1]["ex_date"]
    except Exception:
        pass

    try:
        records = await dc.earnings(sym) or []
        records = records if isinstance(records, list) else records.get("results") or []
        today_str = date.today().isoformat()
        upcoming = [e for e in records if e.get("date") and e["date"] >= today_str]
        if upcoming:
            result["earnings_date"] = upcoming[-1]["date"]
    except Exception:
        pass

    return result


@app.get("/api/options/trader/fundamentals/{ticker}")
async def get_trader_fundamentals(ticker: str, token: str = ""):
    """
    Company fundamentals for the Options Trader panel.
    Merges: Polygon/massive (details + market cap) + massive dividends + earnings.
    """
    check_token(token)
    sym = ticker.upper()

    api_key = os.getenv("MASSIVE_API_KEY", "") or _read_env_file().get("MASSIVE_API_KEY", "")

    # ── 1. Polygon ticker details (name, market cap, description, sector) ──────
    details: dict = {}
    if api_key:
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as session:
                url = f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={api_key}"
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        d = (await resp.json()).get("results", {}) or {}
                        details = {
                            "name":        d.get("name"),
                            "description": d.get("description"),
                            "sector":      _sic_to_sector(d.get("sic_code")),
                            "sic_desc":    d.get("sic_description"),
                            "market_cap":  d.get("market_cap"),
                            "employees":   d.get("total_employees"),
                            "exchange":    d.get("primary_exchange"),
                            "homepage":    d.get("homepage_url"),
                            "list_date":   d.get("list_date"),
                        }
        except Exception as ex:
            log.warning("fundamentals.polygon_error", ticker=sym, error=str(ex))

    # ── 2. Quarterly financials — EPS TTM (last 4 quarters summed) ───────────
    earnings: dict = {}
    if api_key:
        try:
            import aiohttp as _aiohttp
            url = (
                f"https://api.polygon.io/vX/reference/financials"
                f"?ticker={sym}&timeframe=quarterly&limit=4"
                f"&sort=period_of_report_date&order=desc&apiKey={api_key}"
            )
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        fin_results = (await resp.json()).get("results", [])
                        eps_ttm = 0.0
                        rev_ttm = 0.0
                        quarters_counted = 0
                        for q in fin_results:
                            inc = (q.get("financials") or {}).get("income_statement") or {}
                            eps_q = (inc.get("basic_earnings_per_share") or {}).get("value")
                            rev_q = (inc.get("revenues") or {}).get("value")
                            if eps_q is not None:
                                eps_ttm += float(eps_q)
                                quarters_counted += 1
                            if rev_q is not None:
                                rev_ttm += float(rev_q)
                        if quarters_counted > 0:
                            earnings["eps"] = round(eps_ttm, 4)
                        if rev_ttm > 0:
                            earnings["revenue_ttm"] = round(rev_ttm, 0)
        except Exception as ex:
            log.warning("fundamentals.polygon_financials_error", ticker=sym, error=str(ex))

    # ── 3. Upcoming dividend (massive.com) ─────────────────────────────────────
    div: dict = {}
    if api_key:
        try:
            from datetime import date as _date
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {api_key}"}
            ) as session:
                # Next upcoming ex-dividend date
                params = {
                    "ticker": sym,
                    "ex_dividend_date.gte": _date.today().isoformat(),
                    "limit": 1,
                    "sort": "ex_dividend_date",
                    "order": "asc",
                }
                async with session.get(
                    "https://api.massive.com/stocks/v1/dividends",
                    params=params,
                    timeout=_aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        results = (await resp.json()).get("results", [])
                        if results:
                            r = results[0]
                            div = {
                                "ex_dividend_date": r.get("ex_dividend_date"),
                                "pay_date":         r.get("pay_date"),
                                "amount":           r.get("cash_amount"),
                                "frequency":        r.get("frequency"),
                            }
        except Exception as ex:
            log.warning("fundamentals.massive_div_error", ticker=sym, error=str(ex))

    # ── 5. Earnings date via Market Data Gateway ──────────────────────────────────
    try:
        from shared.data_client import DataClient as _DC
        records = await _DC().earnings(sym) or []
        records = records if isinstance(records, list) else records.get("results") or []
        if records:
            today_str = date.today().isoformat()
            upcoming = [e for e in records if e.get("date") and e["date"] >= today_str]
            past = [e for e in records if e.get("date") and e["date"] < today_str]
            if upcoming:
                earnings["earnings_date"] = upcoming[-1]["date"]
            if past:
                last = past[0]
                if last.get("eps_actual") is not None:
                    earnings["eps"]          = last.get("eps_actual")
                if last.get("eps_estimate") is not None:
                    earnings["eps_estimate"] = last.get("eps_estimate")
    except Exception as e:
        log.warning("fundamentals.massive_earnings_error", ticker=sym, error=str(e))

    return {
        "ticker":   sym,
        "details":  details,
        "dividend": div,
        "earnings": earnings,
    }


@app.get("/api/options/trader/risk")
async def get_trader_risk_data(account: str = "", token: str = ""):
    """Risk calculator: available cash, open position count, portfolio risk gauge."""
    check_token(token)

    cash = 0.0
    try:
        pos_data = _positions_cache.get("data") or {}
        for acct in pos_data.get("accounts", []):
            if account and acct.get("label", "") != account:
                continue
            bal = acct.get("balances") or {}
            if isinstance(bal, list):
                bal = bal[0] if bal else {}
            if isinstance(bal, dict):
                # Tradier uses total_cash; Alpaca/Webull use cash or buying_power
                c = float(bal.get("cash") or bal.get("buying_power")
                          or bal.get("total_cash") or bal.get("net_liquidation") or 0)
                if not c:
                    margin = bal.get("margin") or {}
                    c = float(margin.get("option_buying_power") or 0)
                cash += c
    except Exception:
        pass

    position_count = 0
    total_risk = 0.0
    positions_risk: list[dict] = []
    if DB_URL:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                """SELECT underlying, qty, entry_price, strike, option_type,
                          expiration_date, account_label
                   FROM option_positions WHERE status='active'
                   AND ($1 = '' OR account_label = $1)""",
                account,
            )
            position_count = len(rows)
            for r in rows:
                max_loss = float(r["entry_price"] or 0) * float(r["qty"] or 0) * 100
                total_risk += max_loss
                positions_risk.append({
                    "underlying":  r["underlying"],
                    "option_type": r["option_type"],
                    "strike":      float(r["strike"] or 0),
                    "expiration":  r["expiration_date"].isoformat() if r["expiration_date"] else "",
                    "qty":         float(r["qty"] or 0),
                    "max_loss":    round(max_loss, 2),
                    "account":     r["account_label"],
                })
        except Exception:
            pass

    portfolio_value = cash + total_risk
    risk_fraction = round(total_risk / portfolio_value, 4) if portfolio_value > 0 else 0.0

    return {
        "available_cash":  round(cash, 2),
        "position_count":  position_count,
        "total_risk":      round(total_risk, 2),
        "risk_fraction":   risk_fraction,
        "positions_risk":  positions_risk,
    }


# ── Shadow Account ────────────────────────────────────────────────────────────

@app.post("/api/shadow/run")
async def shadow_run(body: dict):
    """
    Run a Shadow Account / Counterfactual P&L analysis.

    Body fields:
      date_from      str   ISO date (default: 30 days ago)
      date_to        str   ISO date (default: today)
      account_label  str   optional account filter
    """
    from .shadow_account import run_analysis as _shadow_run

    pool = await _get_db_pool()
    today  = date.today()
    d_from = date.fromisoformat(body.get("date_from") or str(today - timedelta(days=30)))
    d_to   = date.fromisoformat(body.get("date_to")   or str(today))
    acct   = body.get("account_label") or None

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")

    try:
        result = await _shadow_run(pool, d_from, d_to, acct, openrouter_key)
    except Exception as e:
        import traceback
        log.error("shadow_run.error", error=str(e), traceback=traceback.format_exc())
        raise HTTPException(500, f"Shadow analysis failed: {e}")

    if "error" in result:
        return result  # frontend renders empty state with message

    try:
        run_id = await pool.fetchval(
            """
            INSERT INTO shadow_runs
              (date_from, date_to, account_label, trade_count, actual_pnl,
               ideal_pnl, discipline_cost, categories, rules, top5, trades_detail)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb)
            RETURNING id
            """,
            d_from, d_to, acct,
            result["trade_count"], result["actual_pnl"], result["ideal_pnl"],
            result["discipline_cost"],
            json.dumps(result["categories"]),
            json.dumps(result["rules"]),
            json.dumps(result["counterfactual_top5"]),
            json.dumps(result["trades"]),
        )
        result["run_id"] = str(run_id)
    except Exception as e:
        log.warning("shadow_run.persist_fail", error=str(e))
        result["run_id"] = None

    return result


@app.get("/api/shadow/accounts")
async def shadow_accounts(token: str = ""):
    """Return accounts that have trade data available for hindsight analysis."""
    check_token(token)
    pool = await _get_db_pool()
    eq_rows = await pool.fetch("""
        SELECT account_id AS label, COUNT(*) AS cnt
        FROM trades
        WHERE entry_price IS NOT NULL AND status IN ('closed', 'fill')
        GROUP BY account_id
    """)
    opt_rows = await pool.fetch("""
        SELECT op.account_label AS label, COUNT(*) AS cnt
        FROM (
            SELECT DISTINCT ON (op.account_label, op.underlying, op.strike, op.expiration_date, op.entry_price, op.entry_date)
                op.account_label
            FROM option_trade_log otl
            JOIN option_positions op ON op.id = otl.position_id
            WHERE otl.event_type = 'closed'
              AND otl.ts::date != op.entry_date
              AND op.entry_price IS NOT NULL
            ORDER BY op.account_label, op.underlying, op.strike, op.expiration_date, op.entry_price, op.entry_date, otl.ts ASC
        ) op
        GROUP BY op.account_label
    """)
    counts: dict = {}
    for r in eq_rows:
        counts[r["label"]] = counts.get(r["label"], 0) + r["cnt"]
    for r in opt_rows:
        counts[r["label"]] = counts.get(r["label"], 0) + r["cnt"]
    return {"accounts": [{"label": k, "trade_count": v} for k, v in sorted(counts.items())]}


@app.get("/api/shadow/history")
async def shadow_history(limit: int = 20, token: str = ""):
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT id, ts, date_from, date_to, account_label, trade_count,
               actual_pnl, ideal_pnl, discipline_cost, categories
        FROM shadow_runs
        ORDER BY ts DESC
        LIMIT $1
        """,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        for k in ("categories",):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        out.append(d)
    return out


@app.get("/api/shadow/run/{run_id}")
async def shadow_run_detail(run_id: str, token: str = ""):
    check_token(token)
    pool = await _get_db_pool()
    row = await pool.fetchrow("SELECT * FROM shadow_runs WHERE id=$1::uuid", run_id)
    if not row:
        raise HTTPException(404, "Run not found")
    d = dict(row)
    for k in ("categories", "rules", "top5", "trades_detail"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


# ── FinanceToolkit Analytics (A–F) ───────────────────────────────────────────
# Pure-math helpers — no external deps beyond stdlib + aiohttp (already present)

import math as _math
import statistics as _statistics
import zipfile as _zipfile
import io as _io


def _pct_returns(nav_series: list[float]) -> list[float]:
    """Daily percentage returns from NAV series."""
    return [
        (nav_series[i] - nav_series[i - 1]) / nav_series[i - 1]
        for i in range(1, len(nav_series))
        if nav_series[i - 1] != 0
    ]


def _annualise(r: float, n: int = 252) -> float:
    return (1 + r) ** n - 1 if r > -1 else -1.0


def _sharpe(returns: list[float], rf_daily: float = 0.0, ann: int = 252) -> float:
    excess = [r - rf_daily for r in returns]
    if len(excess) < 2:
        return 0.0
    mu  = sum(excess) / len(excess)
    std = _statistics.stdev(excess)
    return (mu / std * _math.sqrt(ann)) if std > 0 else 0.0


def _sortino(returns: list[float], rf_daily: float = 0.0, ann: int = 252) -> float:
    excess   = [r - rf_daily for r in returns]
    downside = [r for r in excess if r < 0]
    if len(downside) < 2:
        return 0.0
    mu      = sum(excess) / len(excess)
    dd_std  = _statistics.stdev(downside)
    return (mu / dd_std * _math.sqrt(ann)) if dd_std > 0 else 0.0


def _max_drawdown(nav_series: list[float]) -> float:
    peak = nav_series[0]
    mdd  = 0.0
    for v in nav_series:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return mdd


def _ulcer_index(nav_series: list[float]) -> float:
    peak = nav_series[0]
    sq_sum = 0.0
    for v in nav_series:
        if v > peak:
            peak = v
        dd_pct = (peak - v) / peak * 100 if peak > 0 else 0.0
        sq_sum += dd_pct ** 2
    return _math.sqrt(sq_sum / len(nav_series)) if nav_series else 0.0


def _var_historical(returns: list[float], level: float = 0.05) -> float:
    """Historical VaR at given significance level (loss as positive number)."""
    s = sorted(returns)
    idx = max(0, int(len(s) * level) - 1)
    return -s[idx]


def _cvar(returns: list[float], level: float = 0.05) -> float:
    """Expected Shortfall (CVaR) at given level."""
    s = sorted(returns)
    cutoff = int(len(s) * level)
    tail   = s[:max(1, cutoff)]
    return -sum(tail) / len(tail)


def _skewness(returns: list[float]) -> float:
    if len(returns) < 3:
        return 0.0
    n  = len(returns)
    mu = sum(returns) / n
    s  = _statistics.stdev(returns)
    if s == 0:
        return 0.0
    return (sum((r - mu) ** 3 for r in returns) / n) / (s ** 3)


def _kurtosis(returns: list[float]) -> float:
    if len(returns) < 4:
        return 0.0
    n  = len(returns)
    mu = sum(returns) / n
    s  = _statistics.stdev(returns)
    if s == 0:
        return 0.0
    return (sum((r - mu) ** 4 for r in returns) / n) / (s ** 4) - 3.0  # excess kurtosis


def _beta_alpha(port_ret: list[float], mkt_ret: list[float], rf_daily: float = 0.0) -> tuple[float, float]:
    """OLS beta and Jensen's alpha (annualised) against market."""
    n = min(len(port_ret), len(mkt_ret))
    if n < 5:
        return 1.0, 0.0
    pr  = port_ret[-n:]
    mr  = mkt_ret[-n:]
    ep  = [r - rf_daily for r in pr]
    em  = [r - rf_daily for r in mr]
    mu_ep = sum(ep) / n
    mu_em = sum(em) / n
    cov   = sum((ep[i] - mu_ep) * (em[i] - mu_em) for i in range(n)) / (n - 1)
    var_m = sum((r - mu_em) ** 2 for r in em) / (n - 1)
    beta  = cov / var_m if var_m > 0 else 1.0
    alpha_daily = mu_ep - beta * mu_em
    return round(beta, 4), round(_annualise(alpha_daily), 4)


def _information_ratio(port_ret: list[float], bench_ret: list[float], ann: int = 252) -> float:
    n = min(len(port_ret), len(bench_ret))
    if n < 2:
        return 0.0
    active = [port_ret[-n + i] - bench_ret[-n + i] for i in range(n)]
    mu_a   = sum(active) / n
    te     = _statistics.stdev(active)
    return (mu_a / te * _math.sqrt(ann)) if te > 0 else 0.0


# ── A: Performance metrics ────────────────────────────────────────────────────

async def _fetch_spx_returns(days: int) -> list[float]:
    """Fetch SPX daily returns from Polygon (I:SPX index, last N days). Cached 6h."""
    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return []
    try:
        _r = await get_redis()
        ck = f"analytics:spx_ret:{days}"
        cached = await _r.get(ck)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    try:
        import aiohttp as _ah
        from datetime import date as _date, timedelta as _td
        from_dt = (_date.today() - _td(days=days + 10)).isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/day"
            f"/{from_dt}/{_date.today().isoformat()}?adjusted=true&sort=asc&limit={days + 50}&apiKey={api_key}"
        )
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        bars   = data.get("results") or []
        closes = [float(b["c"]) for b in bars if b.get("c")]
        rets   = _pct_returns(closes)
        if _r:
            await _r.setex(ck, 21600, json.dumps(rets))
        return rets
    except Exception:
        return []


@app.get("/api/analytics/performance")
async def analytics_performance(days: int = 252, mode: str = "live", token: str = ""):
    """Risk-adjusted return metrics: Sharpe, Sortino, Treynor, Jensen's alpha, Info ratio, beta."""
    check_token(token)
    cache_key = f"analytics:perf:{mode}:{days}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT snapshot_date, SUM(total_nav) AS nav, SUM(day_pnl) AS dpnl
           FROM portfolio_snapshots
           WHERE mode = $1 AND snapshot_date >= CURRENT_DATE - ($2 || ' days')::INTERVAL
           GROUP BY snapshot_date ORDER BY snapshot_date ASC""",
        mode, str(days),
    )
    if len(rows) < 5:
        return {"error": "Insufficient NAV history (need 5+ snapshots)", "count": len(rows)}

    nav_series = [float(r["nav"]) for r in rows]
    port_ret   = _pct_returns(nav_series)
    spx_ret    = await _fetch_spx_returns(days)

    # Align lengths (take last N)
    n        = min(len(port_ret), len(spx_ret)) if spx_ret else len(port_ret)
    pr_align = port_ret[-n:] if n else port_ret
    mr_align = spx_ret[-n:]  if (spx_ret and n) else []

    rf_daily = 0.045 / 252  # ~4.5% annual risk-free
    sharpe   = _sharpe(pr_align, rf_daily)
    sortino  = _sortino(pr_align, rf_daily)
    beta, alpha = _beta_alpha(pr_align, mr_align, rf_daily) if mr_align else (1.0, 0.0)
    treynor  = ((sum(pr_align) / len(pr_align) - rf_daily) / beta * _math.sqrt(252)) if beta != 0 else 0.0
    info_r   = _information_ratio(pr_align, mr_align) if mr_align else 0.0
    total_r  = (nav_series[-1] - nav_series[0]) / nav_series[0] if nav_series[0] else 0.0
    ann_r    = _annualise(sum(pr_align) / len(pr_align)) if pr_align else 0.0
    vol      = _statistics.stdev(pr_align) * _math.sqrt(252) if len(pr_align) > 1 else 0.0

    result = {
        "sharpe":          round(sharpe, 3),
        "sortino":         round(sortino, 3),
        "treynor":         round(treynor, 4),
        "jensen_alpha":    round(alpha, 4),
        "information_ratio": round(info_r, 3),
        "beta":            round(beta, 3),
        "total_return":    round(total_r * 100, 2),
        "annualised_return": round(ann_r * 100, 2),
        "volatility_ann":  round(vol * 100, 2),
        "days":            len(rows),
        "mode":            mode,
    }
    try:
        if _r:
            await _r.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass
    return result


# ── B: Risk metrics ───────────────────────────────────────────────────────────

@app.get("/api/analytics/risk")
async def analytics_risk(days: int = 252, mode: str = "live", token: str = ""):
    """Portfolio risk metrics: VaR, CVaR, Max Drawdown, Ulcer Index, skewness, kurtosis."""
    check_token(token)
    cache_key = f"analytics:risk:{mode}:{days}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT snapshot_date, SUM(total_nav) AS nav
           FROM portfolio_snapshots
           WHERE mode = $1 AND snapshot_date >= CURRENT_DATE - ($2 || ' days')::INTERVAL
           GROUP BY snapshot_date ORDER BY snapshot_date ASC""",
        mode, str(days),
    )
    if len(rows) < 5:
        return {"error": "Insufficient NAV history", "count": len(rows)}

    nav_series = [float(r["nav"]) for r in rows]
    rets       = _pct_returns(nav_series)
    if len(rets) < 5:
        return {"error": "Insufficient return observations"}

    mdd      = _max_drawdown(nav_series)
    ulcer    = _ulcer_index(nav_series)
    var95    = _var_historical(rets, 0.05)
    var99    = _var_historical(rets, 0.01)
    cvar95   = _cvar(rets, 0.05)
    cvar99   = _cvar(rets, 0.01)
    skew     = _skewness(rets)
    kurt     = _kurtosis(rets)
    ann_r    = _annualise(sum(rets) / len(rets)) if rets else 0.0
    calmar   = ann_r / mdd if mdd > 0 else 0.0

    result = {
        "var_95":          round(var95 * 100, 3),
        "var_99":          round(var99 * 100, 3),
        "cvar_95":         round(cvar95 * 100, 3),
        "cvar_99":         round(cvar99 * 100, 3),
        "max_drawdown":    round(mdd * 100, 2),
        "ulcer_index":     round(ulcer, 3),
        "skewness":        round(skew, 3),
        "kurtosis":        round(kurt, 3),
        "calmar_ratio":    round(calmar, 3),
        "days":            len(rows),
    }
    try:
        if _r:
            await _r.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass
    return result


# ── C: Technical indicators ───────────────────────────────────────────────────

def _rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """RSI at every bar; None for the first `period` bars."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    result: list[float | None] = [None] * (period + 1)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        result.append(round(100 - 100 / (1 + rs), 2))
    return result


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k   = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _macd_series(closes: list[float]) -> tuple[list, list, list]:
    if len(closes) < 35:
        return [], [], []
    ema12     = _ema(closes, 12)
    ema26     = _ema(closes, 26)
    macd_line = [round(ema12[i] - ema26[i], 4) for i in range(len(ema12))]
    signal    = _ema(macd_line, 9)
    hist      = [round(macd_line[i] - signal[i], 4) for i in range(len(signal))]
    return macd_line, [round(v, 4) for v in signal], hist


def _bb_series(closes: list[float], period: int = 20, std_mult: float = 2.0) -> tuple[list, list, list]:
    upper, mid, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None); mid.append(None); lower.append(None)
            continue
        window = closes[i - period + 1: i + 1]
        m      = sum(window) / period
        s      = _statistics.stdev(window)
        upper.append(round(m + std_mult * s, 4))
        mid.append(round(m, 4))
        lower.append(round(m - std_mult * s, 4))
    return upper, mid, lower


@app.get("/api/analytics/technicals/{ticker}")
async def analytics_technicals(ticker: str, token: str = ""):
    """RSI-14, MACD(12,26,9), Bollinger Bands(20,2) with full series for charting. Cached 30m."""
    check_token(token)
    sym       = ticker.upper()
    cache_key = f"analytics:tech2:{sym}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "No API key"}

    try:
        import aiohttp as _ah
        from datetime import date as _date, timedelta as _td
        from_dt = (_date.today() - _td(days=120)).isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
            f"/{from_dt}/{_date.today().isoformat()}?adjusted=true&sort=asc&limit=90&apiKey={api_key}"
        )
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {"error": f"Polygon HTTP {resp.status}"}
                data = await resp.json()
        bars   = data.get("results") or []
        dates  = [b.get("t", 0) for b in bars if b.get("c")]
        closes = [float(b["c"]) for b in bars if b.get("c")]
        # keep last 60 bars for chart readability
        dates  = dates[-60:]
        closes = closes[-60:]
        if len(closes) < 20:
            return {"error": "Insufficient price history"}

        rsi_s                    = _rsi_series(closes)[-60:]
        macd_l, macd_sig, macd_h = _macd_series(closes)
        bb_u, bb_m, bb_l         = _bb_series(closes)
        # truncate to 60
        macd_l   = macd_l[-60:];  macd_sig = macd_sig[-60:];  macd_h = macd_h[-60:]
        bb_u     = bb_u[-60:];    bb_m     = bb_m[-60:];      bb_l   = bb_l[-60:]

        # date labels MM-DD
        from datetime import datetime as _dt
        date_labels = [_dt.fromtimestamp(t / 1000).strftime("%m-%d") if t else "" for t in dates]

        result = {
            "ticker":      sym,
            "close":       closes[-1],
            "bars":        len(closes),
            "dates":       date_labels,
            "closes":      [round(v, 4) for v in closes],
            "rsi":         rsi_s,
            "macd":        macd_l,
            "macd_signal": macd_sig,
            "macd_hist":   macd_h,
            "bb_upper":    bb_u,
            "bb_mid":      bb_m,
            "bb_lower":    bb_l,
        }
        try:
            if _r:
                await _r.setex(cache_key, 1800, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e)}


# ── D: Financial ratios ───────────────────────────────────────────────────────

@app.get("/api/analytics/fundamentals/{ticker}")
async def analytics_fundamentals(ticker: str, token: str = ""):
    """P/E, P/B, EV/EBITDA, current ratio, debt/equity, ROE, ROA, gross margin. Cached 12h."""
    check_token(token)
    sym       = ticker.upper()
    cache_key = f"analytics:fund:{sym}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "No API key"}

    try:
        import aiohttp as _ah

        async def _pg(url: str):
            async with _ah.ClientSession() as s:
                async with s.get(url, timeout=_ah.ClientTimeout(total=8)) as r:
                    return await r.json() if r.status == 200 else {}

        detail_url = f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={api_key}"
        fin_url    = (
            f"https://api.polygon.io/vX/reference/financials"
            f"?ticker={sym}&timeframe=quarterly&limit=4&sort=period_of_report_date&order=desc&apiKey={api_key}"
        )
        detail, fin = await asyncio.gather(_pg(detail_url), _pg(fin_url))

        res_det   = detail.get("results", {})
        market_cap = float(res_det.get("market_cap") or 0)

        quarters = (fin.get("results") or [])[:4]

        def _sum_field(section: str, field: str) -> float:
            return sum(
                float((q.get("financials", {}).get(section, {}).get(field, {}) or {}).get("value") or 0)
                for q in quarters
            )

        def _latest_field(section: str, field: str) -> float:
            for q in quarters:
                v = (q.get("financials", {}).get(section, {}).get(field, {}) or {}).get("value")
                if v is not None:
                    return float(v)
            return 0.0

        # Income statement (TTM)
        revenue_ttm     = _sum_field("income_statement", "revenues")
        net_income_ttm  = _sum_field("income_statement", "net_income_loss")
        gross_profit_ttm = _sum_field("income_statement", "gross_profit")
        ebitda_ttm      = _sum_field("income_statement", "operating_income_loss")
        eps_ttm         = _sum_field("income_statement", "basic_earnings_per_share")

        # Balance sheet (most recent quarter)
        total_assets    = _latest_field("balance_sheet", "assets")
        total_equity    = _latest_field("balance_sheet", "equity")
        curr_assets     = _latest_field("balance_sheet", "current_assets")
        curr_liabilities = _latest_field("balance_sheet", "current_liabilities")
        long_term_debt  = _latest_field("balance_sheet", "long_term_debt")
        total_liabilities = _latest_field("balance_sheet", "liabilities")

        pe  = round(market_cap / net_income_ttm, 2) if net_income_ttm > 0 else None
        pb  = round(market_cap / total_equity, 2)   if total_equity  > 0 else None
        ev  = market_cap + long_term_debt
        ev_ebitda = round(ev / ebitda_ttm, 2)       if ebitda_ttm   > 0 else None
        cr  = round(curr_assets / curr_liabilities, 2) if curr_liabilities > 0 else None
        de  = round((total_liabilities) / total_equity, 2) if total_equity > 0 else None
        roe = round(net_income_ttm / total_equity * 100, 2)  if total_equity > 0 else None
        roa = round(net_income_ttm / total_assets * 100, 2)  if total_assets  > 0 else None
        gm  = round(gross_profit_ttm / revenue_ttm * 100, 2) if revenue_ttm   > 0 else None
        result = {
            "ticker":        sym,
            "market_cap":    market_cap,
            "pe_ttm":        pe,
            "pb":            pb,
            "ev_ebitda":     ev_ebitda,
            "current_ratio": cr,
            "debt_equity":   de,
            "roe":           roe,
            "roa":           roa,
            "gross_margin":  gm,
            "eps_ttm":       round(eps_ttm, 4),
            "revenue_ttm":   revenue_ttm,
            "net_income_ttm": net_income_ttm,
        }
        try:
            if _r:
                await _r.setex(cache_key, 43200, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e)}


# ── E: Fama-French 5-factor attribution ───────────────────────────────────────

async def _fetch_ff5_factors(days: int = 252) -> dict[str, list[float]]:
    """
    Download daily F-F 5-factor data from Dartmouth. Cached 24h in Redis.
    Returns dict: {"Mkt-RF": [...], "SMB": [...], "HML": [...], "RMW": [...], "CMA": [...], "RF": [...]}
    Values are in percent — caller divides by 100.
    """
    cache_key = f"analytics:ff5:{days}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    url = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
    try:
        import aiohttp as _ah
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return {}
                raw = await resp.read()

        with _zipfile.ZipFile(_io.BytesIO(raw)) as zf:
            name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
            text = zf.read(name).decode("utf-8", errors="ignore")

        # Skip header lines until we hit the data (YYYYMMDD,float,...)
        factors: dict[str, list[float]] = {k: [] for k in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF")}
        col_map = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if parts[0] == "" and col_map is None:
                # header row: ,Mkt-RF,SMB,HML,RMW,CMA,RF
                col_map = parts[1:]
                continue
            if col_map is None or not parts[0].isdigit() or len(parts[0]) != 8:
                continue
            try:
                for i, k in enumerate(col_map):
                    if k in factors:
                        factors[k].append(float(parts[i + 1]))
            except (ValueError, IndexError):
                continue

        # Trim to last N trading days
        for k in factors:
            factors[k] = factors[k][-days:]

        try:
            if _r:
                await _r.setex(cache_key, 86400, json.dumps(factors))
        except Exception:
            pass
        return factors
    except Exception as e:
        log.warning("analytics.ff5_fetch_failed", error=str(e))
        return {}


def _ols_regression(y: list[float], xs: list[list[float]]) -> dict:
    """Minimal OLS via normal equations. Returns {alpha, betas, r_squared}."""
    n = len(y)
    k = len(xs)
    if n < k + 5:
        return {}

    # Build design matrix X (add intercept column)
    X = [[1.0] + [xs[j][i] for j in range(k)] for i in range(n)]

    # XtX and Xty
    xt = [[X[i][j] for i in range(n)] for j in range(k + 1)]
    XtX = [[sum(xt[a][i] * X[i][b] for i in range(n)) for b in range(k + 1)] for a in range(k + 1)]
    Xty = [sum(xt[a][i] * y[i] for i in range(n)) for a in range(k + 1)]

    # Gaussian elimination
    m = k + 1
    aug = [XtX[i][:] + [Xty[i]] for i in range(m)]
    for col in range(m):
        pivot = max(range(col, m), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return {}
        for row in range(m):
            if row != col:
                f = aug[row][col] / aug[col][col]
                for j in range(m + 1):
                    aug[row][j] -= f * aug[col][j]
    coeffs = [aug[i][m] / aug[i][i] for i in range(m)]

    # R²
    y_mean = sum(y) / n
    ss_tot = sum((v - y_mean) ** 2 for v in y)
    y_hat  = [sum(coeffs[j] * X[i][j] for j in range(m)) for i in range(n)]
    ss_res = sum((y[i] - y_hat[i]) ** 2 for i in range(n))
    r2     = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {"alpha": coeffs[0], "betas": coeffs[1:], "r_squared": r2}


@app.get("/api/analytics/factors")
async def analytics_factors(days: int = 252, mode: str = "live", token: str = ""):
    """Fama-French 5-factor attribution. Factor data from Dartmouth (cached 24h)."""
    check_token(token)
    cache_key = f"analytics:factors:{mode}:{days}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT SUM(total_nav) AS nav
           FROM portfolio_snapshots
           WHERE mode = $1 AND snapshot_date >= CURRENT_DATE - ($2 || ' days')::INTERVAL
           GROUP BY snapshot_date ORDER BY snapshot_date ASC""",
        mode, str(days),
    )
    if len(rows) < 20:
        return {"error": "Insufficient NAV history (need 20+ snapshots)"}

    nav_series = [float(r["nav"]) for r in rows]
    port_ret   = _pct_returns(nav_series)

    ff5 = await _fetch_ff5_factors(days)
    if not ff5:
        return {"error": "Could not fetch Fama-French factor data from Dartmouth"}

    # Align: take last N matching length
    n = min(len(port_ret), len(ff5.get("Mkt-RF", [])))
    if n < 20:
        return {"error": f"Insufficient overlapping data ({n} days)"}

    rf_daily  = [v / 100 for v in ff5["RF"][-n:]]
    excess_r  = [port_ret[-n + i] - rf_daily[i] for i in range(n)]
    mkt_rf    = [v / 100 for v in ff5["Mkt-RF"][-n:]]
    smb       = [v / 100 for v in ff5["SMB"][-n:]]
    hml       = [v / 100 for v in ff5["HML"][-n:]]
    rmw       = [v / 100 for v in ff5["RMW"][-n:]]
    cma       = [v / 100 for v in ff5["CMA"][-n:]]

    reg = _ols_regression(excess_r, [mkt_rf, smb, hml, rmw, cma])
    if not reg:
        return {"error": "Regression failed"}

    betas   = reg["betas"]
    alpha_d = reg["alpha"]
    ann_alpha = _annualise(alpha_d)

    result = {
        "alpha_annualised": round(ann_alpha * 100, 3),
        "mkt_rf_beta":      round(betas[0], 4),
        "smb_beta":         round(betas[1], 4),
        "hml_beta":         round(betas[2], 4),
        "rmw_beta":         round(betas[3], 4),
        "cma_beta":         round(betas[4], 4),
        "r_squared":        round(reg["r_squared"], 4),
        "days":             n,
        "factor_source":    "Dartmouth (Ken French Data Library)",
    }
    try:
        if _r:
            await _r.setex(cache_key, 21600, json.dumps(result))
    except Exception:
        pass
    return result


# ── F: Portfolio analytics (rolling + benchmark) ──────────────────────────────

@app.get("/api/analytics/portfolio")
async def analytics_portfolio(days: int = 90, mode: str = "live", token: str = ""):
    """Rolling Sharpe (21d), drawdown timeline, benchmark comparison vs SPX."""
    check_token(token)
    cache_key = f"analytics:port:{mode}:{days}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT snapshot_date, SUM(total_nav) AS nav
           FROM portfolio_snapshots
           WHERE mode = $1 AND snapshot_date >= CURRENT_DATE - ($2 || ' days')::INTERVAL
           GROUP BY snapshot_date ORDER BY snapshot_date ASC""",
        mode, str(days),
    )
    if len(rows) < 5:
        return {"error": "Insufficient NAV history", "series": []}

    dates      = [str(r["snapshot_date"]) for r in rows]
    nav_series = [float(r["nav"]) for r in rows]
    port_ret   = _pct_returns(nav_series)
    spx_ret    = await _fetch_spx_returns(days)

    rf_daily = 0.045 / 252

    # Rolling 21-day Sharpe (need at least 22 points)
    roll_sharpe: list[float | None] = [None] * (min(21, len(port_ret)))
    for i in range(21, len(port_ret) + 1):
        window = port_ret[i - 21:i]
        roll_sharpe.append(_sharpe(window, rf_daily, 252))

    # Drawdown series
    peak = nav_series[0]
    dd_series: list[float] = []
    for v in nav_series:
        if v > peak:
            peak = v
        dd_series.append(round((peak - v) / peak * 100, 2) if peak > 0 else 0.0)

    # Benchmark indexed to portfolio start
    bench_indexed: list[float | None] = []
    if spx_ret:
        n       = min(len(port_ret), len(spx_ret))
        base    = nav_series[0]
        bv      = base
        bench_indexed.append(base)
        for i in range(min(n, len(dates) - 1)):
            bv *= (1 + spx_ret[-(n) + i])
            bench_indexed.append(round(bv, 2))

    # Pad/trim benchmark to match dates length
    while len(bench_indexed) < len(dates):
        bench_indexed.append(None)
    bench_indexed = bench_indexed[:len(dates)]

    series = [
        {
            "date":         dates[i],
            "nav":          round(nav_series[i], 2),
            "drawdown_pct": dd_series[i],
            "rolling_sharpe": round(roll_sharpe[i], 3) if (i < len(roll_sharpe) and roll_sharpe[i] is not None) else None,
            "benchmark_nav": bench_indexed[i] if i < len(bench_indexed) else None,
        }
        for i in range(len(dates))
    ]

    # Summary stats
    n_align = min(len(port_ret), len(spx_ret)) if spx_ret else 0
    if n_align:
        pr = port_ret[-n_align:]
        mr = spx_ret[-n_align:]
        port_cum  = round(((1 + sum(pr) / len(pr)) ** len(pr) - 1) * 100, 2)
        bench_cum = round(((1 + sum(mr) / len(mr)) ** len(mr) - 1) * 100, 2)
        excess_cum = round(port_cum - bench_cum, 2)
    else:
        pr = port_ret
        port_cum = round(((1 + sum(pr) / len(pr)) ** len(pr) - 1) * 100, 2) if pr else 0.0
        bench_cum = excess_cum = None

    result = {
        "series":         series,
        "port_return_pct": port_cum,
        "bench_return_pct": bench_cum,
        "excess_return_pct": excess_cum,
        "max_drawdown_pct": round(_max_drawdown(nav_series) * 100, 2),
        "days":           len(rows),
        "benchmark":      "I:SPX",
    }
    try:
        if _r:
            await _r.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass
    return result


# ── LBO / DCF / Optimize / Yield Curve / Breadth / TSMOM ─────────────────────

def _poly_fin(ic: dict, bs: dict, cf: dict, key: str, fallback: float = 0.0) -> float:
    """Extract a Polygon financials value from ic/bs/cf dicts by key name."""
    for d in (ic, bs, cf):
        v = d.get(key)
        if v is not None:
            return float(v.get("value", 0) or 0)
    return fallback


async def _fetch_poly_financials(ticker: str, api_key: str) -> dict:
    """Fetch the most-recent annual financial statement from Polygon vX/reference/financials."""
    import aiohttp as _ah
    url = (f"https://api.polygon.io/vX/reference/financials"
           f"?ticker={ticker}&timeframe=annual&limit=2&apiKey={api_key}")
    try:
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
        results = data.get("results") or []
        if not results:
            return {}
        r  = results[0]
        ic = r.get("financials", {}).get("income_statement", {})
        bs = r.get("financials", {}).get("balance_sheet", {})
        cf = r.get("financials", {}).get("cash_flow_statement", {})
        r1 = results[1].get("financials", {}).get("income_statement", {}) if len(results) > 1 else {}

        revenue_cur  = abs(_poly_fin(ic, bs, cf, "revenues"))
        revenue_cur  = revenue_cur or abs(_poly_fin(ic, bs, cf, "net_revenues"))
        revenue_prev = abs(_poly_fin(r1, {}, {}, "revenues")) or abs(_poly_fin(r1, {}, {}, "net_revenues"))
        ebit         = _poly_fin(ic, bs, cf, "operating_income_loss")
        da           = abs(_poly_fin(ic, bs, cf, "depreciation_depletion_and_amortization"))
        capex        = abs(_poly_fin(ic, bs, cf, "capital_expenditure"))
        interest_exp = abs(_poly_fin(ic, bs, cf, "interest_expense_operating"))
        interest_exp = interest_exp or abs(_poly_fin(ic, bs, cf, "interest_and_debt_expense"))
        tax_exp      = abs(_poly_fin(ic, bs, cf, "income_tax_expense_benefit"))
        pretax       = _poly_fin(ic, bs, cf, "income_loss_from_continuing_operations_before_tax")
        pretax       = pretax or _poly_fin(ic, bs, cf, "net_income_loss")
        lt_debt      = abs(_poly_fin(ic, bs, cf, "long_term_debt"))
        st_debt      = abs(_poly_fin(ic, bs, cf, "current_debt_and_capital_lease_obligations"))
        cash         = abs(_poly_fin(ic, bs, cf, "cash_and_cash_equivalents"))
        shares       = abs(_poly_fin(ic, bs, cf, "basic_average_shares"))
        shares       = shares or abs(_poly_fin(ic, bs, cf, "common_stock_shares_outstanding"))

        ebitda = ebit + da
        total_debt = lt_debt + st_debt
        net_debt   = total_debt - cash
        tax_rate   = (tax_exp / pretax) if pretax > 0 else 0.25
        tax_rate   = max(0.10, min(0.40, tax_rate))
        rev_growth = (revenue_cur / revenue_prev - 1.0) if revenue_prev > 0 else 0.05

        return {
            "revenue":      revenue_cur,
            "rev_growth":   rev_growth,
            "ebit":         ebit,
            "da":           da,
            "ebitda":       ebitda,
            "capex":        capex,
            "interest_exp": interest_exp,
            "tax_rate":     tax_rate,
            "lt_debt":      lt_debt,
            "st_debt":      st_debt,
            "total_debt":   total_debt,
            "cash":         cash,
            "net_debt":     net_debt,
            "shares":       shares,
        }
    except Exception as e:
        log.debug("poly_financials_error %s: %s", ticker, e)
        return {}


async def _fetch_poly_price(ticker: str, api_key: str) -> float:
    """Fetch previous close price from Polygon."""
    import aiohttp as _ah
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey={api_key}"
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return 0.0
                d = await resp.json()
        results = d.get("results") or []
        return float(results[0].get("c", 0)) if results else 0.0
    except Exception:
        return 0.0


@app.get("/api/analytics/lbo/{ticker}")
async def analytics_lbo(
    ticker: str,
    entry_multiple: float = 8.0,
    hold_years: int = 5,
    leverage_ratio: float = 0.60,
    exit_multiple: float = 9.0,
    revenue_growth: float = 0.05,
    cost_of_debt: float = 0.08,
    token: str = "",
):
    """
    LBO model: entry EV, annual debt-paydown schedule, IRR/MOIC,
    500-path Monte Carlo, covenant checks (≤6× leverage, ≥2× coverage), verdict.
    Fetches EBITDA/capex from Polygon vX/reference/financials.
    """
    check_token(token)
    ticker = ticker.upper()
    hold_years = max(1, min(10, int(hold_years)))
    cache_key  = f"analytics:lbo:{ticker}:{entry_multiple:.2f}:{hold_years}:{leverage_ratio:.2f}:{exit_multiple:.2f}:{revenue_growth:.3f}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy unavailable"}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    fin     = await _fetch_poly_financials(ticker, api_key) if api_key else {}
    if not fin or fin.get("ebitda", 0) <= 0:
        return {"error": f"No EBITDA data for {ticker}"}

    ebitda     = fin["ebitda"]
    da         = fin["da"]
    capex      = fin["capex"]
    tax_rate   = fin["tax_rate"]
    rev_growth = revenue_growth  # user override; default to supplied param

    entry_ev     = ebitda * entry_multiple
    entry_debt   = entry_ev * leverage_ratio
    entry_equity = entry_ev * (1.0 - leverage_ratio)

    # ── Annual debt schedule ──────────────────────────────────────────────────
    schedule, debt, ebitda_cur = [], entry_debt, ebitda
    covenants_ok       = True
    first_breach_year  = None

    for yr in range(1, hold_years + 1):
        ebitda_cur = ebitda_cur * (1.0 + rev_growth)
        interest   = debt * cost_of_debt
        ebt        = max(0.0, ebitda_cur - da - interest)
        tax        = ebt * tax_rate
        fcf        = ebitda_cur - da - interest - tax - capex + da  # = EBITDA - interest - tax - capex
        fcf        = ebitda_cur - interest - tax - capex            # simplified
        debt_repay = max(0.0, min(max(0.0, fcf), debt))
        debt       = max(0.0, debt - debt_repay)

        lev  = debt / ebitda_cur if ebitda_cur > 0 else 99.0
        cov  = ebitda_cur / interest if interest > 0 else 99.0
        if lev > 6.0 or cov < 2.0:
            if covenants_ok:
                first_breach_year = yr
            covenants_ok = False

        schedule.append({
            "year":     yr,
            "ebitda":   round(ebitda_cur),
            "interest": round(interest),
            "fcf":      round(fcf),
            "debt":     round(debt),
            "leverage": round(lev, 2),
            "coverage": round(cov, 2),
        })

    exit_ev     = ebitda_cur * exit_multiple
    exit_equity = max(0.0, exit_ev - debt)
    if entry_equity > 0 and exit_equity > 0:
        base_irr  = (exit_equity / entry_equity) ** (1.0 / hold_years) - 1.0
        base_moic = exit_equity / entry_equity
    else:
        base_irr = base_moic = 0.0

    # ── Monte Carlo (500 paths) ───────────────────────────────────────────────
    rng = np.random.default_rng(99)
    mc_irrs: list = []
    for _ in range(500):
        g_s  = float(rng.normal(rev_growth, 0.025))
        em_s = float(rng.normal(exit_multiple, 0.75))
        em_s = max(3.0, em_s)
        d_mc = entry_debt
        e_mc = ebitda
        for _ in range(hold_years):
            e_mc   = e_mc * (1.0 + g_s)
            int_mc = d_mc * cost_of_debt
            ebt_mc = max(0.0, e_mc - da - int_mc)
            fcf_mc = e_mc - int_mc - ebt_mc * tax_rate - capex
            d_mc   = max(0.0, d_mc - max(0.0, min(max(0.0, fcf_mc), d_mc)))
        eq_mc = max(0.0, e_mc * em_s - d_mc)
        if entry_equity > 0 and eq_mc > 0:
            mc_irrs.append((eq_mc / entry_equity) ** (1.0 / hold_years) - 1.0)

    mc_arr    = np.array(mc_irrs) if mc_irrs else np.array([base_irr])
    irr_pct   = base_irr * 100
    verdict   = ("STRONG BUY" if irr_pct >= 25 else "BUY" if irr_pct >= 20
                 else "CONDITIONAL" if irr_pct >= 15 else "PASS")

    result = {
        "ticker":            ticker,
        "entry_ebitda":      round(ebitda),
        "entry_ev":          round(entry_ev),
        "entry_debt":        round(entry_debt),
        "entry_equity":      round(entry_equity),
        "entry_multiple":    entry_multiple,
        "exit_multiple":     exit_multiple,
        "hold_years":        hold_years,
        "schedule":          schedule,
        "exit_ev":           round(exit_ev),
        "exit_equity":       round(exit_equity),
        "base_irr":          round(base_irr * 100, 2),
        "base_moic":         round(base_moic, 2),
        "mc_irr_mean":       round(float(np.mean(mc_arr)) * 100, 2),
        "mc_irr_p5":         round(float(np.percentile(mc_arr, 5)) * 100, 2),
        "mc_irr_p95":        round(float(np.percentile(mc_arr, 95)) * 100, 2),
        "mc_irr_median":     round(float(np.median(mc_arr)) * 100, 2),
        "mc_pct_positive":   round(float(np.mean(mc_arr > 0)) * 100, 1),
        "verdict":           verdict,
        "covenants_ok":      covenants_ok,
        "first_breach_year": first_breach_year,
        "revenue_growth_pct": round(rev_growth * 100, 1),
        "cost_of_debt_pct":  round(cost_of_debt * 100, 1),
    }
    if _r:
        await _r.setex(cache_key, 1800, json.dumps(result))
    return result


@app.get("/api/analytics/dcf/{ticker}")
async def analytics_dcf(ticker: str, token: str = ""):
    """
    DCF valuation: FCFF model, WACC (CAPM cost of equity + after-tax cost of debt),
    10-year explicit forecast with growth fade to terminal, per-share intrinsic value.
    """
    check_token(token)
    ticker    = ticker.upper()
    cache_key = f"analytics:dcf:{ticker}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "no api key"}

    fin, price = await asyncio.gather(
        _fetch_poly_financials(ticker, api_key),
        _fetch_poly_price(ticker, api_key),
    )
    if not fin or fin.get("ebit", 0) == 0:
        return {"error": f"Insufficient financial data for {ticker}"}

    ebit       = fin["ebit"]
    da         = fin["da"]
    capex      = fin["capex"]
    tax_rate   = fin["tax_rate"]
    total_debt = fin["total_debt"]
    net_debt   = fin["net_debt"]
    shares     = fin["shares"]
    rev_growth = min(0.30, max(-0.10, fin["rev_growth"]))  # cap at ±30%
    interest   = fin["interest_exp"]

    # WACC components
    rf          = 0.045          # risk-free rate (approx current 10yr)
    erp         = 0.055          # equity risk premium
    beta        = 1.1            # default; will use from analytics if available
    cost_equity = rf + beta * erp
    cost_debt   = (interest / total_debt) if total_debt > 0 else 0.07
    cost_debt   = max(0.04, min(0.15, cost_debt))
    equity_val  = max(price, 1.0) * max(shares, 1.0)
    total_cap   = equity_val + total_debt
    wacc        = (cost_equity * equity_val / total_cap
                   + cost_debt * (1 - tax_rate) * total_debt / total_cap) if total_cap > 0 else 0.09
    wacc        = max(0.06, min(0.18, wacc))

    # FCFF base = EBIT(1-t) + D&A - CapEx
    base_fcff = ebit * (1.0 - tax_rate) + da - capex

    # 10-year explicit forecast: growth fades linearly from rev_growth to 3% by year 7
    terminal_growth = 0.03
    forecasts   = []
    fcff        = base_fcff
    pv_sum      = 0.0
    for yr in range(1, 11):
        if yr <= 3:
            g = rev_growth
        elif yr <= 7:
            # linear fade from rev_growth to terminal_growth
            g = rev_growth + (terminal_growth - rev_growth) * (yr - 3) / 4.0
        else:
            g = terminal_growth
        fcff = fcff * (1.0 + g)
        pv   = fcff / (1.0 + wacc) ** yr
        pv_sum += pv
        forecasts.append({"year": yr, "fcff": round(fcff), "pv": round(pv), "growth": round(g * 100, 1)})

    # Terminal value (Gordon Growth at year 10)
    tv    = fcff * (1.0 + terminal_growth) / max(0.001, wacc - terminal_growth)
    pv_tv = tv / (1.0 + wacc) ** 10
    ev    = pv_sum + pv_tv
    eq    = ev - net_debt
    intrinsic_per_share = (eq / shares) if shares > 0 else 0.0
    dcf_gap = ((intrinsic_per_share - price) / price * 100) if price > 0 else 0.0

    result = {
        "ticker":               ticker,
        "market_price":         round(price, 2),
        "intrinsic_per_share":  round(intrinsic_per_share, 2),
        "dcf_gap_pct":          round(dcf_gap, 1),
        "enterprise_value":     round(ev),
        "equity_value":         round(eq),
        "pv_explicit":          round(pv_sum),
        "pv_terminal":          round(pv_tv),
        "terminal_value":       round(tv),
        "wacc":                 round(wacc * 100, 2),
        "cost_equity":          round(cost_equity * 100, 2),
        "cost_debt":            round(cost_debt * 100, 2),
        "tax_rate":             round(tax_rate * 100, 1),
        "terminal_growth":      round(terminal_growth * 100, 1),
        "base_fcff":            round(base_fcff),
        "rev_growth_yr1":       round(rev_growth * 100, 1),
        "net_debt":             round(net_debt),
        "shares":               round(shares),
        "forecasts":            forecasts,
        "rating": ("UNDERVALUED" if dcf_gap > 15 else
                   "FAIR" if dcf_gap > -15 else "OVERVALUED"),
    }
    if _r:
        await _r.setex(cache_key, 3600, json.dumps(result))
    return result


@app.get("/api/analytics/optimize")
async def analytics_optimize(
    tickers: str = "",
    method: str  = "hrp",
    days:   int  = 252,
    max_weight: float = 0.40,
    token:  str  = "",
):
    """
    Portfolio optimization: MV (max Sharpe via SLSQP), HRP (hierarchical risk parity
    with Ledoit-Wolf shrinkage), RP (equal risk contribution).
    Fetches price history from Polygon.
    """
    check_token(token)
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if len(ticker_list) < 2:
        return {"error": "Need at least 2 tickers"}
    if len(ticker_list) > 20:
        return {"error": "Max 20 tickers"}
    method = method.lower() if method.lower() in ("mv", "hrp", "rp") else "hrp"
    max_weight = max(1.0 / len(ticker_list), min(1.0, max_weight))

    cache_key = f"analytics:opt:{','.join(sorted(ticker_list))}:{method}:{days}:{max_weight:.2f}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy unavailable"}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "no api key"}

    # Fetch daily closes for each ticker
    import aiohttp as _ah
    from_dt = (date.today() - timedelta(days=days + 30)).isoformat()
    to_dt   = date.today().isoformat()

    async def _get_closes(sym: str) -> list[float]:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
               f"/{from_dt}/{to_dt}?adjusted=true&sort=asc&limit={days + 40}&apiKey={api_key}")
        try:
            async with _ah.ClientSession() as sess:
                async with sess.get(url, timeout=_ah.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        return []
                    d = await resp.json()
            return [float(b["c"]) for b in (d.get("results") or []) if b.get("c")]
        except Exception:
            return []

    closes_list = await asyncio.gather(*[_get_closes(t) for t in ticker_list])

    # Align to shortest series, require at least 60 points
    min_len = min((len(c) for c in closes_list), default=0)
    if min_len < 60:
        return {"error": "Insufficient price history for one or more tickers"}

    # Build returns matrix (n_obs × n_assets)
    ret_matrix = np.array([
        np.diff(np.array(c[-min_len:])) / np.array(c[-min_len:-1])
        for c in closes_list
    ]).T  # (n_obs-1, n_assets)

    n_assets = len(ticker_list)
    mu_daily = np.mean(ret_matrix, axis=0)
    mu_ann   = mu_daily * 252

    # Ledoit-Wolf covariance shrinkage
    from sklearn.covariance import LedoitWolf
    lw  = LedoitWolf().fit(ret_matrix)
    cov = lw.covariance_ * 252   # annualized

    weights = np.ones(n_assets) / n_assets  # default equal weight

    if method == "mv":
        # Max Sharpe via SLSQP
        from scipy.optimize import minimize as _min
        rf_ann = 0.045

        def _neg_sharpe(w):
            w    = np.array(w)
            ret  = float(w @ mu_ann)
            vol  = float(np.sqrt(w @ cov @ w))
            return -(ret - rf_ann) / vol if vol > 1e-9 else 0.0

        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
        bounds      = [(0.0, max_weight)] * n_assets
        w0          = np.ones(n_assets) / n_assets
        res         = _min(_neg_sharpe, w0, method="SLSQP",
                           bounds=bounds, constraints=constraints,
                           options={"ftol": 1e-9, "maxiter": 500})
        if res.success:
            weights = np.array(res.x)
            weights = np.clip(weights, 0, max_weight)
            weights /= weights.sum()

    elif method == "hrp":
        # Hierarchical Risk Parity
        from scipy.cluster.hierarchy import linkage, leaves_list
        from scipy.spatial.distance import squareform

        corr    = np.corrcoef(ret_matrix.T)
        dist    = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, 1.0))
        np.fill_diagonal(dist, 0.0)
        link    = linkage(squareform(dist), method="ward")
        order   = list(leaves_list(link))

        def _hrp_recurse(items: list) -> np.ndarray:
            if len(items) == 1:
                w = np.zeros(n_assets)
                w[items[0]] = 1.0
                return w
            mid   = len(items) // 2
            left  = items[:mid]
            right = items[mid:]
            wl    = _hrp_recurse(left)
            wr    = _hrp_recurse(right)
            # risk of each cluster: w'Σw
            vol_l = float(np.sqrt(wl @ cov @ wl)) or 1e-9
            vol_r = float(np.sqrt(wr @ cov @ wr)) or 1e-9
            alpha = vol_r / (vol_l + vol_r)
            return alpha * wl + (1.0 - alpha) * wr

        weights = _hrp_recurse(order)
        weights = np.clip(weights, 0, max_weight)
        weights /= weights.sum()

    elif method == "rp":
        # Equal risk contribution via Newton's method
        def _rp_obj(w):
            w_arr = np.array(w)
            cov_w = cov @ w_arr
            vol   = float(np.sqrt(w_arr @ cov_w)) or 1e-9
            rc    = w_arr * cov_w / vol     # risk contribution per asset
            target = vol / n_assets          # equal target
            return float(np.sum((rc - target) ** 2))

        from scipy.optimize import minimize as _min
        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
        bounds      = [(0.0, max_weight)] * n_assets
        res         = _min(_rp_obj, np.ones(n_assets) / n_assets,
                           method="SLSQP", bounds=bounds, constraints=constraints,
                           options={"ftol": 1e-12, "maxiter": 1000})
        if res.success:
            weights = np.clip(np.array(res.x), 0, max_weight)
            weights /= weights.sum()

    # Portfolio stats
    port_ret = float(weights @ mu_ann)
    port_vol = float(np.sqrt(weights @ cov @ weights))
    sharpe   = (port_ret - 0.045) / port_vol if port_vol > 0 else 0.0

    # Per-asset risk contribution
    cov_w   = cov @ weights
    port_v  = float(np.sqrt(weights @ cov_w)) or 1e-9
    rc      = [round(float(w * c / port_v) * 100, 2) for w, c in zip(weights, cov_w)]

    result = {
        "tickers":      ticker_list,
        "method":       method,
        "weights":      [round(float(w), 4) for w in weights],
        "port_return":  round(port_ret * 100, 2),
        "port_vol":     round(port_vol * 100, 2),
        "sharpe":       round(sharpe, 3),
        "risk_contrib": rc,
        "days":         min_len - 1,
        "max_weight":   max_weight,
    }
    if _r:
        await _r.setex(cache_key, 3600, json.dumps(result))
    return result


_FRED_SERIES = {
    "1M":  "DGS1MO",
    "3M":  "DGS3MO",
    "6M":  "DGS6MO",
    "1Y":  "DGS1",
    "2Y":  "DGS2",
    "5Y":  "DGS5",
    "10Y": "DGS10",
    "20Y": "DGS20",
    "30Y": "DGS30",
    "TIPS5":  "DFII5",
    "TIPS10": "DFII10",
}
_FRED_MATURITIES = ["1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y"]


async def _fred_latest(series_id: str, lookback_days: int = 30) -> float | None:
    """Fetch latest value for a FRED series via the public CSV endpoint (no API key)."""
    import aiohttp as _ah
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
        lines = [l for l in text.strip().splitlines() if l and not l.startswith("observation")]
        # Walk backwards to find a non-"." value
        for line in reversed(lines[-lookback_days:]):
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in (".", ""):
                try:
                    return float(parts[1].strip())
                except ValueError:
                    pass
        return None
    except Exception:
        return None


async def _fred_history(series_id: str, n_days: int = 252) -> list[tuple[str, float]]:
    """Fetch last n_days of a FRED series as [(date, value)] list."""
    import aiohttp as _ah
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        async with _ah.ClientSession() as sess:
            async with sess.get(url, timeout=_ah.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        rows: list[tuple[str, float]] = []
        for line in text.strip().splitlines():
            if line.startswith("observation") or not line:
                continue
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in (".", ""):
                try:
                    rows.append((parts[0].strip(), float(parts[1].strip())))
                except ValueError:
                    pass
        return rows[-n_days:]
    except Exception:
        return []


@app.get("/api/market/yield-curve")
async def get_yield_curve(token: str = ""):
    """
    US Treasury yield curve from FRED public CSV (no API key required).
    Returns current curve, 2s10s/3m10y spreads + 252-day history, breakeven inflation,
    and regime (INVERTED / FLAT / STEEP / NORMAL).
    Cached 30 min.
    """
    check_token(token)
    cache_key = "market:yield_curve"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    try:
        # Fetch all nominal series + TIPS in parallel
        series_keys = list(_FRED_SERIES.keys())
        values = await asyncio.gather(*[_fred_latest(_FRED_SERIES[k]) for k in series_keys])
        curve_latest: dict[str, float | None] = dict(zip(series_keys, values))

        # Current curve (nominal maturities only)
        curve = [
            {"maturity": m, "yield": curve_latest.get(m)}
            for m in _FRED_MATURITIES
            if curve_latest.get(m) is not None
        ]

        y2    = curve_latest.get("2Y")
        y10   = curve_latest.get("10Y")
        y3m   = curve_latest.get("3M")
        tips5 = curve_latest.get("TIPS5")
        tips10 = curve_latest.get("TIPS10")
        spread_2s10s  = round(y10 - y2, 3)   if (y10 and y2)   else None
        spread_3m10y  = round(y10 - y3m, 3)  if (y10 and y3m)  else None
        be_5y  = round((y2 or 0) - (tips5 or 0), 3)  if (y2 and tips5)   else None  # approx
        be_10y = round((y10 or 0) - (tips10 or 0), 3) if (y10 and tips10) else None

        # Regime classification
        if spread_2s10s is not None:
            if spread_2s10s < -0.05:   regime = "INVERTED"
            elif spread_2s10s < 0.25:  regime = "FLAT"
            elif spread_2s10s > 1.50:  regime = "STEEP"
            else:                       regime = "NORMAL"
        else:
            regime = "UNKNOWN"

        # Historical 2s10s spread (last 252 points)
        hist_2y_raw, hist_10y_raw = await asyncio.gather(
            _fred_history("DGS2", 300),
            _fred_history("DGS10", 300),
        )
        # Merge by date
        d2_map  = {d: v for d, v in hist_2y_raw}
        spread_history = [
            {"date": d, "spread": round(v - d2_map[d], 3)}
            for d, v in hist_10y_raw
            if d in d2_map
        ][-252:]

        result = {
            "curve":           curve,
            "spread_2s10s":    spread_2s10s,
            "spread_3m10y":    spread_3m10y,
            "breakeven_5y":    be_5y,
            "breakeven_10y":   be_10y,
            "regime":          regime,
            "tips_5y":         tips5,
            "tips_10y":        tips10,
            "spread_history":  spread_history,
            "updated":         datetime.now(timezone.utc).isoformat(),
        }
        if _r:
            await _r.setex(cache_key, 1800, json.dumps(result))
        return result

    except Exception as e:
        log.warning("yield_curve.error", error=str(e))
        return {"error": str(e)}


@app.get("/api/market/fred-macro")
async def get_fred_macro(token: str = ""):
    """
    Latest FRED macro indicators: HY OAS, IG OAS, Financial Stress Index, NBER recession flag.
    Priority: Redis cache (set by macro regime scraper) → FREDClient JSON API → FRED CSV fallback.
    Cached 1 hour.
    """
    from shared.fred_client import get_fred_client
    check_token(token)
    cache_key = "fred:macro:latest"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return {**json.loads(cached), "source": "cache"}
    except Exception:
        _r = None

    def _credit_label(hy: float | None) -> str:
        if hy is None:       return "unknown"
        if hy < 300:         return "tight"
        if hy < 500:         return "normal"
        if hy < 700:         return "wide"
        return "distressed"

    def _stress_label(f: float | None) -> str:
        if f is None:        return "unknown"
        if f < -0.5:         return "low"
        if f < 1.0:          return "average"
        if f < 2.0:          return "elevated"
        return "high"

    # Use FREDClient JSON API when key is available
    fred = get_fred_client()
    if fred:
        try:
            from shared.fred_client import FREDClient as _FC
            snap = await fred.bulk_latest(
                _FC.SERIES["hy_oas"], _FC.SERIES["ig_oas"],
                _FC.SERIES["fsi"],    _FC.SERIES["usrec"],
            )
            hy_oas = snap.get(_FC.SERIES["hy_oas"])
            ig_oas = snap.get(_FC.SERIES["ig_oas"])
            fsi    = snap.get(_FC.SERIES["fsi"])
            usrec  = snap.get(_FC.SERIES["usrec"])
            source = "fred_api"
        except Exception as e:
            log.warning("fred_macro.client_error", error=str(e))
            fred = None

    if not fred:
        # CSV fallback — no API key needed
        hy_oas, ig_oas, fsi, usrec = await asyncio.gather(
            _fred_latest("BAMLH0A0HYM2", lookback_days=10),
            _fred_latest("BAMLC0A0CM",   lookback_days=10),
            _fred_latest("STLFSI2",      lookback_days=14),
            _fred_latest("USREC",        lookback_days=45),
        )
        source = "fred_csv"

    result = {
        "hy_oas":        hy_oas,
        "ig_oas":        ig_oas,
        "fsi":           fsi,
        "usrec":         int(usrec) if usrec is not None else None,
        "credit_label":  _credit_label(hy_oas),
        "stress_label":  _stress_label(fsi),
        "in_recession":  usrec == 1 if usrec is not None else None,
        "source":        source,
        "updated":       datetime.now(timezone.utc).isoformat(),
    }
    if _r:
        await _r.setex(cache_key, 3600, json.dumps(result))
    return result


_BREADTH_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","BRK.B","LLY","JPM","V",
    "UNH","XOM","TSLA","MA","JNJ","HD","PG","COST","AVGO","MRK",
    "CVX","ABBV","KO","PEP","ADBE","WMT","TMO","ACN","AMD","CRM",
    "MCD","NKE","ABT","INTC","TXN","QCOM","DHR","AMGN","NEE","RTX",
    "HON","LOW","INTU","LIN","SPGI","GS","BLK","MS","AXP","BA",
]


@app.get("/api/market/breadth")
async def get_market_breadth(token: str = ""):
    """
    Market breadth for 50 large-cap tickers: % above 50/200-day MA,
    advance/decline ratio, net new 52-week highs vs lows.
    Cached 30 min.
    """
    check_token(token)
    cache_key = "market:breadth"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "no api key"}

    import aiohttp as _ah
    from_dt = (date.today() - timedelta(days=320)).isoformat()
    to_dt   = date.today().isoformat()

    async def _fetch_one(sym: str) -> dict | None:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
               f"/{from_dt}/{to_dt}?adjusted=true&sort=asc&limit=300&apiKey={api_key}")
        try:
            async with _ah.ClientSession() as sess:
                async with sess.get(url, timeout=_ah.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    d = await resp.json()
            bars = d.get("results") or []
            if len(bars) < 52:
                return None
            closes = [float(b["c"]) for b in bars]
            cur    = closes[-1]
            prev   = closes[-2] if len(closes) >= 2 else cur
            ma50   = sum(closes[-50:]) / 50  if len(closes) >= 50  else None
            ma200  = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
            hi52   = max(closes[-252:]) if len(closes) >= 252 else max(closes)
            lo52   = min(closes[-252:]) if len(closes) >= 252 else min(closes)
            return {
                "sym":    sym,
                "cur":    cur,
                "chg":    (cur / prev - 1.0) if prev > 0 else 0.0,
                "abv50":  (cur > ma50)  if ma50  else None,
                "abv200": (cur > ma200) if ma200 else None,
                "hi52":   hi52,
                "lo52":   lo52,
                "new_hi": cur >= hi52 * 0.99,
                "new_lo": cur <= lo52 * 1.01,
            }
        except Exception:
            return None

    raw = await asyncio.gather(*[_fetch_one(s) for s in _BREADTH_UNIVERSE])
    records = [r for r in raw if r is not None]

    if not records:
        return {"error": "No data fetched"}

    n         = len(records)
    abv50     = [r for r in records if r["abv50"]  is True]
    abv200    = [r for r in records if r["abv200"] is True]
    advances  = [r for r in records if r["chg"] > 0]
    declines  = [r for r in records if r["chg"] < 0]
    new_his   = [r for r in records if r["new_hi"]]
    new_los   = [r for r in records if r["new_lo"]]
    ad_ratio  = round(len(advances) / len(declines), 2) if declines else 99.0
    net_highs = len(new_his) - len(new_los)

    # Top gainers / losers
    by_chg  = sorted(records, key=lambda r: r["chg"])
    losers  = [{"sym": r["sym"], "chg": round(r["chg"] * 100, 2)} for r in by_chg[:5]]
    gainers = [{"sym": r["sym"], "chg": round(r["chg"] * 100, 2)} for r in by_chg[-5:]]

    result = {
        "universe_count":  n,
        "pct_above_50ma":  round(len(abv50)  / n * 100, 1),
        "pct_above_200ma": round(len(abv200) / n * 100, 1),
        "advance_count":   len(advances),
        "decline_count":   len(declines),
        "ad_ratio":        ad_ratio,
        "new_highs":       len(new_his),
        "new_lows":        len(new_los),
        "net_highs":       net_highs,
        "gainers":         gainers,
        "losers":          losers,
        "updated":         datetime.now(timezone.utc).isoformat(),
    }
    if _r:
        await _r.setex(cache_key, 1800, json.dumps(result))
    return result


def _compute_signal_confidence(strength: float, speed: float, proximity: float) -> float:
    """
    Three-factor weighted confidence score used across signal types.
    strength×0.40 + speed×0.35 + proximity×0.25 → clamped to [0, 1].
    """
    return round(min(1.0, max(0.0,
        float(strength)  * 0.40 +
        float(speed)     * 0.35 +
        float(proximity) * 0.25,
    )), 3)


@app.get("/api/analytics/tsmom")
async def analytics_tsmom(mode: str = "live", token: str = ""):
    """
    12-1 time-series momentum for each portfolio holding.
    Signal: LONG (mom>0) / SHORT (mom<0) / FLAT (near zero).
    Position size: signal × min(2.0, 15%/realized_vol).
    Vol: EWMA half-life 60d, annualized.
    """
    check_token(token)
    cache_key = f"analytics:tsmom:{mode}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    if not DB_URL:
        return {"error": "no db"}

    pool = await _get_db_pool()
    rows = await pool.fetch(
        """SELECT DISTINCT symbol
           FROM positions
           WHERE mode = $1 AND qty > 0""",
        mode,
    )
    tickers = [r["symbol"] for r in rows]
    if not tickers:
        return {"signals": [], "mode": mode}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "no api key"}

    import aiohttp as _ah
    import math as _math

    LOOKBACK = 252 + 21 + 10   # extra buffer
    TARGET_VOL = 0.15
    FLAT_THRESHOLD = 0.01      # momentum must exceed ±1% to get a signal

    from_dt = (date.today() - timedelta(days=LOOKBACK + 20)).isoformat()
    to_dt   = date.today().isoformat()

    async def _tsmom_one(sym: str) -> dict:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day"
               f"/{from_dt}/{to_dt}?adjusted=true&sort=asc&limit={LOOKBACK + 30}&apiKey={api_key}")
        try:
            async with _ah.ClientSession() as sess:
                async with sess.get(url, timeout=_ah.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        return {"sym": sym, "error": "no data"}
                    d = await resp.json()
            closes = [float(b["c"]) for b in (d.get("results") or []) if b.get("c")]
            if len(closes) < 252:
                return {"sym": sym, "error": "insufficient history"}

            # 12-1 momentum: return from -252 days to -21 days (exclude last month)
            idx_start = -252
            idx_end   = -21
            mom = closes[idx_end] / closes[idx_start] - 1.0

            # EWMA realized volatility (halflife=60d) on daily returns
            rets    = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
            hl      = 60.0
            decay   = _math.exp(-_math.log(2) / hl)
            var_ewm = rets[0] ** 2
            for r in rets[1:]:
                var_ewm = decay * var_ewm + (1.0 - decay) * r ** 2
            vol_ann = _math.sqrt(var_ewm * 252)

            # Signal and position size
            if   mom >  FLAT_THRESHOLD:  signal = 1
            elif mom < -FLAT_THRESHOLD:  signal = -1
            else:                         signal = 0

            pos_size = signal * min(2.0, TARGET_VOL / vol_ann) if vol_ann > 0 else 0.0

            # Signal confidence: strength from momentum magnitude, speed from
            # distance above flat threshold, proximity neutral (no price context here)
            mom_abs     = abs(mom * 100)
            strength    = min(1.0, mom_abs / 30.0)     # 30% momentum = full strength
            speed       = min(1.0, max(0.0, (mom_abs - FLAT_THRESHOLD * 100) / 10.0))
            proximity   = abs(pos_size) / 2.0 if vol_ann > 0 else 0.5
            confidence  = _compute_signal_confidence(strength, speed, min(1.0, proximity))

            return {
                "sym":        sym,
                "momentum":   round(mom * 100, 2),
                "vol_ann":    round(vol_ann * 100, 2),
                "signal":     signal,
                "pos_size":   round(pos_size, 3),
                "direction":  "LONG" if signal > 0 else "SHORT" if signal < 0 else "FLAT",
                "confidence": confidence,
            }
        except Exception as e:
            return {"sym": sym, "error": str(e)[:60]}

    signals = await asyncio.gather(*[_tsmom_one(t) for t in tickers])
    signals_list = sorted(signals, key=lambda x: x.get("momentum", 0), reverse=True)

    result = {"signals": signals_list, "mode": mode,
              "target_vol_pct": TARGET_VOL * 100,
              "flat_threshold_pct": FLAT_THRESHOLD * 100}
    if _r:
        await _r.setex(cache_key, 1800, json.dumps(result))
    return result


@app.get("/api/signals/imbalance")
async def signals_imbalance(mode: str = "live", token: str = ""):
    """
    Bid/ask size imbalance for current portfolio holdings (stocks/options only).
    Fetches Polygon quote snapshot per ticker and computes bid_size/(bid_size+ask_size).
    >0.60 = bullish, <0.40 = bearish, else neutral.
    Cached 5 min.
    """
    check_token(token)
    cache_key = f"signals:imbalance:{mode}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    if not DB_URL:
        return {"error": "no db"}

    pool = await _get_db_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT symbol FROM positions WHERE mode=$1 AND qty>0",
        mode,
    )
    tickers = [r["symbol"] for r in rows]
    if not tickers:
        return {"signals": [], "mode": mode}

    api_key = os.getenv("MASSIVE_API_KEY", "")
    if not api_key:
        return {"error": "no api key"}

    import aiohttp as _ah

    async def _fetch_imbalance(sym: str) -> dict:
        url = (
            f"https://api.polygon.io/v2/last/nbbo/{sym}"
            f"?apiKey={api_key}"
        )
        try:
            async with _ah.ClientSession() as sess:
                async with sess.get(url, timeout=_ah.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return {"sym": sym, "error": "no quote"}
                    d = await resp.json()
            res = d.get("results") or {}
            bid_size = float(res.get("bs", res.get("bid_size", 0)) or 0)
            ask_size = float(res.get("as", res.get("ask_size", 0)) or 0)
            bid      = float(res.get("b", res.get("bid", 0)) or 0)
            ask      = float(res.get("a", res.get("ask", 0)) or 0)
            total    = bid_size + ask_size
            if total <= 0:
                return {"sym": sym, "error": "no size data"}
            ratio     = bid_size / total
            direction = "bullish" if ratio > 0.60 else "bearish" if ratio < 0.40 else "neutral"
            spread    = round(ask - bid, 4) if bid > 0 and ask > 0 else None
            # Confidence: ratio imbalance × volume magnitude × spread tightness
            strength  = min(1.0, abs(ratio - 0.5) * 4)   # 0.75 ratio → strength=1
            speed     = min(1.0, total / 500.0)           # 500+ lots = full speed
            proximity = min(1.0, 1.0 / (1.0 + spread / bid)) if spread and bid > 0 else 0.5
            confidence = _compute_signal_confidence(strength, speed, proximity)
            return {
                "sym":        sym,
                "bid_size":   int(bid_size),
                "ask_size":   int(ask_size),
                "ratio":      round(ratio, 3),
                "bid":        bid,
                "ask":        ask,
                "spread":     spread,
                "direction":  direction,
                "confidence": confidence,
            }
        except Exception as e:
            return {"sym": sym, "error": str(e)[:50]}

    raw = await asyncio.gather(*[_fetch_imbalance(t) for t in tickers])
    signals = sorted(
        [r for r in raw if not r.get("error")],
        key=lambda x: abs(x.get("ratio", 0.5) - 0.5),
        reverse=True,
    )
    errors = [r for r in raw if r.get("error")]

    result = {"signals": signals, "errors": errors, "mode": mode}
    if _r and signals:
        await _r.setex(cache_key, 300, json.dumps(result))
    return result


@app.get("/api/signals/recent")
async def signals_recent(limit: int = 50, token: str = ""):
    """
    Combined signal timeline: last N signals across all sources.
    Sources: ML predictor (Redis stream), OI wall alerts (DB signals table),
    options ATR alerts (DB option_trade_log), TSMOM (computed on demand).
    Returns newest-first list with type, ticker, direction, confidence, ts.
    """
    check_token(token)
    if limit > 200:
        limit = 200

    results = []

    # ── 1. DB signals table (oi_wall, imbalance, etc.) ────────────────────────
    if DB_URL:
        try:
            pool = await _get_db_pool()
            rows = await pool.fetch(
                """SELECT ts, source, ticker, direction, confidence, payload
                   FROM signals
                   ORDER BY ts DESC
                   LIMIT $1""",
                limit,
            )
            for r in rows:
                payload = {}
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else {}
                except Exception:
                    pass
                results.append({
                    "ts":         r["ts"].isoformat() if r["ts"] else None,
                    "source":     r["source"],
                    "ticker":     r["ticker"],
                    "direction":  r["direction"],
                    "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                    "payload":    payload,
                })
        except Exception:
            pass

    # ── 2. Redis predictor signals stream ─────────────────────────────────────
    try:
        redis = await get_redis()
        entries = await redis.xrevrange(STREAMS["signals"], "+", "-", count=limit)
        for _id, fields in entries:
            try:
                ticker = (fields.get(b"ticker") or fields.get("ticker") or b"").decode() if isinstance(fields.get(b"ticker") or fields.get("ticker"), bytes) else str(fields.get(b"ticker") or fields.get("ticker") or "")
                direction = (fields.get(b"direction") or fields.get("direction") or b"").decode() if isinstance(fields.get(b"direction") or fields.get("direction"), bytes) else str(fields.get(b"direction") or fields.get("direction") or "")
                conf_raw = fields.get(b"confidence") or fields.get("confidence")
                conf = float(conf_raw) if conf_raw else None
                # Parse ts from stream ID (milliseconds)
                ts_ms = int(str(_id.decode() if isinstance(_id, bytes) else _id).split("-")[0])
                ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                if ticker:
                    results.append({
                        "ts":         ts_str,
                        "source":     "ml_predictor",
                        "ticker":     ticker,
                        "direction":  direction,
                        "confidence": conf,
                        "payload":    {},
                    })
            except Exception:
                pass
    except Exception:
        pass

    # ── 3. Recent options ATR alerts ───────────────────────────────────────────
    if DB_URL:
        try:
            pool = await _get_db_pool()
            alert_rows = await pool.fetch(
                """SELECT ts, underlying as ticker, event_type, notes
                   FROM option_trade_log
                   WHERE event_type LIKE 'alert_%'
                   ORDER BY ts DESC
                   LIMIT $1""",
                min(20, limit),
            )
            for r in alert_rows:
                direction = "bearish" if "emergency" in r["event_type"] or "exit" in r["event_type"] else "bullish"
                results.append({
                    "ts":         r["ts"].isoformat() if r["ts"] else None,
                    "source":     "options_monitor",
                    "ticker":     r["ticker"],
                    "direction":  direction,
                    "confidence": None,
                    "payload":    {"event": r["event_type"], "notes": r["notes"]},
                })
        except Exception:
            pass

    # Sort combined results by ts descending, return top N
    results.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return {"signals": results[:limit], "count": len(results[:limit])}


# ── Code Insights — telemetry consumer + API ─────────────────────────────────

_TEL_STREAM        = "system.telemetry"
_TEL_CONSUMER_GRP  = "webui-insights"
_TEL_CONSUMER_NAME = "webui-0"


async def _telemetry_consumer() -> None:
    """Background task: drain system.telemetry stream → execution_events table."""
    redis = await get_redis()
    try:
        await redis.xgroup_create(_TEL_STREAM, _TEL_CONSUMER_GRP, id="$", mkstream=True)
    except Exception:
        pass  # group already exists

    if not DB_URL:
        return

    pool = await _get_db_pool()
    while True:
        try:
            msgs = await redis.xreadgroup(
                _TEL_CONSUMER_GRP, _TEL_CONSUMER_NAME,
                {_TEL_STREAM: ">"},
                count=50, block=5000,
            )
            if not msgs:
                continue
            for _stream, entries in msgs:
                for msg_id, fields in entries:
                    try:
                        payload_raw = fields.get("payload", "{}")
                        payload     = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                        dur_raw     = fields.get("duration_ms")
                        duration    = float(dur_raw) if dur_raw else None
                        await pool.execute(
                            """INSERT INTO execution_events
                               (agent, event_name, severity, duration_ms, payload, traceback_str)
                               VALUES ($1,$2,$3,$4,$5::jsonb,$6)""",
                            fields.get("agent", "unknown"),
                            fields.get("event_name", "event"),
                            fields.get("severity", "info"),
                            duration,
                            json.dumps(payload),
                            fields.get("traceback_str") or None,
                        )
                        await redis.xack(_TEL_STREAM, _TEL_CONSUMER_GRP, msg_id)
                    except Exception as e:
                        log.warning("telemetry_consumer.row_failed", error=str(e))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("telemetry_consumer.loop_error", error=str(e))
            await asyncio.sleep(10)


@app.get("/api/telemetry/events")
async def get_telemetry_events(
    token:      str = "",
    limit:      int = 200,
    agent:      Optional[str] = None,
    severity:   Optional[str] = None,
    event_name: Optional[str] = None,
    resolved:   Optional[bool] = None,
    hours:      int = 24,
):
    check_token(token)
    if not DB_URL:
        return {"events": [], "total": 0}
    try:
        pool = await _get_db_pool()
        wheres = ["ts >= NOW() - INTERVAL '1 hour' * $1"]
        params: list = [hours]
        if agent:
            params.append(agent)
            wheres.append(f"agent = ${len(params)}")
        if severity:
            params.append(severity)
            wheres.append(f"severity = ${len(params)}")
        if event_name:
            params.append(event_name)
            wheres.append(f"event_name = ${len(params)}")
        if resolved is not None:
            params.append(resolved)
            wheres.append(f"resolved = ${len(params)}")
        params.append(min(limit, 1000))
        where_sql = " AND ".join(wheres)
        rows = await pool.fetch(
            f"""SELECT id, ts, agent, event_name, severity, duration_ms,
                       payload, traceback_str, resolved, notes
                FROM execution_events WHERE {where_sql}
                ORDER BY ts DESC LIMIT ${len(params)}""",
            *params,
        )
        events = []
        for r in rows:
            ev = dict(r)
            ev["ts"] = ev["ts"].isoformat() if ev.get("ts") else None
            if isinstance(ev.get("payload"), str):
                try:
                    ev["payload"] = json.loads(ev["payload"])
                except Exception:
                    ev["payload"] = {}
            events.append(ev)
        return {"events": events, "total": len(events)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/telemetry/summary")
async def get_telemetry_summary(token: str = "", hours: int = 24):
    check_token(token)
    if not DB_URL:
        return {"agents": [], "by_severity": {}, "by_event": {}, "total": 0}
    try:
        pool = await _get_db_pool()
        rows = await pool.fetch(
            """SELECT agent, severity, event_name, COUNT(*) as cnt,
                      AVG(duration_ms) as avg_ms, MAX(ts) as last_seen
               FROM execution_events
               WHERE ts >= NOW() - INTERVAL '1 hour' * $1
               GROUP BY agent, severity, event_name
               ORDER BY cnt DESC""",
            hours,
        )
        agents: dict = {}
        by_sev: dict = {}
        by_ev:  dict = {}
        total = 0
        for r in rows:
            cnt = int(r["cnt"])
            total += cnt
            a = r["agent"]
            sv = r["severity"]
            en = r["event_name"]
            agents.setdefault(a, {"total": 0, "errors": 0, "warns": 0, "last_seen": None})
            agents[a]["total"] += cnt
            if sv in ("error", "critical"):
                agents[a]["errors"] += cnt
            elif sv == "warn":
                agents[a]["warns"] += cnt
            ls = r["last_seen"].isoformat() if r["last_seen"] else None
            if not agents[a]["last_seen"] or (ls and ls > agents[a]["last_seen"]):
                agents[a]["last_seen"] = ls
            by_sev[sv] = by_sev.get(sv, 0) + cnt
            by_ev[en]  = by_ev.get(en, 0) + cnt
        return {
            "agents":      [{"agent": k, **v} for k, v in sorted(agents.items(), key=lambda x: -x[1]["total"])],
            "by_severity": by_sev,
            "by_event":    by_ev,
            "total":       total,
            "hours":       hours,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


class _TelemetryResolveBody(BaseModel):
    resolved: bool = True
    notes:    Optional[str] = None


@app.patch("/api/telemetry/events/{event_id}/resolve")
async def resolve_telemetry_event(event_id: int, body: _TelemetryResolveBody, token: str = ""):
    check_token(token)
    if not DB_URL:
        raise HTTPException(503, "DB not configured")
    try:
        pool = await _get_db_pool()
        await pool.execute(
            "UPDATE execution_events SET resolved=$1, notes=$2 WHERE id=$3",
            body.resolved, body.notes, event_id,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


class _TelemetryAnalyzeBody(BaseModel):
    agent:   Optional[str] = None
    hours:   int = 24
    message: Optional[str] = None


@app.post("/api/telemetry/analyze")
async def analyze_telemetry(body: _TelemetryAnalyzeBody, token: str = ""):
    """Stream an LLM analysis of recent telemetry events for an agent."""
    check_token(token)
    if not DB_URL:
        raise HTTPException(503, "DB not configured")
    try:
        pool = await _get_db_pool()
        params: list = [body.hours]
        where = "ts >= NOW() - INTERVAL '1 hour' * $1"
        if body.agent:
            params.append(body.agent)
            where += f" AND agent = ${len(params)}"
        rows = await pool.fetch(
            f"""SELECT ts, agent, event_name, severity, duration_ms, payload, traceback_str
                FROM execution_events WHERE {where}
                ORDER BY ts DESC LIMIT 100""",
            *params,
        )
        events_txt = []
        for r in rows:
            ts_str = r["ts"].strftime("%Y-%m-%d %H:%M:%S") if r["ts"] else "?"
            pl = r["payload"] or {}
            if isinstance(pl, str):
                try:
                    pl = json.loads(pl)
                except Exception:
                    pl = {}
            line = f"[{ts_str}] {r['agent']} | {r['event_name']} | {r['severity']}"
            if r["duration_ms"]:
                line += f" | {r['duration_ms']:.0f}ms"
            if pl:
                line += f" | {json.dumps(pl)[:200]}"
            if r["traceback_str"]:
                line += f"\n  TB: {r['traceback_str'][:400]}"
            events_txt.append(line)

        context = "\n".join(events_txt) or "No events in selected window."
        user_q  = body.message or "Analyze these events and identify root causes, patterns, and remediation steps."
        prompt  = (
            "You are an expert platform reliability engineer analyzing OpenTrader agent telemetry.\n"
            "Below are the most recent execution events from the platform's telemetry stream:\n\n"
            f"{context}\n\n"
            f"User question: {user_q}\n\n"
            "Provide a concise structured analysis: identified issues, likely root causes, "
            "recommended fixes, and any patterns worth monitoring."
        )

        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_key or openrouter_key.startswith("your_"):
            return JSONResponse({"text": "OPENROUTER_API_KEY not configured"})

        messages = [{"role": "user", "content": prompt}]

        async def _stream():
            import aiohttp as _aiohttp
            try:
                async with _aiohttp.ClientSession() as sess:
                    async with sess.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                        json={
                            "model":       os.getenv("LLM_REVIEW_MODEL", "anthropic/claude-sonnet-4-5"),
                            "messages":    messages,
                            "max_tokens":  1500,
                            "temperature": 0.3,
                            "stream":      True,
                        },
                        timeout=_aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8").strip()
                            if not line.startswith("data: "):
                                continue
                            payload_s = line[6:]
                            if payload_s == "[DONE]":
                                break
                            try:
                                chunk   = json.loads(payload_s)
                                content = chunk["choices"][0]["delta"].get("content", "")
                                if content:
                                    yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                            except Exception:
                                pass
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)[:200]})}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Serve frontend ────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# FRED Economic Dashboard — time series for macro indicators
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/fred/dashboard")
async def fred_dashboard(token: str = "", force: bool = False):
    """
    FRED macro time series for the Economic Dashboard page.
    Returns 18 months of inflation, employment, monetary policy, money supply, and
    credit spreads. Uses public FRED CSV (no API key required).
    Cached 1 hour. Pass force=true to bypass cache.
    """
    check_token(token)
    cache_key = "fred:dashboard:v2"
    _r = None
    if not force:
        try:
            _r = await get_redis()
            cached = await _r.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            _r = None

    def _fmt(pairs: list) -> list:
        return [{"date": d, "value": v} for d, v in pairs]

    try:
        (cpi, core_cpi, core_pce, unrate, claims,
         fedfunds, m2, hy_oas, ig_oas) = await asyncio.gather(
            _fred_history("CPIAUCSL",    18),   # monthly, 18 months
            _fred_history("CPILFESL",    18),
            _fred_history("PCEPILFE",    18),
            _fred_history("UNRATE",      18),
            _fred_history("ICSA",        78),   # weekly, ~18 months
            _fred_history("FEDFUNDS",    18),
            _fred_history("M2SL",        18),
            _fred_history("BAMLH0A0HYM2", 390), # daily, ~18 months
            _fred_history("BAMLC0A0CM",   390),
        )

        result = {
            "inflation": {
                "cpi":      _fmt(cpi),
                "core_cpi": _fmt(core_cpi),
                "core_pce": _fmt(core_pce),
            },
            "employment": {
                "unemployment":    _fmt(unrate),
                "jobless_claims":  _fmt(claims),
            },
            "monetary": {
                "fed_funds": _fmt(fedfunds),
            },
            "money_supply": {
                "m2": _fmt(m2),
            },
            "credit_spreads": {
                "hy_oas": _fmt(hy_oas),
                "ig_oas": _fmt(ig_oas),
            },
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if _r is None:
                _r = await get_redis()
            if _r:
                await _r.setex(cache_key, 3600, json.dumps(result))
        except Exception:
            pass
        return result
    except Exception as e:
        log.error("fred_dashboard.error", error=str(e))
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# Analytics — monthly returns heatmap + trade statistics
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/analytics/monthly-returns")
async def analytics_monthly_returns(mode: str = "live", token: str = ""):
    """
    Monthly returns calendar from portfolio_snapshots.
    Returns a grid of {year, month, return_pct} suitable for a heatmap.
    """
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT date_trunc('month', snapshot_date) AS month_start,
               SUM(total_nav) AS nav
        FROM portfolio_snapshots
        WHERE mode = $1
        GROUP BY month_start
        ORDER BY month_start ASC
        """,
        mode,
    )
    if len(rows) < 2:
        return {"heatmap": [], "best": None, "worst": None, "positive": 0, "negative": 0}

    heatmap = []
    for i in range(1, len(rows)):
        prev_nav = float(rows[i - 1]["nav"])
        curr_nav = float(rows[i]["nav"])
        ret_pct  = round((curr_nav / prev_nav - 1) * 100, 2) if prev_nav > 0 else 0.0
        dt = rows[i]["month_start"]
        heatmap.append({"year": dt.year, "month": dt.month, "return_pct": ret_pct})

    rets = [h["return_pct"] for h in heatmap]
    return {
        "heatmap":   heatmap,
        "best":      max(rets) if rets else None,
        "worst":     min(rets) if rets else None,
        "positive":  sum(1 for r in rets if r > 0),
        "negative":  sum(1 for r in rets if r < 0),
        "avg_month": round(sum(rets) / len(rets), 2) if rets else None,
    }


@app.get("/api/analytics/trade-stats")
async def analytics_trade_stats(mode: str = "live", days: int = 252, token: str = ""):
    """
    Trade-level statistics from the trades table: win rate, profit factor,
    avg win/loss, expectancy, consecutive wins/losses.
    """
    check_token(token)
    pool = await _get_db_pool()
    rows = await pool.fetch(
        """
        SELECT pnl, ts
        FROM trades
        WHERE mode = $1
          AND status IN ('closed', 'fill')
          AND pnl IS NOT NULL
          AND ts >= NOW() - ($2 || ' days')::INTERVAL
        ORDER BY ts ASC
        """,
        mode, str(days),
    )
    if not rows:
        return {"error": "No closed trades with P&L found", "mode": mode, "days": days}

    pnls    = [float(r["pnl"]) for r in rows]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    total   = len(pnls)
    n_win   = len(wins)
    n_loss  = len(losses)

    avg_win  = sum(wins)  / n_win  if n_win  else 0.0
    avg_loss = sum(losses) / n_loss if n_loss else 0.0
    profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float("inf")
    expectancy = (n_win / total * avg_win + n_loss / total * avg_loss) if total else 0.0

    # Consecutive streaks
    max_consec_wins = max_consec_losses = cur_wins = cur_losses = 0
    for p in pnls:
        if p > 0:
            cur_wins += 1; cur_losses = 0
            max_consec_wins = max(max_consec_wins, cur_wins)
        else:
            cur_losses += 1; cur_wins = 0
            max_consec_losses = max(max_consec_losses, cur_losses)

    return {
        "total_trades":        total,
        "wins":                n_win,
        "losses":              n_loss,
        "win_rate":            round(n_win / total * 100, 1) if total else 0.0,
        "profit_factor":       round(profit_factor, 2) if profit_factor != float("inf") else None,
        "avg_win":             round(avg_win, 2),
        "avg_loss":            round(avg_loss, 2),
        "total_pnl":           round(sum(pnls), 2),
        "expectancy":          round(expectancy, 2),
        "max_consec_wins":     max_consec_wins,
        "max_consec_losses":   max_consec_losses,
        "mode":                mode,
        "days":                days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Options — volatility surface (IV across strikes × expirations)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/options/volatility-surface/{ticker}")
async def options_volatility_surface(ticker: str, token: str = ""):
    """
    Build an IV surface matrix for a ticker: strikes (rows) × expirations (cols).
    Fetches up to 6 nearest expirations from Tradier market API.
    Returns separate call and put surfaces.
    Cached 15 min.
    """
    check_token(token)
    ticker    = ticker.upper()
    cache_key = f"options:vol_surface:{ticker}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        _r = None

    tradier_key = os.getenv("TRADIER_MARKET_TOKEN", "") or os.getenv("TRADIER_ACCESS_TOKEN", "")
    if not tradier_key:
        return {"error": "Tradier API key not configured"}

    import aiohttp as _ah
    base_url = "https://api.tradier.com/v1"
    headers  = {"Authorization": f"Bearer {tradier_key}", "Accept": "application/json"}
    timeout  = _ah.ClientTimeout(total=12)

    try:
        async with _ah.ClientSession(headers=headers) as s:
            # Get current price
            price = 0.0
            async with s.get(f"{base_url}/markets/quotes",
                             params={"symbols": ticker, "greeks": "false"},
                             timeout=timeout) as r:
                if r.status == 200:
                    q = ((await r.json(content_type=None))
                         .get("quotes", {}).get("quote", {}))
                    if isinstance(q, dict):
                        price = float(q.get("last") or q.get("prevclose") or 0)

            # Get expirations
            async with s.get(f"{base_url}/markets/options/expirations",
                             params={"symbol": ticker, "includeAllRoots": "false"},
                             timeout=timeout) as r:
                exps = []
                if r.status == 200:
                    d = await r.json(content_type=None)
                    raw = (d.get("expirations") or {})
                    if raw and raw != "null":
                        dates = raw.get("date", [])
                        exps = dates if isinstance(dates, list) else [dates]
            exps = exps[:6]
            if not exps:
                return {"error": "No expirations found", "ticker": ticker}

            # Fetch chains in parallel
            async def _fetch(exp: str):
                async with s.get(f"{base_url}/markets/options/chains",
                                 params={"symbol": ticker, "expiration": exp, "greeks": "true"},
                                 timeout=timeout) as r:
                    if r.status != 200:
                        return exp, []
                    d = await r.json(content_type=None)
                    opts = d.get("options") or {}
                    raw  = opts.get("option", []) if opts and opts != "null" else []
                    return exp, (raw if isinstance(raw, list) else [raw])

            chain_results = await asyncio.gather(*[_fetch(e) for e in exps])

        # Build strike-aligned IV matrices
        atm  = price or 100
        lo_s = atm * 0.80
        hi_s = atm * 1.20

        # Gather all valid strikes in range
        all_strikes: set[float] = set()
        exp_data: dict[str, dict] = {}   # exp → {strike: {call_iv, put_iv}}
        for exp, contracts in chain_results:
            exp_data[exp] = {}
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                strike = float(c.get("strike") or 0)
                if not (lo_s <= strike <= hi_s):
                    continue
                iv  = float(c.get("greeks", {}).get("smv_vol") or c.get("implied_volatility") or 0) * 100
                otyp = (c.get("option_type") or "").lower()
                if strike not in exp_data[exp]:
                    exp_data[exp][strike] = {"call_iv": None, "put_iv": None}
                if otyp == "call":
                    exp_data[exp][strike]["call_iv"] = round(iv, 2) if iv > 0 else None
                else:
                    exp_data[exp][strike]["put_iv"]  = round(iv, 2) if iv > 0 else None
                all_strikes.add(strike)

        strikes = sorted(all_strikes)
        call_surface = []
        put_surface  = []
        for exp in exps:
            call_row = [exp_data[exp].get(s, {}).get("call_iv") for s in strikes]
            put_row  = [exp_data[exp].get(s, {}).get("put_iv")  for s in strikes]
            call_surface.append(call_row)
            put_surface.append(put_row)

        result = {
            "ticker":      ticker,
            "price":       round(price, 2),
            "expirations": exps,
            "strikes":     strikes,
            "call_surface": call_surface,
            "put_surface":  put_surface,
            "updated":     datetime.now(timezone.utc).isoformat(),
        }
        try:
            if _r:
                await _r.setex(cache_key, 900, json.dumps(result))
        except Exception:
            pass
        return result

    except Exception as e:
        log.error("vol_surface.error", ticker=ticker, error=str(e))
        return {"error": str(e), "ticker": ticker}


# ══════════════════════════════════════════════════════════════════════════════
# Investor Persona Agents — Buffett / Graham / Lynch / Munger / Klarman
# ══════════════════════════════════════════════════════════════════════════════

_PERSONA_PROMPTS: dict[str, str] = {
    "buffett": """You are Warren Buffett conducting a stock analysis. Focus on:
- Economic moat (durable competitive advantage: brand, network effects, switching costs, cost advantage, efficient scale)
- Owner earnings (net income + D&A - maintenance capex) and their predictability over 10 years
- Return on invested capital (ROIC) — ideally >15% consistently
- Management quality and capital allocation (buybacks, dividends, acquisitions)
- Margin of safety vs. intrinsic value (DCF with conservative assumptions)
- Simple, understandable business model ("can I understand it in 10 minutes?")
Verdict format: BUY / HOLD / AVOID with a confidence 1–10 and 3–4 concise bullet points.""",

    "graham": """You are Benjamin Graham conducting a defensive stock screen. Focus on:
- P/E ratio (ideally < 15x) and P/B ratio (ideally < 1.5x)
- Earnings yield vs. bond yield (equity must offer premium)
- Current ratio > 2x and long-term debt < 2x net current assets
- Earnings stability: no losses in last 10 years
- Dividend record: uninterrupted payments for 20 years
- Net-net value: NCAV (current assets - total liabilities) vs. market cap
- Margin of safety (require at least 1/3 discount to intrinsic value)
Verdict format: BUY / HOLD / AVOID with a confidence 1–10 and 3–4 concise bullet points.""",

    "lynch": """You are Peter Lynch conducting a growth stock analysis. Focus on:
- PEG ratio (P/E ÷ growth rate) — ideally < 1.0; below 0.5 is exceptional
- Category classification: stalwart / fast grower / slow grower / cyclical / turnaround / asset play
- The "story" — can you explain in 2 sentences why this company will grow?
- Institutional ownership (low = opportunity; high = crowded)
- Insider buying (smart money signal)
- Cash position and debt level relative to earnings
- 5-year EPS growth rate consistency
Verdict format: BUY / HOLD / AVOID with a confidence 1–10 and 3–4 concise bullet points.""",

    "munger": """You are Charlie Munger conducting a business quality assessment. Focus on:
- Invert: what would cause this business to fail? How likely is that?
- Mental models: network effects, economies of scale, brand loyalty, regulatory moat
- Management integrity and long-term track record (not just recent quarters)
- Return on tangible equity — does the business require little capital to grow?
- Pricing power — can they raise prices without losing customers?
- Circle of competence — is this business truly understandable?
- Price matters only after quality is confirmed; a wonderful company at a fair price
Verdict format: BUY / HOLD / AVOID with a confidence 1–10 and 3–4 concise bullet points.""",

    "klarman": """You are Seth Klarman conducting a value/distressed analysis. Focus on:
- Downside scenario first: what is the liquidation value? What is the worst-case outcome?
- Asymmetric risk/reward: how much can you lose vs. how much can you gain?
- Catalyst: what specific event will unlock value? (earnings inflection, spin-off, buyback, debt payoff)
- Margin of safety: require at least 30–50% discount to conservative intrinsic value
- Balance sheet strength: cash, tangible book, debt covenant headroom
- Quality of earnings: recurring vs. one-time; free cash flow vs. reported net income
- Investor sentiment: is this hated, ignored, or misunderstood?
Verdict format: BUY / HOLD / AVOID with a confidence 1–10 and 3–4 concise bullet points.""",
}

_PERSONA_NAMES: dict[str, str] = {
    "buffett": "Warren Buffett",
    "graham":  "Benjamin Graham",
    "lynch":   "Peter Lynch",
    "munger":  "Charlie Munger",
    "klarman": "Seth Klarman",
}


# ── Phase 3: Spread Screeners ────────────────────────────────────────────────

async def _get_broker_option_chain(ticker: str) -> dict:
    """Fetch option chain via broker gateway (first available connector)."""
    import uuid as _uuid, json as _json, redis.asyncio as _aioredis
    REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
    _r = await _aioredis.from_url(
        REDIS_URL, encoding="utf-8", decode_responses=True,
        socket_connect_timeout=5, socket_timeout=35,
    )
    req_id = str(_uuid.uuid4())
    await _r.xadd(STREAMS["broker_commands"], {
        "command": "get_option_chain", "request_id": req_id,
        "symbol": ticker, "issued_by": "webui-screener",
    })
    result = await _r.blpop([f"broker:reply:{req_id}"], timeout=30)
    await _r.aclose()
    if not result:
        raise Exception(f"Option chain gateway timeout for {ticker}")
    raw = _json.loads(result[1])
    r   = raw[0] if isinstance(raw, list) else raw
    if r.get("status") != "ok":
        raise Exception(r.get("error", "Chain fetch failed"))
    return r.get("data", {})


def _compute_net_debit(legs: list[dict]) -> float:
    """Positive = net debit (paid), negative = net credit (received)."""
    total = 0.0
    for leg in legs:
        price  = float(leg.get("limit_price") or leg.get("mid") or 0)
        signed = price if "buy" in leg.get("action", "") else -price
        total += signed
    return round(total, 2)


def _snap_price(price: float) -> float:
    tick = 0.05 if price >= 3.0 else 0.01
    return round(round(price / tick) * tick, 2)


@app.get("/api/options/screener/bull-call-spread")
async def screener_bull_call_spread(
    ticker:      str,
    min_delta:   float = 0.50,
    max_delta:   float = 0.70,
    min_prot:    float = 0.05,
    min_oi:      int   = 100,
    token:       str   = "",
):
    """
    Bull call spread screener.
    Long high-delta call (delta ≥ 0.80 ATM/ITM) + Short OTM call (delta 0.50–0.70).
    Returns ranked spread candidates with net_debit, max_loss, max_gain, ann_roo, score.
    """
    check_token(token)
    import math
    from datetime import date, timedelta

    try:
        chain = await _get_broker_option_chain(ticker.upper())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    spot   = float(chain.get("price") or 0)
    if spot <= 0:
        raise HTTPException(status_code=422, detail="No underlying price available")

    calls = chain.get("calls", [])
    if not calls:
        raise HTTPException(status_code=422, detail="No call contracts found")

    today = date.today()
    results = []

    # Group by expiry
    expirations = sorted({c.get("expiration") for c in calls if c.get("expiration")})
    for exp_str in expirations:
        try:
            exp_date = date.fromisoformat(exp_str)
        except Exception:
            continue
        dte = (exp_date - today).days
        if dte < 7 or dte > 90:
            continue

        exp_calls = [c for c in calls if c.get("expiration") == exp_str]

        # Long leg: delta ≥ 0.75 (ATM or slightly ITM)
        long_candidates = [
            c for c in exp_calls
            if float(c.get("delta") or 0) >= 0.75
            and float(c.get("oi") or 0) >= min_oi
        ]
        # Short leg: delta in [min_delta, max_delta]
        short_candidates = [
            c for c in exp_calls
            if min_delta <= float(c.get("delta") or 0) <= max_delta
            and float(c.get("oi") or 0) >= min_oi
        ]

        for short in short_candidates:
            short_strike = float(short.get("strike") or 0)
            short_delta  = float(short.get("delta") or 0)
            short_theta  = float(short.get("theta") or 0)
            short_mid    = float(short.get("mid") or 0)

            # Downside protection: how far OTM the short is
            prot_pct = (short_strike - spot) / spot if spot > 0 else 0
            if prot_pct < min_prot:
                continue

            for long in long_candidates:
                long_strike = float(long.get("strike") or 0)
                if long_strike >= short_strike:
                    continue
                long_theta = float(long.get("theta") or 0)
                long_mid   = float(long.get("mid") or 0)

                net_debit = _snap_price(long_mid - short_mid)
                if net_debit <= 0:
                    continue

                width    = short_strike - long_strike
                max_gain = round((width - net_debit) * 100, 2)
                max_loss = round(net_debit * 100, 2)
                if max_gain <= 0:
                    continue

                theta_spread = short_theta - long_theta
                ann_roo = (theta_spread / net_debit) * (365 / dte) if net_debit > 0 else 0
                score   = (prot_pct * 200) + (ann_roo * 1.5) + (theta_spread / net_debit * 10 if net_debit > 0 else 0)

                results.append({
                    "underlying":    ticker.upper(),
                    "strategy":      "bull_call_spread",
                    "expiry":        exp_str,
                    "dte":           dte,
                    "long_strike":   long_strike,
                    "short_strike":  short_strike,
                    "long_contract": long.get("contract", ""),
                    "short_contract":short.get("contract", ""),
                    "long_delta":    round(float(long.get("delta") or 0), 3),
                    "short_delta":   round(short_delta, 3),
                    "net_debit":     net_debit,
                    "max_loss":      max_loss,
                    "max_gain":      max_gain,
                    "prot_pct":      round(prot_pct * 100, 2),
                    "ann_roo":       round(ann_roo, 4),
                    "theta_spread":  round(theta_spread, 4),
                    "score":         round(score, 3),
                    "legs": [
                        {"symbol": long.get("contract",""),  "action": "buy_to_open",  "qty": 1,
                         "limit_price": _snap_price(long_mid),  "option_type": "call",
                         "strike": long_strike,  "expiry": exp_str},
                        {"symbol": short.get("contract",""), "action": "sell_to_open", "qty": 1,
                         "limit_price": _snap_price(short_mid), "option_type": "call",
                         "strike": short_strike, "expiry": exp_str},
                    ],
                })

    results.sort(key=lambda r: -r["score"])
    return {"ticker": ticker.upper(), "spot": spot, "candidates": results[:25]}


@app.get("/api/options/screener/pmcc")
async def screener_pmcc(
    ticker:      str,
    min_short_dte: int   = 7,
    max_short_dte: int   = 30,
    min_long_dte:  int   = 45,
    max_long_dte:  int   = 180,
    otm_pct:       float = 0.04,
    token:         str   = "",
):
    """
    Poor Man's Covered Call screener.
    Long deep-ITM LEAP (45–180 DTE, delta ≥ 0.70) + Short OTM weekly call (7–30 DTE).
    Returns viable PMCC pairs ranked by annualised gain.
    """
    check_token(token)
    from datetime import date

    try:
        chain = await _get_broker_option_chain(ticker.upper())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    spot = float(chain.get("price") or 0)
    if spot <= 0:
        raise HTTPException(status_code=422, detail="No underlying price available")

    calls = chain.get("calls", [])
    today = date.today()

    def _dte(exp_str):
        try:
            return (date.fromisoformat(exp_str) - today).days
        except Exception:
            return -1

    long_calls  = [
        c for c in calls
        if min_long_dte  <= _dte(c.get("expiration","")) <= max_long_dte
        and float(c.get("delta") or 0) >= 0.70
    ]
    short_calls = [
        c for c in calls
        if min_short_dte <= _dte(c.get("expiration","")) <= max_short_dte
        and float(c.get("strike") or 0) >= spot * (1 + otm_pct)
    ]

    results = []
    for long_leg in long_calls:
        long_price      = float(long_leg.get("mid") or long_leg.get("ask") or 0)
        long_time_value = float(long_leg.get("extrinsic") or 0)
        long_dte        = _dte(long_leg.get("expiration",""))

        for short_leg in short_calls:
            short_bid = float(short_leg.get("bid") or 0)
            short_dte = _dte(short_leg.get("expiration",""))

            if short_dte >= long_dte:
                continue
            if short_bid <= long_time_value:
                continue

            out_of_pocket  = long_price - short_bid
            if out_of_pocket <= 0:
                continue

            max_gain_pp    = (float(short_leg.get("strike") or 0) -
                              float(long_leg.get("strike") or 0)) - out_of_pocket
            if max_gain_pp <= 0:
                continue

            ann_gain = (max_gain_pp / out_of_pocket) * (365 / short_dte) if short_dte > 0 else 0

            results.append({
                "underlying":     ticker.upper(),
                "strategy":       "pmcc",
                "long_expiry":    long_leg.get("expiration",""),
                "long_dte":       long_dte,
                "long_strike":    float(long_leg.get("strike") or 0),
                "long_delta":     round(float(long_leg.get("delta") or 0), 3),
                "long_contract":  long_leg.get("contract",""),
                "short_expiry":   short_leg.get("expiration",""),
                "short_dte":      short_dte,
                "short_strike":   float(short_leg.get("strike") or 0),
                "short_contract": short_leg.get("contract",""),
                "short_bid":      short_bid,
                "out_of_pocket":  round(out_of_pocket * 100, 2),
                "max_gain":       round(max_gain_pp * 100, 2),
                "ann_gain":       round(ann_gain, 4),
                "score":          round(ann_gain, 4),
                "legs": [
                    {"symbol": long_leg.get("contract",""),  "action": "buy_to_open",  "qty": 1,
                     "limit_price": _snap_price(long_price), "option_type": "call",
                     "strike": float(long_leg.get("strike") or 0), "expiry": long_leg.get("expiration","")},
                    {"symbol": short_leg.get("contract",""), "action": "sell_to_open", "qty": 1,
                     "limit_price": _snap_price(short_bid),  "option_type": "call",
                     "strike": float(short_leg.get("strike") or 0), "expiry": short_leg.get("expiration","")},
                ],
            })

    results.sort(key=lambda r: -r["score"])
    return {"ticker": ticker.upper(), "spot": spot, "candidates": results[:20]}


@app.post("/api/options/spread-greeks")
async def spread_greeks(request: Request):
    """
    Compute net Greeks for a multi-leg spread.

    Body:
      legs: [{contract, underlying, option_type, strike, expiry, side, qty,
               mid, delta, gamma, theta, vega, rho, iv}]
      spot: float (optional — underlying price for dollar-delta calc)

    Greeks lookup priority per leg:
      1. Values supplied in the leg dict
      2. Active option_positions row (lookup by contract symbol)
    """
    body = await request.json()
    check_token(body.get("token", ""))

    legs_in  = body.get("legs", [])
    spot     = float(body.get("spot") or 0)
    pool     = await _get_db_pool()

    _GREEKS = ("delta", "gamma", "theta", "vega", "rho")
    net     = {g: 0.0 for g in _GREEKS}
    legs_out = []

    for leg in legs_in:
        side   = str(leg.get("side", "buy")).lower()
        qty    = float(leg.get("qty") or 1)
        mid    = float(leg.get("mid") or 0)
        sign   = 1.0 if side.startswith("buy") else -1.0
        mult   = sign * qty * 100   # 1 contract = 100 shares

        # Resolve Greeks
        greeks       = {}
        greeks_src   = "none"

        # Priority 1 — caller-supplied
        for g in _GREEKS:
            v = leg.get(g)
            if v is not None:
                greeks[g] = float(v)
        if len(greeks) == len(_GREEKS):
            greeks_src = "chain"

        # Priority 2 — DB lookup by contract symbol
        if greeks_src == "none" and leg.get("contract") and pool:
            try:
                row = await pool.fetchrow(
                    """SELECT delta, gamma, theta, vega, rho
                       FROM option_positions
                       WHERE contract_symbol = $1 AND status = 'active'
                       LIMIT 1""",
                    leg["contract"],
                )
                if row:
                    for g in _GREEKS:
                        if row[g] is not None:
                            greeks[g] = float(row[g])
                    if greeks:
                        greeks_src = "db"
            except Exception:
                pass

        # Compute this leg's scaled contribution
        contrib = {}
        for g in _GREEKS:
            v = greeks.get(g)
            if v is not None:
                c = round(v * mult, 4)
                contrib[g] = c
                net[g] = round(net[g] + c, 4)

        legs_out.append({
            "contract":    leg.get("contract", ""),
            "underlying":  leg.get("underlying", ""),
            "option_type": leg.get("option_type", ""),
            "strike":      leg.get("strike"),
            "expiry":      leg.get("expiry", ""),
            "side":        side,
            "qty":         qty,
            "mid":         mid,
            "greeks":      greeks,
            "greeks_src":  greeks_src,
            "contribution": contrib,
        })

    # Net premium: positive = credit received, negative = debit paid
    net_premium = round(sum(
        (1.0 if l["side"].startswith("buy") else -1.0) * l["mid"] * l["qty"] * 100
        for l in legs_out
    ), 2)

    # Dollar delta (directional $ exposure per $1 move in underlying)
    delta_dollars = round(net["delta"] * spot, 2) if spot else None

    # Theta per day in dollars
    theta_day = round(net["theta"], 2) if net.get("theta") else None

    # Break-even estimates for simple 2-leg vertical spreads
    break_evens: list[float] = []
    if len(legs_out) == 2:
        buy_legs  = [l for l in legs_out if l["side"].startswith("buy")]
        sell_legs = [l for l in legs_out if not l["side"].startswith("buy")]
        if buy_legs and sell_legs:
            debit = abs(net_premium) / 100  # per share
            bl = buy_legs[0]
            if bl["option_type"] == "call" and bl["strike"] is not None:
                break_evens.append(round(float(bl["strike"]) + debit, 2))
            elif bl["option_type"] == "put" and bl["strike"] is not None:
                break_evens.append(round(float(bl["strike"]) - debit, 2))

    return {
        "legs":         legs_out,
        "net":          {k: round(v, 4) for k, v in net.items()},
        "net_premium":  net_premium,
        "delta_dollars": delta_dollars,
        "theta_day":    theta_day,
        "break_evens":  break_evens,
        "legs_count":   len(legs_out),
    }


@app.post("/api/options/spreads/place")
async def place_spread_order(request: Request):
    """
    Place a multi-leg spread order through the broker gateway.
    Body: {token, account_label, underlying, strategy_type, legs, net_debit, duration}
    """
    body = await request.json()
    check_token(body.get("token", ""))

    account_label = body.get("account_label", "")
    underlying    = body.get("underlying", "").upper()
    strategy_type = body.get("strategy_type", "")
    legs          = body.get("legs", [])
    net_debit     = body.get("net_debit")
    duration      = body.get("duration", "day")

    if not underlying or not legs or len(legs) < 2:
        raise HTTPException(status_code=400, detail="underlying and at least 2 legs are required")

    import uuid as _uuid, json as _json, redis.asyncio as _aioredis
    try:
        REDIS_URL = os.getenv("REDIS_URL", "redis://ot-redis:6379/0")
        redis = await _aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    req_id = str(_uuid.uuid4())
    cmd = {
        "command":       "place_spread_order",
        "request_id":    req_id,
        "account_label": account_label,
        "underlying":    underlying,
        "strategy_type": strategy_type,
        "legs":          _json.dumps(legs),
        "net_debit":     str(net_debit) if net_debit is not None else "",
        "duration":      duration,
        "issued_by":     "webui",
    }
    await redis.xadd(STREAMS["broker_commands"], cmd)
    result = await redis.blpop([f"broker:reply:{req_id}"], timeout=20)
    await redis.aclose()

    if not result:
        raise HTTPException(status_code=504, detail="Spread order gateway timeout")

    raw = _json.loads(result[1])
    r   = raw[0] if isinstance(raw, list) else raw
    if r.get("status") != "ok":
        raise HTTPException(status_code=502, detail=r.get("error", "Spread order failed"))

    return {"status": "ok", "data": r.get("data", {})}


@app.post("/api/market/stock-analysis/{ticker}/persona")
async def stock_persona_analysis(ticker: str, request: Request):
    """
    Investor-persona stock analysis using an LLM.
    Body: {"persona": "buffett"|"graham"|"lynch"|"munger"|"klarman", "token": "..."}
    Returns structured analysis in the persona's investment style.
    """
    from llm.connector import LLMConnector as _LLM
    body    = await request.json()
    token   = body.get("token", "")
    persona = body.get("persona", "buffett").lower()
    check_token(token)

    ticker = ticker.upper()
    if persona not in _PERSONA_PROMPTS:
        return {"error": f"Unknown persona: {persona}. Use: {list(_PERSONA_PROMPTS.keys())}"}

    cache_key = f"persona:{persona}:{ticker}"
    try:
        _r = await get_redis()
        cached = await _r.get(cache_key)
        if cached:
            return {**json.loads(cached), "source": "cache"}
    except Exception:
        _r = None

    # Gather fundamental data
    api_key = os.getenv("MASSIVE_API_KEY", "")
    fin, price = await asyncio.gather(
        _fetch_poly_financials(ticker, api_key) if api_key else asyncio.sleep(0, result={}),
        _fetch_poly_price(ticker, api_key)      if api_key else asyncio.sleep(0, result=0.0),
    )

    # Fetch recent news headlines for context
    news_lines: list[str] = []
    try:
        _r2 = await get_redis()
        cached_news = await _r2.get(f"eodhd_news:{ticker}")
        if cached_news:
            articles = json.loads(cached_news)[:5]
            news_lines = [f"- {a.get('title', '')} ({a.get('source', '')})" for a in articles]
    except Exception:
        pass

    # Build data context for the LLM
    fin_ctx = ""
    if fin:
        rev_b   = round(fin.get("revenue", 0) / 1e9, 2)
        ebitda_b = round(fin.get("ebitda", 0) / 1e9, 2)
        margin  = round(fin.get("ebit", 0) / fin.get("revenue", 1) * 100, 1) if fin.get("revenue") else None
        rev_g   = round(fin.get("rev_growth", 0) * 100, 1)
        net_d   = round(fin.get("net_debt", 0) / 1e9, 2)
        fin_ctx = (
            f"Revenue: ${rev_b}B (YoY growth: {rev_g}%)\n"
            f"EBITDA: ${ebitda_b}B\n"
            f"Operating margin: {margin}%\n"
            f"Net debt: ${net_d}B\n"
            f"Tax rate: {round(fin.get('tax_rate', 0) * 100, 0)}%\n"
        )

    news_ctx = "\nRecent news:\n" + "\n".join(news_lines) if news_lines else ""

    prompt = (
        f"Ticker: {ticker}\n"
        f"Current price: ${round(float(price or 0), 2)}\n"
        f"{fin_ctx}"
        f"{news_ctx}\n\n"
        f"Provide your analysis as {_PERSONA_NAMES[persona]}."
    )

    try:
        llm    = _LLM("persona")
        result_text = await llm.complete(
            prompt=prompt,
            system=_PERSONA_PROMPTS[persona],
            max_tokens=600,
            temperature=0.3,
        )
    except Exception as e:
        return {"error": f"LLM error: {str(e)}", "ticker": ticker, "persona": persona}

    result = {
        "ticker":      ticker,
        "persona":     persona,
        "persona_name": _PERSONA_NAMES[persona],
        "price":       round(float(price or 0), 2),
        "analysis":    result_text,
        "ts":          datetime.now(timezone.utc).isoformat(),
    }
    try:
        if _r:
            await _r.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass
    return {**result, "source": "fresh"}


@app.get("/api/predictor/schedule")
async def get_predictor_schedule(token: str = ""):
    """Return predictor daily limit and scheduled-run config from Redis."""
    check_token(token)
    import redis.asyncio as _aioredis
    _r = await _aioredis.from_url(
        os.getenv("REDIS_URL", "redis://ot-redis:6379/0"),
        encoding="utf-8", decode_responses=True,
    )
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        limit_raw    = await _r.get("config:predictor:daily_limit")
        enabled_10am = await _r.get("config:predictor:schedule_10am")
        enabled_2pm  = await _r.get("config:predictor:schedule_2pm")
        time_10am    = await _r.get("config:predictor:time_10am")
        time_2pm     = await _r.get("config:predictor:time_2pm")
        run_count    = await _r.get(f"predictor:runs:{today}")
        return {
            "daily_limit":    int(limit_raw) if limit_raw else 2,
            "schedule_10am":  (enabled_10am or "true").lower() not in ("false", "0", "no"),
            "schedule_2pm":   (enabled_2pm  or "true").lower() not in ("false", "0", "no"),
            "time_10am":      time_10am or "10:00",
            "time_2pm":       time_2pm  or "14:00",
            "runs_today":     int(run_count) if run_count else 0,
        }
    finally:
        await _r.aclose()


@app.post("/api/predictor/schedule")
async def set_predictor_schedule(body: dict):
    """Persist predictor daily limit, scheduled-run toggles, and run times to Redis.
    Time changes are hot-reloaded into APScheduler via the scheduler:reload pub/sub channel."""
    check_token(body.get("token", ""))
    import redis.asyncio as _aioredis
    import json as _json
    import re as _re
    _r = await _aioredis.from_url(
        os.getenv("REDIS_URL", "redis://ot-redis:6379/0"),
        encoding="utf-8", decode_responses=True,
    )
    try:
        if "daily_limit" in body:
            limit = max(1, min(20, int(body["daily_limit"])))
            await _r.set("config:predictor:daily_limit", str(limit))
        if "schedule_10am" in body:
            await _r.set("config:predictor:schedule_10am", "true" if body["schedule_10am"] else "false")
        if "schedule_2pm" in body:
            await _r.set("config:predictor:schedule_2pm", "true" if body["schedule_2pm"] else "false")

        for time_key, job_id, default_t in [
            ("time_10am", "predict_10am", "10:00"),
            ("time_2pm",  "predict_2pm",  "14:00"),
        ]:
            if time_key not in body:
                continue
            raw_t = str(body[time_key]).strip()
            if not _re.match(r"^\d{1,2}:\d{2}$", raw_t):
                raise HTTPException(status_code=400, detail=f"Invalid time format for {time_key}: use HH:MM")
            h, m = map(int, raw_t.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise HTTPException(status_code=400, detail=f"Time out of range for {time_key}")
            t_str = f"{h:02d}:{m:02d}"
            await _r.set(f"config:predictor:{time_key}", t_str)
            # Update job record so scheduler can reschedule via reload pub/sub
            raw_rec = await _r.get(f"scheduler:job:{job_id}")
            rec = _json.loads(raw_rec) if raw_rec else {}
            rec["cron_hour"]   = h
            rec["cron_minute"] = m
            rec["cron_days"]   = "mon-fri"
            await _r.set(f"scheduler:job:{job_id}", _json.dumps(rec))
            await _r.publish("scheduler:reload", job_id)

        return {"ok": True}
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Webhook Signal Intake — external signal ingestion (TradingView, custom)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/signal")
async def webhook_signal(request: Request):
    """Accept an external trading signal (TradingView, custom bots) and publish to predictor.signals."""
    import redis.asyncio as _aioredis

    _whk = os.getenv("WEBHOOK_SECRET", "")
    if not _whk:
        return JSONResponse(status_code=503, content={"error": "Webhook not configured — set WEBHOOK_SECRET"})

    body = await request.json()

    # Validate token — accept from X-Webhook-Secret header OR 'secret' body field
    _hdr_tok  = request.headers.get("X-Webhook-Secret", "")
    _body_tok = body.get("secret", "")
    _candidate = _hdr_tok or _body_tok
    if not _candidate or not hmac.compare_digest(_candidate, _whk):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    ticker    = (body.get("ticker") or "").strip().upper()
    direction = (body.get("direction") or "").strip().lower()
    source    = (body.get("source") or "external").strip()

    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    if direction not in ("long", "short"):
        raise HTTPException(status_code=400, detail="direction must be 'long' or 'short'")

    confidence = body.get("confidence")
    if confidence is None:
        confidence = 0.7
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise HTTPException(status_code=400, detail="confidence must be between 0.0 and 1.0")

    entry  = body.get("entry")
    stop   = body.get("stop")
    target = body.get("target")

    log.info("webhook.signal", ticker=ticker, direction=direction, source=source)

    _r = await _aioredis.from_url(
        os.getenv("REDIS_URL", "redis://ot-redis:6379/0"),
        encoding="utf-8", decode_responses=True,
    )
    try:
        payload = {
            "ticker":      ticker,
            "direction":   direction,
            "confidence":  str(confidence),
            "asset_class": "equity",
            "source":      f"webhook:{source}",
            "ttl_ms":      "30000",
            "issued_by":   "webhook",
        }
        if entry  is not None: payload["entry"]  = str(entry)
        if stop   is not None: payload["stop"]   = str(stop)
        if target is not None: payload["target"] = str(target)
        await _r.xadd(STREAMS["signals"], payload)
    finally:
        await _r.aclose()

    return {"status": "ok", "ticker": ticker, "direction": direction, "stream": "predictor.signals"}


# ══════════════════════════════════════════════════════════════════════════════
# Options — Full Black-Scholes Greeks
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/options/greeks")
async def options_greeks(
    spot: float, strike: float, T: float,
    option_type: str = "call",
    r: float = 0.045,
    sigma: float | None = None,
    market_price: float | None = None,
    token: str = "",
):
    """Full Black-Scholes Greeks: delta, gamma, theta, vega, rho plus intrinsic/time-value."""
    check_token(token)
    import math as _m

    if spot <= 0 or strike <= 0:
        raise HTTPException(status_code=400, detail="spot and strike must be positive")

    opt = option_type.lower().strip()
    if opt not in ("call", "put"):
        raise HTTPException(status_code=400, detail="option_type must be 'call' or 'put'")

    if sigma is None:
        if market_price is not None:
            def _bs_price(S, K, t, sig, ri, o):
                if t <= 0 or sig <= 0:
                    return max(0.0, S - K) if o == "call" else max(0.0, K - S)
                st  = _m.sqrt(t)
                d1_ = (_m.log(S / K) + (ri + 0.5 * sig ** 2) * t) / (sig * st)
                d2_ = d1_ - sig * st
                nc  = lambda x: (1 + _m.erf(x / _m.sqrt(2))) / 2
                er  = _m.exp(-ri * t)
                return S * nc(d1_) - K * er * nc(d2_) if o == "call" else K * er * nc(-d2_) - S * nc(-d1_)

            def _bs_vega_fn(S, K, t, sig, ri):
                if t <= 0 or sig <= 0:
                    return 0.0
                d1_ = (_m.log(S / K) + (ri + 0.5 * sig ** 2) * t) / (sig * _m.sqrt(t))
                return S * _m.exp(-0.5 * d1_ ** 2) / _m.sqrt(2 * _m.pi) * _m.sqrt(t)

            sig = 0.20
            for _ in range(100):
                diff_ = _bs_price(spot, strike, T, sig, r, opt) - market_price
                if abs(diff_) < 1e-6:
                    break
                vg = _bs_vega_fn(spot, strike, T, sig, r)
                if abs(vg) < 1e-8:
                    break
                sig = max(0.001, min(5.0, sig - diff_ / vg))
            sigma = sig
        else:
            sigma = 0.20

    if sigma <= 0:
        raise HTTPException(status_code=400, detail="sigma must be positive")

    intrinsic = max(0.0, spot - strike) if opt == "call" else max(0.0, strike - spot)
    if T <= 0:
        return {
            "delta": round(1.0 if (opt == "call" and spot > strike) else (0.0 if opt == "call" else (-1.0 if spot < strike else 0.0)), 6),
            "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0,
            "implied_vol": round(sigma, 6),
            "d1": 0.0, "d2": 0.0,
            "intrinsic":  round(intrinsic, 6),
            "time_value": 0.0,
        }

    _nc  = lambda x: (1 + _m.erf(x / _m.sqrt(2))) / 2
    _phi = lambda x: _m.exp(-0.5 * x * x) / _m.sqrt(2 * _m.pi)

    sq_T = _m.sqrt(T)
    d1   = (_m.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * sq_T)
    d2   = d1 - sigma * sq_T
    er   = _m.exp(-r * T)

    if opt == "call":
        delta    = _nc(d1)
        rho      = strike * T * er * _nc(d2) / 100
        theta    = (-(spot * _phi(d1) * sigma) / (2 * sq_T) - r * strike * er * _nc(d2)) / 365
        price_bs = spot * _nc(d1) - strike * er * _nc(d2)
    else:
        delta    = _nc(d1) - 1
        rho      = -strike * T * er * _nc(-d2) / 100
        theta    = (-(spot * _phi(d1) * sigma) / (2 * sq_T) + r * strike * er * _nc(-d2)) / 365
        price_bs = strike * er * _nc(-d2) - spot * _nc(-d1)

    gamma      = _phi(d1) / (spot * sigma * sq_T)
    vega       = spot * _phi(d1) * sq_T / 100
    time_value = max(0.0, price_bs - intrinsic)

    return {
        "delta":       round(delta,      6),
        "gamma":       round(gamma,      6),
        "theta":       round(theta,      6),
        "vega":        round(vega,       6),
        "rho":         round(rho,        6),
        "implied_vol": round(sigma,      6),
        "d1":          round(d1,         6),
        "d2":          round(d2,         6),
        "intrinsic":   round(intrinsic,  6),
        "time_value":  round(time_value, 6),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Factor Research — IC / IR scoring for alpha factor discovery
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/research/factors/{ticker}")
async def research_factors(ticker: str, lookback_days: int = 90, token: str = ""):
    """Alpha factor IC/IR scoring for a ticker over a lookback window."""
    check_token(token)
    import redis.asyncio as _aioredis
    import json as _json
    from predictor.factors import compute_factor_ic

    cache_key = f"research:factors:{ticker.upper()}:{lookback_days}"
    _r = await _aioredis.from_url(
        os.getenv("REDIS_URL", "redis://ot-redis:6379/0"),
        encoding="utf-8", decode_responses=True,
    )
    try:
        cached = await _r.get(cache_key)
        if cached:
            return JSONResponse(content=_json.loads(cached))

        try:
            result = await compute_factor_ic(ticker, lookback_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            log.error("research.factors.error", ticker=ticker, error=str(exc))
            raise HTTPException(status_code=503, detail="Factor computation unavailable")

        await _r.set(cache_key, _json.dumps(result), ex=900)
        return result
    finally:
        await _r.aclose()


# Geopolitics — GPRI and macro conflict signals
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/research/geopolitics")
async def get_geopolitics(token: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        _fred_key = os.getenv("FRED_API_KEY", "")
        if not _fred_key:
            return {"error": "FRED_API_KEY not configured", "gpri": None}

        cache_key = "research:geopolitics"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        async with _aiohttp.ClientSession() as session:
            result = {"gpri": None, "gprh": None, "updated": datetime.now(timezone.utc).isoformat()}

            for series_id in ["GPRI", "GPRH"]:
                url = (
                    f"https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id={series_id}&api_key={_fred_key}&limit=12&sort_order=desc&file_type=json"
                )
                try:
                    async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            obs = data.get("observations", [])
                            history = []
                            for o in obs:
                                try:
                                    history.append({"date": o["date"], "value": float(o["value"])})
                                except (ValueError, KeyError):
                                    pass
                            latest = history[0]["value"] if history else None
                            result[series_id.lower()] = {"latest": latest, "history": history}
                except Exception:
                    pass

        await _r.set(cache_key, json.dumps(result), ex=3600)
        return result
    finally:
        await _r.aclose()


@app.get("/api/research/geopolitics/events")
async def get_geopolitics_events(token: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = "research:geopolitics:events"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        events = []
        try:
            url = (
                "https://api.gdeltproject.org/api/v2/doc/doc"
                "?query=war+conflict+sanctions+geopolitical&mode=artlist&maxrecords=20&format=json&timespan=7d"
            )
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        articles = data.get("articles", [])
                        for a in articles:
                            events.append({
                                "title": a.get("title", ""),
                                "url": a.get("url", ""),
                                "seendate": a.get("seendate", ""),
                                "domain": a.get("domain", ""),
                            })
        except Exception:
            pass

        result = {"events": events, "count": len(events)}
        await _r.set(cache_key, json.dumps(result), ex=1800)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Corporate Relationship Map
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/market/relationships/{ticker}")
async def get_market_relationships(ticker: str, token: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        ticker = ticker.upper()
        cache_key = f"market:relationships:{ticker}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        minimal = {
            "ticker": ticker,
            "nodes": [{"id": ticker, "label": ticker, "type": "self"}],
            "edges": [],
        }

        try:
            async with _aiohttp.ClientSession() as session:
                # Resolve ticker -> CIK via SEC EDGAR full-text search
                search_url = (
                    f"https://efts.sec.gov/LATEST/search-index?q={ticker}&forms=10-K"
                    f"&dateRange=custom&startdt=2020-01-01&enddt=2025-01-01"
                )
                headers = {"User-Agent": "OpenTrader research@opentrader.app"}
                cik = None
                async with session.get(search_url, headers=headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        hits = data.get("hits", {}).get("hits", [])
                        for hit in hits:
                            s = hit.get("_source", {})
                            if s.get("entity_name", "").upper() == ticker or ticker in s.get("display_names", []):
                                cik_raw = s.get("entity_id", "")
                                if cik_raw:
                                    cik = str(cik_raw).zfill(10)
                                break

                nodes = [{"id": ticker, "label": ticker, "type": "self"}]
                edges = []

                if cik:
                    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                    async with session.get(sub_url, headers=headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp2:
                        if resp2.status == 200:
                            sub_data = await resp2.json(content_type=None)
                            company_name = sub_data.get("name", ticker)
                            nodes[0]["label"] = company_name

                            former_names = sub_data.get("formerNames", [])
                            for fn in former_names[:3]:
                                node_id = fn.get("name", "")
                                if node_id:
                                    nodes.append({"id": node_id, "label": node_id, "type": "former"})
                                    edges.append({"source": ticker, "target": node_id, "type": "formerly"})

                result = {"ticker": ticker, "nodes": nodes, "edges": edges}
        except Exception:
            result = {**minimal, "error": "relationship data unavailable"}

        await _r.set(cache_key, json.dumps(result), ex=86400)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Trade Visualization — OHLCV price chart with trade entry/exit overlays
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/trades/viz/{ticker}")
async def get_trade_viz(ticker: str, days: int = 90, token: str = ""):
    check_token(token)
    import aiohttp as _aiohttp
    ticker = ticker.upper()
    MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://ot-market-data:8090")
    ohlcv = []
    trades = []

    # Fetch OHLCV from market data gateway
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                f"{MARKET_DATA_URL}/ohlcv/{ticker}?days={days}",
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    ohlcv = data if isinstance(data, list) else data.get("bars", data.get("data", []))
    except Exception:
        pass

    # Fetch trades from DB
    try:
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
        try:
            rows = await pool.fetch(
                """
                SELECT id, ticker, direction, qty, entry_price, exit_price, entry_ts, exit_ts, pnl
                FROM completed_trades
                WHERE ticker=$1 AND entry_ts >= NOW() - ($2 || ' days')::INTERVAL
                ORDER BY entry_ts
                """,
                ticker,
                str(days),
            )
            for row in rows:
                trades.append({
                    "entry_ts": row["entry_ts"].isoformat() if row["entry_ts"] else None,
                    "exit_ts": row["exit_ts"].isoformat() if row["exit_ts"] else None,
                    "direction": row["direction"],
                    "entry_price": float(row["entry_price"]) if row["entry_price"] is not None else None,
                    "exit_price": float(row["exit_price"]) if row["exit_price"] is not None else None,
                    "pnl": float(row["pnl"]) if row["pnl"] is not None else None,
                    "qty": row["qty"],
                })
        finally:
            await pool.close()
    except Exception:
        pass

    return {"ticker": ticker, "ohlcv": ohlcv, "trades": trades}


# ══════════════════════════════════════════════════════════════════════════════
# Polymarket — Prediction market signals
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sentiment/polymarket")
async def get_polymarket_signals(token: str = "", category: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = f"sentiment:polymarket:{category}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        signals = []
        # Try DB first
        try:
            pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
            try:
                query = """
                    SELECT market_id, ticker, question, yes_price, no_price, volume, category, scraped_at
                    FROM polymarket_signals
                    WHERE scraped_at > NOW() - INTERVAL '24 hours'
                """
                params = []
                if category:
                    query += " AND category=$1"
                    params.append(category)
                query += " ORDER BY volume DESC LIMIT 50"
                rows = await pool.fetch(query, *params)
                for row in rows:
                    signals.append({
                        "market_id": row["market_id"],
                        "ticker": row["ticker"],
                        "question": row["question"],
                        "yes_price": float(row["yes_price"]) if row["yes_price"] is not None else None,
                        "no_price": float(row["no_price"]) if row["no_price"] is not None else None,
                        "volume": float(row["volume"]) if row["volume"] is not None else None,
                        "category": row["category"],
                        "scraped_at": row["scraped_at"].isoformat() if row["scraped_at"] else None,
                    })
            finally:
                await pool.close()
        except Exception:
            pass

        # Fallback: live Polymarket API
        if not signals:
            try:
                async with _aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://gamma-api.polymarket.com/markets?active=true&limit=30",
                        timeout=_aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            markets = data if isinstance(data, list) else data.get("markets", [])
                            for m in markets:
                                cat = m.get("category", "")
                                if category and cat.lower() != category.lower():
                                    continue
                                outcome_prices = m.get("outcomePrices", [])
                                signals.append({
                                    "market_id": m.get("id", ""),
                                    "ticker": m.get("slug", ""),
                                    "question": m.get("question", ""),
                                    "yes_price": outcome_prices[0] if outcome_prices else None,
                                    "no_price": outcome_prices[1] if len(outcome_prices) > 1 else None,
                                    "volume": m.get("volume", 0),
                                    "category": cat,
                                    "scraped_at": None,
                                })
            except Exception:
                pass

        result = {"signals": signals, "count": len(signals)}
        await _r.set(cache_key, json.dumps(result), ex=300)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# M&A Analytics — Merger and acquisition deal tracking
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/research/ma-deals")
async def get_ma_deals(token: str = "", days: int = 30):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = f"research:ma-deals:{days}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        deals = []
        # Try DB first
        try:
            pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
            try:
                rows = await pool.fetch(
                    """
                    SELECT acquirer, target, form_type, filing_date, deal_url, tickers, scraped_at
                    FROM ma_deals
                    WHERE scraped_at > NOW() - ($1 || ' days')::INTERVAL
                    ORDER BY scraped_at DESC LIMIT 50
                    """,
                    str(days),
                )
                for row in rows:
                    deals.append({
                        "acquirer": row["acquirer"],
                        "target": row["target"],
                        "form_type": row["form_type"],
                        "filing_date": row["filing_date"].isoformat() if row["filing_date"] else None,
                        "deal_url": row["deal_url"],
                        "tickers": row["tickers"],
                        "scraped_at": row["scraped_at"].isoformat() if row["scraped_at"] else None,
                    })
            finally:
                await pool.close()
        except Exception:
            pass

        # Fallback: SEC EDGAR search for SC 13D/14D filings
        if not deals:
            try:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                async with _aiohttp.ClientSession() as session:
                    for form in ["SC+13D", "SC+14D"]:
                        feed_url = (
                            f"https://efts.sec.gov/LATEST/search-index?forms={form}"
                            f"&dateRange=custom&startdt={cutoff}&enddt={today_str}"
                        )
                        async with session.get(feed_url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json(content_type=None)
                                hits = data.get("hits", {}).get("hits", [])
                                for hit in hits[:25]:
                                    src = hit.get("_source", {})
                                    file_num = src.get("file_num", "")
                                    deals.append({
                                        "acquirer": src.get("entity_name", ""),
                                        "target": file_num,
                                        "form_type": src.get("form_type", form.replace("+", " ")),
                                        "filing_date": src.get("period_of_report", ""),
                                        "deal_url": (
                                            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                                            f"&filenum={file_num}&type={form}&dateb=&owner=include&count=40"
                                        ),
                                        "tickers": [],
                                        "scraped_at": None,
                                    })
            except Exception:
                pass

        updated = datetime.now(timezone.utc).isoformat()
        result = {"deals": deals, "count": len(deals), "updated": updated}
        await _r.set(cache_key, json.dumps(result), ex=3600)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Universe Scanner — real-time scan alerts
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/scanner/alerts")
async def get_scanner_alerts(token: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"scanner:alerts:{today}"
        raw = await _r.zrangebyscore(key, "-inf", "+inf", withscores=True, start=0, num=100)
        alerts = []
        for member, score in raw:
            try:
                obj = json.loads(member)
                obj["_score"] = score
                alerts.append(obj)
            except Exception:
                alerts.append({"raw": member, "_score": score})
        return {"alerts": alerts, "date": today, "count": len(alerts)}
    finally:
        await _r.aclose()


@app.post("/api/scanner/run")
async def run_scanner(request: Request):
    body = await request.json()
    check_token(body.get("token", ""))
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        universe = body.get("universe", [])
        rules = body.get("rules", [])
        msg = json.dumps({"job": "scrape_scanner", "universe": universe, "rules": rules})
        await _r.xadd("system.commands", {"data": msg})
        return {"status": "triggered", "universe_size": len(universe)}
    finally:
        await _r.aclose()


_SCAN_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "scan_rules.json")
_DEFAULT_SCAN_RULES = [
    {"field": "volume_ratio", "op": ">", "value": 2.0, "label": "Volume Surge"},
    {"field": "rsi_14", "op": "<", "value": 30, "label": "Oversold RSI"},
    {"field": "rsi_14", "op": ">", "value": 70, "label": "Overbought RSI"},
    {"field": "price_change_pct", "op": ">", "value": 5.0, "label": "Price Breakout +5%"},
    {"field": "price_change_pct", "op": "<", "value": -5.0, "label": "Price Drop -5%"},
]


@app.get("/api/scanner/rules")
async def get_scanner_rules(token: str = ""):
    check_token(token)
    try:
        rules_path = os.path.abspath(_SCAN_RULES_PATH)
        if os.path.exists(rules_path):
            with open(rules_path) as f:
                rules = json.load(f)
        else:
            rules = _DEFAULT_SCAN_RULES
        return {"rules": rules}
    except Exception as exc:
        log.error("scanner.rules.read.error", error=str(exc))
        return {"rules": _DEFAULT_SCAN_RULES}


@app.post("/api/scanner/rules")
async def save_scanner_rules(request: Request):
    body = await request.json()
    check_token(body.get("token", ""))
    rules = body.get("rules", [])
    for rule in rules:
        if not all(k in rule for k in ("field", "op", "value")):
            raise HTTPException(status_code=400, detail="Each rule must have 'field', 'op', and 'value' keys")
    try:
        rules_path = os.path.abspath(_SCAN_RULES_PATH)
        os.makedirs(os.path.dirname(rules_path), exist_ok=True)
        with open(rules_path, "w") as f:
            json.dump(rules, f, indent=2)
        return {"status": "ok", "rules": rules}
    except Exception as exc:
        log.error("scanner.rules.write.error", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to save rules: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# DBnomics — 100+ macro data providers
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/market-data/dbnomics/search")
async def dbnomics_search(q: str, token: str = "", limit: int = 10):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = f"dbnomics:search:{q}:{limit}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        series = []
        total = 0
        try:
            url = f"https://api.db.nomics.world/v22/series?q={q}&limit={limit}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        items = data.get("series", {})
                        docs = items.get("docs", []) if isinstance(items, dict) else []
                        total = items.get("num_found", len(docs)) if isinstance(items, dict) else len(docs)
                        for s in docs:
                            series.append({
                                "provider_code": s.get("provider_code", ""),
                                "dataset_code": s.get("dataset_code", ""),
                                "series_code": s.get("series_code", ""),
                                "name": s.get("series_name", s.get("name", "")),
                            })
        except Exception as exc:
            log.warning("dbnomics.search.error", q=q, error=str(exc))

        result = {"series": series, "total": total}
        await _r.set(cache_key, json.dumps(result), ex=3600)
        return result
    finally:
        await _r.aclose()


@app.get("/api/market-data/dbnomics/{provider}/{dataset}/{series_id}")
async def dbnomics_series(provider: str, dataset: str, series_id: str, token: str = "", observations: int = 24):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = f"dbnomics:{provider}:{dataset}:{series_id}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        result = {"name": "", "provider": provider, "observations": []}
        try:
            url = f"https://api.db.nomics.world/v22/series/{provider}/{dataset}/{series_id}?observations=1&limit={observations}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        series_list = data.get("series", {}).get("docs", [])
                        if series_list:
                            s = series_list[0]
                            result["name"] = s.get("series_name", s.get("name", ""))
                            periods = s.get("period", [])
                            values = s.get("value", [])
                            obs_list = []
                            for p, v in zip(periods, values):
                                try:
                                    obs_list.append({"period": p, "value": float(v) if v is not None and v != "NA" else None})
                                except (ValueError, TypeError):
                                    obs_list.append({"period": p, "value": None})
                            result["observations"] = obs_list
        except Exception as exc:
            log.warning("dbnomics.series.error", provider=provider, dataset=dataset, series_id=series_id, error=str(exc))

        await _r.set(cache_key, json.dumps(result), ex=3600)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Asia Markets — AkShare connector proxy
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/market-data/asia/{ticker}")
async def get_asia_market_data(ticker: str, market: str = "hk", days: int = 30, token: str = ""):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        cache_key = f"asia:ohlcv:{market}:{ticker}:{days}"
        cached = await _r.get(cache_key)
        if cached:
            return json.loads(cached)

        MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://ot-market-data:8090")
        bars = []

        # Try market data gateway first
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(
                    f"{MARKET_DATA_URL}/data?type=ohlcv_asia&ticker={ticker}&market={market}&days={days}",
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        bars = data if isinstance(data, list) else data.get("bars", [])
        except Exception:
            pass

        # Fallback: direct akshare
        if not bars:
            try:
                import akshare as ak  # type: ignore
                start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
                end_date = datetime.now(timezone.utc).strftime("%Y%m%d")

                def _fetch_asia():
                    if market.lower() == "hk":
                        df = ak.stock_hk_hist(symbol=ticker, period="daily", start_date=start_date, end_date=end_date, adjust="")
                    else:
                        df = ak.stock_zh_a_hist(symbol=ticker, period="daily", start_date=start_date, end_date=end_date, adjust="")
                    return df.to_dict(orient="records")

                rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_asia)
                for row in rows:
                    bars.append({
                        "date": str(row.get("日期", row.get("date", ""))),
                        "open": row.get("开盘", row.get("open", None)),
                        "high": row.get("最高", row.get("high", None)),
                        "low": row.get("最低", row.get("low", None)),
                        "close": row.get("收盘", row.get("close", None)),
                        "volume": row.get("成交量", row.get("volume", None)),
                    })
            except ImportError:
                pass
            except Exception as exc:
                log.warning("asia.akshare.error", ticker=ticker, market=market, error=str(exc))

        result = {"ticker": ticker, "market": market, "bars": bars}
        await _r.set(cache_key, json.dumps(result), ex=1800)
        return result
    finally:
        await _r.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Reinforcement Learning — Q-learning trading signal
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/predictor/rl-signal/{ticker}")
async def get_rl_signal_endpoint(ticker: str, token: str = "", retrain: bool = False):
    check_token(token)
    import redis.asyncio as _aioredis
    import aiohttp as _aiohttp
    _r = await _aioredis.from_url(os.getenv("REDIS_URL", "redis://ot-redis:6379/0"), encoding="utf-8", decode_responses=True)
    try:
        ticker = ticker.upper()
        cache_key = f"rl:signal:{ticker}"

        if not retrain:
            cached = await _r.get(cache_key)
            if cached:
                return json.loads(cached)

        try:
            from predictor.rl_agent import get_rl_signal as _get_rl_signal  # type: ignore
            result = await _get_rl_signal(ticker, force_retrain=retrain)
        except Exception as exc:
            log.error("rl.signal.error", ticker=ticker, error=str(exc))
            raise HTTPException(status_code=503, detail=f"RL signal unavailable: {exc}")

        await _r.set(cache_key, json.dumps(result), ex=3600)
        return result
    finally:
        await _r.aclose()


@app.post("/api/predictor/rl-signal/{ticker}/train")
async def train_rl_signal(ticker: str, request: Request):
    body = await request.json()
    check_token(body.get("token", ""))
    ticker = ticker.upper()
    try:
        from predictor.rl_agent import train_from_factors  # type: ignore
        lookback_days = body.get("lookback_days", 180)
        await train_from_factors(ticker, lookback_days=lookback_days)
        trained_at = datetime.now(timezone.utc).isoformat()
        return {"status": "ok", "ticker": ticker, "trained_at": trained_at}
    except Exception as exc:
        log.error("rl.train.error", ticker=ticker, error=str(exc))
        raise HTTPException(status_code=503, detail=f"RL training failed: {exc}")



@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("/app/webui/static/index.html") as f:
        html = f.read()
    # Inject version meta tag and cache-bust static asset references
    html = html.replace(
        "<head>",
        f'<head>\n<meta name="ot-version" content="{APP_VERSION}">',
        1,
    )
    # Stamp static asset URLs with ?v=<version> so CDN/browser re-fetches on each release
    import re as _re
    html = _re.sub(r'(src|href)="/static/([^"?]+)"', lambda m: f'{m.group(1)}="/static/{m.group(2)}?v={APP_VERSION}"', html)
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
            "Surrogate-Control": "no-store",
            "CDN-Cache-Control": "no-store",
        },
    )
