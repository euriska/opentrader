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
-- option_positions chain_id (added 2026-04-26) — links rolled legs into a roll chain
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS chain_id UUID;
CREATE INDEX IF NOT EXISTS option_positions_chain_id ON option_positions (chain_id) WHERE chain_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ovtlyr_intel (
    id                 UUID NOT NULL DEFAULT gen_random_uuid(),
    ts                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker             TEXT NOT NULL,
    signal             TEXT,
    signal_active      BOOLEAN,
    signal_date        DATE,
    nine_score         INT,
    oscillator         TEXT,
    fear_greed         NUMERIC,
    last_close         NUMERIC,
    avg_vol_30d        BIGINT,
    channels           TEXT,
    capital_efficiency TEXT,
    raw                JSONB,
    PRIMARY KEY (ts, id)
);
ALTER TABLE ovtlyr_intel ADD COLUMN IF NOT EXISTS channels           TEXT;
ALTER TABLE ovtlyr_intel ADD COLUMN IF NOT EXISTS capital_efficiency TEXT;
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
    status       VARCHAR(20) NOT NULL DEFAULT 'purchased' CHECK (status IN ('reading','read','purchased','reference')),
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
-- Extended Greeks: rho (rate sensitivity) and volga/vomma (vega convexity)
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS rho   NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS volga NUMERIC;
-- Higher-order Greeks: vanna (∂Δ/∂σ), charm (∂Δ/∂t), pop (risk-neutral ITM probability)
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS vanna NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS charm NUMERIC;
ALTER TABLE option_positions ADD COLUMN IF NOT EXISTS pop   NUMERIC;

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

-- ── FIRE portfolio monthly value snapshots (SnapTrade positions total) ───────
-- One row per calendar month, captured on the 1st. Retention: 18 months (pruned
-- by the capturing endpoint, not by this schema).
CREATE TABLE IF NOT EXISTS fire_monthly_snapshots (
    id            SERIAL      PRIMARY KEY,
    snapshot_date DATE        NOT NULL,
    total_value   NUMERIC     NOT NULL,
    source        TEXT,
    user_id       TEXT        NOT NULL DEFAULT 'default',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_date, user_id)
);
CREATE INDEX IF NOT EXISTS fire_monthly_snapshots_date ON fire_monthly_snapshots (snapshot_date DESC);

-- ── FIRE portfolio daily value snapshots (SnapTrade positions total) ─────────
-- One row per calendar day, captured once daily. Retention: 90 days (pruned by
-- the capturing endpoint, not by this schema).
CREATE TABLE IF NOT EXISTS fire_daily_snapshots (
    id            SERIAL      PRIMARY KEY,
    snapshot_date DATE        NOT NULL,
    total_value   NUMERIC     NOT NULL,
    source        TEXT,
    user_id       TEXT        NOT NULL DEFAULT 'default',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_date, user_id)
);
CREATE INDEX IF NOT EXISTS fire_daily_snapshots_date ON fire_daily_snapshots (snapshot_date DESC);

-- ── FIRE transaction categories (user-assigned, keyed by broker transaction id) ─
-- Transactions themselves aren't persisted (fetched live from SnapTrade/P2P and
-- cached briefly in Redis); this table only stores the user's category tags,
-- joined back onto the live transaction list by id at request time.
CREATE TABLE IF NOT EXISTS fire_transaction_categories (
    transaction_id TEXT        NOT NULL,
    user_id        TEXT        NOT NULL DEFAULT 'default',
    category       TEXT        NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (transaction_id, user_id)
);

-- ── FIRE category vocabulary (add/rename/delete via Manage Categories) ───────
-- Auto-seeded with starter categories on first read; independent of whether any
-- transaction currently uses a given name. `parent` supports one level of
-- sub-categories (NULL = top-level).
CREATE TABLE IF NOT EXISTS fire_categories (
    name       TEXT        NOT NULL,
    user_id    TEXT        NOT NULL DEFAULT 'default',
    parent     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name, user_id)
);

