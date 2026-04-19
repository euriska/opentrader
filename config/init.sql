CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS trades (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id  TEXT NOT NULL,
    broker      TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('live','paper','sandbox')),
    ticker      TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('long','short')),
    qty         NUMERIC NOT NULL,
    entry_price NUMERIC,
    exit_price  NUMERIC,
    pnl         NUMERIC,
    signal_src  TEXT,
    strategy    TEXT,
    status      TEXT DEFAULT 'open'
);
SELECT create_hypertable('trades', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS signals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    confidence  NUMERIC,
    payload     JSONB
);
SELECT create_hypertable('signals', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS sentiment (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    score       NUMERIC,
    mention_count INT,
    payload     JSONB
);
SELECT create_hypertable('sentiment', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS review_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trade_count     INT NOT NULL,
    findings        TEXT,
    recommendations JSONB,
    applied         BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS heartbeats (
    service   TEXT PRIMARY KEY,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status    TEXT DEFAULT 'healthy'
);

CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    schedule              TEXT,
    minutes               INT,
    seconds               INT,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    notify                BOOLEAN NOT NULL DEFAULT TRUE,
    command               TEXT,
    payload               JSONB,
    intraday_start        TEXT,
    intraday_end          TEXT,
    intraday_interval_min INT,
    intraday_days         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Idempotent migration for existing deployments
ALTER TABLE scheduler_jobs ADD COLUMN IF NOT EXISTS intraday_start        TEXT;
ALTER TABLE scheduler_jobs ADD COLUMN IF NOT EXISTS intraday_end          TEXT;
ALTER TABLE scheduler_jobs ADD COLUMN IF NOT EXISTS intraday_interval_min INT;
ALTER TABLE scheduler_jobs ADD COLUMN IF NOT EXISTS intraday_days         TEXT;
-- option_positions delta column (added 2026-04-11)
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS delta NUMERIC;
-- option_positions expiry_locked flag (added 2026-04-11)
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS expiry_locked BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS ovtlyr_intel (
    id           UUID NOT NULL DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker       TEXT NOT NULL,
    signal       TEXT,
    signal_active BOOLEAN,
    signal_date  DATE,
    nine_score   INT,
    oscillator   TEXT,
    fear_greed   NUMERIC,
    last_close   NUMERIC,
    avg_vol_30d  BIGINT,
    raw          JSONB,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('ovtlyr_intel', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ovtlyr_intel_ticker_ts ON ovtlyr_intel (ticker, ts DESC);

CREATE TABLE IF NOT EXISTS ovtlyr_lists (
    id           UUID NOT NULL DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    list_type    TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    name         TEXT,
    sector       TEXT,
    signal       TEXT,
    signal_date  DATE,
    last_price   NUMERIC,
    avg_vol_30d  BIGINT,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('ovtlyr_lists', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ovtlyr_lists_type_ts ON ovtlyr_lists (list_type, ts DESC);
CREATE INDEX IF NOT EXISTS ovtlyr_lists_ticker ON ovtlyr_lists (ticker, ts DESC);

-- Market breadth snapshots — bull/bear ratio from OVTLYR lists (updated every 3 min during market hours)
CREATE TABLE IF NOT EXISTS ovtlyr_breadth (
    ts           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    bull_count   INT          NOT NULL,
    bear_count   INT          NOT NULL,
    total_count  INT          NOT NULL,
    breadth_pct  NUMERIC(5,2) NOT NULL,  -- bull / (bull + bear) * 100
    signal       TEXT,                   -- bullish_cross | bearish_cross | bullish | bearish
    raw          JSONB
);
SELECT create_hypertable('ovtlyr_breadth', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ovtlyr_breadth_ts ON ovtlyr_breadth (ts DESC);

-- Per-ticker Fear & Greed scores (one row per ticker per trading day)
-- Regular table (not hypertable) — small data, needs simple UNIQUE(ticker,date)
CREATE TABLE IF NOT EXISTS ticker_sentiment (
    id         UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date       DATE        NOT NULL,
    ticker     TEXT        NOT NULL,
    score      NUMERIC,        -- composite 0-100 (50=neutral, <50=fear, >50=greed)
    rsi        NUMERIC,        -- RSI-14 component (0-100)
    ma_score   NUMERIC,        -- price vs 20d/50d MA component (0-100)
    momentum   NUMERIC,        -- 10-day ROC component (0-100)
    vol_score  NUMERIC,        -- realised vol percentile, inverted (0-100)
    close      NUMERIC,        -- closing price used for calculation
    raw        JSONB,
    UNIQUE (ticker, date)
);
CREATE INDEX IF NOT EXISTS ticker_sentiment_ticker_date ON ticker_sentiment (ticker, date DESC);

-- Trading book library
CREATE TABLE IF NOT EXISTS library_books (
    id           UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    isbn         VARCHAR(20) UNIQUE,
    title        TEXT        NOT NULL,
    author       TEXT,
    description  TEXT,
    category     TEXT,
    publisher    TEXT,
    published_date TEXT,
    pages        INTEGER,
    cover_url    TEXT,
    price        NUMERIC(10,2),
    rating       SMALLINT    CHECK (rating BETWEEN 1 AND 5),
    status       VARCHAR(20) NOT NULL DEFAULT 'purchased' CHECK (status IN ('reading','purchased','reference')),
    review       TEXT,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS library_books_author   ON library_books (author);
CREATE INDEX IF NOT EXISTS library_books_category ON library_books (category);
CREATE INDEX IF NOT EXISTS library_books_status   ON library_books (status);

-- Library categories (managed list, persists even if no books use them)
CREATE TABLE IF NOT EXISTS library_categories (
    id         UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name       TEXT        NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Migrate any categories already stored in books
INSERT INTO library_categories (name)
    SELECT DISTINCT category FROM library_books WHERE category IS NOT NULL
    ON CONFLICT (name) DO NOTHING;

-- ── Options position tracker ──────────────────────────────────────────────────
-- Tracks open option contracts imported from broker accounts at EOD.
-- Retained for 18 months (managed by option_trade_log retention policy).
CREATE TABLE IF NOT EXISTS option_positions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Contract identity
    contract_symbol     TEXT        NOT NULL,   -- e.g. AAPL250418C00200000
    underlying          TEXT        NOT NULL,   -- e.g. AAPL
    option_type         TEXT        NOT NULL CHECK (option_type IN ('call','put','unknown')),
    strike              NUMERIC,                   -- NULL for non-OCC symbols (e.g. Webull)
    expiration_date     DATE,                      -- NULL for non-OCC symbols
    -- Account
    account_label       TEXT        NOT NULL,
    account_name        TEXT,                   -- human-friendly display name
    broker              TEXT        NOT NULL,
    mode                TEXT        NOT NULL DEFAULT 'live',
    -- Position details
    qty                 NUMERIC     NOT NULL,
    entry_price         NUMERIC,                -- option premium per share at entry
    underlying_entry    NUMERIC,                -- underlying stock price at entry (ATR anchor)
    entry_date          DATE        NOT NULL,
    -- ATR data (14-period daily ATR on the underlying)
    atr_14              NUMERIC,
    atr_calculated_at   TIMESTAMPTZ,
    -- ATR price levels (based on underlying_entry ± n * atr_14)
    level_emergency     NUMERIC,                -- underlying_entry - 3 * ATR  (Emergency Exit)
    level_exit_alert    NUMERIC,                -- underlying_entry - 2 * ATR  (Exit Alert)
    level_roll_1        NUMERIC,                -- underlying_entry + 0.5 * ATR (1st Roll)
    level_roll_2        NUMERIC,                -- underlying_entry + 1 * ATR  (2nd Roll)
    level_roll_3        NUMERIC,                -- underlying_entry + 2 * ATR  (3rd Roll)
    -- Additional dynamic roll levels stored as JSONB: [{label,price}, ...]
    extra_roll_levels   JSONB       DEFAULT '[]',
    -- Alert state (which levels have been triggered)
    alerts_fired        JSONB       DEFAULT '{}', -- {"emergency":false,"exit_alert":false,"roll_1":false,...}
    -- Earnings & expiration metadata
    next_earnings_date  DATE,
    -- Lifecycle
    status              TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','closed','rolled','expired')),
    closed_at           TIMESTAMPTZ,
    close_reason        TEXT,
    last_scan_at        TIMESTAMPTZ,
    -- Greeks (updated each scan from option chain)
    delta               NUMERIC,
    -- Raw broker snapshot
    raw                 JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS option_positions_contract_account
    ON option_positions (contract_symbol, account_label) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS option_positions_underlying
    ON option_positions (underlying, status);
CREATE INDEX IF NOT EXISTS option_positions_expiry
    ON option_positions (expiration_date) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS option_positions_account
    ON option_positions (account_label, status);

-- ── Option position event log ─────────────────────────────────────────────────
-- One row per scan/alert/close event. Retained 18 months via TimescaleDB retention.
CREATE TABLE IF NOT EXISTS option_trade_log (
    id               UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    position_id      UUID        NOT NULL,
    contract_symbol  TEXT        NOT NULL,
    underlying       TEXT        NOT NULL,
    -- Event
    event_type       TEXT        NOT NULL,
        -- scan | alert_emergency | alert_exit | alert_roll_1 | alert_roll_2
        -- | alert_roll_3 | alert_roll_extra | closed | imported
    -- Prices at event time
    underlying_price NUMERIC,
    contract_price   NUMERIC,
    atr_value        NUMERIC,
    -- Distance from levels at scan time
    distance_emergency  NUMERIC,   -- underlying_price - level_emergency
    distance_exit_alert NUMERIC,
    distance_roll_1     NUMERIC,
    -- Misc
    notes            TEXT,
    payload          JSONB,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('option_trade_log', 'ts', if_not_exists => TRUE);
-- 18-month retention
SELECT add_retention_policy('option_trade_log',
    INTERVAL '18 months', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS option_trade_log_position
    ON option_trade_log (position_id, ts DESC);
CREATE INDEX IF NOT EXISTS option_trade_log_underlying
    ON option_trade_log (underlying, ts DESC);

-- ── Options P&L columns (added 2026-04-14) ───────────────────────────────────
-- P&L-related fields on option_trade_log for trading log / history
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS qty          NUMERIC;   -- contracts at event time
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS entry_cost   NUMERIC;   -- premium paid/received at position open (per-share × qty × 100)
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS exit_cost    NUMERIC;   -- premium paid/received at close/roll event
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC;   -- exit_cost - entry_cost (positive = profit)
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS pnl_pct      NUMERIC;   -- realized_pnl / abs(entry_cost) × 100
ALTER TABLE option_trade_log ADD COLUMN IF NOT EXISTS risk_level   TEXT;      -- low | moderate | high | emergency
-- Post-close AI analysis on option_positions
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS total_realized_pnl NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS ai_analysis   TEXT;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS ai_analyzed_at TIMESTAMPTZ;

-- Options Greeks per position (added for portfolio-level aggregation)
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS theta NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS vega  NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS gamma NUMERIC;

-- ── Portfolio NAV snapshots (added for performance curve) ────────────────────
-- One row per account per calendar day at EOD (~16:10 ET).
-- Regular table — lookups by date range, no time-series compression needed.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id            UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    snapshot_date DATE        NOT NULL,
    account_label TEXT        NOT NULL,
    broker        TEXT,
    mode          TEXT        NOT NULL DEFAULT 'live',
    total_nav     NUMERIC     NOT NULL,   -- total market value + cash
    cash          NUMERIC,               -- cash / buying power
    equity_value  NUMERIC,               -- long market value
    day_pnl       NUMERIC,               -- unrealized + realized P&L for the day
    UNIQUE (snapshot_date, account_label)
);
CREATE INDEX IF NOT EXISTS portfolio_snapshots_date ON portfolio_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS portfolio_snapshots_account ON portfolio_snapshots (account_label, snapshot_date DESC);

-- ── Daily loss tracking (circuit breaker) ────────────────────────────────────
-- Persists intraday realized P&L per account for loss-limit enforcement.
-- Reset each morning at market open by the scheduler.
CREATE TABLE IF NOT EXISTS daily_loss_log (
    id            UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    log_date      DATE        NOT NULL DEFAULT CURRENT_DATE,
    account_label TEXT        NOT NULL,
    realized_pnl  NUMERIC     NOT NULL DEFAULT 0,
    trade_count   INT         NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (log_date, account_label)
);
