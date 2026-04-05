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
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS library_books_author   ON library_books (author);
CREATE INDEX IF NOT EXISTS library_books_category ON library_books (category);
CREATE INDEX IF NOT EXISTS library_books_status   ON library_books (status);