-- ── FIRE auto-categorization rules (Transactions page Auto-Categorize panel) ─
-- Applied automatically whenever transactions are fetched: any uncategorized
-- transaction matching an enabled rule gets tagged with its category and the
-- assignment is persisted to fire_transaction_categories.
CREATE TABLE IF NOT EXISTS fire_category_rules (
    id         SERIAL      PRIMARY KEY,
    user_id    TEXT        NOT NULL DEFAULT 'default',
    field      TEXT        NOT NULL,               -- 'type' | 'symbol' | 'description'
    operator   TEXT        NOT NULL DEFAULT 'equals',  -- 'equals' | 'contains'
    value      TEXT        NOT NULL,
    category   TEXT        NOT NULL,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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

-- ── Portfolio Groups (custom grouped portfolios with allocation and strategy) ──
CREATE TABLE IF NOT EXISTS portfolio_groups (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name                TEXT        NOT NULL,
    type                TEXT        NOT NULL DEFAULT 'parent' CHECK (type IN ('parent','sub')),
    parent_id           UUID        REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    max_stocks          INTEGER     NOT NULL DEFAULT 25,
    alloc_mode          TEXT        NOT NULL DEFAULT 'equal' CHECK (alloc_mode IN ('equal','custom')),
    strategy_family_id  TEXT,       -- references strategies.json family_id
    strategy_name       TEXT,       -- denormalised display name
    color               TEXT        NOT NULL DEFAULT '#60a5fa',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pg_sub_limit CHECK (type = 'parent' OR parent_id IS NOT NULL),
    CONSTRAINT pg_max_check  CHECK (
        (type = 'parent' AND max_stocks <= 25) OR
        (type = 'sub'    AND max_stocks <= 10)
    )
);
CREATE INDEX IF NOT EXISTS pg_parent ON portfolio_groups (parent_id) WHERE parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS portfolio_group_holdings (
    id          UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id    UUID        NOT NULL REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    ticker      TEXT        NOT NULL,
    alloc_pct   NUMERIC     CHECK (alloc_pct > 0 AND alloc_pct <= 100),
    lot_size    INTEGER     NOT NULL DEFAULT 1 CHECK (lot_size > 0),
    sort_order  INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (group_id, ticker)
);
CREATE INDEX IF NOT EXISTS pgh_group ON portfolio_group_holdings (group_id);

CREATE TABLE IF NOT EXISTS portfolio_group_accounts (
    group_id        UUID    NOT NULL REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    account_label   TEXT    NOT NULL,
    broker          TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, account_label)
);

