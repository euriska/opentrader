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
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    schedule   TEXT,
    minutes    INT,
    seconds    INT,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    notify     BOOLEAN NOT NULL DEFAULT TRUE,
    command    TEXT,
    payload    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