CREATE TABLE IF NOT EXISTS portfolio_group_rebalance_log (
    id          UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    group_id    UUID        REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    ticker      TEXT        NOT NULL,
    action      TEXT        NOT NULL CHECK (action IN ('buy','sell')),
    qty         NUMERIC     NOT NULL,
    price       NUMERIC,
    delta_usd   NUMERIC,
    request_id  TEXT,
    status      TEXT        NOT NULL DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS pgrl_group_ts ON portfolio_group_rebalance_log (group_id, ts DESC);

CREATE TABLE IF NOT EXISTS portfolio_group_dca_schedules (
    id              UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id        UUID        UNIQUE REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    amount_usd      NUMERIC     NOT NULL CHECK (amount_usd > 0),
    frequency       TEXT        NOT NULL DEFAULT 'weekly'
                                    CHECK (frequency IN ('daily','weekly','monthly')),
    day_of_week     INTEGER,    -- 0=Mon … 6=Sun (weekly only)
    day_of_month    INTEGER,    -- 1–28 (monthly only)
    hour_et         INTEGER     NOT NULL DEFAULT 10,
    minute_et       INTEGER     NOT NULL DEFAULT 0,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS portfolio_group_dca_log (
    id          UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schedule_id UUID        REFERENCES portfolio_group_dca_schedules(id) ON DELETE SET NULL,
    group_id    UUID        REFERENCES portfolio_groups(id) ON DELETE CASCADE,
    ticker      TEXT        NOT NULL,
    qty         NUMERIC     NOT NULL,
    price       NUMERIC,
    amount_usd  NUMERIC,
    request_id  TEXT,
    status      TEXT        NOT NULL DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS pgdl_group_ts ON portfolio_group_dca_log (group_id, ts DESC);

-- ── Shadow Account: Counterfactual P&L Runs ──────────────────────────────────
-- Each row is one analysis run. trades_detail JSONB holds the full scored list.
CREATE TABLE IF NOT EXISTS shadow_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_from       DATE        NOT NULL,
    date_to         DATE        NOT NULL,
    account_label   TEXT,
    trade_count     INT,
    actual_pnl      NUMERIC,
    ideal_pnl       NUMERIC,
    discipline_cost NUMERIC,
    categories      JSONB,      -- {noise_trade, early_exit, late_exit, overtrading, clean}
    rules           JSONB,      -- LLM-extracted + backtested rules
    top5            JSONB,      -- counterfactual top-5 trades
    trades_detail   JSONB       -- full scored trade list
);
CREATE INDEX IF NOT EXISTS shadow_runs_ts    ON shadow_runs (ts DESC);
CREATE INDEX IF NOT EXISTS shadow_runs_dates ON shadow_runs (date_from, date_to);

-- Equity position journal (notes + commission per account+ticker)
CREATE TABLE IF NOT EXISTS equity_journal (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id  TEXT NOT NULL,
  ticker      TEXT NOT NULL,
  notes       TEXT,
  trade_cost  NUMERIC(10,4),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (account_id, ticker)
);

-- Sector leader history (daily per-sector rankings + streak tracking)
CREATE TABLE IF NOT EXISTS sector_leader_history (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trade_date  DATE NOT NULL,
  sector      TEXT NOT NULL,
  ticker      TEXT NOT NULL,
  rank        INT NOT NULL,
  change_pct  NUMERIC(8,4),
  price       NUMERIC(10,4),
  volume      BIGINT,
  UNIQUE (trade_date, sector, ticker)
);
CREATE INDEX IF NOT EXISTS slh_date_sector ON sector_leader_history (trade_date DESC, sector);
CREATE INDEX IF NOT EXISTS slh_ticker_date ON sector_leader_history (ticker, trade_date DESC);

-- ── Feature 1: Intraday portfolio NAV snapshots ───────────────────────────────
-- High-frequency NAV captures (every 30 min during market hours).
-- Pruning job compresses: full res (24h) → 15-min (7d) → hourly (30d) → daily retained in portfolio_snapshots.
CREATE TABLE IF NOT EXISTS portfolio_intraday_snapshots (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_label TEXT        NOT NULL,
    broker        TEXT,
    mode          TEXT        NOT NULL DEFAULT 'live',
    total_nav     NUMERIC     NOT NULL,
    cash          NUMERIC,
    equity_value  NUMERIC,
    day_pnl       NUMERIC,
    bucket        TEXT        NOT NULL DEFAULT 'raw',  -- raw | 15min | hourly
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('portfolio_intraday_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS pis_account_ts ON portfolio_intraday_snapshots (account_label, ts DESC);
CREATE INDEX IF NOT EXISTS pis_bucket_ts  ON portfolio_intraday_snapshots (bucket, ts DESC);

-- ── Feature 3: ETF capital flow snapshots ────────────────────────────────────
-- Dollar-volume flow relative to 30-day average for key ETFs.
CREATE TABLE IF NOT EXISTS etf_flow_snapshots (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker        TEXT        NOT NULL,
    name          TEXT,
    category      TEXT,       -- equity | sector | bond | commodity | volatility
    price         NUMERIC,
    volume        BIGINT,
    dollar_volume NUMERIC,    -- price × volume
    avg_volume_30d BIGINT,
    flow_ratio    NUMERIC,    -- dollar_volume / 30d_avg_dollar_volume
    change_pct    NUMERIC,    -- daily % change
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('etf_flow_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS efs_ticker_ts ON etf_flow_snapshots (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS efs_category  ON etf_flow_snapshots (category, ts DESC);

-- ── Feature 4: Macro regime snapshots ────────────────────────────────────────
-- Aggregate macro signals into a single regime snapshot.
CREATE TABLE IF NOT EXISTS macro_regime_snapshots (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    regime          TEXT        NOT NULL,  -- risk_on | risk_off | neutral
    bull_signals    INT         NOT NULL DEFAULT 0,
    bear_signals    INT         NOT NULL DEFAULT 0,
    total_signals   INT         NOT NULL DEFAULT 0,
    regime_score    NUMERIC,    -- -1.0 (bear) to +1.0 (bull)
    spy_trend       TEXT,       -- above_200sma | below_200sma
    vix_level       NUMERIC,
    dxy_trend       TEXT,       -- rising | falling | neutral
    tlt_trend       TEXT,       -- rising | falling | neutral
    breadth_pct        NUMERIC,    -- from OVTLYR
    raw                JSONB,
    technical_regime   TEXT,       -- STRONG_OVERBOUGHT | OVERBOUGHT | NEUTRAL | OVERSOLD | STRONG_OVERSOLD
    technical_score    NUMERIC,    -- consensus oscillator vote sum
    oscillators        JSONB,      -- per-oscillator latest values
    PRIMARY KEY (ts, id)
);
-- Backfill columns if table pre-exists
ALTER TABLE macro_regime_snapshots ADD COLUMN IF NOT EXISTS technical_regime TEXT;
ALTER TABLE macro_regime_snapshots ADD COLUMN IF NOT EXISTS technical_score NUMERIC;
ALTER TABLE macro_regime_snapshots ADD COLUMN IF NOT EXISTS oscillators JSONB;
SELECT create_hypertable('macro_regime_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS mrs_ts ON macro_regime_snapshots (ts DESC);

-- ── Feature 5: News sentiment snapshots ──────────────────────────────────────
-- Categorized financial news from Alpha Vantage.
CREATE TABLE IF NOT EXISTS news_sentiment_snapshots (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category        TEXT        NOT NULL,  -- equities | macro | energy | technology | etc.
    ticker          TEXT,                  -- NULL for category-level entries
    title           TEXT,
    source          TEXT,
    url             TEXT,
    overall_score   NUMERIC,    -- -1 to +1
    relevance_score NUMERIC,    -- 0 to 1
    topics          JSONB,
    raw             JSONB,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('news_sentiment_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS nss_category_ts ON news_sentiment_snapshots (category, ts DESC);
CREATE INDEX IF NOT EXISTS nss_ticker_ts   ON news_sentiment_snapshots (ticker, ts DESC) WHERE ticker IS NOT NULL;

-- ── Feature 6: Per-symbol technical analysis snapshots ───────────────────────
-- BUY/HOLD/SELL composite with support/resistance levels.
CREATE TABLE IF NOT EXISTS stock_analysis_snapshots (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker          TEXT        NOT NULL,
    signal          TEXT        NOT NULL,  -- BUY | HOLD | SELL
    confidence      NUMERIC,    -- 0.0 to 1.0
    price           NUMERIC,
    rsi             NUMERIC,
    atr             NUMERIC,
    support         NUMERIC,
    resistance      NUMERIC,
    ma_50           NUMERIC,
    ma_200          NUMERIC,
    trend           TEXT,       -- uptrend | downtrend | sideways
    bullish_factors JSONB,
    bearish_factors JSONB,
    raw             JSONB,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('stock_analysis_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS sas_ticker_ts ON stock_analysis_snapshots (ticker, ts DESC);

-- ── Feature 8: Polymarket paper trading ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS polymarket_positions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    condition_id    TEXT        NOT NULL,
    token_id        TEXT        NOT NULL,
    market_slug     TEXT,
    market_question TEXT,
    outcome         TEXT,       -- YES | NO | outcome label
    side            TEXT        NOT NULL CHECK (side IN ('buy','sell')),
    qty             NUMERIC     NOT NULL,
    entry_price     NUMERIC     NOT NULL,  -- probability 0-1
    current_price   NUMERIC,
    status          TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','settled')),
    exit_price      NUMERIC,
    pnl             NUMERIC,
    settled_at      TIMESTAMPTZ,
    raw             JSONB
);
CREATE INDEX IF NOT EXISTS pp_condition   ON polymarket_positions (condition_id, ts DESC);
CREATE INDEX IF NOT EXISTS pp_status      ON polymarket_positions (status);

CREATE TABLE IF NOT EXISTS polymarket_trades (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    position_id     UUID        REFERENCES polymarket_positions(id),
    action          TEXT        NOT NULL,  -- open | close | settle
    qty             NUMERIC     NOT NULL,
    price           NUMERIC     NOT NULL,
    pnl             NUMERIC,
    PRIMARY KEY (ts, id)
);
SELECT create_hypertable('polymarket_trades', 'ts', if_not_exists => TRUE);

-- ── Greeks history snapshots (added for time-series analytics) ───────────────
-- One row per scan cycle per active position. Allows charting how Greeks evolve.
CREATE TABLE IF NOT EXISTS greeks_history (
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    position_id      UUID        NOT NULL REFERENCES option_positions(id) ON DELETE CASCADE,
    contract_symbol  TEXT        NOT NULL,
    underlying       TEXT        NOT NULL,
    underlying_price NUMERIC,
    contract_price   NUMERIC,
    delta            NUMERIC,
    gamma            NUMERIC,
    theta            NUMERIC,
    vega             NUMERIC,
    rho              NUMERIC,
    iv               NUMERIC,
    dte              INTEGER,
    PRIMARY KEY (ts, position_id)
);
SELECT create_hypertable('greeks_history', 'ts', if_not_exists => TRUE);
SELECT add_retention_policy('greeks_history', INTERVAL '6 months', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS greeks_history_position ON greeks_history (position_id, ts DESC);

-- ── Stock risk clustering (added for portfolio risk segmentation) ─────────────
-- One row per clustering run (metadata / audit trail).
CREATE TABLE IF NOT EXISTS stock_cluster_runs (
    id          UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    run_date    DATE        NOT NULL UNIQUE,
    n_tickers   INTEGER     NOT NULL,
    n_clusters  INTEGER     NOT NULL DEFAULT 4,
    features    TEXT[]      NOT NULL DEFAULT '{}',
    silhouette  NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per ticker per run — stores cluster assignment + raw feature values.
CREATE TABLE IF NOT EXISTS stock_risk_clusters (
    run_date        DATE    NOT NULL,
    ticker          TEXT    NOT NULL,
    cluster_id      INTEGER NOT NULL,
    risk_tier       TEXT    NOT NULL CHECK (risk_tier IN ('very_low','low','medium','high')),
    volatility      NUMERIC,
    price_change    NUMERIC,
    beta            NUMERIC,
    pe_ratio        NUMERIC,
    pb_ratio        NUMERIC,
    roe             NUMERIC,
    roa             NUMERIC,
    fcf_yield       NUMERIC,
    earnings_yield  NUMERIC,
    features        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_date, ticker)
);
CREATE INDEX IF NOT EXISTS src_ticker_date ON stock_risk_clusters (ticker, run_date DESC);
CREATE INDEX IF NOT EXISTS src_run_tier    ON stock_risk_clusters (run_date DESC, risk_tier);

-- investment_amount on portfolio_groups (added for sub-portfolio capital allocation)
ALTER TABLE portfolio_groups ADD COLUMN IF NOT EXISTS investment_amount NUMERIC;
