# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

## v4.0.91
- fix: broker configuration page fails to render when Redis is unavailable — wrap config:trade_mode Redis read in try/except and fall back to TRADE_MODE env var v4.0.91

## v4.0.90
- feat: margin metrics on equity dashboard — Alpaca get_balances() now exposes initial_margin, maintenance_margin, daytrading_buying_power, leverage_ratio, multiplier, pattern_day_trader; main.py computes maintenance_margin_pct, initial_margin_pct, leverage_ratio, day_trade_bp for all brokers (Tradier via current_requirement + pdt.day_trade_buying_power); equity dashboard account cards show a Margin Metrics strip with color-coded maintenance margin %, initial margin %, leverage ratio, day trade BP, multiplier chip (1×/2×/4×), PDT badge v4.0.90

## v4.0.89
- feat: Polymarket scraper — PolymarketScraperAgent fetches active prediction markets, filters by finance keywords, persists to polymarket_signals DB table; GET /api/sentiment/polymarket endpoint v4.0.89
- feat: M&A deals scraper — MADealsScraperAgent pulls SC TO-T/S-4/8-K filings from SEC EDGAR ATOM feed, extracts acquirer/target, persists to ma_deals table; GET /api/research/ma-deals endpoint v4.0.89
- feat: Universe scanner agent — ScannerAgent evaluates RSI-14/vol_ratio/momentum/price rules across configurable universe, publishes to scanner.alerts stream and Redis sorted set; GET /api/scanner/alerts + POST /api/scanner/run + rules management v4.0.89
- feat: DBnomics connector — free access to 100+ macro data providers (ECB, Eurostat, IMF, BIS); GET /api/market-data/dbnomics/search + GET /api/market-data/dbnomics/{provider}/{dataset}/{series} v4.0.89
- feat: AkShare connector — HK and China A-share OHLCV via akshare library; GET /api/market-data/asia/{ticker} v4.0.89
- feat: RL trading agent — tabular Q-learning agent (5×5×5 state grid, 5 actions) trains on OHLCV factor history; GET /api/predictor/rl-signal/{ticker} + POST /api/predictor/rl-signal/{ticker}/train v4.0.89
- feat: Geopolitics monitor — GPRI + GPRH from FRED, GDELT conflict event feed; GET /api/research/geopolitics + /events v4.0.89
- feat: Corporate relationship map — D3-style Canvas force graph in equity research; GET /api/market/relationships/{ticker} resolves SEC EDGAR CIK v4.0.89
- feat: Trade visualization — Plotly candlestick with entry/exit markers from completed_trades; GET /api/trades/viz/{ticker} v4.0.89
- feat: 8 new WebUI panels — Trade Viz, Geopolitics, Polymarket Signals, M&A Deals, Relationship Map, Scanner Alerts, Strategy Visualizer, Report Builder v4.0.89

## v4.0.88
- feat: paper trading engine — PaperTrader agent subscribes to predictor.signals, simulates fills, tracks positions in Redis, persists to paper_trades DB table, publishes to paper.fills stream v4.0.88
- feat: smart order engine — SmartOrderEngine slices large orders into TWAP/VWAP child orders with configurable slices and interval v4.0.88
- feat: factor discovery — compute_factor_ic() computes momentum_20, RSI-14, vol_ratio, BB%B, mean_rev_5, price_to_sma50 IC/IR/hit-rate against forward returns; /api/research/factors/{ticker} endpoint with 15-min cache v4.0.88
- feat: webhook signal intake — POST /webhook/signal accepts TradingView / external signals, validates HMAC secret, publishes to predictor.signals stream v4.0.88
- feat: full Black-Scholes Greeks — GET /api/options/greeks returns delta/gamma/theta/vega/rho + intrinsic/time-value; solves IV via Newton-Raphson when market_price provided v4.0.88
- feat: 3D IV surface chart — Plotly interactive surface (calls + puts) rendered from /api/options/volatility-surface data, with toggle and dark-theme styling in Options tab v4.0.88

## v4.0.87
- fix: XSS in news feed article cards — escape title/source/ticker via _escHtml, validate url scheme (reject javascript:), move ticker fallback text to data-fb attribute to eliminate inline JS string injection v4.0.87
- fix: daily report duplicate sends — add per-day idempotency guard (report:daily:sent:{date} Redis key, 24h TTL); manual trigger bypasses guard via force=True; scheduler silently skips if already sent v4.0.87
- fix: scheduler cron overrides lost on restart — _publish_jobs now preserves cron_hour/cron_minute/cron_days; _reload_loop gains cron trigger path; save_reports_config publishes scheduler:reload so live reschedule takes effect immediately v4.0.87
- fix: import logging in risk_clustering looked dead after structlog migration — added comment clarifying it backs logging.basicConfig() in main() v4.0.87

## v4.0.86
- feat: daily report watchlist alerts — add tickers with per-ticker earnings/ex-div checkboxes and configurable "alert N days before" window; highlighted section appears at top of report when events fall within the window

## v4.0.85 - 2026-06-08
- fix: session cookie extended to 7 days (was 24h); 401 responses redirect to /login instead of showing error in container

## [4.0.84] - 2026-06-06

### Fixed
- **Equity Trades — Webull existing holdings now imported on first run**: broker gateway emits fill events for all current Webull positions on initial startup (guarded by a Redis flag so it only fires once per account, not on every restart); subsequent new purchases/sales are detected via position-qty diff

## [4.0.83] - 2026-06-06

### Fixed
- **Equity Trades — Webull completed trades missing**: broker gateway now uses position-change detection as fallback when Webull order-history API returns 404; buy/sell fills inferred from position qty changes between 60-second polls, written to `orders.events` with `source: position_poll`
- **Portfolio allocation bar**: only the shortage/overage portion is highlighted yellow; the normal-colour section shows the actual (shortage) or base (overage) fill; yellow section displays deviation `%` and `$` value
- **Boot startup**: removed invalid `--ignore-pull-failures` flag from systemd `ExecStartPre`; fixed `ot-mcp-yahoo` crash loop (`FastMCP.run()` host/port kwargs moved to `.settings`)
- **Completed trades direction/price**: API now reads `side` as fallback for `direction`, and `fill_price` as fallback for `price`, for backward compat with older stream entries

## [4.0.82] - 2026-05-31

### Changed
- **Portfolio row — target vs actual with shortage/overage**: segments now sized by DEFINED target allocation (from investment_amount split or equal-slot fallback); actual live % computed from market values; segments ≥3% off-target shown in yellow with ▲/▼ delta indicator (e.g. "HOOW 40% ▲+29%"); on-target segments stay group color; hover tooltip shows full detail (target%, actual%, shortage/overage label)

## [4.0.81] - 2026-05-31

### Changed
- **Portfolio row in account heatmap**: now shows one segment per ticker (parent direct + sub holdings) instead of one block per group; each segment sized by actual market value in the account and labeled with ticker + current % of total portfolio allocation; segments ≥8% show "TICKER X%", ≥4% show ticker only, narrower segments show nothing (tooltip always available on hover)

## [4.0.80] - 2026-05-31

### Added
- **Portfolio Group modal — distribution editor**: sub-portfolio "Investment $" field replaced with "% of parent" input; setting the % auto-computes the dollar amount from the parent total; live allocation bar shows direct-holdings % vs each sub % updating in real time; overallocation warning shown when sub totals exceed parent

## [4.0.79] - 2026-05-31

### Fixed
- **Portfolio row in account heatmap**: parent group now renders a block for its direct holdings AND a separate tinted block for each sub-portfolio (sized by actual market values held in this account); sub blocks use the sub's color with a left border

## [4.0.78] - 2026-05-31

### Changed
- **Portfolio Groups broker bar**: parent bar now shows direct holdings and sub-portfolio(s) as proportional blocks in a single combined bar; sub block uses sub's color with left border and abbreviated name; sizing uses investment_amount if set, otherwise equal slots (1 per direct holding, 1 per sub); sub breakdown bar shown beneath with indented border

## [4.0.77] - 2026-05-31

### Changed
- **Portfolio Groups broker bar**: parent group now shows its direct holdings bar + all sub-portfolio bars nested beneath (indented with left border in sub color); sub-portfolio label shown above each sub bar; total stock count includes sub holdings

## [4.0.76] - 2026-05-31

### Changed
- Rename "Portfolio Allocation" row label to "Portfolio"

## [4.0.75] - 2026-05-31

### Changed
- **Equity Dashboard account heatmap**: Groups row renamed to "Portfolio Allocation"; left/right scroll buttons added matching Tickers and Sectors rows

## [4.0.74] - 2026-05-31

### Changed
- **Equity Dashboard**: removed standalone Portfolio Groups card grid from the Portfolio Risk section at the top; groups are now visible only in the Groups row within each account card heatmap

## [4.0.73] - 2026-05-31

### Changed
- **Groups row in equity dashboard account heatmap**: changed from clickable pills to a proportional horizontal bar matching the Tickers and Sectors rows; each segment is sized by the sum of market values for that group's holdings held in the account; clicking a segment still opens the Rebalance modal

## [4.0.72] - 2026-05-31

### Fixed
- **Webull Margin Account card showing no positions**: localStorage collapse state introduced in v4.0.71 was persistently hiding the card body after a single accidental ▲ click — every page reload restored the collapsed state; removed localStorage persistence so cards always reset to expanded on navigation (same behaviour as before v4.0.71); also clears any stale collapse keys on load

## [4.0.71] - 2026-05-31

### Fixed
- **Portfolio group Groups row never appearing below Sectors**: `loadUnifiedPositions` referenced an undeclared `token` variable when fetching portfolio groups — the ReferenceError was caught silently, leaving `portfolioGroups=[]` always; fixed by using `apiFetch('/api/portfolio-groups')` directly (auto-injects token)
- **Account card positions hidden after toggle**: collapse state from ▲/▼ button now persisted in `localStorage` per account key; restored on every re-render so cards do not unexpectedly show empty

## [4.0.70] - 2026-05-31

### Fixed
- **Portfolio group cards each show only their own holdings**: parent cards show only parent direct holdings in their bar; sub-portfolio cards show only their own holdings; both appear as separate cards in the grid — sub-portfolio hierarchy detail remains in the Portfolio Groups management page

## [4.0.69] - 2026-05-31

### Fixed
- **Portfolio group sub-portfolios no longer appear as separate sibling cards**: standalone Portfolio Groups section and per-broker bar now only render parent groups at the top level; sub-portfolios are nested inside their parent card with a labeled indented allocation bar and ticker chips; account heatmap Groups row also filters to parent groups only

## [4.0.68] - 2026-05-31

### Fixed
- **Webull account card blank on equity dashboard**: portfolio group filter and groups-row chip rendering in account heatmap now wrapped in try-catch so any JS error degrades gracefully instead of breaking the whole card; group names HTML-escaped in title attributes

## [4.0.67] - 2026-05-31

### Added
- **Portfolio groups on equity dashboard account cards**: when a portfolio group (or sub-portfolio) has a trading account assigned, it now appears as a colour-coded chip in a "Groups" row below the Sectors row in that account's heatmap section; clicking a chip opens the Rebalance modal for that group

## [4.0.66] - 2026-05-31

### Fixed
- **Portfolio group rebalance/cost-basis/DCA ignore sub-portfolios**: `_pg_load_full(pool, group_id)` only fetches the requested row — sub-portfolios were never attached when acting on a parent group; added `_pg_load_with_subs()` helper that fetches the parent then attaches all sub-group dicts; added `_pg_flatten_holdings()` that merges parent + sub holdings with investment-amount weighting (sub holdings weighted by `sub.investment_amount / parent.investment_amount`); all three action endpoints (cost-basis, rebalance-preview/execute, DCA run) now use these helpers so parent rebalancing correctly includes sub-portfolio tickers; per-holding account routing so sub holdings go to sub-assigned accounts

## [4.0.65] - 2026-05-31

### Fixed
- **Sidebar version display**: was stuck on hardcoded "v2" fallback; now also updated from `data.app_version` in the dashboard API callback, so it always reflects the live server version regardless of meta-tag injection timing

## [4.0.64] - 2026-05-31

### Added
- **Portfolio Group Cost Basis**: `GET /api/portfolio-groups/{id}/cost-basis` — per-ticker broker-reported cost basis, market value, unrealized P&L and P&L% aggregated across all accounts assigned to the group; totals row; "Cost Basis" button on every group card opens a table modal
- **Portfolio Group Rebalancing**: `GET /api/portfolio-groups/{id}/rebalance-preview` — drift analysis comparing target allocation% vs actual broker market values; shows action (buy/sell/hold), Δ qty, Δ USD per ticker; `POST /api/portfolio-groups/{id}/rebalance` places market orders for each drifted holding (>1% drift, qty≥1) via broker gateway; logged to `portfolio_group_rebalance_log`; "Rebalance" button on group card opens preview modal with colour-coded rows and Execute button
- **Portfolio Group DCA**: full schedule CRUD (`GET/POST/DELETE /api/portfolio-groups/{id}/dca`); manual trigger (`POST /api/portfolio-groups/{id}/dca/run`) buys proportional slices of investment amount across holdings; `POST /api/portfolio-groups/dca/run-all` runs all active due schedules (called by scheduler); "DCA" button opens form modal with frequency (daily/weekly/monthly), day, time, active toggle, Run Now, Save, Delete; logged to `portfolio_group_dca_log`; DB: `portfolio_group_dca_schedules`, `portfolio_group_dca_log`
- **DCA scheduler job**: `job_dca_run` fires daily at 09:45 ET on trading days; calls `/api/portfolio-groups/dca/run-all` via internal HTTP (same pattern as `job_eod_nav_snapshot`)
- **DB schema**: `portfolio_group_rebalance_log`, `portfolio_group_dca_schedules`, `portfolio_group_dca_log` tables

## [4.0.63] - 2026-05-31

### Added
- **Technical Regime Classifier** (`shared/technical_regime.py`): 5-class overbought/oversold regime from consensus oscillator voting — RSI-14, Stochastic %K, Williams %R, Stochastic RSI, MFI-14, Ultimate Oscillator; classes: STRONG_OVERBOUGHT → STRONG_OVERSOLD; pure pandas/numpy, no new deps
- **Macro regime now feeds the predictor**: `predictor/main.py` loads `macro_regime:latest` on each run; `technical_regime` extracted and passed to scorer; confidence adjustment ±10% (strong) / ±5% (moderate) for aligned/opposing market regime; both `technical_regime` and `macro_regime` published in signal metadata
- **2D Regime Gate in equity_trader** (`shared/regime_gate.py`): reads macro+technical regime at trade-time; 2D matrix produces position size multiplier (0.50–1.25) or hard SKIP based on macro (risk_on/neutral/risk_off) × technical (5-class) × direction; staleness guard (48h TTL); skip logged with reason; multiplier applied to `max_pos_usd` before sizing
- **CNN Regime Classifier** (`predictor/regime_cnn.py`): optional 4th MLEnsemble signal; 6-layer 1D CNN trained on SPY daily OHLCV → 5-class regime probabilities; 14 features (MACD hist, BB%, ADX-14, ATR%, OBV, RSI, Williams %R, Stoch %K, MFI, SMA%, momentum); class-weighted CrossEntropyLoss (no SMOTE/leakage); daily model cache at /tmp; enabled via `REGIME_CNN_ENABLED=true`; blended into `ml_confidence` at 85%/15% via `regime_to_confidence_adjustment()`
- **DB schema update**: `macro_regime_snapshots` gains `technical_regime TEXT`, `technical_score NUMERIC`, `oscillators JSONB` columns

## [4.0.55] - 2026-05-31

### Added
- **Portfolio Groups**: full CRUD system for grouping stocks into custom portfolios — parent portfolios (max 25 stocks, max 3 subs) and sub-portfolios (max 10 stocks each); equal or custom % allocation per holding; lot size per ticker; optional strategy assignment from strategies.json; multi-broker/account assignment; rename at any time; color-coded for visual identification
- **Portfolio Groups page**: new nav item under Equity with full management UI — group cards showing allocation bars, ticker chips, strategy and account badges; create/edit modal with holdings editor, allocation mode toggle, strategy picker, account checkboxes, and real-time allocation total validation
- **Horizontal allocation bar on broker risk cards**: each broker card on the Equity Dashboard now shows a "Portfolio Groups" section below the sector donut — one labeled segmented bar per assigned group, each segment colored by ticker with label and allocation %; strategy name shown as sub-label; parent and sub groups displayed separately
- **9 new API endpoints**: `GET/POST /api/portfolio-groups`, `PATCH/DELETE /api/portfolio-groups/{id}`, `PUT /api/portfolio-groups/{id}/holdings`, `POST /api/portfolio-groups/{id}/accounts`, `GET /api/portfolio-groups/strategies`; DB: `portfolio_groups`, `portfolio_group_holdings`, `portfolio_group_accounts` tables

## [4.0.54] - 2026-05-31

### Added
- **Stock risk clustering**: new `ot-risk-clustering` agent runs K-Means (k=4) weekly on the active ticker universe; 9 features: 13-week volatility + price change, beta, P/E, P/B, ROE, ROA, FCF yield, earnings yield (from FMP); assigns very_low/low/medium/high tiers by cluster volatility rank; results in `stock_risk_clusters` TimescaleDB table; scheduler job every Monday 7:30 AM ET; new Agents page panel with tier cards, ticker grid, and feature table

## [4.0.53] - 2026-05-31

### Added
- **Spread Greeks aggregation**: `POST /api/options/spread-greeks` computes net delta/gamma/theta/vega/rho for any multi-leg spread; Greeks sourced from chain data (passed by frontend) with DB fallback by contract symbol; returns net premium (debit/credit), dollar delta, and break-even estimates for 2-leg verticals; live Spread Greeks bar appears in Options Trader below the order legs bar as soon as a leg is selected, updating on every add/remove

## [4.0.52] - 2026-05-30

### Added
- **Greeks history tracking**: new `greeks_history` TimescaleDB hypertable (6-month retention); options_monitor writes delta/gamma/theta/vega/rho/iv/dte on every scan cycle; `GET /api/options/positions/{id}/greeks-history` endpoint; "Greeks History" tab in position modal with summary cards, sparklines, and data table
- **Global live/paper trading mode toggle**: `GET|POST /api/system/trading-mode`; broker gateway injects `mode=sandbox` on all `place_*` commands when `paper_only` is active; toggle button + badge on System page with confirmation modal
- **Request correlation IDs**: `X-Request-ID` middleware stamps every HTTP request (reads header or generates UUID4), binds to structlog context, echoes in response headers for end-to-end log tracing

## [4.0.51] - 2026-05-30

### Added
- **Orchestrator email monitor**: polls `ot-orchestrator@agentmail.to` every 5 minutes for new messages; runs each through the LLM to extract content type, key tickers with directional bias, actionable insights, and risks; delivers a structured research brief via email and Telegram; deduplicates via Redis so no message is analyzed twice; interval configurable via `EMAIL_MONITOR_INTERVAL_SEC`

## [4.0.50] - 2026-05-30

### Fixed
- **Daily report Webull fills**: EOD report `_get_all_broker_fills()` now accepts `FULL_FILL` as a valid filled status (Webull's terminal fill status); previously only `FILLED`/`PARTIALLY_FILLED`/`PARTIAL_FILLED` were accepted so all Webull fills were silently dropped from the report; also added `filled_at` to the date field list to match the broker gateway's detection logic

## [4.0.49] - 2026-05-29

### Fixed
- **Broker gateway order poll loop**: added Webull-specific open statuses (`working`, `partial_filled`) to the tracking set so Webull orders are correctly detected as open and their transitions to filled are emitted; added today-fill detection path to surface fills from manually-placed trades or fills that occurred before gateway startup (deduped per order_id); emit both `account_id` and `account_label` fields so the Equity Trades dashboard correctly resolves account names
- **Webull `get_orders`**: falls back to v2 OpenAPI endpoint (`/openapi/trade/order/list`) when v1 returns 404; both paths return 404 on the current Webull API subscription (order history requires a higher-tier plan) — platform-initiated trades still flow through the equity trader → gateway → orders.events pipeline regardless
- **`/api/trades` endpoint**: reads `account_id` with fallback to `account_label` so poll-loop-emitted fills (which use `account_label`) show the correct account in the dashboard

## [4.0.48] - 2026-05-29

### Fixed
- **FMP connector**: migrated all endpoints from legacy `/api/v3/` to the new `/stable/` base URL (v3 endpoints now return "Legacy Endpoint" error for accounts created after August 2025); updated response field names (`marketCap` vs `mktCap`, flat array for OHLCV history, `ratings-snapshot` for analyst data); removed `news` and `earnings` from capabilities (restricted/empty on free tier)
- **FMP test endpoint** (`/api/config/test/fmp`): updated from `/api/v3/profile/AAPL` to `/stable/profile?symbol=AAPL`; fixed `mktCap` → `marketCap` field name in success response
- **data_providers.toml**: removed `fmp` from `earnings` and `news` chains (connector no longer declares those capabilities on the free plan)

## [4.0.47] - 2026-05-29

### Fixed / Added
- **Platform Dashboard — Service Connectors diagram**: FMP (and any future connector without an API key) now always renders in the bezier diagram as a grey "off" node instead of disappearing once gateway health loads; unconfigured connectors use a `{configured:false}` placeholder so the complete provider list is always visible
- **Agents Dashboard — Market Data Connectors panel**: new card showing all connectors (including FMP) with status dot, state badge (ok/warn/down/off), and capability count; loads automatically when the Agents page opens and has its own Refresh button; also syncs `_connectorHealth` so the platform dashboard bezier stays current

## [4.0.46] - 2026-05-29

### Added
- **FMP (Financial Modeling Prep) connector**: new `HTTPConnector` at `market_data/connectors/fmp.py` serving `quote`, `bars_daily`, `fundamentals`, `analyst_consensus`, `earnings`, `dividends`, and `news` from `financialmodelingprep.com/api/v3`; free-tier friendly (5 req/min rate limiter); activates automatically when `FMP_API_KEY` is set
- **FMP in data provider routing**: added to `quote`, `bars_daily`, `fundamentals`, `analyst_consensus`, `earnings`, `dividends`, and `news` chains in `config/data_providers.toml` as a mid-priority fallback
- **FMP service connector UI**: `FMP_API_KEY` in the env-var registry, test-connector endpoint (`/api/config/test-connector` with `service=fmp`), FMP card in the Service Connectors settings panel, and FMP node in the Market Data Gateway bezier diagram

## [4.0.45] - 2026-05-28

### Fixed
- **EOD report — missing live trades**: broker fills now check all date fields (`transaction_date`, `create_date`, `filledTime`, etc.) instead of short-circuiting on the first non-empty one; trades placed yesterday but filled today are now captured
- **EOD report — live vs paper separation**: trades and fills are now grouped into LIVE ACCOUNT and PAPER/SANDBOX sections in both template and LLM-generated reports; stats include `live_filled`/`paper_filled` and `live_trades`/`paper_trades` counts
- **1pm daily report — live vs paper separation**: both the Options Positions and Stock Positions tables now show a "Live Accounts" header row followed by a "Paper / Sandbox" header row, with accounts sorted live-first
- **1pm daily report — OVTLYR signal in stock positions**: stock positions table now includes a Signal column (▲ BUY / ▼ SELL with confidence %) sourced from OVTLYR via Redis position intel, screener cache, and DB fallback — same enrichment logic as the options positions table

## [4.0.44] - 2026-05-27

### Fixed
- **Options Trader account dropdown**: was using `/api/broker/positions` (live gateway query — only returns accounts that responded) which caused accounts to be missing if any broker was slow or offline; switched to `/api/broker/connections` (reads `accounts.toml` directly) so all configured enabled accounts always appear in the selector

## [4.0.43] - 2026-05-27

### Fixed
- **Options chain expirations**: all three sources (Tradier broker connector, Alpaca connector, webui Tradier fallback) were hard-capped at 8 expirations; now filter to ~18 months (~548 days) with a max of 60 expirations; concurrent chain fetches use a 15-slot semaphore to avoid rate-limiting

## [4.0.42] - 2026-05-27

### Changed
- **Options Chain — single-leg strategies**: clicking a new strike when one is already selected (Long Call, Long Put, Covered Call, CSP) now automatically replaces the previous selection instead of stacking
- **Options Chain — double-click to clear**: double-clicking any selected row clears that leg (guided mode resets to step 1; normal mode removes the specific leg)

## [4.0.41] - 2026-05-27

### Changed
- **Options Chain ITM highlighting**: raised green (calls) opacity from 0.08→0.22 and red (puts) from 0.08→0.20 for clearer in-the-money visibility
- **Options Chain selected-leg row**: entire row now highlights in blue (`rgba(96,165,250,.14)` + `1.5px inset border`) regardless of buy/sell side; strike cell also goes blue; previously the row-level highlight was broken (wrong key format) and was never shown — fixed

## [4.0.40] - 2026-05-27

### Changed
- **Options Trader chain header**: ticker now 17px bold, price 15px bold — both full-brightness `var(--fg)` instead of muted
- **Options Trader chain header**: after chain loads, fetches OVTLYR signal and renders: ↑/↓ trend arrow (green/red), % change vs OVTLYR last close, and BUY/SELL signal badge — non-blocking, silent-fail when no OVTLYR data exists for the ticker

## [4.0.39] - 2026-05-27

### Changed
- **Options Trader Strategy Selector**: strategies now sorted Credit-first then Debit, with full-width group headers ("Credit Strategies — Collect Premium Upfront" / "Debit Strategies — Pay Premium Upfront"); removed redundant per-card CREDIT/DEBIT badge (group header provides the context)

## [4.0.38] - 2026-05-27

### Added
- **Options Trader: Strategy Selector Dashboard** — new full-width panel at the top of the Options Trader page with 14 visual strategy cards (Long Call, Long Put, Covered Call, CSP, Bull/Bear Call/Put Spreads, PMCC, Iron Condor, Iron Butterfly, Straddle, Strangle, Calendar Spread). Each card shows an SVG payoff diagram, Credit/Debit badge, and multi-leg count badge. Collapsible via ▲/▼ toggle.
- **Guided Leg Selection** — selecting a strategy enters guided mode: a step bar above the options chain shows the current leg (side, type, hint). Clicking a chain row auto-assigns the prescribed side (BUY/SELL) and advances to the next leg. Wrong option type clicks flash a ✕ warning inline. Removing a chip from the order bar cascades back to that step. Success toast when all legs filled.

## [4.0.36] - 2026-05-27

### Changed
- **UI-wide refresh buttons**: standardized all ↺ refresh buttons to `btn btn-ghost btn-xs` with `color:var(--fg)` and `title="Refresh"` — Signal Timeline, Traffic Stats, Platform Insights CI, Active Directives, ETF Capital Flows (new button), EODHD News, and Options Dashboard panels (YTD Performance, Portfolio Greeks, Unusual Options Flow, Trading Log YTD Performance) all updated; Options Dashboard text-link "Refresh" spans converted to icon buttons

## [4.0.35] - 2026-05-26

### Changed
- **Trading Dashboard**: refresh buttons brightened (color:var(--fg) instead of muted); Macro Regime ↺ moved to far right after "Full View →"

## [4.0.34] - 2026-05-26

### Changed
- **Trading Dashboard**: added ↺ icon refresh buttons to Market Breadth, Portfolio NAV, and Daily P&L card headers; replaced text-based "Refresh" link on Daily P&L with the icon button

## [4.0.33] - 2026-05-26

### Fixed
- **Trading Dashboard SPY/QQQ cards**: `ovtlyr:position_intel` expires after 2h and `sentiment:latest` is empty after-hours — both were the only sources. Added fallbacks: signal falls back to `scanner:ovtlyr:latest` (24h TTL); price falls back to last 2 daily bars from the market data gateway

## [4.0.32] - 2026-05-26

### Fixed
- **Trading Dashboard**: Live banner auto-hide now works correctly — added `_liveBannerStarted` flag to block WS re-renders from resetting `opacity:1` while the 5-second countdown is active
- **Landing page**: browser refresh now opens the Trading Dashboard instead of the Platform Dashboard

## [4.0.31] - 2026-05-26

### Fixed
- **Trading Dashboard**: Live banner auto-hide now survives WebSocket re-renders — added session dismissed flag so WS updates (which call `_applyTradeModeBanner` on every push) no longer reset the opacity/display; flag resets if mode is actually toggled

## [4.0.30] - 2026-05-26

### Changed
- **Trading Dashboard**: Live Trading banner ("Real money at risk") auto-fades out after 5 seconds with a 0.4s opacity transition; sandbox banner remains permanently visible

## [4.0.29] - 2026-05-26

### Fixed
- **Trading Dashboard / Market Breadth**: added padding between the percentage number and the gauge arc (changed margin-top from -8px to 4px)

## [4.0.28] - 2026-05-26

### Fixed
- **Equity Dashboard**: Sectors row now has left/right scroll arrows matching the Tickers row; scroll functions made generic so both rows share the same implementation

## [4.0.27] - 2026-05-26

### Added
- **OVTLYR scraper**: captures `channels` (Pass/Fail) and `capital_efficiency` from the ticker dashboard; stored in Redis position intel, `scanner:ovtlyr:latest`, and persisted to `ovtlyr_intel` DB table (new columns added via live migration)

## [4.0.26] - 2026-05-26

### Fixed
- **OVTLYR signal semantics**: `scrape_ticker()` now captures both fields separately — `signal` (Current Signal, entry/transition trigger) and `active_signal` (Current Active Signal, exit trigger for open positions). The 1pm report uses `active_signal` so it correctly surfaces sell signals on open positions. The scanner/predictor enrichment path continues using `signal` for new entry decisions.

## [4.0.25] - 2026-05-26

### Fixed
- **OVTLYR signal scraper**: `scrape_ticker()` now checks "Current Active Signal" before "Current Signal" — previously the regex matched the generic "Current Signal" field first, causing stale/inactive buy signals to override the active sell signal in the 1pm report

## [4.0.24] - 2026-05-26

### Added
- **Multi-leg options execution** (5 phases):
  - `SpreadLeg` + `SpreadOrderPayload` data models in `shared/envelope.py`
  - Tradier native `multileg` order class (`place_multileg_order`); Alpaca + Webull sequential-leg fallback
  - Broker gateway `place_spread_order` command + `get_option_contract` + `place_option_order` commands
  - DB schema: `spread_group_id`, `spread_role`, `spread_type`, `spread_meta` columns on `option_positions`
  - Screener API endpoints: `/api/options/screener/bull-call-spread` and `/api/options/screener/pmcc`
  - Spread order API: `/api/options/spreads/place`
  - Options trader confidence-based spread routing (0.55–0.85 confidence + `signal_type=spread`)
  - Options Dashboard: **Spread Screener** collapsible panel + **New Spread** button + spread order modal
    with leg preview, net debit/max-loss/max-gain summary, and one-click "Open in modal" from screener

## [4.0.23] - 2026-05-25

### Changed
- **Service Connectors diagram**: reduced SVG width from 90% → 81% (another 10% reduction)

## [4.0.22] - 2026-05-25

### Added
- **EODHD gateway connector** (`market_data/connectors/eodhd.py`): capabilities news, fundamentals, dividends, earnings, insider_transactions, breadth; added as fallback in routing table for each
- **EODData gateway connector** (`market_data/connectors/eoddata.py`): breadth indicator capability (MMFI, MMTH, HIGN, LOWN via INDEX quotes); wired as primary breadth provider (previously `[breadth]` had no providers)
- Both appear in the Service Connectors diagram once the market-data container is rebuilt

## [4.0.21] - 2026-05-25

### Fixed
- **Service Connectors state display**: provider chips now show a right-aligned state badge ("ok" / "warn" / "down" / "off") with the border color matching the state (bright green/amber/red instead of near-invisible dark shades); `renderPlatformDashboard` retries health load on each WS tick until `_connectorHealth` is populated
- **Service Connectors size**: diagram scaled to 90% width (centred) per user request

## [4.0.20] - 2026-05-25

### Changed
- **Service Connectors diagram**: replaced bus+stems layout with smooth cubic bezier curves; active lines teal, inactive lines muted dark-blue; chips render on top of curves; CONSUMERS / PROVIDERS column headers added; gateway box vertically centred on the full span

## [4.0.19] - 2026-05-25

### Fixed
- **CDN/browser cache bypass**: `serve_ui()` now appends `?v=<version>` to all `/static/` asset URLs and adds `Surrogate-Control: no-store` + `CDN-Cache-Control: no-store` headers so Cloudflare does not serve stale HTML after a release

## [4.0.18] - 2026-05-25

### Fixed
- **Service Connectors — grey providers and unexplained dots**: Redesigned provider chips to place the status dot *inside* the chip (left-aligned) instead of as a separate orphan circle; removed external circles entirely; provider border now uses subtle tinted backgrounds (dark green/amber/red) rather than bright strokes to avoid false alarm visuals; added a bottom legend explaining dot colours (healthy / degraded / dead / not configured); agent consumer chips now also show an internal dot matching their health colour; data-type count badge shows number of capabilities on each provider chip (hover for full list)

## [4.0.17] - 2026-05-25

### Added
- **Platform Dashboard — Service Connectors panel**: SVG flow diagram showing data-flow arrows between consumers, the Market Data Gateway, and providers
  - Left column: 7 agent consumer nodes (aggregator, predictor, traders, options-monitor, ETF flows, macro regime) with health-colored borders
  - Center: Market Data Gateway node with health border
  - Right column: connector provider nodes (Massive, TradingView, Yahoo Finance, Alpha Vantage, Finnhub, FRED, Tradier) with green/amber/red health dots and capability tooltips
  - Arrows: agents → gateway (teal, labeled "data request") and gateway → providers (teal, labeled "data")
  - Below: direct-connection chips for Chat Agent → MCP servers and Broker Gateway ← MCP Alpaca
  - Badge in card header shows "N/M connectors online"
  - Re-renders on every WebSocket heartbeat cycle (agent health) and gateway health poll

## [4.0.16] - 2026-05-25

### Fixed
- **mcp-yahoo dead**: Added `mcp-yahoo` service to `compose.yml` using `mcp/yahoo-finance-mcp/Dockerfile.http`; container `ot-mcp-yahoo` now runs on the `trading-net` network at port 8000

## [4.0.15] - 2026-05-25

### Fixed
- **Market Data Gateway crash on startup**: `fastapi` and `uvicorn` were missing from `requirements.txt` (the base agent Dockerfile); added both so `ot-market-data` can start

## [4.0.14] - 2026-05-25

### Fixed
- **Topology: market-data and mcp-yahoo showing "unknown" status**: Added `market-data` and `mcp-yahoo` to `KNOWN_AGENTS`, `PODMAN_HEALTH_ONLY`, and `CONTAINER_MAP` in webui; removed `mcp-eodhd` from all three — both new nodes now derive health from Podman container state (same pattern as other MCP servers)

## [4.0.13] - 2026-05-25

### Changed
- **Platform Dashboard — Market Data Gateway integration**: Agent Topology and stat grid updated to reflect the new gateway architecture
  - Added `Market Data GW` node (core group, centre of the topology)
  - Removed `MCP EODHD` node (EODHD subscription eliminated); replaced with `MCP Yahoo` node
  - Edges redrawn: all data consumers (aggregator, predictor, traders, options-monitor, ETF/macro scrapers) now point to `market-data`; `market-data` fans out to MCP providers (Massive, TradingView, Unusual Whales, Yahoo, Alpaca)
  - Old direct MCP→agent data edges removed (mcp-massive→predictor, mcp-massive→aggregator, mcp-tradingview→traders, mcp-eodhd→*)
  - Scheduler trigger edge `scheduler→market-data` added; `market-data→orchestrator` heartbeat added
  - `resetTopoLayout` default positions updated; `mcp-eodhd` removed from log viewer dropdown
  - New **Data Gateway** stat card on the Platform Dashboard — shows `N/M online` connector status via `/api/market-data/health`
  - `GET /api/market-data/health` added to webui backend (proxies `ot-market-data:8090/health`)
- **Finnhub connector logo**: `finnhub_avatar.png` added to webui static assets as `logo-finnhub.png`

## [4.0.12] - 2026-05-25

### Changed
- **Market Data Gateway — Phase 2–5 complete**: All backend agents now route market data through `ot-market-data:8090` instead of calling MCP servers or Polygon directly
  - `aggregator/main.py` — UW flow/darkpool/analyst calls delegated to DataClient; local Redis cache wrappers removed (gateway handles TTLs)
  - `shared/mcp_client.py` — `get_tv_indicators()` and `get_avg_volume()` rewritten to call DataClient; all traders/webui callers unchanged
  - `options_monitor/main.py` — ATR bars, underlying price, earnings, dividends, and chart bars all via DataClient; removed direct Massive MCP + TradingView MCP calls
  - `predictor/ml_predictor.py` — `_fetch_ohlcv()` replaced: Polygon SDK removed, synchronous `urllib.request` call to gateway (safe in `run_in_executor` thread)
  - `webui/main.py` — 7 direct Massive MCP callsites migrated (dividends, analyst consensus, fundamentals, news, short interest, quote, earnings)
  - `scrapers/macro_regime/scraper.py` — SPY/QQQ/TLT bars via DataClient; FRED via DataClient.macro() parallel calls; `api_key` param now optional
  - `scrapers/etf_flows/scraper.py` — full rewrite: all 26 ETF bars via DataClient with semaphore-limited concurrency; `api_key` param removed
  - Yahoo connector tool_map corrected (`get_historical_stock_prices`); `call()` override translates `days` → yfinance `period` string
  - Massive connector duplicate `get_avg_volume` key removed
  - 3 scheduler jobs added: `market_data_warmup` (9am ET), `market_data_eod_refresh` (4:45pm ET), `market_data_probe` (every 30m)

## [4.0.11] - 2026-05-25

### Added
- **Predictor Schedule time pickers** — each scheduled run (morning / afternoon) now has a native `<input type="time">` clock selector; defaults to 10:00 and 14:00 ET; saving hot-reloads the APScheduler CronTrigger via `scheduler:reload` pub/sub — no container restart needed; times persisted to `config:predictor:time_10am` / `config:predictor:time_2pm` Redis keys

## [4.0.10] - 2026-05-25

### Changed
- **All Agents count label** — updated from "14 total" to "Total Active Agents: 14"

## [4.0.9] - 2026-05-25

### Changed
- **Topology zoom anchor** — zoom now anchors to the top-left corner of the diagram (viewBox origin stays at 0,0; only width/height scale), so content stays pinned to the top-left as you zoom in
- **All Agents count** — agent total shown inline next to the "All Agents" card title (e.g. "14 total"); updates on every refresh

## [4.0.8] - 2026-05-25

### Added
- **Topology zoom controls** — `−` / `+` buttons and a live `100%` label in the Agent Topology card header; zooms from 30% to 300% in 15% increments by adjusting the SVG viewBox (center-anchored); Reset Layout also resets zoom to 100%

## [4.0.7] - 2026-05-25

### Changed
- **Predictor Schedule** moved from OpenRouter connector modal to its own card on the API Configuration (User Configuration) page — daily limit, 10 AM, and 2 PM toggles now visible alongside System Settings and Service Connectors

## [4.0.6] - 2026-05-25

### Added
- **Predictor daily run limit** (`predictor/main.py`) — configurable cap on how many times the predictor fires per calendar day; reads `config:predictor:daily_limit` Redis key (default 2); counter stored in `predictor:runs:{YYYY-MM-DD}` with 28h TTL; excess trigger commands are silently dropped with a log line
- **Scheduled predictor runs** (`scheduler/jobs.py`, `scheduler/main.py`) — two new APScheduler jobs: `predict_10am` (10:00 ET) and `predict_2pm` (14:00 ET), each gated by Redis toggle keys `config:predictor:schedule_10am` / `config:predictor:schedule_2pm`; market-hours check preserved
- **Predictor Schedule API** (`webui/main.py`) — `GET /api/predictor/schedule` and `POST /api/predictor/schedule` expose daily limit, 10am/2pm toggles, and today's run count; hot-reload — no container restart needed
- **Predictor Schedule UI** (`index.html`) — OpenRouter connector modal gains a "Predictor Schedule" section: daily run limit input, 10 AM and 2 PM toggle checkboxes; saved alongside API key on connector save
- **Personas model field** in OpenRouter connector — `LLM_PERSONAS_MODEL` now shown in Settings → OpenRouter modal

### Fixed
- **Options Trader / Pending Orders** — panel now filters to options-only orders (by `class === 'option'` or `option_symbol` present); previously showed equity orders such as stock GTC orders

## [4.0.5] - 2026-05-25

### Added
- **Investor Persona Voting** (`predictor/personas.py`) — six legendary investor personas (Druckenmiller, Buffett, Lynch, Wood, Burry, Greenblatt) each independently evaluate all momentum candidates in a single batch LLM call (haiku); weighted consensus adjusts confidence ±0.15 and gates weak signals; results cached to `predictor:persona_votes:latest` Redis key and persisted to signals DB; controlled by `PERSONA_VOTING` env var (default on)
- **Earnings Gap feature** (`ml_predictor.py`) — Post-Earnings Announcement Drift (PEAD) proxy: signed max overnight gap > 2% in last 63 trading days added to ML feature set as a momentum direction indicator; requires no additional API calls
- **Sector relative momentum features** (`ml_predictor.py`) — `spy_rel_21` and `spy_rel_63`: ticker return minus SPY return over 21 and 63 days; fetched via existing Polygon API key; SPY data cached 1h via new `SyncTTLCache` to avoid redundant fetches across concurrent ticker predictions
- **TTL data cache** (`shared/data_cache.py`) — `TTLCache` (async) and `SyncTTLCache` (thread-safe sync) for reducing redundant API calls; used by ML predictor for benchmark data; available for all agents
- **Market impact simulation** (`broker_gateway/fill_sim.py`) — Almgren-Chriss `estimate_impact()` computes slippage_bps, slippage_usd, fill_prob, participation rate, and recommended lot count; called automatically by broker gateway when `avg_volume` is present in the order command; logged at WARNING when slippage > 50bps
- **Fill impact metadata in broker results** — gateway attaches `fill_impact` dict to each `place_order` result; equity trader passes `avg_volume` to the command when liquidity checks are active
- **`personas` LLM role** (`llm/connector.py`) — new model slot mapped to `LLM_PERSONAS_MODEL` env var (default: haiku); used by persona voting system

### Changed
- **Predictor LLM enhancement** — single-LLM enhance replaced by multi-persona consensus when `PERSONA_VOTING=true` (default); single-LLM path retained as fallback when personas disabled
- **ML feature count** — expanded from 26 to 29 features (earnings_gap, spy_rel_21, spy_rel_63)

## [4.0.4] - 2026-05-25

### Added
- **Finnhub Insider Scraper** (`ot-scraper-finnhub-insider`) — new agent that fetches insider transactions and monthly MSPR sentiment from Finnhub free API; persists to `insider_transactions` + `insider_sentiment` tables; publishes net buy/sell signal to `market.ticks`; runs daily at 17:05 ET
- **Alpha Vantage ticker-targeted sentiment** — enhanced existing AV scraper to also call `NEWS_SENTIMENT?tickers=...`; stores per-ticker relevance + sentiment scores in new `av_ticker_sentiment` table; publishes structured scores to `market.ticks`; endpoint `GET /api/sentiment/av-ticker`
- **Finnhub insider API endpoint** — `GET /api/market/insider/{ticker}` returns transactions, MSPR sentiment history, and net shares signal; Redis-cached 24h
- **Insider Activity panel** in Quick Intel — shows last 8 insider transactions with buy/sell color coding and net position summary
- **Finnhub service connector** in Settings — API key configuration with live test endpoint
- **Ticker consistency guard** (`shared/instrument_utils.py`) — `build_instrument_context()` / `guard_tickers()` injected into predictor LLM prompt to prevent symbol hallucination

## [4.0.3] - 2026-05-25

### Changed
- **Navigation** — renamed "Market Groups" to "Sector Groups" (nav label, page heading, page title)

## [4.0.2] - 2026-05-25

### Changed
- **Navigation** — renamed "Sector Insight" to "Market Insights"; moved Macro Hub inside Market Insights; added "Sector Insights" sub-label with Market Groups and Sector Leaders underneath it
- **Service Connectors** — FRED, EODHD, and Alpha Vantage now display their avatar images instead of letter placeholders

## [4.0.1] - 2026-05-25

### Fixed
- **FRED Economic Dashboard** — refresh button now works: frontend always shows "Loading…" spinner and passes `?force=true` to API; backend skips Redis cache and re-fetches all series when `force=true`, then re-caches the fresh result

## [4.0.0] - 2026-05-25

### Changed
- **FRED Economic Dashboard** — extended all trend windows to 18 months (monthly series: 18 pts; weekly jobless claims: 78 wks; daily credit spreads: ~390 trading days)

## [3.9.9] - 2026-05-25

### Added
- **FRED Economic Dashboard** (`Sector Insight → FRED Macro`) — dedicated macro page with sparkline charts for inflation (CPI, Core CPI, Core PCE), credit spreads (HY/IG OAS), employment (unemployment rate, initial jobless claims), monetary policy (Fed Funds Rate), and M2 money supply; uses FRED public CSV (no API key required); cached 1 hour
- **Monthly Returns Heatmap** — new card on Portfolio Analytics page; calendar grid (years × months) color-coded by monthly P&L (green=positive, red=negative, intensity = magnitude); summary chips for best/worst month, positive month count, avg monthly return
- **Trade Statistics** — new card on Portfolio Analytics page; win rate, profit factor, expectancy, avg win/loss, max consecutive wins/losses computed from the `trades` table
- **Volatility Surface** — new panel on Options Dashboard (below Chain Analytics); IV heatmap table (strikes × expirations, color-coded blue→red for low→high IV); ATM strikes highlighted in amber; call/put toggle; loads from Tradier market API
- **DCF Valuation inline** — Quick Intel now auto-fetches the DCF model after analysis renders; shows intrinsic value, market price, margin of safety %, and WACC in a compact strip; hidden when DCF data is unavailable
- **Investor Persona Agents** — Quick Intel now has five persona buttons (Buffett, Graham, Lynch, Munger, Klarman); each runs an LLM analysis through a persona-specific system prompt encoding their investment philosophy; results cached 1 hour per ticker/persona

## [3.9.8] - 2026-05-25

### Added
- **EODHD News Scraper** — new `scraper-eodhd-news` agent (`ot-scraper-eodhd-news`) fetches per-ticker news from EODHD for all assigned tickers + SPY/QQQ/IWM every 30 min; EODHD natively provides per-article sentiment scores (`polarity`, `pos`, `neg`, `neu`) so no extra sentiment LLM is required for basic scoring
- **LLM article enrichment** — for articles where `|polarity| >= 0.2`, an optional LLM pass extracts a 2–3 sentence market-impact summary and up to 5 keywords/catalysts; enabled by default, configurable via `EODHD_NEWS_LLM_ENRICH` and `EODHD_NEWS_LLM_THRESHOLD`
- **`eodhd_news` DB table** — persists all fetched articles with EODHD sentiment scores, LLM summary, and LLM keywords; `ON CONFLICT` upsert preserves existing enrichment when polarity is unchanged
- **Aggregator integration** — scraper publishes per-ticker average polarity to `market.ticks` stream as `source=eodhd` so the aggregator folds EODHD news sentiment into the predictor signal alongside WSB/SeekingAlpha/Yahoo
- **`GET /api/sentiment/eodhd-news`** — returns articles with full sentiment fields; supports `?ticker=`, `?hours=`, `?limit=`; Redis cache for single-ticker recent requests
- **EODHD Ticker News panel** — added to Trading Dashboard below Market News; shows per-article polarity chip (bullish/bearish/neutral), score, LLM summary, keyword chips, and external headline link; ticker input + time range selector

## [3.9.7] - 2026-05-25

### Added
- **Split Order** — equity trader splits large orders into sequential chunks when position size exceeds `SPLIT_ORDER_THRESHOLD_USD` (default disabled); configurable via `SPLIT_ORDER_CHUNKS` (default 3) and `SPLIT_ORDER_DELAY_MS` (default 500ms) env vars; each chunk emits its own fill event to the orders stream
- **Analyzer Mode** — both equity and options traders short-circuit order placement when `TRADE_MODE=analyze`; would-be orders are logged to telemetry (`analyze_order` event) and published to the orders stream with `mode=analyze` and `broker=analyze` so they appear in the Trading Log without touching any broker
- **Analyze chip** — Trading Log mode column now renders `analyze` orders with a purple chip (distinct from live green / sandbox amber) in both the active-trades and trade-history views

## [3.9.6] - 2026-05-25

### Added
- **Order Latency Tracker** — broker-gateway now emits a `broker_latency` TelemetryEvent (via `system.telemetry`) for every command processed, recording `duration_ms`, broker, command, account_label, and status; persisted to `execution_events` by the existing telemetry consumer
- **`GET /api/broker/latency`** — queries `execution_events` for `broker_latency` events and returns per-broker/command aggregate stats (count, avg, p50, p95, errors) plus 200 most-recent entries; supports `?hours=` window
- **Broker Latency panel** — added to Broker Configuration page: stat chips (orders, avg, p50, p95), by-broker/command breakdown table, and collapsible recent-orders list; refreshes with the page and has an independent time-range selector
- **HTTP Traffic Monitor** — new `traffic_middleware` in webui records every `/api/*` request (method, path, status code, duration_ms) as a fire-and-forget DB write to the new `api_traffic` TimescaleDB table (7-day retention, indexed on `ts` and `path`)
- **`GET /api/system/traffic`** — returns traffic summary (total, errors, avg/p95 ms), top-20 paths by call count with per-path avg/p95/error counts, and 200 most-recent requests
- **API Traffic panel** — added to System page: summary stat chips, top-paths table, collapsible recent-requests log; time-range selector (1h/4h/24h/7d); loads automatically with the System page

## [3.9.5] - 2026-05-25

### Added
- **`mcp/insights-mcp/`** — new MCP server exposing OpenTrader telemetry and codebase to Claude Desktop; listens on `localhost:8094`; 6 tools: `get_summary`, `get_events`, `get_event_detail`, `resolve_event`, `read_source_file`, `search_codebase`, `list_source_files`
- **`compose.yml` `mcp-insights` service** — builds from `./mcp/insights-mcp`, mounts `./python` read-only at `/repo/python`, binds port `127.0.0.1:8094:8000` (localhost only, no external exposure)
- **Claude Desktop configured** — `~/.config/Claude/claude_desktop_config.json` updated with `opentrader-insights` MCP server entry pointing to `http://localhost:8094/mcp`; connection is permanent (survives Claude Desktop restarts)

## [3.9.4] - 2026-05-25

### Added
- **Code Insights dashboard** — new Platform nav page (`page-platform-insights`) providing real-time visibility into agent execution telemetry: stat cards (total, critical, error, warn, info counts), agent health table, filterable event log with traceback expansion, and an AI analysis chat panel backed by OpenRouter streaming
- **`python/shared/telemetry.py`** — fire-and-forget telemetry module: `TelemetryEvent` dataclass, async `emit()` writing to Redis stream `system.telemetry`, and `@instrument(slow_ms)` decorator for async functions that captures slow calls and unhandled exceptions without swallowing them
- **BaseAgent instrumented** — `heartbeat_loop()` emits `heartbeat_failed` telemetry on exception; new `run_safe(coro)` method wraps agent run loops to emit `unhandled_exception` telemetry (with full traceback) before re-raising
- **MCP client instrumented** — `call_mcp_tool()` now times every call: emits `slow_call` telemetry when `MCP_SLOW_THRESHOLD_MS` exceeded (default 8 s), emits `mcp_call_failed` telemetry on exception; both are fire-and-forget so callers are unaffected
- **`execution_events` TimescaleDB table** — `(id, ts, agent, event_name, severity, duration_ms, payload, traceback_str, resolved, notes)`; created on startup with three indices; 30-day retention enforced on each startup
- **`_telemetry_consumer()` background task** — drains `system.telemetry` Redis stream via consumer group `webui-insights`, persists each event row to `execution_events`; runs for the lifetime of the webui container
- **Four new API endpoints**:
  - `GET /api/telemetry/events` — filterable (agent, severity, event_name, resolved, hours, limit)
  - `GET /api/telemetry/summary` — per-agent error/warn counts and last-seen timestamps
  - `PATCH /api/telemetry/events/{id}/resolve` — mark event resolved with optional notes
  - `POST /api/telemetry/analyze` — streaming SSE LLM analysis of recent telemetry for any agent or the whole platform

## [3.9.3] - 2026-05-25

### Fixed
- `mcp-eodhd` was deployed in compose but absent from `KNOWN_AGENTS`, `PODMAN_HEALTH_ONLY`, `CONTAINER_MAP` in webui/main.py — now registered and monitored alongside all other MCP servers
- Logs dashboard dropdown was missing `scraper-news`, `scraper-etf-flows`, `scraper-macro-regime`, and `mcp-eodhd` — all four added; all 22 deployed agents now selectable
- `mcp-eodhd` added to Platform Dashboard topology: node (group mcp, x=460 y=520), two data edges (→ aggregator: fundamentals, → predictor: earnings/ratings), one chat-agent query edge, and default reset position

### Changed
- README.md: complete rewrite with all new features (v3.7–v3.9), full signal flow diagram, expanded architecture ASCII diagram with MCP layer and options monitor detail, updated Services table (now includes `ot-mcp-eodhd`), new Data Subscriptions section (FRED free key, EODHD, Alpha Vantage added), Options Monitor four-task breakdown, Strategy Payoff Builder feature list, Macro Hub, WebUI navigation table updated

## [3.9.2] - 2026-05-25

### Added
- **Unusual Options Flow panel in Options Dashboard**: new card between Portfolio Greeks and the positions table showing incremental volume activity across all active underlyings; loaded on page open and via Refresh button; displays contract, strike, type, expiry, Vol Δ, notional, delta, direction badge (BULL/BEAR), and a scored bar chart
- **Volume delta tracking in options_monitor**: on each scan cycle `_scan_unusual_flow()` fetches Polygon v3 snapshot per underlying, diffs current `day.volume` against a per-contract Redis baseline (`options:vol_snap:{underlying}`), filters by configurable `FLOW_MIN_VOL_DELTA` (default 10 contracts) and `FLOW_MIN_NOTIONAL` (default $5K), scores hits using weighted formula (0.40 × normalized notional + 0.30 × normalized vol delta + 0.20 × type weight + 0.10 × direction confidence), and writes top `FLOW_TOP_N` (default 25) hits to `options:flow:latest` (90-min TTL)
- **Three-method direction inference**: uses `greeks.delta` sign when present (confidence 0.5–0.9 scaled by |Δ|), falls back to `day.change_percent` sign (confidence 0.65), then call/put type heuristic (confidence 0.5); confidence feeds the importance score
- **`GET /api/options/unusual-flow`** endpoint reading `options:flow:latest` from Redis; returns structured hits array with `ts`, `count`, and `source` fields

## [3.9.1] - 2026-05-25

### Added
- **Non-equidistant butterfly lot sizing**: Long Butterfly strategy now uses GCD-based lot sizing for uneven wings — when Low–Mid and Mid–High widths differ, `_spbGcd()` computes the minimum integer lot ratio (n1 = upperWidth/GCD, n3 = lowerWidth/GCD, n2 = n1+n3); payoff, metrics (maxP, maxL, BEs), time-slice B-S curves, Greeks sparklines, and per-leg table all reflect the correct asymmetric lot structure; stats bar shows "Lots: ×n1 / ×n2 / ×n3"; equidistant wings (e.g., 490/500/510) degenerate to the standard ×1/×2/×1
- **Bull Put Spread strategy**: new credit spread — sell higher put, buy lower put; max profit = net credit × 100, max loss = (width − net credit) × 100, single break-even at sell-strike − net credit; payoff, B-S time-slice curves, Greeks sensitivity charts, and per-leg table all supported; added to strategy dropdown
- **Bear Call Spread strategy**: new credit spread — sell lower call, buy higher call; max profit = net credit × 100, max loss = (width − net credit) × 100, single break-even at sell-strike + net credit; payoff, B-S time-slice curves, Greeks sensitivity charts, and per-leg table all supported; added to strategy dropdown

## [3.9.0] - 2026-05-25

### Added
- **Greeks sensitivity line charts in Strategy Payoff Builder**: after "Build Payoff", four sparkline SVGs render in a 2×2 grid below the main payoff diagram — Δ (green), Γ (violet), ν/1%IV (sky), Θ/day (orange) — each plotted vs. spot across the full payoff range (50 points); portfolio Greeks are summed across all active legs weighted by sign and multiplier; current spot marked with a vertical grey line; zero-crossing line shown when curve changes sign; only renders when Spot + IV% + DTE are all provided
- **Per-leg price + Greeks breakdown table in Strategy Payoff Builder**: compact 10-column table rendered below the sensitivity charts for every "Build Payoff" call — shows each leg's label, strike, direction/type/multiplier, entry premium, current B-S theoretical value, net dollar P&L per contract (B-S minus entry × sign × mult × 100), and individual Δ/Γ/ν/Θ contributions; when market params are absent the table renders with strike and entry only, prompting the user to enter Spot + IV% + DTE for live values; uses `_spbLegGreeks()` for individual leg B-S and `_spbPortfolioGreeks()` for the sensitivity aggregation

## [3.8.9] - 2026-05-25

### Added
- **P&L curves at multiple time slices in Strategy Payoff Builder**: when Spot, IV%, and DTE are provided, two additional B-S theoretical value curves are overlaid on the payoff SVG — cyan dashed "Today" (full DTE remaining) and amber dashed "50% DTE" — computed via Black-Scholes using `_spbBsValue()` with per-strategy leg type definitions (`_SPB_LEGS`); y-axis scale expands to cover all three curves; legend row below SVG identifies each line; at-expiry intrinsic curve remains the solid white reference
- **Greeks surface in chain analytics**: after the price sensitivity heatmap, a new "Greeks Surface" section shows a color-coded 2D table (spot ±30% × DTE) for the selected Greek — Delta, Gamma, Vega, or Theta; Greek selector dropdown triggers instant client-side re-render via `_showCGS()`; uses ATM-IV from chain data as constant sigma; current spot column highlighted in cyan; same 5-stop deep-blue→red color scale as the price heatmap; precomputed for all four Greeks on each chain load via `_cgsData`

## [3.8.8] - 2026-05-24

### Added
- **Strategy Payoff Builder** (replaces Iron Condor Builder): 12 strategies — Long Call, Long Put, Covered Call, Cash-Secured Put, Long Straddle, Short Straddle, Long Strangle, Short Strangle, Bull Call Spread, Bear Put Spread, Long Butterfly, Iron Condor; 4-leg input form with labels/visibility driven by strategy selection; client-side SVG payoff diagram with profit/loss zone fills (SVG clipPath), strike markers, break-even markers, current spot line, max profit/loss horizon lines; stats row shows Max Profit, Max Loss, Net Credit/Debit, Break-Even(s), and risk-neutral PoP (Gaussian quadrature over lognormal density, 400 points)
- **Newton-Raphson IV Solver UI**: IV Solver row in Strategy Payoff Builder calls `/api/options/implied-vol`; uses Leg 1 strike; displays `IV = X.XX% (N iters)` or convergence warning
- **Per-position Payoff Modal** (📈 button in Options positions table): renders at-expiry P&L SVG for any active option position using strike, entry price, qty, and live/entry spot; delta shown as PoP proxy; reuses `_spbPayoffSvg` helper
- **Portfolio VaR panel in Options Greeks**: `loadOptionsGreeksPanel()` now fetches `/api/options/portfolio-var` in parallel; `_renderOptionsGreeks()` renders a Delta-Gamma Risk row below the per-underlying table showing VaR 95%, CVaR 95%, Max Scenario Loss, Portfolio PoP, and Portfolio Theta/day

## [3.8.7] - 2026-05-24

### Added
- **Iron Condor Builder on Options Dashboard**: new panel with four-column strike/premium inputs (Long Put wing · Short Put body · Short Call body · Long Call wing) + market params (spot, IV%, DTE); client-side SVG payoff diagram with green profit zone / red loss zone fills using SVG clipPath; break-even markers, per-leg vertical strike lines, current spot marker; stats row shows Max Profit, Max Loss (each wing separately), Net Credit, Break-even Lower/Upper, and risk-neutral PoP via B-S `N(-d2_sc) − N(-d2_sp)` formula

## [3.8.6] - 2026-05-24

### Added
- **Finite-difference Greeks for American binomial tree**: `_binomial_greeks()` wraps `_binomial_american_price()` with central-difference FD to produce delta/gamma/theta/vega/rho that correctly capture early-exercise premium; volga/vanna/charm/pop remain B-S analytical (negligible early-exercise correction); used as the Greek source in `_fetch_option_chain_details()` replacing the B-S closed-form fallback for all equity options
- **Price sensitivity heatmap in chain analytics**: `/api/options/chain-analytics` now returns `vol_heatmap` — a 12×10 grid of B-S call and put prices across spot (±30%) × vol (5–150%) space anchored at Max Pain strike and selected expiry; rendered in the chain analytics panel as two side-by-side color-coded tables (deep blue=cheap → red=expensive) with the current spot column highlighted

## [3.8.5] - 2026-05-24

### Added
- **Higher-order Greeks — Vanna, Charm, PoP**: `_bs_greeks()` now returns `vanna` (∂Δ/∂σ, delta sensitivity to vol), `charm` (∂Δ/∂t per calendar day, delta decay rate), and `pop` (risk-neutral probability of expiring ITM: N(d2) for calls, N(−d2) for puts); all three persisted to `option_positions` and aggregated in `/api/options/portfolio-greeks`
- **American-style binomial tree pricer**: `_binomial_american_price()` — CRR 100-step binomial tree with backward induction and early-exercise check at each node; supports continuous dividend yield `q`; available for American option pricing alongside the existing B-S European pricer

## [3.8.4] - 2026-05-24

### Added
- **Extended B-S Greeks — Rho and Volga**: `_bs_greeks()` now returns `rho` (∂price/∂r per 1% rate move) and `volga` (∂²price/∂σ², vega convexity) in addition to delta/gamma/theta/vega; both are persisted to `option_positions` and aggregated in `/api/options/portfolio-greeks`
- **Dividend yield in B-S pricing**: `_bs_greeks()` and `_bs_price()` now accept a `q` parameter (continuous dividend yield, default 0); d1/d2, delta, and theta formulas corrected to use `r - q`, eliminating systematic mispricing on dividend-paying stocks

## [3.8.3] - 2026-05-24

### Added
- **ATM IV Percentile in Chain Analytics**: `/api/options/chain-analytics` now fetches 400 days of daily stock bars from Polygon, computes 21-day rolling annualized realized vol, and returns `atm_iv` (ATM implied vol %), `iv_percentile` (% of hist HV days below current ATM IV), `iv_rank` (IVR 0–100), and `iv_label` (Low/Normal/High). Displayed as a new badge with a percentile gauge bar.
- **PCR (Put/Call Ratio) in Chain Analytics**: chain-analytics now sums call and put OI across all strikes and returns `pcr` and `pcr_label` (Bullish/Neutral/Bearish). Displayed as a new badge alongside Max Pain, Zero Gamma, and Net GEX.

## [3.8.2] - 2026-05-24

### Added
- **SMFI-inspired features in ML predictor**: added `open_gap` (feature 25) and `intraday_ret` (feature 26) to `ml_predictor._engineer_features()`; daily-bar approximation of the Smart Money Flow Index — `open_gap = (open − prev_close) / prev_close` captures the overnight emotional/retail reaction to news, `intraday_ret = (close − open) / open` captures the intraday institutional move; the ensemble can now weight these independently to detect divergence (e.g. gap up + intraday sell-off = distribution)

## [3.8.1] - 2026-05-24

### Added
- **MFI slope feature in ML predictor**: added `mfi_slope_2` as feature 24 — 2-bar rate of change of raw MFI normalized to 0–1 scale; captures whether volume pressure is building or fading (the confirmation signal used by EA31337's MFI strategy: `IsIncreasing(2)` / `IsDecreasing(2)`); combined with `mfi_14` the ensemble can learn crossover momentum without hardcoded thresholds

## [3.8.0] - 2026-05-24

### Added
- **MFI feature in ML predictor**: added Money Flow Index(14) as feature 23 in `ml_predictor._engineer_features()`; MFI is a volume-weighted pressure oscillator (0–100, normalized to 0–1) that is distinct from RSI (price-only) and the existing force-index proxy, giving the ensemble a bounded volume/price confluence signal

## [3.7.99] - 2026-05-24

### Fixed
- **Webui live-reload broken**: Podman file bind-mounts hold the original inode — when the Edit/Write tool atomically replaces a file (new inode) the container never sees the update; replaced five individual `.py` file mounts + `static/` dir mount with a single `./python/webui:/app/webui` directory mount, which resolves by name so inode changes are always visible

## [3.7.98] - 2026-05-24

### Changed
- **Dockerfile.webui**: added `--reload` to uvicorn so volume-mounted `main.py` edits take effect immediately without a container restart

## [3.7.97] - 2026-05-24

### Added
- **FRED connector in API Configuration UI**: FRED appears in the connector grid with Configure + Test buttons; `FRED_API_KEY` added to `KNOWN_SECRETS` (Market Data section); test handler at `POST /api/config/test/fred` validates key by calling `GET /fred/series?series_id=BAMLH0A0HYM2`

## [3.7.96] - 2026-05-24

### Added
- **`shared/fred_client.py` — FRED service connector**: `FREDClient` class with rate limiting (120 req/min), exponential-backoff retries, and typed methods: `latest()`, `history()`, `series_info()`, `search()`, `release_dates()`, `bulk_latest()`, `macro_snapshot()`; 35 pre-defined series IDs in `FREDClient.SERIES` covering credit spreads, rates, inflation, employment, yield curve, and money supply; module-level `get_fred_client()` singleton
- Macro regime scraper refactored to use `FREDClient.bulk_latest()` instead of inline HTTP; webui `/api/market/fred-macro` uses `get_fred_client()` when key is available, falls back to CSV otherwise

## [3.7.95] - 2026-05-24

### Added
- **FRED macro integration**: macro regime scraper now fetches HY OAS (`BAMLH0A0HYM2`), IG OAS (`BAMLC0A0CM`), St. Louis Financial Stress Index (`STLFSI2`), and NBER recession indicator (`USREC`) from FRED API on each daily run; these 4 signals (USREC double-weighted) are added to the bull/bear regime score
- **`fred_macro_snapshots` DB table**: stores each FRED fetch alongside the existing `macro_regime_snapshots`; data cached in Redis at `fred:macro:latest`
- **`GET /api/market/fred-macro` endpoint**: returns current HY OAS, IG OAS, FSI, recession flag with human-readable `credit_label` and `stress_label`; falls back to direct FRED CSV fetch if scraper cache is empty

## [3.7.94] - 2026-05-24

### Added — AI-Trading-Alert-Bot features (items 1, 2, 3, 5)

- **Options OI Wall Detector** (Item 1): `OptionsMonitor._scan_oi_walls()` scans every active underlying's Polygon options chain each scan cycle; identifies strikes with ≥3,000 OI; fires a Redis alert and writes a DB signal when any wall drops ≥40% between scans; confidence = oi_size_pct×0.40 + drop_speed×0.35 + price_proximity×0.25
- **Bid/Ask Imbalance Signal** (Item 2): Added `bid_ask_proxy` feature (22nd ML feature) to `_engineer_features()` — `(close-low)/(high-low)` buying pressure proxy derived from OHLCV; new `/api/signals/imbalance` endpoint fetches Polygon NBBO quote per holding, computes `bid_size/(bid_size+ask_size)`, classifies >0.60 bullish / <0.40 bearish / else neutral, 5-min cache
- **Signal Confidence Scorer** (Item 3): `_compute_signal_confidence(strength, speed, proximity)` helper using three-factor weighted score (0.40/0.35/0.25); wired into TSMOM signals and bid/ask imbalance; TSMOM table gains a colour-coded confidence bar column (green ≥70%, amber ≥45%, red otherwise)
- **Signal Timeline Panel** (Item 5): `/api/signals/recent` endpoint merges DB `signals` table (OI wall, etc.), Redis predictor stream, and recent options ATR alerts into a unified newest-first list; Platform Dashboard now shows "Signal Timeline" card with source badge, direction arrow, confidence bar, and relative timestamp; stocks and options only — no crypto

## [3.7.93] - 2026-05-24

### Added — mini-bloomberg-terminal features (6 items)
- **LBO Analysis** (`/api/analytics/lbo/{ticker}` + card): entry EV, annual debt-paydown schedule (EBITDA/interest/FCF/leverage/coverage per year), base IRR + MOIC, 500-path Monte Carlo (p5/median/p95), covenant checks (≤6× leverage, ≥2× coverage), STRONG BUY/BUY/CONDITIONAL/PASS verdict. Fetches EBITDA/capex from Polygon vX/reference/financials.
- **DCF Valuation** (`/api/analytics/dcf/{ticker}` + card): 10-year FCFF explicit forecast with linear growth fade to 3% terminal, WACC via CAPM equity cost + after-tax debt cost, terminal value (Gordon Growth), per-share intrinsic vs market price gap, UNDERVALUED/FAIR/OVERVALUED rating. FCFF chart SVG.
- **Portfolio Optimizer** (`/api/analytics/optimize` + card): three methods — HRP (hierarchical risk parity with Ledoit-Wolf shrinkage + scipy seriation), Max Sharpe (SLSQP constrained), Risk Parity (equal risk contribution). Per-asset weight + risk contribution bar chart.
- **Yield Curve Analytics** (`/api/market/yield-curve` + card): FRED public CSV (no API key) for 9 nominal Treasury maturities (1M–30Y) + TIPS 5Y/10Y real rates. 2s10s/3m10y spreads, breakeven inflation, INVERTED/FLAT/STEEP/NORMAL regime. 252-day 2s10s spread bar chart. Cached 30 min.
- **Market Breadth** (`/api/market/breadth` + card): 50 large-cap universe — % above 50/200-day MA, advance/decline ratio, net 52-week new highs vs lows, top-5 gainers/losers. Cached 30 min.
- **Time-Series Momentum** (`/api/analytics/tsmom` + panel): 12-1 momentum for each portfolio holding, EWMA vol (halflife=60d), LONG/SHORT/FLAT signal, vol-targeted position sizing `min(2×, 15%/realized_vol)`. Loads with analytics page.
- Added `_fetch_poly_financials()` and `_fetch_poly_price()` shared helpers for Polygon vX financials
- Added `_fred_latest()` and `_fred_history()` helpers for FRED public CSV endpoint

## [3.7.92] - 2026-05-24

### Added
- **Market Regime Classifier** (`/api/market/ml-regime`): GradientBoosting classifier trained on 5yr daily SPY (aggregated to weekly closes). Features: 4w/13w/26w/52w returns, 12w realized vol, RSI(14), cumulative level. Label: +5% over next 13 weeks = bull. 75% validation accuracy. Cached 6h.
- **Retirement Glidepath** (`/api/retirement/glidepath`): Age-based equity→bond allocation from current age through retirement + 20 years. Three styles (aggressive: 120−age, moderate: 110−age, conservative: 100−age). New collapsible card on Dividend page with SVG filled-area chart, retirement-date dashed line, key allocation badges.
- **Regime-aware Monte Carlo** (`regime_aware=true` on `/api/dividends/monte-carlo`): Calls ML regime classifier; in bull mode blends mu toward +10% / sigma × 0.75; in bear mode blends mu toward −12% / sigma × 1.40. Toggle checkbox on Monte Carlo card with bull/bear regime confidence badge.
- Added `scikit-learn` and `numpy` to `requirements.webui.txt` (webui container)

## [3.7.91] - 2026-05-24

### Added
- **Multi-timeframe feature engineering** in ML Predictor: expanded feature set from 12 to 21 features
  - Four additional return lookbacks: `ret_21` (1-month), `ret_63` (1-quarter), `ret_126` (6-month), `ret_252` (1-year)
  - Two cross-timeframe momentum ratios: `mom_accel` (short vs medium momentum = ret_21 − ret_63), `trend_slope` (medium vs long-term trend = ret_63 − ret_252)
  - Three volume features: `vol_ratio` (unchanged), `vol_trend` (5-day vs 20-day avg volume), `vol_momentum` (force-index proxy: volume × price change / avg volume)
  - Models are retrained daily so new features take effect on next training cycle

## [3.7.90] - 2026-05-23

### Added
- **Chain Analytics panel** on Options Dashboard (Gold-options-terminal-inspired): new collapsible card with ticker + expiry selector, loads all 5 metrics from a single `/api/options/chain-analytics` endpoint (cached 30 min)
- **Max Pain** — computes the strike where aggregate option $ value is minimised (where market makers collect maximum premium); displayed as badge + vertical annotation line on all charts
- **GEX (Gamma Exposure) heatmap** — per-strike call GEX (green bars) and put GEX (red bars) with net GEX overlay line; net GEX total with positive/negative regime label (positive = vol suppression, negative = vol amplification)
- **Zero Gamma level** — interpolated strike where cumulative GEX crosses zero; shown as badge + annotation line; acts as intraday support/resistance pivot
- **IV Smile / Skew chart** — call IV and put IV plotted as separate lines across all strikes; Y-axis labelled in %; reveals skew, kink, and ATM vs OTM vol premium
- **OI Distribution heatmap** — grouped call/put open interest bars per strike; all charts annotated with Max Pain, Zero Gamma, and current spot price lines
- Spot price fetch uses Polygon prev-close agg with last-trade fallback; all charts scoped to ±30% of spot for readability

## [3.7.89] - 2026-05-23

### Fixed
- **Options monitor market-aware valuation fallback** (BYE pattern): when a broker returns price=0 (Webull scan dropout, after-hours gap), the monitor now falls through a 4-tier chain instead of logging 0 — (1) Polygon option snapshot mid-price, (2) Black-Scholes using VIX from Redis as sigma estimate, (3) intrinsic value floor `max(0, S-K)` for calls / `max(0, K-S)` for puts, (4) zero as last resort
- Roll scoring `current_contract_bid` now receives the resolved mark price instead of the raw broker 0, so roll candidate credit calculations remain valid during quote gaps
- Scan log `contract_price` field now records the best available mark; `notes` field records `mark_source=polygon|bs_model|intrinsic` when fallback was used
- Added missing `timedelta` to module-level `datetime` imports (was imported inline only inside functions)
- Removed unused `ask` variable in `_score_roll_candidates`

## [3.7.88] - 2026-05-23

### Changed
- **Technical Indicators panel**: replaced text stats with three inline SVG charts — Bollinger Bands (price channel with filled ±2σ band + %B readout), RSI-14 (line with overbought/oversold reference lines + neutral zone fill), MACD(12,26,9) (colour-coded histogram bars + MACD/signal lines); backend now returns full 60-bar series for all three indicators (uses Wilder smoothing for RSI, rolling window for BB series)

## [3.7.87] - 2026-05-23

### Added
- **Portfolio Analytics page** (FinanceToolkit-inspired): new sidebar nav item with 5 panels
- **A — Performance metrics** (`/api/analytics/performance`): Sharpe, Sortino, Treynor, Jensen's alpha, Information ratio, beta, total/annualised return, volatility — all pure-Python, no scipy
- **B — Risk metrics** (`/api/analytics/risk`): VaR 95/99, CVaR 95/99, Max Drawdown, Ulcer Index, skewness, excess kurtosis, Calmar ratio
- **C — Technical indicators** (`/api/analytics/technicals/{ticker}`): RSI-14, MACD(12,26,9), Bollinger Bands(20,2) via Polygon OHLCV
- **D — Financial ratios** (`/api/analytics/fundamentals/{ticker}`): P/E, P/B, EV/EBITDA, Current Ratio, Debt/Equity, ROE, ROA, Gross Margin from Polygon vX financials + ticker details
- **E — Fama-French 5-factor attribution** (`/api/analytics/factors`): OLS regression against Mkt-RF, SMB, HML, RMW, CMA; annualised alpha + R² — factor data fetched from Dartmouth (Ken French Data Library), cached 24h
- **F — Rolling portfolio analytics** (`/api/analytics/portfolio`): 21-day rolling Sharpe, drawdown timeline, NAV vs SPX benchmark comparison; SVG line charts inline

## [3.7.86] - 2026-05-23

### Added
- Options Monitor: **Roll-out scoring** — when any roll ATR level fires, `_score_roll_candidates()` fetches the Polygon option chain for higher-DTE expirations and ranks candidates using option_screener's formula: `credit_pct + buy_up_pct + 0.5 credit bonus - duration penalty (10 - dte/7*3) - 2.0 ex-div risk penalty`; top 5 stored in Redis `options:roll_candidates:{pos_id}` (4h TTL) and logged; most scoring detail appended to alert note in `option_trade_log`
- Options Monitor: **Early assignment risk flag** — `_fetch_ex_dividend_date()` fetches next ex-div date via Massive MCP; `_check_early_assignment_risk()` fires a Redis warning alert when a call's ex-date falls before expiration and is ≤10 days away; prevents unexpected dividend-driven assignment
- Options Trader + Shared: **VIX-keyed market tone framework** — new `shared/market_tone.py` fetches VIX from Polygon daily aggs (30m Redis cache), classifies into bullish/neutral/bearish/forgiving tones from `config/market_tones.json`; options trader applies the stricter of strategy `min_confidence` and tone threshold, and overrides the limit price tier to match tone aggressiveness (bearish→passive, forgiving→natural)
- Config: `config/market_tones.json` — four tone definitions with `min_confidence`, `price_tier_override`, delta range, OI floor, downside protection %, and annualised ROO thresholds

### Planned
- Multi-leg execution (spreads, PMCC, iron condor): full plan saved to `/home/claude/.claude/plans/multi-leg-options-execution.md`

## [3.7.85] - 2026-05-23

### Added
- Options Trader: **tick-size validation** — `_snap_to_tick()` rounds every limit price to the CBOE/OCC tick grid ($0.01 increments below $3.00, $0.05 at or above) before submission, eliminating a class of broker price-rejection errors
- Options Trader: **four-tier limit pricing** — `_compute_limit_price()` maps signal confidence to price aggressiveness: natural/ask (≥0.85), mid (≥0.70), passive/bid (≥0.55), skip (<0.55); orders now route as `limit` with `limit_price` + `price_tier` fields; falls back to market order only when no bid/ask available
- Options Monitor: **Quote→Trade price fallback** — `_fetch_underlying_price()` now has a two-tier fetch: standard MCP get_quote first, then Polygon daily aggs prev-close for cash index underlyings (VIX→I:VIX, SPX→I:SPX, NDX→I:NDX, RUT→I:RUT, SPXW, XSP, DJX) that have no quote stream; mirrors tasty-agent's `stream_quotes_with_trade_fallback()` pattern

## [3.7.84] - 2026-05-23

### Added
- Dividend Dashboard: **Dividend Quality Scores** — per-ticker CAGR, EWM-weighted growth rate (com=0.5), consistency ratio, and cut count computed from dividend_history; collapsible card sorted by CAGR; 6h Redis cache (`/api/dividends/quality-scores`)
- Dividend Dashboard: **Ex-Date Price Patterns** — fetches last 8 Polygon ex-dates per held ticker, pulls OHLCV around each, computes average 21-day pre-ex drift and 14-day post-ex drift; green/red colored table; 24h cache (`/api/dividends/timing`)
- Dividend Dashboard: **Historical DRIP Simulation** — replays actual dividend_history payments at real Polygon close prices to compute lot accumulation and current reinvestment value per ticker; summary stats + per-ticker table; 4h cache (`/api/dividends/drip-historical`)
- Dividend Dashboard: **Dividend Calendar** — projects 12 months of ex-dates and pay-dates forward per held ticker using dividend_meta frequency + last pay date; chip display grouped by month with estimated totals; 6h cache (`/api/dividends/calendar`)
- Dividend Dashboard: **Monthly Income Seasonality** — groups dividend_history by month-of-year across all tracked years, computes mean/p25/p50/p75/min/max; SVG bar chart with IQR band overlay; 6h cache (`/api/dividends/seasonality`)
- Dividend Dashboard: **Dividend Quality Screener** — composite percentile rank across 5 factors (yield, CAGR, consistency, cuts, payout ratio) for all held dividend payers; ranked table with per-factor color-coded scores; 6h cache (`/api/dividends/screener`)

## [3.7.83] - 2026-05-23

### Added
- Dividend Dashboard: Monte Carlo Income Distribution — 1,000-path 5-year simulation from historical dividend growth rates; SVG fan chart with p5/p25/p50/p75/p95 confidence bands; backend `/api/dividends/monte-carlo` uses numpy (already available as pandas dep)
- Dividend Dashboard: Dividend Sustainability Score — colored dot badge (●) in ticker cell; green <50% payout ratio, amber 50–75%, red >75% or negative EPS; batch Polygon EPS-TTM calls with Semaphore(5) + 6h Redis cache via `/api/dividends/sustainability`
- Dividend Dashboard: DRIP Compound Projection — 10-year two-line SVG chart comparing reinvested vs constant-share income; pure frontend computation using portfolio yield as DRIP compounding factor; shows year-10 bonus income and percentage uplift

## [3.7.82] - 2026-05-23

### Fixed
- Average Down Calculator / Unrealized P&L / YOC: Webull positions expose `avg_entry_price` (per share) instead of `cost_basis` (total); cost was always 0, silently dropping every Webull position from all three features; now computes `cost_basis = avg_entry_price × qty` as fallback when `cost_basis` is missing

## [3.7.81] - 2026-05-23

### Fixed
- Average Down Calculator: positions with no `current_price` field (Webull and others) were silently skipped; now derives price from `market_value ÷ qty` as fallback in both backend and JS so all underwater positions appear correctly

## [3.7.80] - 2026-05-23

### Added
- Dividend Dashboard: YOC (Yield-on-Cost) column in holdings table — annual income ÷ original cost basis per position
- Dividend Dashboard: Unrealized P&L per position (market value vs cost basis) + two new summary stat cards (Total Cost Basis, Unrealized P&L with % on cost basis)
- Dividend Dashboard: Average Down Calculator panel — identifies underwater positions, shows shares/cost to halve the gap, and computes custom average-down from a user-entered budget
- Dividend Dashboard: Dividend Growth Streak badge on ticker symbols — consecutive years of dividend increases sourced from dividend_history (★ gold ≥10yr, ★ green ≥5yr, ↑ blue ≥1yr)
- Dividend Dashboard: Goal Portfolio Allocation panel — user-defined target % per ticker with Add/Remove UI, visual allocation bar vs actual, gap analysis, and buy/trim rebalancing suggestions
- Backend: `/api/dividends/growth-streaks` endpoint computes consecutive growth years per ticker from dividend_history
- Backend: `/api/dividends/targets` GET/POST/DELETE endpoints with `portfolio_targets` DB table for persistent goal allocations
- Backend: `div_holdings` enriched with `current_price`, `unrealized_pnl`, `unrealized_pnl_pct`, `yoc_pct`, and `total_cost_basis`/`total_unrealized_pnl`/`total_unrealized_pnl_pct` in summary

## [3.7.79] - 2026-05-23

### Fixed
- Dividend Holdings fundamentals panel: P/E ratio, EPS, and payout ratio were always blank because `shared.mcp_client` (which imports the `mcp` package) fails silently in the webui container; replaced earnings fetch with a direct Polygon `/vX/reference/financials` REST call (sum of last 4 quarters = EPS TTM), no MCP SDK needed

## [3.7.78] - 2026-05-23

### Added
- Dividend Holdings: click any ticker symbol to expand an inline fundamentals panel showing company description, market cap, P/E ratio, EPS (TTM), payout ratio (computed from forward annual rate / EPS), next earnings date, employee count, and website link; data fetched lazily and cached 5 min per ticker via existing `/api/options/trader/fundamentals/{ticker}` endpoint

## [3.7.77] - 2026-05-23

### Changed
- Trading Hindsights: run history now shows last 20 runs (was 10)

## [3.7.76] - 2026-05-22

### Fixed
- Trading Hindsights: noise_trade now fires for losing trades with no signal backing (confidence=0); late_exit takes priority over noise_trade in categorisation order
- Trading Hindsights: early_exit now computed for option positions using underlying OHLCV peak vs exit underlying price
- Trading Hindsights: option entry date (`op.entry_date`) used for profit-window window calculation instead of close date; also adds `underlying_price` at exit for early-exit detection
- Trading Hindsights: overtrading detection checks both close date and entry date for duplicates
- Trading Hindsights: category cards show `$0.00` (green) instead of `—` when no discipline cost found, making it clear the analysis ran successfully

## [3.7.75] - 2026-05-22

### Fixed
- Trading Hindsights: OHLCV fetch now uses Polygon REST API directly via aiohttp (shared.mcp_client requires `mcp` package not installed in webui); discipline categories now populate correctly

## [3.7.74] - 2026-05-22

### Fixed
- Trading Hindsights: discipline categories (noise trades, early exits, late exits) showed hyphens because OHLCV was fetched via polygon-api-client (not installed); replaced with async `get_massive_daily_bars` MCP call
- `_backtest_rule`: `float()` on LLM filter value crashed when LLM returned a ticker name instead of a number; changed to `_safe_float()`
- Added full traceback to `shadow_run.error` log for easier future debugging

## [3.7.73] - 2026-05-22

### Fixed
- Trading Hindsights: `shadow_run` 500 error when `trades.entry_price` column contains ticker symbols instead of numeric values — SQL now filters rows via `entry_price ~ '^[0-9]+(\.[0-9]*)?$'` and casts to numeric; `_safe_float()` helper guards all remaining `float()` conversions in `_analyze_trade`; performance metrics row now renders correctly after analysis

## [3.7.72] - 2026-05-22

### Added
- Trading Hindsights (Shadow Account): performance metrics row — Sharpe ratio, max drawdown, win rate, profit factor, CAGR (estimated), and cumulative P&L sparkline now displayed after every analysis run
- `.gitignore`: plans directory excluded from git

## [3.7.71] - 2026-05-22

### Added
- EODHD MCP server (`ot-mcp-eodhd`): new container with 10 tools — `get_quote`, `get_eod_bars`, `get_fundamentals`, `get_analyst_consensus`, `get_earnings`, `get_dividends`, `get_insider_transactions`, `get_news`, `get_macro_indicator`, `get_breadth_indicators` (MMFI, MMTH, HIGN, LOWN via `.INDX` exchange suffix)
- `compose.yml`: `mcp-eodhd` service block (builds from `mcp/eodhd-mcp`, injects `EODHD_API_KEY`)
- `shared/mcp_client.py`: `EODHD_MCP_URL` constant
- API Configuration: dedicated EODHD service connector with API Key + MCP URL fields; hint text documents All-in-One package coverage
- Test endpoint `/api/config/test/eodhd` — validates key with live AAPL quote
- `EODHD_API_KEY` and `EODHD_MCP_URL` added to `KNOWN_SECRETS` for import-env migration

### Verified
- EODHD demo key returns live AAPL EOD bar data (breadth indicators require paid subscription)

## [3.7.70] - 2026-05-22

### Added
- Yahoo Finance MCP (`ot-mcp-yahoo`): new `get_analyst_consensus` tool — returns consensus rating, price target (mean/high/low), buy/hold/sell counts, analyst count, and upside %, sourced from Yahoo Finance
- `shared/mcp_client.py`: `YAHOO_MCP_URL` constant + `get_analyst_consensus()` async function routing to Yahoo MCP
- Aggregator: fetches analyst consensus per ticker via Yahoo MCP (4-hour Redis cache), passes to `build_intelligence()` which now populates `analyst_consensus`, `analyst_buy_pct`, `analyst_upside_pct` fields that were previously always zero

### Verified
- AAPL: buy consensus, 48 analysts, \$308.65 price target, 30 buy / 16 hold / 2 sell

## [3.7.69] - 2026-05-22

### Added
- API Configuration: dedicated Alpha Vantage service connector (was embedded inside the Massive connector)
- Test endpoint for Alpha Vantage (`/api/config/test/alpha_vantage`) — validates key and returns AAPL last price
- Test endpoint for Massive now returns result count in success message

### Verified
- Massive.com API returning live data (AAPL OHLCV confirmed)

## [3.7.68] - 2026-05-22

### Removed
- User Configuration: removed "API Keys & Secrets" panel entirely — all credentials are now managed through API Configuration (service connectors) and Broker Configuration
- Removed associated JS functions: `secretsImportEnv`, `secretToggleShow`, `secretSave`, `secretDelete`

## [3.7.67] - 2026-05-22

### Removed
- `POLYGON_API_KEY` removed from KNOWN_SECRETS and retired from user_secrets DB — no code reads it; `MASSIVE_API_KEY` (Massive connector, formerly Polygon.io) is the active key

## [3.7.66] - 2026-05-22

### Changed
- API Configuration connector modal: button renamed "Save to .env" → "Save"; subtitle updated to "Credentials saved to your profile"
- Broker Configuration modal: same renames — "Save to .env" → "Save", subtitle updated
- Broker Configuration page subtitle: updated from ".env restart" note to profile/agent sync message

## [3.7.65] - 2026-05-22

### Changed
- API Configuration: moved Configure/Test action buttons to immediately right of the Service name column

## [3.7.64] - 2026-05-22

### Changed
- API Configuration: Service Connectors changed from tile grid to table view, sorted alphabetically by service name

## [3.7.63] - 2026-05-22

### Fixed
- User Configuration / API Keys & Secrets: keys managed by API Configuration connectors are now filtered from the profile display (CFG_META-derived exclusion list), restoring the de-duplication that was inadvertently reverted when KNOWN_SECRETS was expanded for the import-env migration in v3.7.61

## [3.7.62] - 2026-05-22

### Fixed
- User Configuration / API Keys & Secrets: `CLOUDFLARE_TUNNEL_TOKEN` deleted from `user_secrets` DB at startup — removes the stale entry for users who had it saved before v3.7.60

### Changed
- Broker Configuration (Tradier, Alpaca, Webull): removed "API keys are managed in User Configuration" notification box from all three broker panels — keys are now managed in API Configuration

## [3.7.61] - 2026-05-22

### Changed
- API Configuration / System Settings: Save Settings now stores values in user profile (user_secrets DB) instead of writing to `.env` directly; toast updated accordingly
- Startup: after loading user secrets into `os.environ`, also sync them back to `.env` so non-webui agents (broker_gateway, scheduler, traders) see the current values after container restart
- KNOWN_SECRETS expanded from 9 to 45 keys — now covers all API Configuration connector fields (OpenRouter, Telegram, Discord, AgentMail, OVTLYR, Massive, Unusual Whales, Alpha Vantage, EODData, Google Books, Alpaca MCP) so the "Import from .env" migration tool picks them all up
- EODData breadth indicator lookup: use `os.getenv("EODDATA_API_KEY")` instead of `_read_env_file()` — reads from the in-memory env seeded from user_secrets at login

## [3.7.60] - 2026-05-22

### Changed
- User Configuration / API Keys & Secrets: removed `CLOUDFLARE_TUNNEL_TOKEN` — it is server infrastructure, set in `.env` at deployment time, not a user profile secret

## [3.7.59] - 2026-05-22

### Changed
- User Configuration / API Keys & Secrets: removed all keys that have a dedicated connector in API Configuration (Massive, Alpha Vantage, Unusual Whales, OpenRouter, Telegram, Discord, AgentMail, OVTLYR, Google Books); only `POLYGON_API_KEY` and `CLOUDFLARE_TUNNEL_TOKEN` remain as profile-level keys
- API Configuration: connector Configure modal now loads values from `user_secrets` DB via `/api/user/secrets/batch-reveal`; Save writes to `user_secrets` DB via `/api/user/secrets/batch` (same path as all other credential saves); connector status cards also load from DB
- API Configuration: Save toast updated from "Settings written to .env" to "Credentials stored in your profile"

## [3.7.58] - 2026-05-22

### Changed
- Nav: renamed "System Configuration" to "API Configuration"; updated page subtitle from "saved to .env" to "saved to your profile and synced to agents"

## [3.7.57] - 2026-05-22

### Changed
- Broker Configuration: Configure button now loads credentials from `user_secrets` DB via new `/api/user/secrets/batch-reveal` endpoint — eliminates "Failed to load credentials: Invalid token" error caused by stale session cookies hitting the old token-auth `/api/broker/env/reveal` path
- Broker Configuration: Save button now stores all broker fields (API keys, account numbers, display names, IRA flags) to `user_secrets` DB via new `/api/user/secrets/batch` endpoint; credentials are synced to `.env` for agent consumption and `ot-broker-gateway` is restarted automatically
- Broker Configuration: removed `if (!token) return` guard from `saveBrokerCfg` — was silently blocking all saves
- User Configuration / API Keys & Secrets: removed "Brokers" section — broker credentials are now exclusively managed through the Broker Configuration panel; broker-managed keys are flagged in the backend and filtered from the profile secrets grid

### Added
- `POST /api/user/secrets/batch-reveal` — batch decrypt any list of keys from `user_secrets`; session-cookie auth via `_resolve_user_id`
- `POST /api/user/secrets/batch` — batch upsert multiple keys to `user_secrets`; syncs to `.env` and restarts broker-gateway when broker keys change
- `_sync_secrets_to_env(user_id)` — writes all user secrets to `.env` so non-webui agents pick them up on restart

## [3.7.56] - 2026-05-22

### Fixed
- System Configuration: Service Connector cards showed "API Key not set" for all connectors — `reveal_broker_env` only checked the `.env` file and `os.environ` (which is only seeded from the DB at login); after a container restart, DB-stored secrets (set via My Profile) were absent from `os.environ` until re-login; endpoint now queries the `user_secrets` table directly so connector status reflects the actual stored values regardless of restart state

## [3.7.55] - 2026-05-22

### Changed
- System Configuration: EODData panel now shows the `eoddata_avatar.png` logo instead of the letter fallback

## [3.7.54] - 2026-05-22

### Fixed
- Broker Configuration: Configure button showed "Failed to load credentials: Invalid token" for all three brokers — `/api/broker/env/reveal` and `/api/broker/env` were checking `body.token` against `WEBUI_TOKEN` but the browser sends an empty token (session-cookie auth); both endpoints now accept a valid JWT session cookie as an alternative to `WEBUI_TOKEN`

## [3.7.53] - 2026-05-22

### Fixed
- `ot-mcp-tradingview`: `ModuleNotFoundError: No module named 'pkg_resources'` on startup — `setuptools` (which provides `pkg_resources`) is not included in Python 3.12 slim images; added explicit `pip install setuptools` before the package install in the Dockerfile

## [3.7.52] - 2026-05-22

### Changed
- Nav: added "Configuration" collapsible parent menu in Platform section containing Broker Configuration, System Configuration, and User Configuration as sub-items; auto-opens when any of the three pages is navigated to
- Broker Configuration page: broker cards now stack vertically (single column, max 860px wide) so all account text is readable
- Broker Configuration: fixed Configure button — was silently no-op because `getToken()` returns empty string (session-cookie auth); removed the now-incorrect `if (!token) return` guard

## [3.7.51] - 2026-05-22

### Changed
- Nav: renamed "Broker" to "Broker Configuration"

## [3.7.50] - 2026-05-22

### Changed
- Nav: renamed "Configuration" to "System Configuration"
- Nav: Broker dashboard now enforced in Platform section via guaranteed DOM placement (survives saved nav_order from old position in Trading)
- System Configuration page: added EODData connector card (EODDATA_API_KEY — breadth indicator fallback source)

## [3.7.49] - 2026-05-22

### Changed
- Nav: moved Broker dashboard from Trading section to Platform section

## [3.7.48] - 2026-05-22

### Changed
- Webull broker setup: API Keys row now has a blue `?` help balloon that opens the Webull Individual Application API authentication docs

## [3.7.47] - 2026-05-22

### Fixed
- Webull 404 on `/trade/order/list` retried 3× per poll cycle (wasteful, noisy); 404 is now non-retryable in the client — raises immediately with a clear "endpoint not available" message
- `get_orders` now returns an empty list on any exception (404 or otherwise) rather than propagating to the broker gateway order-poll loop; accounts without trading API access no longer spam error logs every 60 s
- Webull setup panel: added info note explaining that v1 (API Key + Secret) and v2 (App Key + App Secret) are both required, and that 404 errors on order/position endpoints indicate a Market Data–only API subscription that needs to be upgraded to include trading API access

## [3.7.46] - 2026-05-22

### Fixed
- Options dashboard: Webull positions no longer listed — `_fetch_option_chain_details` used the `polygon` Python SDK (`from polygon import RESTClient`) which is not installed in the container; replaced with a direct `aiohttp` call to Polygon's v3/snapshot/options REST endpoint, matching the pattern used elsewhere in the codebase

## [3.7.45] - 2026-05-19

### Changed
- Market Health dashboard default tickers updated to include Nasdaq breadth companions and TRIN:
  `PCL, MMFI, MMTH, HIGN, MAHQ, LOWN, LOWQ, TRIN`
  - Added `MAHQ` (52-Week Highs Nasdaq) — pairs with `HIGN` (NYSE) to show both exchange new-high counts
  - Added `LOWQ` (Nasdaq New Lows) — pairs with `LOWN` (NYSE) to show both exchange new-low counts
  - Added `TRIN` (NYSE Arms Index) — volume-weighted advance/decline ratio; <0.7 bullish, >1.2 bearish
  - Removed `VIX` from defaults (already visible on Trading Dashboard macro panel)
  - All three new symbols served by existing EODData fallback; no backend changes required
- Storage keys bumped to `mh_tickers_v2` / `mh_order_v2` so existing users receive the new defaults

## [3.7.44] - 2026-05-18

### Fixed
- Futures Market Map currencies: `DX=F` replaced with `DX-Y.NYB` (Yahoo Finance's correct Dollar Index ticker); `DX=F` returned no data
- Futures Market Map: change % now computed from unrounded raw price to avoid precision loss on small-priced futures like `6J=F` (Japanese Yen at ~0.0063 — previously rounded to 0.01, producing a spurious +58% change)

## [3.7.43] - 2026-05-18

### Fixed
- Futures sector map (Futures Market Map) now shows live prices and change % via Yahoo Finance quote API; previously all tiles showed 0% because Polygon's stock snapshot endpoint doesn't accept `=F`-format futures tickers
- Sector map sparklines now fall back to Yahoo Finance chart API for any ticker Polygon can't serve (including all futures)

## [3.7.39] - 2026-05-18

### Fixed
- Market Health breadth charts (MMFI, MMTH, HIGN, LOWN): all four now reliably populate on simultaneous page load; EODData requests are serialized via asyncio semaphore and cached 15 min to prevent rate-limit drops
- EODData integration uses `EODDATA_API_KEY` (not username/password); HIGN mapped to `MAHN` (52-Week Highs NYSE); history clamped to 2026-01-01 to match plan limitations

## [3.7.38] - 2026-05-18

### Changed
- Market Health bars: EODData.com (`api.eoddata.com`) added as second-priority source; carries MMFI, MMTH, HIGN (→MAHN), LOWN on INDEX exchange — activate by adding `EODDATA_API_KEY` to `.env`

## [3.7.37] - 2026-05-17

### Changed
- Market Health bars: Tradier removed as breadth data source (doesn't carry breadth symbols); intermediate fallbacks evaluated before settling on EODData

## [3.7.36] - 2026-05-17

### Changed
- Market Health bars: Tradier removed as second-priority data source for breadth indicators

## [3.7.35] - 2026-05-17

### Changed
- Market Health charts: Tradier `/markets/history` added as second-priority data source (after Polygon, before Yahoo Finance), enabling breadth indicators MMFI, MMTH, HIGN, LOWN to populate from Tradier

### Fixed
- WebUI session persistence: added stable `SECRET_KEY` to `.env` so session cookies survive container restarts

## [3.7.34] - 2026-05-15

### Added
- Market Health: double-click any chart card to open it fullscreen; EMA lines, right-side labels, and price meta all render at full viewport size; close with Esc or the × button

## [3.7.33] - 2026-05-15

### Fixed
- `/api/market/bars` now falls through two extra sources when Polygon returns no data: (1) Polygon index format `I:{ticker}` (fixes VIX), (2) Yahoo Finance chart API with `{ticker}` then `^{ticker}` (fixes MMFI, MMTH, HIGN, LOWN and other breadth/volatility indices)

## [3.7.32] - 2026-05-15

### Fixed
- Market Health: EMA labels (name + last value) now pinned to the right edge of each chart via HTML overlay; removed from crosshair top-left legend

## [3.7.31] - 2026-05-15

### Fixed
- Market Health: EMA labels now display on the right price axis of each chart

## [3.7.30] - 2026-05-15

### Added
- **Market Health** dashboard under Sector Insight: 2-wide draggable chart grid for PCL, MMFI, MMTH, HIGN, LOWN, VIX with EMA 10 (white) / 20 (blue) / 50 (green) / 200 (red) overlaid on candlestick bars
- Ticker add/remove controls — new tickers persist via localStorage; card order also persisted and restored across sessions
- Bumped Polygon.io `limit` from 365→750 bars to fully warm up EMA200 (needs ~200+ trading days of history)

## [3.7.29] - 2026-05-15

### Added
- CI pipeline (`.github/workflows/ci.yml`): lint (ruff), test (pytest), and Docker build jobs run on every push and PR to main
- `ruff.toml` lint config: E9 + pyflakes rules, line length 120, `mcp/` excluded
- CLAUDE.md: CI/branch rules, branch protection guidance, test file conventions

## [3.7.28] - 2026-05-13

### Fixed
- Fresh-install startup errors: Caddy service moved to `profiles: [caddy]` so it no longer starts by default and no longer blocks `webui` dependency resolution on first bring-up
- Hardcoded `/run/user/1003/podman/podman.sock` replaced with `${PODMAN_SOCK:-/run/user/1003/podman/podman.sock}` in `orchestrator` and `webui` services; set `PODMAN_SOCK` in `.env` if your UID differs
- Added `PODMAN_SOCK` entry to `.env.sample` with instructions

## [3.7.27] - 2026-05-12

### Changed
- Reports > Daily Report (renamed from "1pm Options Report") — schedule is now fully user-configurable with day-of-week checkboxes and hour/minute inputs
- Added content toggles: Include Options positions, Include Stock positions, Include Earnings dates, Include Ex-dividend dates
- Daily report config persists to `report_config` DB table; no longer lost on container restart
- `_build_daily_report_html` replaces `_build_options_report_html`; renders optional equity section and earnings/ex-div columns based on saved config
- Scheduler `_apply_config_overrides` now handles CronTrigger (hour/minute/days) for user-overridden report schedule
- Preview and auto-send paths read config from DB and conditionally fetch equity positions and ex-div dates

## [3.7.26] - 2026-05-12

### Added
- Live mode acknowledgment gate — switching the Trade Mode toggle to "live" now requires typing `I understand the risks and accept full responsibility` exactly before any broker connection is opened
- `live_mode_ack` DB table stores the acknowledgment with timestamp, typed phrase, and SHA-256 of `RISK_DISCLOSURE.md`; a changed disclosure invalidates the record and forces re-acknowledgment
- `GET /api/live-mode/ack-status` and `POST /api/live-mode/acknowledge` endpoints
- `POST /api/trade-mode` now returns 403 `acknowledgment_required` if no valid ack exists when switching to live
- Full-screen acknowledgment modal (first-time / re-ack) with 3-attempt limit and clear error messages
- Brief reminder modal (already acknowledged) showing the original acknowledgment date

## [3.7.25] - 2026-05-12

### Added
- Risk warning blockquote at the top of `README.md` linking to `RISK_DISCLOSURE.md` and `TERMS.md`

## [3.7.24] - 2026-05-12

### Added
- `RISK_DISCLOSURE.md` — 10-section risk disclosure document covering capital loss, no-advice disclaimer, user responsibility, algorithmic failure modes, backtest limitations, third-party broker risk, API key security, regulatory risk, and a pre-live checklist

## [3.7.23] - 2026-05-12

### Added
- Terms of Use link on the login page — clicking opens a full-screen scrollable modal containing all 14 sections of the legal agreement (no warranties, assumption of risk, limitation of liability, governing law, etc.)

## [3.7.22] - 2026-05-11

### Added
- Reporting page in Platform nav section — shows both scheduled reports (1pm Options and 4:05pm EOD) with last-sent status, channel config, enable toggle, Preview and Send Now actions
- `report_log` DB table to track every report send/skip with HTML/text body, recipient, channels, and meta (position_count, trade_count)
- API endpoints: `GET/POST /api/reports/config`, `GET /api/reports/history`, `GET /api/reports/entry/{id}`, `GET /api/reports/preview/options`, `GET /api/reports/preview/eod`, `POST /api/reports/trigger/options`, `POST /api/reports/trigger/eod`, `POST /api/reports/log`
- `review/main.py` now POSTs to `/api/reports/log` after each EOD report send so history is captured automatically
- Legacy `review_log` rows surfaced in Report History (backfill for pre-3.7.22 EOD reports)

## [3.7.21] - 2026-05-11

### Fixed
- Sidebar logo now shares the same flex row as "OpenTrader" title text so it aligns with the title baseline rather than floating between the title and subtitle

## [3.7.20] - 2026-05-11

### Changed
- Sidebar brand: logo moved inline to the right of "OpenTrader" title instead of centered above it

## [3.7.19] - 2026-05-11

### Fixed
- Logo white background: flood-filled opaque white pixels to transparent in logo-opentrader.png

## [3.7.18] - 2026-05-11

### Changed
- Updated setup.html logo: replaced ⚡ emoji with blue arrow image to match login and sidebar

## [3.7.17] - 2026-05-11

### Changed
- Replaced inline bull/bear SVG logo with `blue_arrow.png` on login page and sidebar

## [3.7.16] - 2026-05-11

### Fixed — Platform topology cleanup
- Removed `scrape_wsb`, `scrape_seekalpha`, `scrape_ovtlyr`, `run_predictor` labels from scheduler trigger edges — dashed arrows convey the trigger relationship without floating midpoint labels
- Moved `Scheduler` node off the scrapers' x-column so trigger edges are diagonal curves rather than invisible vertical lines
- Added 3 missing scraper nodes: `AV News` (scraper-news), `ETF Flows` (scraper-etf-flows), `Macro Regime` (scraper-macro-regime) with scheduler trigger and heartbeat edges
- Updated `resetTopoLayout` defaults to match new positions

## [3.7.15] - 2026-05-11

### Fixed
- Portfolio Optimizer: replaced `from polygon import RESTClient` with direct `urllib.request` HTTP calls to Polygon REST API — `polygon-api-client` package was never installed in the webui container
- Backtest Runner: same fix — replaced RESTClient with direct HTTP calls; bar fields changed from object attributes (`b.close`) to dict keys (`b["c"]`)

## [3.7.14] - 2026-05-11

### Changed — Documentation refresh
- README: removed all Yahoo Finance references; updated services table to match actual compose.yml containers (27 services); added Ticker Classification section; updated Market Intelligence Pipeline and Dividend Data Sources tables; fixed pre-built image pull commands; updated MCP Agents list to reflect Massive.com as primary data source
- PROJECT_STATE: full rewrite to reflect current architecture (correct container list, MCP servers, scraper list, ticker_classification DB table, updated scheduler jobs)
- CLAUDE.md: committed previously staged working-style and workflow additions

## [3.7.13] - 2026-05-11

### Removed — Phase 5: Yahoo Finance frontend cleanup
- Removed `scraper-yahoo`, `scraper-yahoo-sentiment`, `mcp-yahoo` from service filter dropdown
- Removed `yahoo_trending` strategy signal entry and description from strategy config UI
- Removed 3 Yahoo topology nodes (`scraper-yahoo`, `scraper-yahoo-sentiment`, `mcp-yahoo`) and all 7 associated edges from topology diagram
- Removed Yahoo node saved layout coordinates from `resetTopoLayout()`
- Removed `yahoo_trending` from `STRATEGY_SIGNALS` array and `SIGNAL_COLORS` map
- Relabeled `// Yahoo Finance GICS sector names` and industry taxonomy comments to `// GICS` (data unchanged — used by Exclusions UI)
- Renamed `yfinance Est` column header in dividend income table to `History Est`
- Cleaned up stale `yfinance fallback` JS comments in dividend forecast

## [3.7.12] - 2026-05-11

### Added
- `ticker_classification` DB table — stores sector, industry, market_type per ticker (primary key: ticker, updated at most every 30 days)
- `_fetch_classification_yahoo()` — direct HTTP call to Yahoo Finance quoteSummary API (`assetProfile` module) returning proper GICS sector + industry for equities
- `_enrich_ticker_classifications()` — background task that runs 15s after startup and on demand; collects all unique underlyings from active `option_positions` + `broker:position_tickers`, skips tickers updated within 30 days, fetches from Yahoo, writes to DB and populates `ticker:sectors` / `ticker:industries` Redis hashes
- `GET /api/market/ticker-classifications` — returns all stored classifications
- `POST /api/market/ticker-classifications/refresh?token=...` — triggers enrichment on demand
- `get_position_sector_map` now checks `ticker_classification` DB (step 4b) before falling back to Polygon SIC mapping

## [3.7.11] - 2026-05-11

### Removed — Phase 4: Yahoo Finance container & config teardown
- `ot-mcp-yahoo` service removed from compose.yml and CI build matrix
- `ot-scraper-yahoo` service removed from compose.yml
- `ot-scraper-yahoo-sentiment` service removed from compose.yml
- `YAHOO_MCP_URL` env var removed from options-monitor and chat-agent in compose.yml
- `yahoo=...` entry removed from chat-agent `MCP_SERVERS`
- `mcp-yahoo` dependency removed from options-monitor and chat-agent `depends_on`
- `scraper-yahoo`, `scraper-yahoo-sentiment`, `mcp-yahoo` removed from orchestrator `CONTAINER_MAP`
- `scraper-yahoo`, `scraper-yahoo-sentiment` consumer groups removed from `shared/redis_client.py`
- `YAHOO_MCP_URL` constant removed from `shared/mcp_client.py` and `webui/main.py`
- `yfinance>=0.2.36` removed from `requirements.txt` and `requirements.webui.txt`

### Changed — remaining yfinance callers replaced or dropped
- `get_market_bars()` yfinance fallback dropped (Polygon REST is primary and reliable)
- Options dashboard price fill yfinance fallback dropped (Polygon handles all equities)
- Quick Intel `_generate_stock_analysis()` yfinance bars fallback dropped
- Options dashboard signal 3rd-tier: replaced yfinance `recommendationKey` with Massive `get_analyst_consensus` (Benzinga ratings, 4-hr Redis cache under `consensus:{sym}`)
- Quick Intel "Record Reflection" price fetch: replaced yfinance with Polygon REST `get_aggs` for 5-day return and alpha vs SPY calculation

## [3.7.10] - 2026-05-11

### Added
- Bull & bear facing-off SVG logo (blue outlined) on login page replacing the ⚡ icon
- Same logo added to left navigation sidebar above OpenTrader title

### Fixed
- Quick Intel "Analysis unavailable: bullish map is not a function" — `bullish_factors`/`bearish_factors` columns are TEXT JSON in DB; now parsed with `json.loads()` before returning to frontend

## [3.7.9] - 2026-05-11

### Added
- **Massive MCP — 3 new tools**: `get_ticker_news` (Polygon/Benzinga headlines), `get_short_interest` (shares short, days-to-cover), `get_analyst_consensus` (Benzinga consensus rating + price target)

### Removed
- **Yahoo Finance Phase 3 — 3f, 3j, 3k, 3l**:
  - **3f** `main.py` dividend functions: removed `_div_fetch_yahoo` / `_div_fetch_yahoo_sync`; dividend backfill now uses Polygon `get_dividends`; SGOV ex-div alert uses Massive MCP `get_dividends`
  - **3j** `chat_agent/commands.py`: replaced all Yahoo Finance commands with Massive equivalents (`!quote`/`!q` → Massive `get_quote`, `!news` → `get_ticker_news`, `!short` → `get_short_interest`, `!consensus` → `get_analyst_consensus`, `!earnings` → `get_earnings`, `!divs` → `get_dividends`); removed Yahoo-only commands (trending, history, options, chain, financials, holders, upgrades, recommend)
  - **3k** `_fetch_fundamentals`: replaced yfinance insider_transactions with Massive `get_analyst_consensus` + `get_short_interest`; function is now async and uses MCP directly
  - **3l** `get_macro_news`: replaced yfinance SPY/QQQ/^VIX news with Polygon `get_ticker_news` for SPY and QQQ

## [3.7.8] - 2026-05-11

### Removed
- **Yahoo Finance Phase 3 — caller migration (3a–3i)**: replaced all remaining yfinance / Yahoo MCP calls with Polygon.io (Massive MCP) equivalents:
  - `backtest_runner.py`: replaced `yf.download()` with Polygon `get_aggs` via REST client
  - `portfolio_optimizer.py`: replaced `yf.download()` multi-ticker with per-ticker Polygon `get_aggs`
  - `shadow_account.py`: replaced `yf.Ticker().history()` with Polygon `get_aggs`
  - `ml_predictor.py`: replaced `yf.download(period="2y")` with Polygon `get_aggs(days=730)`
  - `aggregator/combiner.py`: replaced `fetch_yfinance` / `_fetch_yfinance_sync` with `fetch_massive_fundamentals` (Massive MCP `get_dividends` + `get_earnings`); removed `yahoo` from `SOURCE_WEIGHTS`, renormalized to wsb=0.38 / seekalpha=0.62
  - `aggregator/main.py`: renamed cache key `aggregator:yf:{ticker}` → `aggregator:massive:{ticker}`; wired `fetch_massive_fundamentals`
  - `webui/main.py` `get_trader_ticker_meta`: replaced yfinance price/ex-div/earnings with Massive MCP `get_quote` + `get_dividends` + `get_earnings`
  - `webui/main.py` `get_trader_fundamentals`: replaced `_fetch_earnings` yfinance block with Massive MCP `get_earnings`
  - `shared/mcp_client.py` `get_avg_volume`: switched from `YAHOO_MCP_URL` to `MASSIVE_MCP_URL`
  - `options_monitor/main.py`: replaced Yahoo MCP `get_stock_info` with Massive `get_quote` for price; `get_earnings` for earnings date; Polygon `list_snapshot_options_chain` for contract detail enrichment (replaces Yahoo `get_option_expiration_dates` + `get_option_chain`)

## [3.7.7] - 2026-05-11

### Removed
- **Yahoo Finance Phase 1 — safe fallback removal**: dropped 5 yfinance code blocks that were last-resort fallbacks with Polygon/Massive already covering the primary path:
  - Sector heatmap 5-day price batch (`main.py`) — Polygon is primary
  - Options position price fill batch (`main.py`) — Polygon is primary
  - SPY YTD calculation (`main.py`) — Polygon is primary
  - Price alert ticker price fetch (`main.py`) — Polygon is primary
  - Options chain last-resort (`main.py`) — Tradier/broker chain is sufficient
- **Yahoo Finance Phase 1 — GICS classification fallback** (`shared/mcp_client.py`) — removed Yahoo MCP `get_classification` call; Massive MCP SIC mapping is the only source now

## [3.7.6] - 2026-05-11

### Added
- **Massive MCP — `get_ohlcv_history`** — up to 2 years (730 days) of adjusted daily OHLCV bars; primary replacement for yfinance in ML predictor, backtesting, and portfolio optimizer
- **Massive MCP — `get_avg_volume`** — average daily trading volume over N days (default 30); replaces Yahoo MCP `get_avg_volume` used in options liquidity screening
- **Massive MCP — `get_dividends`** — dividend history (ex-date, pay-date, cash amount, frequency, type) via Polygon `list_dividends`; replaces yfinance `Ticker.dividends`
- **Massive MCP — `get_splits`** — stock split history via Polygon `list_splits`
- **Massive MCP — `get_earnings`** — upcoming and recent earnings dates, EPS/revenue estimates and actuals via Polygon Benzinga `list_benzinga_earnings`; replaces yfinance `info.get("earningsDate")`

## [3.7.5] - 2026-05-11

### Changed
- **Charts — OVTLYR signal card** — moved OVTLYR signal from a small badge in the Sentiment header to its own full card sitting alongside the Sentiment card; shows BUY/SELL in large text with a "Since YYYY-MM-DD" date line; card is hidden when no signal exists for the ticker

## [3.7.4] - 2026-05-11

### Fixed
- **Trade Directives "invalid token"** — stale `ot_token` in localStorage (from the old token-based auth) was being injected into every `apiFetch` call; `check_token` rejected it because it no longer matches the current `WEBUI_TOKEN`; `getToken()` now clears the key and always returns empty string — all auth goes through the session cookie
- **Charts — OVTLYR signal** — added live OVTLYR buy/sell signal badge in the Sentiment card header; fetches fresh from `/api/ovtlyr/ticker/{sym}` on every chart load (no client-side cache); badge is green for BUY, red for SELL, hidden if no signal exists for the ticker

## [3.7.3] - 2026-05-11

### Changed
- **README** — refreshed installation instructions (first-run `/setup` flow, `SECRET_KEY` guidance, pre-built image version updated to 3.7.2); updated feature list (secure login system, encrypted secret storage, Quick Intel, Charts position picker with Alpaca OCC resolution, My Profile page); architecture diagram updated to note auth layer

## [3.7.2] - 2026-05-11

### Fixed
- **CI build failure (ot-mcp-yahoo)** — `Dockerfile.http` used `uv pip install --system -e .` (editable install) which requires `server.py` to be present at build time via hatchling; replaced with direct `pip install` of the two dependencies (`mcp[cli]`, `yfinance`), eliminating the editable-install complexity entirely

## [3.7.1] - 2026-05-11

### Fixed
- **Charts / nav label readability** — nav section labels (Trading, Equities, Options, etc.) were using `--dim` color (`#3d4566`) making them hard to read; changed to `--muted` (`#6b7394`)
- **Charts / Alpaca options positions** — position pills used the raw OCC contract ID (e.g. `AEHR250117C00003000`) as the chart symbol causing the chart to fail; the backend now extracts the underlying ticker from OCC contract IDs

## [3.7.0] - 2026-05-11

### Fixed
- **WebUI token prompt on every login (v2)** — the `prompt('Enter WebUI token:')` dialog still appeared on fresh installs with the new username/password auth because `getToken()` prompts whenever `localStorage` has no `ot_token`; removed the prompt entirely — session cookie auth makes the WEBUI_TOKEN unnecessary for normal use; updated `check_token()` to only reject an *explicitly wrong* token (empty token is now allowed since the middleware already verified the JWT session cookie)
- **Fresh-install build failure** — `mcp/tradingview-mcp/Dockerfile` pulled from `ghcr.io` (GitHub Container Registry) and ran `git clone` + `uv pip install` during build, any of which can fail on restricted networks; switched to `docker.io/python:3.12-slim` base with standard `pip`; `mcp/yahoo-finance-mcp/Dockerfile.http` used a BuildKit `--mount=type=cache` directive unsupported by some podman-compose versions, and also pulled from `ghcr.io`; switched to `docker.io/python:3.12-slim` with `pip install uv` instead
- **Lint: 101 errors across 5 files** — fixed all F821 (6 undefined names: `redis` and `aiohttp` used before assignment in 5 endpoints), F841 (9 unused variables), F601 (duplicate AMZN/TSLA dict keys), E741 (2 ambiguous `l` variable names), E731 (4 lambda assignments), E402 (6 module-level imports not at top)

## [3.6.99] - 2026-05-11

### Fixed
- **WebUI token prompt on every login** — token was stored in `sessionStorage` (cleared on tab/browser close); switched to `localStorage` so the token persists across sessions and the prompt only appears once

## [3.6.98] - 2026-05-11

### Fixed
- **OVTLYR signal missing from option report for Alpaca positions** — Alpaca option positions report their symbol as the OCC contract string (e.g. `AEHR260529C00080000`) rather than the underlying ticker; `broker:position_tickers` was storing the OCC symbol verbatim so the OVTLYR scraper was looking up the contract string on OVTLYR (which returns nothing) instead of the underlying `AEHR`; fixed by stripping OCC suffixes to the underlying when writing the tickers key; also added DB fallback to the report email function so stale Redis misses still surface a signal from `ovtlyr_intel`

## [3.6.97] - 2026-05-10

### Fixed
- **Options chain headers invisible** — `var(--card-header)` is a CSS class not a variable so it resolved to transparent; replaced with `var(--surface)` (#13161f) giving headers a solid opaque background

## [3.6.96] - 2026-05-10

### Fixed
- **Options chain sticky headers** — column headings now stay visible while scrolling; moved `position:sticky;top:0` from `<tr>` (not cross-browser) to each `<th>` individually, and added solid `background:var(--card-header)` so rows below don't show through

## [3.6.95] - 2026-05-10

### Changed
- **Trading Hindsights** — renamed from "Equities Hindsight" and moved from the Equities nav group to the Resources nav group

## [3.6.94] - 2026-05-10

### Fixed
- **Equities Hindsight (shadow account) broken** — `shadow_account.py` was not mounted into the `ot-webui` container; every `/api/shadow/run` POST returned HTTP 500 with `ModuleNotFoundError: No module named 'webui.shadow_account'`
- **`portfolio_optimizer.py` not mounted** — same gap; would fail when portfolio optimization endpoint was called
- **`shared/crypto.py` and `shared/db_retry.py` missing from container** — added bind-mount of entire `python/shared/` directory so all shared modules are live-reloaded without a container rebuild
- Fixed in `compose.yml`: added three volume entries; container restarted with correct mounts

## [3.6.93] - 2026-05-10

### Changed
- **Webull market-data endpoints** — migrated three unofficial internal API paths to the official Webull OpenAPI v2 paths in `python/brokers/webull/connector.py`:
  - Option expiration dates: `/quotes/option/queryExpireDates` → `/openapi/market-data/v1/options/expiration-dates` (`ticker` param)
  - Real-time quote: `/quotes/ticker/getTickerRealTime` → `/openapi/market-data/v1/snapshot/quotes` (`tickers` param, handles list response unwrapping)
  - Option chain by expiry: `/quotes/option/queryOptionByExpireDate` → `/openapi/market-data/v1/options/chain` (`ticker` + `expiration_date` params)
  - All three calls now use `get_v2()` (APP_KEY/APP_SECRET + `x-version: v2` header) instead of the unsigned `get()` method
  - Field extraction updated for official API field names (`strike_price`, `option_type`, `bid_price`, `ask_price`, `last_price`, `open_interest`, `implied_volatility`, nested `greeks` object) with fallbacks for legacy field names

## [3.6.92] - 2026-05-10

### Removed
- **Polymarket** — removed all Polymarket code: `/api/polymarket/summary` and `/api/polymarket/trades` endpoints deleted from `main.py`; `page-polymarket` HTML, nav item, `PAGE_TITLES` entry, `navigate` case, and `loadPolymarketPage()` JS function all removed from `index.html`

## [3.6.91] - 2026-05-10

### Added
- **Dashboard enhancements (Wave 4)** — Platform Dashboard circuit breaker gauge:
  - **Loss Limit gauge** — new stat card on Platform Dashboard showing daily loss limit proximity (% used) with color-coded mini progress bar (green→amber→orange→red); turns red if circuit breaker tripped; clicking navigates to Trading Dashboard
  - `loadPlatformDailyPnl` now also updates the loss limit gauge from the same `/api/trading/daily-pnl` response

## [3.6.90] - 2026-05-10

### Added
- **Dashboard enhancements (Wave 3)** — backtest strategy label, OVTLYR consolidation:
  - **Backtest chart and results** now show the correct strategy name (`EMA 10/21 Crossover`, `RSI Mean Reversion`, `Volatility Breakout (ATR)`) in both the PNG chart title and the results panel header; `_build_chart` accepts `strategy_label` param
  - **`_ovtlyrSignalRow()` shared helper** — extracted duplicated cell-building logic from `loadOvtlyrListSignals` into a reusable function, reducing duplication between Trading Dashboard and Options Trader OVTLYR signal panels

## [3.6.89] - 2026-05-10

### Added
- **Dashboard enhancements (Wave 2)** — Macro Hub page, sentiment breakdown, real-time P&L polling:
  - **Macro Regime Hub page** (`page-macro-hub`) — dedicated full-page view consolidating regime state, 5 signal tiles (SPY/VIX/DXY/TLT/OVTLYR Breadth), 30-day regime score sparkline SVG, and sortable history table; "Full View →" link on Trading Dashboard widget
  - **Sentiment 5-component breakdown** in Quick Intel panel — automatically appended after analysis loads; shows Composite, RSI Score, MA Cross, Momentum, and Vol Score as horizontal bar chart with fear/neutral/greed color coding
  - **Real-time P&L auto-refresh** — 30-second polling timer starts when Trading Dashboard is active, stops when navigating away; updates Daily P&L widget including the 10-day sparkline
  - **Review Log** now loads on Equities Hindsight page navigation alongside shadow account run history

## [3.6.88] - 2026-05-10

### Added
- **Dashboard enhancements (Wave 1)** — backend API additions and backtester strategy engine expansion:
  - `/api/trading/daily-loss-history` — 10-day P&L history from `daily_loss_log` table for sparkline chart in Daily P&L widget
  - `/api/review/recommendations` — surfaces discipline review data from `review_log` table (findings + recommendations + applied status)
  - `/api/polymarket/summary` — Polymarket open positions with unrealized P&L, total exposure, realized P&L summary
  - `/api/polymarket/trades` — Polymarket trade history with market context
  - `/api/market/etf-flows/anomalies` — ETF flow z-score anomaly detection (configurable σ threshold, default 2.0, uses 30-day rolling mean/stddev)
  - `BacktestRunBody` extended with `strategy`, `direction`, RSI params, and volatility breakout params; `quick_backtest` now forwards all strategy params
  - **RSI Mean Reversion strategy** — `RSIMeanReversionStrategy` in `backtest_runner.py`; buys oversold (RSI < 30), exits above 50, shorts overbought with stop/TP
  - **Volatility Breakout strategy** — `VolatilityBreakoutStrategy`; enters on ATR-multiple breakout above N-day high/low, ATR-based stop-loss
  - `_run_on_df` now accepts `strategy` parameter; `result` includes `strategy_label` field
- **Backtester page UI** — strategy selector (EMA / RSI / Volatility Breakout), strategy-specific param panels (RSI: period/oversold/overbought; VB: lookback/atr_mult/stop_atr), "Compare All" button runs all 3 strategies in parallel and renders comparison table ranked by Sharpe
- **ETF Capital Flows** — "⚡ Anomalies" toggle button opens inline anomaly panel showing ETFs with >2σ flow deviation from 30-day mean; displays z-score, direction (inflow/outflow), and day %
- **Daily P&L widget** — enhanced with 10-day mini sparkline bar chart from `daily_loss_log`; green/red bars per day with tooltip showing date and P&L
- **Equities Hindsight** — "Discipline Review Log" panel loads latest 5 entries from `review_log`; shows findings excerpt, recommendation list, applied/pending badge
- **Polymarket page** — new nav item + full page with summary stat cards (open count, exposure, unrealized/realized P&L), open positions table, and trade history table

## [3.6.87] - 2026-05-10

### Fixed
- **mcp-yahoo missing Dockerfile.http and run_http.py** — `podman-compose up` was failing with "Dockerfile not found in ./mcp/yahoo-finance-mcp" and "image not known" on fresh installs; added `Dockerfile.http` (uv-based build, exposes port 8000) and `run_http.py` (starts FastMCP with `transport="streamable-http"` on `FASTMCP_HOST`/`FASTMCP_PORT`) so the service builds and serves correctly at `http://ot-mcp-yahoo:8000/mcp`

## [3.6.86] - 2026-05-10

### Fixed
- **options_trader: hardcoded "fill" event type** — broker reply status is now checked and mapped to `fill`/`reject` matching equity_trader logic; rejected orders emit a `reject` stream event instead of silently emitting a false `fill`
- **equity_trader / options_trader: race in positions_today** — ticker is only added to `_positions_today` after a confirmed fill event in the gateway reply, not before iterating results; prevents a rejected order from blocking future signals for the same ticker that day
- **equity_trader: _size_position returns 0 for price ≤ 0** — previously returned 1 share, which would submit an order at zero price; caller already guards `qty < 1`
- **options_monitor: close + audit log not atomic** — wrapped `UPDATE option_positions SET status='closed'` and `INSERT INTO option_trade_log` in a single asyncpg transaction; prevents orphaned closed positions with no audit trail on mid-operation failures
- **options_monitor: untracked fire-and-forget chart task** — added `done_callback` to chart `create_task()` to surface exceptions instead of silently swallowing them

### Security
- **WEBUI_TOKEN / SECRET_KEY no longer have insecure defaults** — if not set in env, a cryptographically random value is generated at startup with a warning; eliminates the `"opentrader"` / `"change-me-please"` predictable defaults
- **WebSocket /ws endpoint now authenticated** — validates JWT session cookie or `?token=` param before accepting; previously any client could stream live trade signals, account positions, and agent health
- **WEBUI_TOKEN removed from HTML meta tag** — token was visible in page source and extractable via DevTools; frontend no longer seeds from meta, uses sessionStorage + prompt fallback
- **HTTPException detail strings sanitized** — infrastructure endpoints (agent restart, broker env, agentmail provision, accounts fetch) now log the full error server-side and return generic messages; backtest errors are truncated to 200 chars

### Refactored
- **shared/redis_client: `ensure_consumer_group()` added** — single implementation replaces identical 6-line xgroup_create/BUSYGROUP blocks duplicated across 11 agents and scrapers (zero remaining duplications)
- **shared/redis_client: `get_redis()` accepts `socket_timeout`** — removes inconsistent inline `aioredis.from_url()` calls with varying timeouts (15–100 s) scattered across traders and monitor
- **shared/crypto.py added** — `encrypt_secret()`/`decrypt_secret()` extracted from webui and alphavantage scraper into a shared module; removes duplicated Fernet+SHA256 key derivation

## [3.6.85] - 2026-05-10

### Added
- **Distribution backtest** — `run_distribution_backtest()` samples entry points every 21 trading days across full history; returns per-run metrics and a percentile summary (p10–p90) answering "what distribution of outcomes does this strategy produce across all entry points?"
- **Probability-of-loss by holding period** — `probability_of_loss_by_holding_period()` computes fraction of trades ending as a loss for each holding-period bucket (1d, 1w, 2w, 1mo, 1q); mirrors S&P 500 research that longer holds reduce loss probability
- **Options in Shadow Account** — shadow account now includes closed option positions from `option_trade_log` alongside equity trades; option trades skip OHLCV-based ideal-exit analysis (option premium ≠ underlying price); `trade_type`, `opt_type`, `opt_strike`, `opt_expiry` added to analysis output
- **Library book status "read"** — added `read` to the `library_books.status` check constraint alongside `reading`, `purchased`, `reference`

## [3.6.84] - 2026-05-09

### Fixed
- **Backtest 401 through Cloudflare tunnel** — auth middleware validates via session cookie or `?token=` query param only; backtest POST calls were sending the token only in the request body where the middleware can't see it; added `?token=` to the URL for `/api/backtest/quick`, `/api/backtest/quick/trades.pdf`, `/api/backtest/validate`, and `/api/strategies/.../backtest/run`

## [3.6.83] - 2026-05-09

### Fixed
- **Remove mcp-webull phantom agent** — removed `mcp-webull` from `KNOWN_AGENTS`, `PODMAN_HEALTH_ONLY`, and `CONTAINER_MAP` in the WebUI backend; removed its node, edges, and log-viewer dropdown entry from the platform dashboard topology — no container exists for it so it was permanently showing as failed

## [3.6.82] - 2026-05-09

### Fixed
- **WebSocket mixed content over HTTPS** — WebSocket URL now uses `wss://` when the page is served over HTTPS (e.g. via Cloudflare tunnel), preventing the browser from blocking the `ws://` connection as mixed content; fixes live updates and library page loading through the public tunnel

## [3.6.81] - 2026-05-06

### Added
- **1pm report — SGOV ex-dividend alert** — `_get_sgov_alert()` fetches SGOV's next ex-dividend date at report time via yfinance; if tomorrow is the ex-div date, a yellow banner appears instructing to SELL SGOV today in IRA accounts (webull-live-2/3/4); if today is the ex-div date, a green banner instructs to BUY today; IRA accounts are resolved dynamically from `WEBULL_LIVE_ACCOUNT_{N}_IRA` env vars

## [3.6.80] - 2026-05-05

### Fixed
- **Options scanner phantom close prevention** — added `MISS_THRESHOLD` (default 3) consecutive-miss counter per position using Redis keys `options:miss:{id}`; scanner only closes a position after 3 successive absent scans (~15 min at 5-min interval), preventing transient Webull drop-offs from creating phantom positions; counter clears immediately when position reappears
- **NVDA option P&L correction** — corrected 7 phantom positions created by scanner loop; fixed actual position (190C→197.5C→202.5C roll chain, all 5/15 expiry) with accurate fill data; corrected realized P&L to -$1,386.57
- **CVE option P&L correction** — corrected phantom positions in both IRA accounts (`webull-live-4` qty=2, `webull-live-2` qty=1) for 26C→27C→28C roll chain (06/18 expiry); applied actual fill data for both accounts

## [3.6.79] - 2026-05-04

### Added
- **Quick Intel — Fundamental data** — `_fetch_fundamentals()` fetches P/E, P/B, ROE, profit margin, revenue growth, D/E, market cap, analyst target price, and recommendation key from yfinance; P/E extremes (<15 bullish, >35 bearish), revenue growth, and net insider buys/sells (90d) are automatically injected as bullish/bearish factors
- **Quick Intel — 5-tier investment rating** — signal now outputs Buy / Overweight / Hold / Underweight / Sell (confidence >0.75 → Buy/Sell, else Overweight/Underweight) replacing binary BUY/HOLD/SELL
- **Quick Intel — Fundamentals panel** — Equity Dashboard Quick Intel card shows P/E, P/B, ROE, Margin, D/E, Analyst Target in a dedicated mini-grid alongside Technicals
- **Quick Intel — Enhanced LLM prompt** — fundamentals line and past outcome reflections injected into `_generate_stock_summary()` for richer, context-aware analysis paragraphs
- **Quick Intel — Bear-case challenge** — for Buy/Overweight signals, a second LLM call appends the 2 strongest bear-case risks to the summary
- **Quick Intel — Signal outcome memory** — new `signal_reflections` DB table; `POST /api/market/stock-analysis/{ticker}/reflect` computes 5-day return vs SPY alpha and generates an LLM reflection; past reflections injected into future Quick Intel prompts; collapsible "Past Outcomes" section in UI with "Record Reflection" button
- **Global Macro News card** — new `GET /api/market/macro-news` endpoint fetches deduplicated market news from SPY/QQQ/^GSPC/^DJI/^VIX via yfinance (15-min Redis cache); new card on Trading Dashboard

## [3.6.78] - 2026-05-03

### Added
- **Quick Intel technical indicators** — `_fetch_technical_indicators()` fetches 280 days of daily OHLCV bars from Massive/Polygon (yfinance fallback) and computes RSI-14, MA50, MA200, ATR-14, support (avg 3 lowest lows / 60 bars), and resistance (avg 3 highest highs / 60 bars); trend derived from price vs MA relationship; MA crossover and RSI extremes added as bullish/bearish factors; UI shows a Technicals mini-grid alongside the signal display

## [3.6.77] - 2026-05-03

### Added
- **Quick Intel LLM summary** — `_generate_stock_summary()` calls OpenRouter after assembling signal data and appends a concise natural-language market snapshot (≤60 words) below the signal/confidence/factors display; falls back to a template string when `OPENROUTER_API_KEY` is not configured; `summary` column added to `stock_analysis_snapshots` table

## [3.6.76] - 2026-05-03

### Fixed
- **Quick Intel** — missing `getToken()` call caused ReferenceError caught as "analysis unavailable"; fixed JS to fetch token correctly
- **Quick Intel** — backend query referenced non-existent columns `ml_confidence` and `raw`; corrected to use `payload AS raw` matching actual `signals` table schema
- **Quick Intel** — `payload` JSON string from asyncpg now parsed before `.get()` access

## [3.6.75] - 2026-05-03

### Changed
- **Quick Intel** — moved from Trading Dashboard to Equity Dashboard, positioned between the portfolio risk pie charts and the positions table

## [3.6.74] - 2026-05-03

### Fixed
- **User Configuration → Market Data** — `ALPHA_VANTAGE_API_KEY` and `MASSIVE_API_KEY` now appear in the Market Data secrets section alongside `POLYGON_API_KEY`

## [3.6.73] - 2026-05-03

### Changed
- **User Configuration** — moved `ALPHA_VANTAGE_API_KEY` field into the Massive (market data) panel under an "Alpha Vantage" separator; removed standalone Alpha Vantage panel

## [3.6.72] - 2026-05-03

### Changed
- **User Configuration** — removed standalone "Report Delivery" section; `REPORT_RECIPIENT_EMAIL` now lives solely in the AgentMail panel with an updated hint describing all delivery types (reports, alerts, EOD summaries, trade confirmations)

## [3.6.71] - 2026-05-03

### Fixed
- **Alpha Vantage API key** — added `alphavantage` entry to `SERVICE_CONFIG` in User Configuration page so the `ALPHA_VANTAGE_API_KEY` field is visible and editable alongside other service credentials

## [3.6.70] - 2026-05-03

### Added
- **Feature 1: Tiered intraday portfolio NAV snapshots** — `portfolio_intraday_snapshots` hypertable; 30-min captures during market hours via new scheduler job; tiered pruning (24h full res → 7d 15-min → 30d hourly); new `/api/portfolio/intraday-snapshot` and `/api/portfolio/intraday-nav` endpoints
- **Feature 2: DB write retry decorator** — `shared/db_retry.py`; `@db_retry(max_attempts=3)` decorator with exponential backoff for PostgreSQL deadlock/serialization/lock-timeout errors
- **Feature 3: ETF capital flow scraper** — `scrapers/etf_flows/` container (`ot-scraper-etf-flows`); tracks 26 ETFs (broad, sector, bond, commodity, volatility, crypto); `etf_flow_snapshots` hypertable; `/api/market/etf-flows` endpoint; Trading Dashboard "ETF Capital Flows" panel
- **Feature 4: Macro regime snapshot** — `scrapers/macro_regime/` container (`ot-scraper-macro-regime`); aggregates SPY trend, QQQ/TLT/DXY/HYG/VIX momentum + OVTLYR breadth into bull/bear score; `macro_regime_snapshots` hypertable; `/api/market/macro-regime` endpoint; Trading Dashboard "Macro Regime" widget
- **Feature 5: Alpha Vantage news sentiment scraper** — `scrapers/alphavantage/` container (`ot-scraper-news`); fetches categorized news (equities/macro/energy/technology) with sentiment scores; `news_sentiment_snapshots` hypertable; `/api/market/news-sentiment` endpoint; Trading Dashboard "News Sentiment" feed
- **Feature 6: Per-symbol stock analysis snapshots** — `stock_analysis_snapshots` hypertable; `/api/market/stock-analysis/{ticker}?generate=true` generates snapshot from predictor signals + OVTLYR + sentiment; Trading Dashboard "Quick Intel" panel
- **Feature 7: Trending symbols engine** — `/api/market/trending` + `/api/market/trending/refresh`; scores tickers by signal frequency (×3), OVTLYR list presence (×2), portfolio presence (×1), sentiment magnitude; Redis 5-min cache; scheduler refreshes every 5m; Trading Dashboard "Trending Symbols" widget
- **Feature 8: Polymarket paper trading** — `polymarket_positions` + `polymarket_trades` tables; Gamma API market browser; CLOB orderbook prices; paper buy/sell/close positions; auto-settlement check; full "Polymarket" page under Resources nav
- **Scheduler** — 6 new jobs: `intraday_nav_snapshot` (30m), `prune_portfolio_history` (nightly), `scrape_etf_flows` (16:30 ET), `scrape_macro_regime` (16:35 ET), `scrape_news_sentiment` (30m), `update_trending_symbols` (5m)
- **3 new containers** — `ot-scraper-etf-flows`, `ot-scraper-macro-regime`, `ot-scraper-news`

## [3.6.69] - 2026-05-03

### Changed
- **Platform Dashboard topology** — added `MCP Webull` node (`ot-mcp-webull`) with heartbeat edge to Orchestrator, mcp/tools edge to Broker Gateway, and chat-agent query edges; matches all 28 running containers
- **Logs page** — added `mcp-webull` to container select dropdown
- **`CONTAINER_MAP`** — added `mcp-webull → ot-mcp-webull` mapping for log fetch

## [3.6.68] - 2026-05-03

### Changed
- **Resources nav** — Market Groups, Sector Leaders, and all Sector Map sub-items grouped under a new collapsible `▸ Sector Insight` parent; Sector Map entries remain accessible with a "Sector Map" section label inside the group

## [3.6.67] - 2026-05-03

### Changed
- **Sector Leaders** nav item moved from Equities to Resources

## [3.6.66] - 2026-05-03

### Added
- **Sector Leaders — bar chart header** — horizontal bar chart above the sector cards ranks all 11 sectors by daily avg % change; bars are column-normalized (best sector = full width), green for positive / red for negative with a colored leading-edge border; rank number and ETF ticker shown alongside each bar; #1 row has a subtle gold highlight

## [3.6.65] - 2026-05-03

### Added
- **Sector Leaders page** (Equities nav) — grid of 11 sector cards showing daily top performers; each card ranks all stocks within the sector by % change with gold/silver/bronze rank badges; consecutive-day streak tracked in `sector_leader_history` DB table: 2d = 🔥 amber, 3d+ = 🔥 orange, 5d+ = 🔥🔥 red; streak stocks get a colored left-border accent and slightly larger font for visual prominence; cards sorted by sector avg change (best performing sector first); 15-min Redis cache per date
- **`sector_leader_history` table** — one row per (date, sector, ticker) with rank, change_pct, price, volume; streak computed by counting consecutive trading days each ticker appeared in top-5 of its sector
- **`GET /api/market/sector-leaders?refresh=false`** — fetches today's Polygon snapshot on first call, stores rankings to DB, returns sector cards with streak data; `refresh=true` bypasses cache and re-fetches

## [3.6.64] - 2026-05-03

### Added
- **Equity Journal** — ✏ journal button on every row in the Equity Dashboard; clicking opens a modal with two fields: **Commission / Fees** (dollar amount paid entering/exiting the position) and **Notes** (thesis, rationale, lessons); fees are shown in a new "Fees" column in the table (amber, visible at a glance); button turns amber when a note or fee exists
- **`equity_journal` DB table** — stores journal entries keyed by `(account_id, ticker)` so a single journal entry covers all fills in a position; survives position re-loads from broker
- **`GET /api/equity/journal/{account_id}`** — returns all journal entries for an account (batch, called on page load)
- **`GET /api/equity/journal/{account_id}/{ticker}`** — single-position lookup
- **`PATCH /api/equity/journal/{account_id}/{ticker}`** — upsert notes and trade_cost; re-renders the equity card in-place after save without a full page reload

## [3.6.63] - 2026-05-03

### Changed
- **Market Groups** — performance cells replaced with Finviz-style horizontal bar charts; bar width is column-normalized (largest absolute value in the column = full width); positive bars grow left-to-right (green), negative grow right-to-left (red); colored border accent on the leading edge of each bar

## [3.6.62] - 2026-05-03

### Changed
- **Sector Map** nav group moved from Equities to Resources

## [3.6.61] - 2026-05-02

### Added
- **Market Groups page** under Resources nav — Finviz Groups-style ETF performance table with three tabs: Sectors (11 SPDR ETFs), Industries (17 thematic ETFs: semis, software, biotech, banks, homebuilders, retail, oil & gas, cybersecurity, gold miners, ARK, cloud, fintech), and Indices (15 ETFs: S&P 500, Nasdaq, Dow, Russell 2000, mid-cap, international, commodities, fixed income); columns: Name, ETF, Price, 1D, 1W, 1M, 3M, 6M, YTD, 1Y, Volume; all percentage cells color-coded (deep green → neutral → deep red) with intensity proportional to magnitude
- **`GET /api/market/groups?group=sectors|industries|indices`** — single Polygon.io snapshot call for 1D change + 8 concurrent agg fetches per tab for period returns; 30-minute Redis cache per group

## [3.6.60] - 2026-05-02

### Added
- **Multi-index Sector Maps** — collapsible `▸ Sector Map` nav parent with 5 sub-pages: S&P 500, Nasdaq 100, Dow 30, Russell 2000, Futures; each index has its own stock universe and Redis cache key (`market:sector_map_{index}_v3`); Futures map uses asset-class groupings (Equity Index, Energy, Metals, Agriculture, Currencies, Fixed Income) with Yahoo Finance `=F` symbols and notional weights for tile sizing
- **Three-level treemap** — Sector → Subsector header bar (dark `#141828` spanning the group) → individual stock tiles; subsector header stored in `_smSubsectorRects` for highlight and popup positioning
- **Subsector header highlight** — hovering a stock tile turns the subsector header amber and draws yellow borders on all peer tiles in the same subsector; singleton fallback (≤1 peers) expands to full sector
- **Equities Hindsight page** — Shadow Account renamed to Equities Hindsight and moved to the Equities nav group for better discoverability
- **Backtester expanded metrics** — `_expanded_metrics()` in `backtest_runner.py` computes Sortino ratio, Calmar ratio, recovery factor, average win/loss, and profit factor from existing backtest data; `_run_on_df()` and `_fetch_ohlcv()` exposed as public helpers for the new `backtest_validator.py` cross-validation layer

### Fixed
- **Shadow Account visibility** — page was permanently hidden due to `style="display:none"` inline attribute on the page div (overrides `.page.active{display:block}`); removed the inline style
- **Shadow `_apiFetch` undefined** — all shadow account functions were calling the undefined `_apiFetch` instead of `apiFetch`; corrected throughout

## [3.6.59] - 2026-05-02

### Fixed
- **Sector Map** — yellow subsector highlight now covers all peers: when hovering a stock in a singleton/small subsector (≤2 members), the highlight falls back to the full sector so all related stocks get the yellow border; consolidated singleton subsectors (Consumer Tech→Hardware, E-Commerce→Retail, Social Media+Internet→Social & Search, Streaming+Gaming+Media→Media & Entertainment) for more meaningful groupings

## [3.6.58] - 2026-05-02

### Changed
- **Sector Map** — full two-level nested treemap matching Finviz layout: each sector panel now contains ~10–15 individual S&P 500 stock tiles (120+ stocks total); tile size = market cap, color = daily % change; sector header bar shows name + weighted avg change; hover tooltip shows ticker, company name, price, % change, and market cap; data cached in Redis for 5 minutes after first load

## [3.6.57] - 2026-05-02

### Added
- **Sector Map page** under Equities nav — Finviz-style squarified treemap of the 11 S&P 500 SPDR sector ETFs (XLK, XLV, XLF, XLY, XLI, XLC, XLP, XLE, XLRE, XLU, XLB); block size = market cap weight, color = daily % change (dark red → gray → dark green); hover tooltip shows sector name, ETF ticker, price, and weight; data fetched live via yfinance from new `/api/market/sector-map` endpoint

## [3.6.56] - 2026-04-29

### Added
- **Shadow Account page** (`python/webui/shadow_account.py`) — Counterfactual P&L analysis that answers "what did discipline lapses cost?" across four categories: Noise Trades (low-confidence entries that lost), Early Exits (left gains on the table), Late Exits (held through a profitable window into a loss), and Overtrading (repeat entries same ticker/day); runs against the full trade history for any date range and account filter
- **LLM rule extraction** — after scoring all trades, the LLM (OpenRouter) extracts 3–5 specific, quantified discipline rules from winner/loser patterns (e.g., "Skip confidence < 0.70", "Never hold a loss past 2%"); each rule is immediately backtested against the full trade set and returned with `backtested_gain` and `trades_affected`
- **Counterfactual top-5** — ranked list of the 5 trades with the largest discipline gaps (ideal P&L vs actual P&L), with category badge and gap amount
- **`shadow_runs` DB table** — persists every analysis run; run history is shown on the page and clicking any row reloads the full result without re-running
- **`POST /api/shadow/run`**, **`GET /api/shadow/history`**, **`GET /api/shadow/run/{id}`** — three new WebUI endpoints; analysis runs in-process with yfinance OHLCV fetched concurrently per ticker; open positions (status='fill') use current close price as exit for unrealized P&L analysis

## [3.6.55] - 2026-04-28

### Added
- **Portfolio Optimizer page** — new "Portfolio Optimizer" entry in the Trading Plan nav group; five allocation strategies: Max Sharpe (MVO/Markowitz), Min Variance, Risk Parity (equal risk contribution), Equal Volatility (inverse-vol weighting), and Max Diversification; parameter controls for total capital, lookback window (90–504 days), risk-free rate, and per-asset weight cap; results show per-asset weight/allocation/annual-return/volatility/risk-contribution table, stacked weight bar, risk-contribution bar chart, and correlation heat-map with color-coded cells; "Load from Signals" button pre-fills tickers from recent predictor signals; Sharpe/volatility/return portfolio summary cards
- **`python/webui/portfolio_optimizer.py`** — standalone optimizer module using scipy SLSQP; fetches daily log-returns via yfinance; drops tickers with <80% data coverage; iterative weight-cap redistribution; returns full JSON including correlation matrix and per-asset risk contributions
- **`POST /api/portfolio/optimize`** and **`GET /api/portfolio/signals-tickers`** endpoints added to WebUI backend; optimizer runs in a thread-pool executor to avoid blocking the async event loop
- **scipy** added to `requirements.txt` and installed in webui container

## [3.6.54] - 2026-04-28

### Added
- **Predictor Signals card on Trading Dashboard** — full table of recent predictor signals with composite confidence bar, ML Score badge (▲/~/▼ colored by agreement with direction, rule-base % alongside), Val Accuracy %, OVTLYR raw score, analyst consensus, and sentiment columns; ticker cell shows LLM reason on hover; auto-loads on page navigate and has a manual refresh button
- **`/api/signals` endpoint fixed** — previously parsed old envelope format and returned empty fields; now reads the predictor's flat stream format (ticker, direction, confidence, metadata JSON) and surfaces all ML fields (ml_confidence, ml_val_accuracy, ml_model_count, ml_rule_base, llm_reason)

## [3.6.53] - 2026-04-28

### Added
- **ML Predictor Ensemble** (`predictor/ml_predictor.py`) — walk-forward RandomForest + GradientBoosting + Ridge ensemble trained daily per ticker on 2 years of OHLCV data with 12 engineered features (momentum returns, RSI, MACD histogram, Bollinger position, SMA20/50/200, volume ratio, ATR, candle body); blended with rule-based confidence as a 35%/65% weighted composite before LLM refinement; models cached in-memory per calendar day; gracefully degrades when sklearn is unavailable or validation accuracy < 52%
- **scikit-learn** added to `requirements.txt` and installed in predictor container
- ML metadata (`ml_confidence`, `ml_val_accuracy`, `ml_model_count`, `ml_composite_weight`) propagated through signal payloads to TimescaleDB and Redis streams
- Env-var controls: `ML_ENABLED`, `ML_WEIGHT` (default 0.35), `ML_MIN_VAL_ACC` (default 0.52)

## [3.6.52] - 2026-04-28

### Fixed
- **Webull Margin Account buying power** — `cash_power` is returned as the string `'0.00'` (truthy) so `or` never reached `margin_power`; now converts to float first so margin accounts correctly report their `margin_power` value

## [3.6.51] - 2026-04-28

### Changed
- **Equity Dividend Income: broker account chips now show Buying Power** — buying power is extracted from the broker balance response and displayed as a full-width blue row below Payers / Annual in each account card

## [3.6.50] - 2026-04-28

### Changed
- **Strategy assignments: Daily Log button** — button now labeled "Daily Log" (text); modal shows the full `ot-trader-equity` container execution log from market open (09:30 ET) today, sorted by time in the left column with event+fields in the right column; level-colored (warning=yellow, error=red)

## [3.6.49] - 2026-04-28

### Added
- **Strategy assignments: Daily Log button** — small calendar button (📅) to the left of Detail on each assignment row opens a modal showing today's trades, order stream events, and realized P&L summary for that strategy+account

## [3.6.48] - 2026-04-28

### Fixed
- **Options monitor: chain lookup now caps expiry search to 90 days from entry date** — prevents deep-ITM near-term calls (e.g. 202.50C May) from being misidentified as far-OTM long-dated calls (e.g. 240C Oct) that happen to share a similar price
- **Options monitor: tightened price-match tolerance from 40% to 25%** and increased early-expiry bias (later expiry must now beat current best by 50%, up from 25%)
- **Options monitor: removed redundant `existing is None` from `needs_enrichment`** — prevents unnecessary chain re-enrichment when v2 API data already provided strike/expiry
- **Options monitor: corrected long-position P&L formula** — was computing `(entry − exit) × qty × 100` (short formula), now correctly `(exit − entry) × qty × 100`; historical NVDA records with inverted P&L manually corrected in DB

## [3.6.47] - 2026-04-27

### Changed
- **yahoo-finance-mcp vendored** — removed git submodule (local-only commit blocked cloning on other systems); files now committed directly with `get_avg_volume` tool preserved.

## [3.6.46] - 2026-04-27

### Changed
- **Options chain filters: highlight color changed from green to blue** — spread and extrinsic qualifying cells now use blue (`rgba(96,165,250,.22)`) to match the position-highlight palette.

## [3.6.45] - 2026-04-27

### Changed
- **Options chain spread/extrinsic filters now highlight instead of hide** — when ≤10% Spread or ≤$30 Extrinsic checkboxes are enabled, contracts meeting the criteria are highlighted green (bid/ask/mid cells for spread; extrinsic cell for extrinsic value) rather than hiding contracts that don't qualify.

## [3.6.44] - 2026-04-27

### Added
- **Options Trader chain: ~80 delta highlight** — strike cell turns yellow and delta value is bolded amber when `|delta|` is in the 0.75–0.85 range (both call and put sides). Selection (red) and position (blue) highlights take priority.
- **Options Trader chain: ≤10% Spread filter** — checkbox hides contracts where `(ask−bid)/mid × 100 > 10%`. Applied independently to calls and puts so rows with one valid side still appear.
- **Options Trader chain: ≤$30 Extrinsic filter** — checkbox hides contracts where extrinsic value exceeds $30, filtering out high-premium deep-ITM or high-IV contracts.

## [3.6.43] - 2026-04-26

### Fixed
- **Options Trading Log: CHAIN RISK bar now appears for close-and-reopen roll patterns** — prior implementation required `status='rolled'` on previous legs to detect a chain, but all positions in this dataset are `status='closed'`. Removed the status-based reset; credits now accumulate forward across all prior closed positions on the same underlying/account/option-type, ordered by entry date. LUNR ×14, UBS ×12, NVDA ×11, etc. will now correctly show cumulative credits and net risk reduction.

## [3.6.42] - 2026-04-26

### Fixed
- **Options Trading Log: completed roll chains now appear in Historical** — prior logic treated any `status='rolled'` position as "active", which permanently kept its ticker in the active section and caused the closed final leg to be invisible in both sections. Active/historical split now uses only `status='active'` to determine live tickers; rolled prior legs and the closed final leg of a completed chain all route to Historical together, making the CHAIN RISK bar visible there.

## [3.6.41] - 2026-04-26

### Added
- **Options Trading Log: roll-chain risk reduction** — positions linked by rolls now display a "CHAIN RISK" bar showing gross risk, cumulative credits banked from prior legs, and the resulting net risk on the current leg. A progress bar tracks how much prior credits have offset the current exposure. When cumulative credits fully cover the current leg's cost basis, the position is flagged "HOUSE MONEY" in teal. Debit rolls (negative credits) correctly increase the displayed net risk. DB migration adds `chain_id UUID` to `option_positions` for future explicit roll-chain linking. Chain computation runs at query time in both the summary and ticker endpoints, grouped by broker/account/underlying/option-type with resets on closed/expired legs.

## [3.6.40] - 2026-04-26

### Fixed
- **Options Trader: Alpaca "client_order_id must be unique" error** — tag was hardcoded `"webui-trader"` for all orders. Now set to `wt-{req_id}` (unique UUID per leg) so every order gets a distinct `client_order_id`.

### Added
- **Options Trader: Pending Orders panel** — loads automatically on page open and after each successful order placement. Displays open/pending orders in a table (symbol, side, qty, type, limit price, status, account) with a Cancel button on the far right of each row. Cancel fades and removes the row on success. Includes a Refresh button in the panel header.
- **New API endpoint `POST /api/broker/cancel-order`** — routes `cancel_order` command to broker gateway and awaits confirmation.

## [3.6.39] - 2026-04-26

### Added
- **Options Trader: ATR template persistence** — when an order is placed, the ATR(14) value and anchor price are saved to `option_atr_templates` DB table (ticker, anchor, ATR, trade date, order IDs). On every chart load, saved templates for that ticker are fetched and overlaid as **solid, thicker** price lines distinct from the live dashed lines, labeled `[YYYY-MM-DD] Level Name`. Supports multiple templates per ticker. Legend row shows both live ATR and all saved template anchors.
- **New DB table `option_atr_templates`** — created at startup; indexed by ticker + trade_date DESC.
- **New endpoints** `POST /api/options/trader/save-atr-template` and `GET /api/options/trader/atr-template/{ticker}`.
- **Bug fix**: `last` was referenced before declaration in `_otLoadChart`; moved `last`/`prev` definitions to before the ATR section.

## [3.6.38] - 2026-04-26

### Added
- **Options Trader: ATR levels overlay on chart** — six horizontal dashed price lines rendered on every chart load using ATR(14): −3 ATR Emergency Exit (red), −2 ATR Stop Loss (amber), +½ ATR First Roll, +1 ATR Second Roll, +2 ATR Roll, +3 ATR Roll (blue shades). Anchor is the position's underlying entry price when held, otherwise last close. An ATR legend row below the chart shows each level with its dollar price.
- **Options Trader: Chart snapshot on order placement** — when an order is successfully submitted, the chart (with ATR overlays) is captured via `chart.takeScreenshot()` and saved server-side as `/static/snapshots/{TICKER}_{YYYY-MM-DD}.png`. Filename shown in the order status bar.
- **New API endpoint `POST /api/options/trader/save-chart-snapshot`** — accepts base64 PNG and writes to `/app/webui/static/snapshots/`.

## [3.6.37] - 2026-04-25

### Added
- **Options Trader: Place Order button now functional** — clicking "Place Order" in the Risk Calculator submits all selected legs to the broker gateway via `/api/options/trader/place-order`. Collects account, contracts count, premium, order type, and duration; validates legs have OCC symbols; shows submission status (order ID on success, error message on failure). Button turns red for sell orders, stays green for buys.

## [3.6.36] - 2026-04-25

### Added
- **Options Trader: Fundamentals panel** — new card in the left column below OVTLYR Signals; loads on ticker selection. Pulls from three sources: Polygon/massive.com (company name, sector, exchange, market cap, employees, description), massive.com dividends API (next ex-dividend date, pay date, amount, frequency), and yfinance (earnings date, P/E, forward P/E, EPS, revenue, profit margin, dividend yield, beta, 52-week range).
- **New API endpoint `/api/options/trader/fundamentals/{ticker}`** — merges the three data sources above into a single response.

## [3.6.35] - 2026-04-25

### Changed
- **Options Trader: OVTLYR Signals panel shows per-ticker intel on selection** — renamed from "OVTLYR Buy Signals"; when a ticker is selected the panel fetches and displays signal direction, nine-score (X/9 dot indicator), oscillator direction, fear & greed score, last close, and 30d avg volume. A "← All Signals" button returns to the buy-signals list.
- **New API endpoint `/api/ovtlyr/ticker/{ticker}`** — returns OVTLYR intel for a single ticker (Redis position_intel → screener cache → DB fallback).

## [3.6.34] - 2026-04-25

### Changed
- **Options Trading Log: "By Account" moved above most/least profitable tickers** — account breakdown now appears directly after the YTD Performance panel, before the ticker profitability strip.
- **Options Trader: unified ticker input in top bar** — removed the symbol input from inside the chart card header; a single "Ticker / Go" input now lives in the top bar to the left of the account selector and drives the chart, options chain, and risk calculator together.

## [3.6.33] - 2026-04-25

### Fixed
- **Dividend history table now filters by selected broker account** — clicking a broker card now syncs the history account-filter dropdown and re-renders the history table/chart to show only records for that account. Previously the history section used its own independent dropdown and was never updated when the broker card filter changed.

## [3.6.32] - 2026-04-25

### Fixed
- **Upcoming dividends now filters by selected broker account** — selecting a broker card on the Dividend page now re-renders the upcoming ex-dividend table to show only tickers held in that account, with qty and estimated total recomputed from that account's actual share count. Previously the table always showed all-account qty/totals regardless of the active filter.

## [3.6.31] - 2026-04-25

### Fixed
- **Dividend forecast avg now anchored to first actual payment, not 18-month window** — `per_account_monthly_avg` denominator changed from the fixed 18-month calendar window to the number of months since the account's first recorded payment. Previously, accounts with only 3 months of history were divided by 18, producing a 6× under-estimate of projected monthly income. Now each account's denominator reflects how long it has actually been receiving dividends. The portfolio-level `history_monthly_avg` uses the same logic.
- **Removed stale `min(hist, yf)` projection logic** — v3.6.29 removed yfinance from income projection, making the `min(history, yfinance)` guard from v3.6.28 incorrect (both values now come from the same history source). Future bar heights now use history avg directly, falling back to forward-rate estimate only when no payment history exists.

## [3.6.30] - 2026-04-25

### Changed
- **Dividend metadata now uses three-tier fetch: massive.com → dividend.com → yfinance** — `_div_get_meta` (which supplies ex-dates, pay-dates, yield%, frequency for the Dividend page display) now tries massive.com (`api.massive.com/stocks/v1/dividends`, authenticated with `MASSIVE_API_KEY`) as primary source, dividend.com scrape as secondary, and yfinance only as last resort. Frequency is derived from the count of massive.com payments in the trailing 12 months. dividend.com scrape gracefully falls through if Cloudflare blocks the request.

## [3.6.29] - 2026-04-25

### Changed
- **Dividend income projection no longer uses yfinance rates** — `forward_annual_rate` is now computed exclusively from actual `dividend_history` records: `most_recent_payment_per_share × payments_in_last_12_months`. This eliminates yfinance's trailing-average inflation (which was 3× actual after HOOW's dividend cut) and makes the forecast directly reflect what the broker has actually paid. Positions with no payment history show `$0` projected income rather than a speculative yfinance estimate. yfinance is still used for ex/pay dates, sector, industry, and yield% display only.

## [3.6.28] - 2026-04-25

### Fixed
- **Dividend forecast — use minimum of history avg and yfinance per account** — when a dividend is cut, yfinance immediately reflects the new rate while the 18-month history still contains the old higher payments. Taking `min(history, yfinance)` per account ensures the forecast uses the lower (more accurate) value in both directions: cuts are caught by yfinance, stable/growing dividends are smoothed by history.

## [3.6.27] - 2026-04-25

### Fixed
- **Dividend forecast — forward rate now uses most-recent payment, not trailing 12-month sum** — for high-frequency payers (weekly ETFs like HOOW), the trailing-12-month sum was 3× the actual forward rate after a dividend cut (old payments dominated). Now: when `last_dividend_value × frequency` is more than 20% below the trailing sum, the recalibrated per-payment rate is used. This immediately reflects dividend cuts without historical drag (e.g. HOOW: $0.283/wk × 52 = $14.72/yr instead of trailing $38.04/yr).

## [3.6.26] - 2026-04-25

### Added
- **Dividend forecast diagnostics panel** — expandable "Forecast Diagnostics" card on the Dividend page shows per-account breakdown: record count, 18-month total received, history-based monthly avg (÷ months elapsed), and yfinance estimate side-by-side with per-ticker detail. Makes it immediately visible whether history or yfinance is driving the forecast and whether the stored data matches expected share counts.

## [3.6.25] - 2026-04-24

### Fixed
- **Dividend forecast — projected months now use history-based per-account monthly avg** — future and current-month bars now project from `total_received / months_elapsed` computed per broker account from actual `dividend_history` records (backfilled at current share quantities). This eliminates yfinance `forward_annual_rate` inflation as a source of error. Each account contributes its own avg so the margin account filter shows only margin-account income, not the full portfolio total. Falls back to yfinance rate only when no history exists.
- **Stat card monthly sub-line is now account-filter aware** — previously always showed the all-accounts yfinance total from the server cache (`avg_monthly_income`); now shows the per-account filtered monthly avg (history-based or yfinance fallback) that exactly matches the bar chart projection.

## [3.6.24] - 2026-04-25

### Fixed
- **Dividend forecast — per-account projection now computed directly from holdings** — for current and future months, `projected_income` is now derived straight from each account's `projected_monthly_income` positions (qty × forward_annual_rate / 12), bypassing forecast breakdown filtering entirely. This eliminates all sensitivity to Redis cache format, account_label matching, and stale cached data. Past months continue to use per-(account,ticker) breakdown from the server.
- **Forecast cache key is now version-scoped** — changed from `dividend:forecast:cache` to `dividend:forecast:{APP_VERSION}:cache` so every new deployment automatically gets a fresh cache with no manual Refresh required.

## [3.6.23] - 2026-04-24

### Fixed
- **Dividend forecast — future projection now uses current holdings, not inflated history** — the history-based average was producing projections 3–4× too high because the backfill records historical payments at *current* share quantities (not the quantities held at the time of each payment). Future months now project from `current qty × forward annual rate / 12` per position per account — the only value that correctly reflects what you actually hold today. Past months continue to display real recorded income from `dividend_history`. The stat card monthly sub-line shows the API-based monthly estimate plus a history-months count.

## [3.6.22] - 2026-04-24

### Fixed
- **Dividend forecast — per-account filtering now works for all months** — past-month breakdown entries now include `account_label` (same shape as future-month entries) so `_divComputeFromAccounts` correctly sums only the shares held in the selected broker for every bar in the chart. Previously past months aggregated all accounts into one per-ticker total.
- **Dividend forecast — past months no longer wiped to $0** — `_divBlendMonthly` was overriding backend-computed past-month income with `histByMonth[ym]` which was always 0 because `_divHistoryRecords` hadn't been set yet (load-order bug). Past months now use `m.projected_income` (already account-filtered by `_divComputeFromAccounts`) directly. History records are still used for the current-month confirmed/unconfirmed split.
- **Load order** — `_divRenderHistory` now sets `_divHistoryRecords` before `_divApplyFilter` runs, ensuring current-month history blend has data on first render.
- **Backfill** — after completion, triggers `loadDividendPage(true)` (clears forecast cache + full reload) so the new history is immediately reflected in the chart.

## [3.6.21] - 2026-04-24

### Fixed
- **Dividend forecast — correct normalization denominator** — `avg_monthly` was dividing by the number of calendar months *that had payments* (e.g. 6 for a quarterly payer over 18 months), making the projection 3× too high for quarterly payers and proportionally wrong for all other frequencies. Denominator is now total elapsed calendar months in the 18-month window (≈18) so the per-calendar-month average is correct regardless of payment frequency. Response now includes `months_elapsed` and `total_received_history` for transparency.

## [3.6.20] - 2026-04-24

### Fixed
- **Dividend forecast — per-account accuracy** — the DB query now groups by `(month, account_label, ticker)` so each broker's actual share quantities drive its projected income independently. Future-month breakdown entries include `account_label`; the frontend account-filter now matches on both symbol and account label so filtering to one broker shows only that broker's projected dividends, not the whole portfolio's.

## [3.6.19] - 2026-04-24

### Changed
- **Dividend forecast — history-based average projection** — `/api/dividends/forecast` now queries 18 months of actual `dividend_history` records from the DB. Completed past months display real recorded income with per-ticker breakdown. The average of all captured months (excluding the current partial month) is used as the projection for the current and all future months, replacing the previous API-metadata-based per-ticker calculation. Future months retain a proportional per-ticker breakdown (scaled from current holdings weights) so account-level filtering continues to work in the dashboard. The stat card monthly sub-line now reads actual avg/mo with count of captured months instead of `annual ÷ 12`.

## [3.6.18] - 2026-04-24

### Fixed
- **Equity Dashboard nav position (definitive fix)** — replaced the migration-based approach with a guaranteed two-phase DOM enforcement: saved order is applied first, then a post-load pass finds `page-trading-trades` and `page-trading-unified` anywhere in the nav (handles cross-group drag) and unconditionally moves Equity Dashboard to the slot immediately after Equity Trades, then saves the corrected order. Previous approaches only patched the saved array before applying it, so cross-group or already-applied bad orders were never corrected.

## [3.6.17] - 2026-04-24

### Fixed
- **Equity Dashboard nav position (for real)** — v3.6.16 migration had an off-by-one: it re-inserted `page-trading-unified` at the trades index (before trades) instead of after it. Changed splice offset to `+ 1` so the corrected saved order is `[trades, unified, dividends]`.

## [3.6.16] - 2026-04-24

### Fixed
- **Equity Dashboard nav position** — `_loadNavOrder` now detects when the server-persisted nav order has Equity Dashboard (`page-trading-unified`) above Equity Trades (`page-trading-trades`) and swaps them before applying, then saves the correction. First page load after deploy automatically fixes any users with the wrong order.

## [3.6.15] - 2026-04-24

### Changed
- **Nav rename: "Trades" → "Equity Trades"**
- **Nav rename: "Active Positions" → "Equity Dashboard"** (nav, page heading, title map)
- **Nav rename: "Dividend Income" → "Equity Dividend Income"** (nav, page heading, title map)
- **Options positions column alignment** — header padding now matches cell padding per column: left-aligned columns (Ticker, Contract, Account, Alerts) use `10px` horizontal padding; all right/center columns use `6px`, eliminating the visible header/data gap.

## [3.6.14] - 2026-04-24

### Added
- **Earnings Date column in Open Option Positions** — new "Earnings" column shows the actual next earnings date (`next_earnings_date`) with the days-until count as a sub-line in orange when ≤14 days. Replaces the old days-only "D/E" column.
- **Movable columns** — all columns in Open Option Positions can be reordered by drag-and-drop (grip ⠿ handle on each header). Order is persisted in localStorage (`opt_pos_col_order_v1`). Ticker and Journal (✏) columns are fixed anchors.
- **Hideable columns** — each non-fixed column header has a ✕ button to hide it. Hidden count is shown in a footer with a "Reset columns" link to restore all. Hidden set persisted in localStorage (`opt_pos_col_hide_v1`).

## [3.6.13] - 2026-04-24

### Changed
- **Nav: "Trading Log" → "Options Trading Log"** — label updated in the Options nav group for clarity.
- **Expiry Calendar merged into Options Dashboard** — the Expiry Calendar is no longer a separate page; it appears as a card at the bottom of the Options Dashboard and loads automatically alongside positions, Greeks, and performance when the dashboard opens. The standalone nav item is removed. Any deep-link to `page-options-expiry` redirects to the Options Dashboard.

## [3.6.12] - 2026-04-24

### Fixed
- **HOOW (weekly payer) missing from Upcoming Ex-Dividend panel**: massive.com does not carry weekly ETF ex-dates that haven't been formally declared yet. Added dividendchannel.com as a Tier-2 fallback: fetches recent ex-dividend history from `tickertech.net` (the backend for dividendchannel.com), calculates average interval from the last 4 gaps, projects the next ex-date forward, and includes it if it falls within the 7-day window. For HOOW: last ex `2026-04-20`, 7-day interval → projected `2026-04-27`, avg payout `$0.278/share`.

### Changed
- **`GET /api/dividends/upcoming`** — upgraded to three-tier lookup: (1) massive.com declared dates, (2) dividendchannel.com projected dates for tickers with no massive.com results, (3) `dividend_meta` DB yfinance cache as last-resort guard. Each tier deduplicates via a `(ticker, ex_date)` set so no event appears twice.

## [3.6.11] - 2026-04-24

### Added
- **Upcoming Ex-Dividend Dates panel** — new card on the Dividend Income page showing all ex-dividend events for held tickers in the next 7 days. Columns: Ticker, Ex-Date, Pay Date, $/Share, Shares, Est. Total, Days Until. Data sourced from massive.com REST API (`/stocks/v1/dividends`). Panel shows total estimated income across all upcoming events.
- **`GET /api/dividends/upcoming`** — new backend endpoint that queries massive.com for each held ticker, filters to `ex_dividend_date` within the next 7 days, and returns sorted results with estimated payout per position.

## [3.6.10] - 2026-04-24

### Fixed
- **April HOOW weeks still missing**: history DB only had April 6 because the backfill was last run April 9 (before April 13 and April 20 payments settled in yfinance). Triggered a fresh 18-month backfill; April 13 ($464.75) and April 20 ($389.13) are now captured.
- **Projection only covered future payments**: advance condition `while pay_date < today` skipped any payment whose pay_date had already passed this month. Changed to a step-back-then-advance algorithm that always starts from the **first payment on or after the 1st of the current month**, so the full-month projection is stable even when history is temporarily stale.
- **Bar chart robustness**: current-month bar now uses `MAX(actual_confirmed, full_month_projection)` as its height. Green portion = history-confirmed income; blue portion = projected but not yet confirmed (unconfirmed remaining weeks). Even with a stale backfill, the bar shows the full expected monthly total.

### Changed
- **"This Month" stat card**: label changed to "April Confirmed"; sub-line now reads "$X,XXX expected · +$Y unconfirmed" so the distinction between received and projected is explicit.
- **April projected income**: updated from $389 (April 27 only) to $1,575 (all 4 HOOW weeks + SGOV + BHE).

## [3.6.9] - 2026-04-24

### Fixed
- **HOOW (and other weekly payers) missing from April forecast**: two compounding bugs:
  1. Frequency detection capped at `n >= 10 → 12 (monthly)` — weekly payers like HOOW with 50+ payments in 18 months were misclassified as monthly. Added `n >= 40 → 52 (weekly)` tier.
  2. Projection algorithm started from `ex_date = today` when API ex_date was null, placing first pay_date 14 days out (May). Now falls back to `last_known_pay_date − pay_lag` from `dividend_history`, so HOOW anchors to April 6 and correctly projects April 27 as the next pending payment.
  3. Advance condition was `while ex < today − interval`, skipping ex_dates within the current pay cycle. Changed to `while ex + pay_lag < today` so any payment whose pay_date is still upcoming is included, even if the ex_date already passed.
- **April forecast now shows HOOW**: April projected income updated from $1.19 to $389.12 (HOOW April 27 weekly payment across all accounts).

### Added
- **13-month forecast window**: bar chart now shows 1 prior month (actual history, gray) + current month (actual received + projected remaining, green/blue split) + 11 future months (projected, blue). Previously started at the current month with no history context.
- **`last_pay` anchor in `_div_project_payments`**: fetches `MAX(pay_date)` per ticker from `dividend_history` at forecast time and uses it to derive the correct ex_date for tickers missing API ex_date data.
- **History fetch limit**: increased from 100 to 500 records to ensure all months in the 13-month window are represented in the blend.

## [3.6.8] - 2026-04-24

### Fixed
- **April (current month) dividends incorrectly reported**: the projection algorithm pushes ex-dates forward past today, so April payments already made (SGOV $30.47, HOOW $295.17) were missing from the bar chart. A `_divBlendMonthly()` function now merges actual `dividend_history` records into the monthly view: past months show actual received, current month shows actual received + projected remaining, future months show the forecast unchanged.

### Added
- **Dividend Income page — "This Month Paid" stat card**: shows the month name (e.g. "April Paid"), actual dividends received so far this month from history, and a sub-line with any projected remaining income for the rest of the month.
- **Bar chart — three-tone visualisation**: past months render as gray bars (actual received), current month as a green base (received) + blue overlay (projected remaining), future months as solid blue (projected). A legend is drawn below the x-axis.
- **Bar chart total label**: updated to "X received · Y projected" reflecting both the historical and forward amounts.

### Changed
- **Nav label**: "Dividends" renamed to "Dividend Income" in the left navigation panel.
- **Bar chart card title**: "12-Month Projected Income" → "Dividend Income · 12 Months".

## [3.6.7] - 2026-04-24

### Fixed
- **Ticker panels — active-position tickers excluded**: tickers with any remaining active position (e.g. NVDA, SKM, SAN, BCS) are now excluded from both Most Profitable and Least Profitable panels; only fully-historical tickers appear, consistent with the History tree filter.
- **Ticker panels — no cross-contamination**: top panel only shows tickers with net positive P&L; bottom panel only shows tickers with net negative P&L. If fewer than 5 qualify, fewer chips are shown rather than bleeding positive tickers into "least profitable."
- **By Account — column alignment**: Positions and Win Rate cells now stack the secondary info (e.g. "3 closed", "2W/1L") on a separate line so the primary value right-aligns cleanly with its column header.

### Added
- **Monthly Trade Analysis — draggable columns**: all 10 columns (Month, Trades, Win Rate, Avg DIT, Avg Win, Avg Loss, Edge/Trade, Top Ticker, Total P&L, Analysis) are draggable; order persists to localStorage (`optlog_mon_col_order_v1`).
- **Monthly Trade Analysis — hide columns**: each non-sticky column has a faint ✕ button; hidden state persists to `optlog_mon_col_hidden_v1`; reset footer restores all columns.
- **Monthly Trade Analysis — rolling 12-month window**: only trades closed within the last 12 months are included; older history is excluded automatically.

## [3.6.6] - 2026-04-24

### Fixed
- **Trading Log / By Account — column header alignment**: `total_pnl` and other right-aligned columns were incorrectly forced to left-align because `sticky:true` columns bypassed the `align` field. Headers now use `c.align` directly; all headers match their data cells.
- **Least Profitable Tickers — active positions included**: `ticker_pnl` accumulation now filters to closed/rolled/expired trades only (`status not in ('active')`), so the panel shows only historical results.

### Added
- **Trading Log / By Account — hide columns**: each non-sticky column header shows a faint ✕ button; clicking it hides that column. Hidden state is persisted to localStorage (`optlog_col_hidden_v1`). A "↺ Reset columns (N hidden)" footer appears in the table when any columns are hidden, restoring all columns in one click.

## [3.6.5] - 2026-04-24

### Fixed
- **Ticker chip hover popup — transparent background**: `var(--card)` is undefined in the theme, causing the popup to render with no background. Replaced with hardcoded solid `#1a1e2b` background, `#3b4263` border, and a stronger `box-shadow:0 12px 40px rgba(0,0,0,.75)`. All inner text colors hardened to explicit hex values (`#e2e8f0`, `#8892a8`, `#cbd5e1`) so they're legible regardless of theme variable state.

## [3.6.4] - 2026-04-24

### Added
- **Trading Log / By Account — draggable columns**: all columns can be dragged to any position; order is persisted to localStorage and survives page refresh. A subtle ⠿ handle appears on each header.
- **Least Profitable Tickers panel**: sits to the right of Most Profitable inside the same card, separated by a divider. Both panels use the same hover popup system.
- **Ticker chip hover popup**: hovering over any ticker chip (top or bottom) shows a 220ms-delayed popup with P&L summary, win rate, avg DIT, best/worst trade, rule-based analysis of what drove results, and improvement suggestions.
- **Monthly Trade Analysis table**: new panel between By Account and the position trees. Columns: Month, Trades, Win Rate, Avg DIT, Avg Win, Avg Loss, Edge/Trade, Top Ticker, Total P&L, Analysis. Analysis column gives a one-line rule-based read (execution quality, risk/reward asymmetry, hold discipline).

## [3.6.3] - 2026-04-24

### Added
- **Trading Log / By Account — Capital Eff. + Avg Risk/Trade columns**: renamed "ROC %" to "Capital Eff." for clarity; added "Avg Risk/Trade" column showing the average premium committed per closed trade (entry price × qty × 100 ÷ closed count), giving a per-account view of position sizing discipline.

## [3.6.2] - 2026-04-24

### Fixed
- **YTD Performance — Proven Edge chip showing "—"**: `pnl_pct` is never stored in `option_trade_log`, so the pct-based formula always returned null. Backend now falls back to a USD-per-trade calculation `(win_rate × avg_win_usd) + ((1−win_rate) × avg_loss_usd)` and returns `proven_edge_usd` + `proven_edge_unit`. Frontend displays the USD value with a `/trade` label when pct data is unavailable.

## [3.6.1] - 2026-04-24

### Fixed
- **Expiry calendar — wrong expiration date for Webull non-OCC positions**: chain lookup was using `entry_price` as a fallback when `current_price` is 0, causing a decayed/OTM option (e.g. NVDA $255 call near expiry) to match a later-dated contract whose current price happened to be close to the original cost basis. Fix: only use live `current_price` for matching; when 0, fall back to strike-based (ATM) matching. Also raised the expiry-displacement threshold from 10% to 25%, so a later expiry must beat the earliest qualifying expiry by 25% to win. After chain enrichment writes an expiry, it is now auto-locked (`expiry_locked=TRUE`) to prevent future re-runs from drifting. NVDA active position corrected in DB to 2026-05-15.

## [3.6.0] - 2026-04-24

### Fixed
- **Daily option report — OVTLYR as authoritative signal**: report now overrides the predictor signal with OVTLYR for every position before sending. When predictor and OVTLYR disagree (e.g. predictor BUY vs OVTLYR SELL), the report shows the OVTLYR signal with a yellow ⚠ badge noting the predictor's opposing view and a conflict banner listing affected tickers at the top of the email.

## [3.5.99] - 2026-04-24

### Added
- **Broker dashboard — per-account risk %**: each account row now has a small Risk % input saved to localStorage; flash-confirms green on save.
- **Options Trader — risk % defaults from broker config**: selecting an account auto-loads that account's configured default risk % into the sizing calculator.
- **Options Trader — deviation warning**: editing the risk % away from the configured default shows an amber warning: *"Deviating from your configured default (X%). Is this in your trading plan?"* Hides automatically when the value is restored.

## [3.5.98] - 2026-04-24

### Changed
- **Options chain — BUY/SELL chip inline**: pill now appears to the right of the strike number on the same line.
- **Options chain — position sell highlight**: when a blue (open position) row is selected for SELL, the blue highlight is replaced with red across all data cells and the strike cell.

## [3.5.97] - 2026-04-24

### Changed
- **Options chain — Puts hidden by default**: puts column is now unchecked on load, showing calls-only view by default.

## [3.5.96] - 2026-04-24

### Added
- **Options chain — BUY/SELL chip on strike cell**: when a row is added to the order, a small colored chip appears below the strike price — green BUY or red SELL — updating live as the side cycles.

## [3.5.95] - 2026-04-24

### Fixed
- **Options Trader — buying power always $0**: `balances` is a dict per account but the risk endpoint iterated it as a list, looping over dict keys (strings) instead of the dict itself; `bal.get(...)` silently failed. Now reads the dict directly, handling `cash`, `buying_power`, `total_cash` (Tradier), and `margin.option_buying_power` as fallback.
- **Options Trader — chain selection persistence**: BUY/SELL badge and row highlight now clear when loading a new ticker's chain.

### Changed
- **Options Trader — multi-leg order builder**: replaced single-row selection with a persistent order legs system. Click a row to add it as BUY (green); click again to switch to SELL (red); click a third time to remove it. Position rows (blue) start as SELL. Selected legs appear as chips in an Order bar above the chain table with individual remove buttons and a Clear All control. Side badge in the Risk panel reflects the last-touched leg.

## [3.5.94] - 2026-04-24

### Added
- **Options chain — strike column blue highlight**: strike cell is now highlighted in blue when either the call or put for that row has an open position, matching the data cell highlight.
- **Options chain — buy/sell row selection**: clicking a chain row activates BUY mode (green tint + green outline); clicking the same row a second time toggles to SELL (red tint). Clicking a position row (blue) goes directly to SELL. Selected side badge (BUY/SELL) appears in the Risk & Sizing Calculator header.

## [3.5.93] - 2026-04-24

### Fixed
- **Options chain fragmented display**: rows were sorted by `(strike, expiration)` so each expiration's strikes were scattered across the table, producing a single row per date header. Sort is now `(expiration, strike)` so all strikes for a date are contiguous under their header.
- **Options chain auto-selects nearest date on load**: instead of showing all expirations at once (overwhelming), the chain now auto-selects the nearest expiration with an open position, or the nearest date if no position. Use "All Expirations" in the dropdown to see all dates.

## [3.5.92] - 2026-04-24

### Fixed
- **Options Trader — position not highlighted in chain**: `has_position` matched on contract symbol (OCC format from Tradier) against DB symbols (Webull `WBL:XXXXXXXXXX` format) — they never matched. Now matches on (strike, expiration_date, option_type) which is broker-agnostic.
- **Options Trader — expiration date missing from positions panel**: added expiration date (MM-DD) between strike and DTE in each position row.

## [3.5.91] - 2026-04-24

### Fixed
- **Options Trader — chain empty for Webull/Alpaca accounts**: gateway returned `status:ok` with 0 expirations/calls when those broker APIs return no data; the non-None result blocked the Tradier fallback so users saw a blank chain. Empty gateway results now count as a miss and fall through to Tradier → Yahoo Finance.

## [3.5.90] - 2026-04-24

### Fixed
- **Options Trader — HTTP error on chain load**: `_gateway_chain` only wrapped the Redis connection setup in try/except; the `xadd` and `blpop` calls were unprotected — a Redis `TimeoutError` during the gateway wait propagated uncaught and caused FastAPI to return HTTP 500. All Redis operations now share a single try/except so any failure falls through to the Tradier → Yahoo Finance fallback chain instead of crashing.
- Added null safety guard so `data` can never be `None` when marking open positions.

## [3.5.89] - 2026-04-24

### Fixed
- **Options Trader — chain date selection**: Near ATM filter was checked by default, hiding most strikes; it now defaults to unchecked so selecting an expiry shows the full strike ladder
- **Options Trader — expiry section headers**: clicking an expiry date row in the chain table now filters the dropdown to that date (click again to clear); selected row is highlighted

## [3.5.88] - 2026-04-24

### Changed
- **Options Trader — broker-routed chain**: options chain requests now route through the broker gateway so the chain is fetched from the selected account's own broker API (Tradier, Webull, or Alpaca); each broker connector gained a native `get_option_chain` implementation
- **Broker Gateway**: added `get_option_chain` command to router; routes to first matching connector for the requested account_label
- **Alpaca connector**: `get_option_chain` uses v1beta1 snapshots API (paginated, up to 20 pages × 250 contracts) with full Greeks and IV
- **Webull connector**: `get_option_chain` uses Webull Developer API expiration + chain endpoints
- **Tradier connector**: `get_option_chain` uses Tradier `/markets/options` API (parallel expiry fetch)
- **Base connector**: added non-abstract `get_option_chain` raising `NotImplementedError` as a clean fallback

## [3.5.87] - 2026-04-23

### Changed
- **Options Trader — chain source**: replaced Yahoo Finance with Tradier API (live or sandbox); falls back to yfinance when no Tradier key is configured. All expirations fetched in parallel. Greeks (delta/gamma/theta/vega) and IV now come from Tradier's real-time chain endpoint.
- **Options Trader — account dropdown**: now shows `display_name` env var when set, then `Broker (mode)`, instead of the raw internal label

## [3.5.86] - 2026-04-23

### Added
- **Options Trader page**: new dedicated trading dashboard at `page-options-trader`
  - Top-left account selector dropdown populates from live broker accounts
  - Left sidebar: scrollable open positions list + OVTLYR buy signals (sorted by nine_score)
  - Right top: LightweightCharts candlestick chart with EMA 10/20/50 overlays; earnings and ex-dividend date markers; entry price line for held positions
  - Right bottom: full options chain table (calls left, puts right, strike center) with bid/ask/mid, **extrinsic value**, delta, IV, OI; current positions highlighted in blue; expiration dates with open position marked with a blue dot
  - Chain controls: expiry selector, Puts toggle, Near ATM filter
  - Risk & Sizing Calculator panel (shown on position/ticker select): available cash, open position count, capital-at-risk, animated SVG risk gauge, contract sizing calculator by % risk
- **4 new API endpoints**: `/api/options/trader/buys`, `/api/options/trader/chain`, `/api/options/trader/ticker-meta`, `/api/options/trader/risk`
- Navigation entry "Options Trader" added to Options nav group

## [3.5.85] - 2026-04-20

### Fixed
- **Options dashboards blank**: duplicate `const tip` in `_greekTipShow` caused a SyntaxError that prevented the entire JS file from loading; renamed first binding to `tipDef`

## [3.5.84] - 2026-04-20

### Removed
- **Position Sizer dashboard**: removed nav item, page HTML, all JS functions, `/api/portfolio/accounts` endpoint, and warmup snapshot write

## [3.5.83] - 2026-04-20

### Fixed
- **All options endpoints returning 401**: `apiFetch` never auto-injected the session token, so every call that didn't explicitly append `?token=` failed — fixed `apiFetch` to always append the token if not already present; this fixes options positions, greeks, performance, expiry calendar, trading log, and position sizer accounts in one shot

## [3.5.82] - 2026-04-20

### Fixed
- **Browser cache serving stale JS**: Added `Cache-Control: no-store` headers to the root HTML response so the browser always fetches the latest frontend code; stale JS was calling `/api/options/greeks` (404) and old unauthenticated `/api/portfolio/accounts` patterns

## [3.5.81] - 2026-04-20

### Fixed
- **Position Sizer dropdown — 401 Unauthorized**: `/api/portfolio/accounts` was missing token auth so the auth middleware rejected every request; added `token` query param + `check_token()` to the endpoint, and updated the frontend `_sizerLoadAccounts()` to pass the session token

## [3.5.80] - 2026-04-20

### Fixed
- **Position Sizer dropdown — two root causes fixed**:
  - `capture_portfolio_snapshot` used the old NAV field lookup (missing `total_equity`) so Tradier accounts were saved with `total_nav=0` and never appeared in the DB fallback — fixed to use the same comprehensive lookup as `get_portfolio_accounts`
  - Startup warmup now writes a fresh portfolio snapshot to DB immediately after populating the in-memory cache, so the DB fallback is always up-to-date after restart (fixes the race where a first request arrives before the warmup completes)

## [3.5.79] - 2026-04-20

### Fixed
- **Position Sizer dropdown — Tradier accounts missing**: Tradier balance API returns `total_equity` for margin accounts while `equity` is always `0`; the lookup chain now checks `total_equity` and `account_value` before the generic `equity` field so Tradier accounts appear correctly

## [3.5.78] - 2026-04-20

### Fixed
- **Position Sizer dropdown**: was reading from `portfolio_snapshots` DB which is only populated by the scheduler and may be empty/stale; now reads from the live broker positions cache (pre-warmed on startup) with DB snapshots as fallback — accounts and NAV values always reflect current state

## [3.5.77] - 2026-04-20

### Fixed
- **Equities/Options dashboards slow load — root cause fixed**: all expensive caches are now pre-warmed on startup via `_warmup_caches()` background task so the first user to open any page hits a warm cache instead of a cold external API
  - Broker positions: populated immediately on startup (was 20-30s cold start on every server restart)
  - Options underlying prices: fetched from Polygon in parallel for all active positions on startup
  - SPY YTD benchmark: fetched on startup so `/api/options/performance` returns instantly
- **Broker positions cache TTL**: extended from 2 min → 5 min to reduce cold-miss frequency

## [3.5.76] - 2026-04-20

### Fixed
- **Options Dashboard — Polygon price fetches parallelized**: was calling Polygon one ticker at a time (sequential awaits); now uses `asyncio.gather` so all tickers resolve in one round trip instead of N serial requests
- **Options Dashboard — Yahoo recs parallelized**: was submitting one job to a thread pool that looped sequentially; now submits one job per ticker (up to 8 threads) so all `.info` calls run concurrently

## [3.5.75] - 2026-04-20

### Fixed
- **Options Dashboard prices**: ticker price fallback now tries Polygon.io (MASSIVE_API_KEY) before yfinance — faster and uses the preferred data source; yfinance only fires for any tickers Polygon doesn't cover

## [3.5.74] - 2026-04-20

### Fixed
- **Options Dashboard load time**: Redis-cache all yfinance fallback calls — prices cached 15 min (`yf:price:{sym}`), analyst recommendations cached 4 hrs (`yf:rec:{sym}`), SPY benchmark cached 1 hr (`yf:spy_ytd`); parallelized recommendation fetches from 1 → 4 workers; first load still calls Yahoo, all subsequent loads within TTL return instantly from Redis

## [3.5.73] - 2026-04-20

### Added
- **Position Sizer — account dropdown**: both Equity and Options calculators now have an account selector dropdown populated from the latest portfolio NAV snapshots; selecting an account prefills the Account Size field and locks it; choosing "Enter custom amount" clears the field for manual entry; backed by new `GET /api/portfolio/accounts` endpoint

## [3.5.72] - 2026-04-20

### Fixed
- **Greek chip tooltips not firing**: JSON.stringify inside a double-quoted HTML attribute broke attribute parsing; refactored to pass a short key string and look up tooltip content from a module-level _GREEK_TIPS map

## [3.5.71] - 2026-04-20

### Added
- **Options Dashboard — Greek chip tooltips**: hovering any of the four Greek chips (Δ, Θ, ν, Γ) shows a rich popup explaining the Greek's name, formula intuition, and impact on the options contract; tooltip is position-aware and flips above the chip when near the bottom of the viewport

## [3.5.70] - 2026-04-20

### Changed
- **Options Dashboard**: YTD Performance panel moved above Portfolio Greeks

## [3.5.69] - 2026-04-20

### Fixed
- **Options Trading Log — Historical shows only all-closed tickers**: a ticker with any active or rolled position now stays entirely in Active. Historical is restricted to tickers where every leg is closed or expired (only IAG in current data).

## [3.5.68] - 2026-04-20

### Fixed
- **Options Trading Log — rolled positions in Active section**: `rolled` status is now treated as Active (not Historical) because a rolled position is an ongoing multi-leg trade, not a completed one. Historical section is restricted to `closed` and `expired` only. Affects both the section filter and the ticker-row badge/collapse logic.

## [3.5.67] - 2026-04-20

### Changed
- **Options Trading Log — Active / Historical split**: positions are now separated into two sections instead of one flat tree. "Active Option Positions" (status=active or rolled) renders first with a blue count badge, always expanded. "Historical Trades" (closed/expired) renders below, collapsed by default with a click-to-expand toggle and count badge. Within each section the existing broker→account→ticker tree structure is preserved. Historical-only ticker rows start collapsed (▸) while active-containing ticker rows start open (▾). Historical position rows are visually muted (surface2 background, reduced opacity). Search filter applies across both sections simultaneously.

## [3.5.66] - 2026-04-20

### Added
- **Trade Journal** (Options Dashboard): ✏ button on every option position row; amber when a note exists, muted when empty; clicking opens a modal textarea that saves free-text notes to `option_positions.journal` via `PATCH /api/options/positions/{id}/journal`; button color and in-memory state update immediately on save without a full refresh; `ALTER TABLE trades ADD COLUMN notes TEXT` also added for future equity trade notes
- **Position Sizer** (new nav item under Trading): two-panel calculator — Equity/ETF side computes shares, dollar risk, position size, % of account, R:R ratio, and potential gain given account size, risk %, entry, stop, and optional target; Options side computes contracts, max risk, total premium, notional exposure, and target profit at a given exit % given account size, risk %, premium, underlying price, and target exit %; all calculations are real-time as you type with no server round-trips
- **Price Alerts** (new nav item under Trading): add ticker+condition(above/below)+target price+optional note alerts via POST `/api/alerts`; background `_price_alert_loop` checks active alerts every 5 minutes via Polygon.io (fallback yfinance) and fires a Telegram notification when triggered, then marks alert as `triggered`; manage page shows active/triggered/dismissed filter, last price with color-coded gap %, created date, re-activate and dismiss actions; `CREATE TABLE price_alerts` with status/triggered_at/last_price/last_checked columns

## [3.5.65] - 2026-04-20

### Fixed
- **EOD report option closures now include account/broker**: `_get_today_option_closures` joins `option_positions` to retrieve `account_label` and `broker`; template and LLM prompt now show `[account_label]` per closure row so the LLM correctly identifies multi-account positions rather than flagging them as duplicate entries

## [3.5.64] - 2026-04-20

### Fixed
- **EOD report missing manual option trades**: `_run_eod_report` now queries `option_trade_log` for `event_type = 'closed'` entries on the report date via `_get_today_option_closures()`; `_compute_stats` accepts the option closures and computes separate opt_closed/opt_wins/opt_losses/opt_win_rate/opt_pnl fields plus combined totals; template report gains an OPTIONS CLOSURES section and a COMBINED section when both equity and options are present; LLM prompt includes the options closure table and includes options P&L analysis in the requested report structure

## [3.5.63] - 2026-04-20

### Added
- **Options YTD Performance panel** (Options Dashboard + Trading Log): new `/api/options/performance` endpoint computes year-to-date trade statistics from `option_trade_log` (total trades, total P&L, win rate, avg win/loss USD, proven edge) and portfolio NAV return from `portfolio_snapshots`; SPY benchmark fetched via Polygon.io (fallback yfinance) to compute YTD/annualized returns, alpha (YTD and annualized); panel rendered on both Options Dashboard (above position controls) and Options Trading Log (above Most Profitable Tickers); avg win/loss shows % if `pnl_pct` is populated, otherwise falls back to USD per trade
- **Library search and sort**: live search field filters by title or ISBN as user types (strips dashes for ISBN matching); two-level sort controls (key select + direction toggle) for title, author, category, year, and date added; sort applies after search filter; all controls in a compact two-row bar above the book grid
- **Avatar upload in User Configuration**: clickable avatar circle (60×60) with camera icon overlay and "Remove avatar" button in username card; canvas center-crops and scales uploaded image to 128×128 JPEG; base64 stored in `user_preferences` key `avatar`; avatar displayed in profile card and 20×20 circle in topbar next to username

### Fixed
- **Options stat card hover tooltips**: `event.currentTarget` is null in inline `onmouseenter` handlers; fixed by passing `this` (the DOM element) instead of `event`, with `getBoundingClientRect()` called on the element directly; affects Active Alerts and Expiring ≤7d chips on Options Dashboard
- **OKLO expiry calendar wrong date**: `COALESCE(existing, new)` in both UPDATE and INSERT ON CONFLICT SQL paths for `expiration_date` and `strike` permanently froze stale v2 API values; fixed to `COALESCE(new, existing)` so fresh scan data overwrites stale; `_parse_v2_legs()` in `brokers/webull/positions.py` now falls back to top-level position fields when `legs[]` is absent; added `_parse_expiry_flexible()` handling ISO, YYYYMMDD, Unix-ms, and Unix-s date formats

## [3.5.62] - 2026-04-19

### Added
- **Portfolio NAV history chart** (Trading Dashboard): new `portfolio_snapshots` table captures EOD portfolio value per account; `job_eod_nav_snapshot` fires at 4:10 PM ET on trading days; `/api/portfolio/nav-history` endpoint returns 90-day series with drawdown %; Trading Dashboard now shows a 90-day SVG performance curve with total NAV, USD/% change vs first snapshot, and a one-click snapshot trigger
- **Options Portfolio Greeks panel** (Options Dashboard): Black-Scholes `_bs_greeks()` function now returns delta, theta, vega, and gamma (using stdlib math only); `option_positions` table gains `theta`, `vega`, `gamma` columns updated each scan; `/api/options/portfolio-greeks` aggregates all active positions to portfolio-level totals scaled by qty×100; Options Dashboard shows a 5-column summary bar (Δ, Θ/day, ν, Γ, positions) plus per-underlying breakdown table
- **Options Expiry Calendar** page (new nav item under Options): `/api/options/expiry-calendar` groups active positions by expiration date with DTE and urgency color-coding (critical ≤3d, warning ≤7d, caution ≤14d, ok); per-expiry totals for portfolio delta/theta/vega; expandable position table per expiry showing underlying, type, strike, qty, delta, theta/day, account
- **Daily drawdown / loss limit circuit breaker**: `max_daily_loss_usd` added to `config:risk_controls` defaults; `check_daily_loss()` and `record_trade_pnl()` added to `shared/risk_controls.py`; equity trader checks circuit breaker and daily loss limit before every order; `job_daily_loss_reset` fires at 9:30 AM ET to reset the intraday Redis counter; `/api/trading/daily-pnl` endpoint returns current P&L, limit, usage %, and circuit state; Trading Dashboard shows Daily P&L card with a loss budget progress bar that turns red when the limit is hit

### Changed
- `_bs_delta()` in `options_monitor` is now a thin wrapper around the new `_bs_greeks()` to maintain backward compatibility

## [3.5.61] - 2026-04-19

### Added
- **Sentiment sub-panel on Charts page**: after loading any chart, a new card appears below MACD showing the ticker's F&G composite score (0–100), color-coded label (Extreme Fear → Extreme Greed), four component progress bars (RSI, MA Score, Momentum, Volatility), and a 30-day sparkline trend line; hides automatically if no sentiment data exists for the symbol
- **Order fill status polling** (`broker_gateway/main.py`): new `_order_poll_loop` runs every 60 s alongside the command loop; tracks open order IDs per account, detects fills by comparing successive `get_orders` results, and emits fill events to `orders.events` stream so the review agent can flip trade status in the DB; interval configurable via `ORDER_POLL_INTERVAL_SEC`
- **Scheduler job execution history**: `@tracked` decorator now writes `last_run`, `last_status`, `last_error`, and `run_count` to Redis after every job execution (success and failure); `_publish_jobs` preserves these fields instead of overwriting with None; Scheduler UI now has a **Status** column — green "ok" chip, red "error" chip with hover tooltip showing the last error message, and run count badge
- **Market Clock color picker** (User Configuration → Clock & Display): `<input type="color">` sets the LED glow color on all six clock elements and the "OpenTrader" brand text simultaneously; persisted to `localStorage`; Reset button restores default blue (`#3b9eff`)
- **12hr/24hr time format toggle** (User Configuration → Clock & Display): radio buttons replace the old checkbox in Configuration; `_fmtTime(date, opts)` global helper respects `_clock12hr` and is used by all time-display call sites (trade tables, dividends, options formatter); AM/PM indicator appears inline next to the seconds digit in 12hr mode
- **User Configuration page** (renamed from My Profile): draggable nav items with persistent order stored in `user_preferences` DB table; Sector + Industry Exclusion panels side-by-side; Stock Exclusion + Risk Controls side-by-side; Industry Exclusion styled with green left border; Report Delivery moved into the Account card

### Fixed
- **`option_trade_log.realized_pnl` NULL for most positions**: three-part fix — (1) `not_in_scan` handler now sets `last_cp = 0.0` when option is past expiration and no price was captured (expired worthless); (2) `if ev["contract_price"]` truthiness checks replaced with `is not None` so `$0.00` prices are not discarded; (3) webui ticker endpoint now writes computed P&L back to `option_trade_log` and `option_positions.total_realized_pnl` on first fetch, preventing re-computation on every request
- **Scheduler execution history key mismatch**: `@tracked` was writing `scheduler:job:job_scrape_ovtlyr` while `_publish_jobs` reads `scheduler:job:scrape_ovtlyr`; fixed by stripping the `job_` prefix in the decorator

### Removed
- **Webull MCP server** (`ot-mcp-webull`): removed `mcp/webull-mcp/` directory, compose service, `webull-token` named volume, `MCP_SERVERS` entry, `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` from `.env.sample`, topology node + 3 edges, logs dropdown option, and service connector config block; broker API key/secret (used by the broker gateway directly) are retained

## [3.5.60] - 2026-04-17

### Added
- **Secure login system**: PBKDF2-SHA256 (260k iterations) password hashing, HMAC-SHA256 JWT session tokens stored in httpOnly + SameSite=Strict cookies; `Secure` flag set automatically when served over HTTPS via Caddy/Cloudflare
- **First-time setup flow**: `/setup` page shown when no users exist — creates the admin account then auto-logs in; redirects to `/login` if users already exist
- **Auth middleware**: all routes protected by session cookie or `?token=` query param (backwards compat); unauthenticated browser requests redirect to `/setup` or `/login` as appropriate
- **Encrypted secret storage** (`user_secrets` DB table): API keys stored encrypted with Fernet (AES-128-CBC + HMAC-SHA256) keyed from `SECRET_KEY` env var; loaded into process env on login and on startup; never returned to the browser
- **Platform → My Profile page**: avatar + username display, change-password form, full API key management grid (22 known secrets with set/unset status, inline update and delete)
- **Topbar**: username chip (links to profile) and Sign Out button added to all pages
- **New DB tables**: `users` (id, username, email, password_hash, is_admin, timestamps) and `user_secrets` (user_id FK, key, encrypted_value, description) — created via `CREATE TABLE IF NOT EXISTS` on startup
- **`cryptography>=42.0.0`** added to `requirements.webui.txt` for Fernet encryption
- **`SECRET_KEY` env var**: used for JWT signing and Fernet key derivation — should be set to a random 32+ char string in `.env`

## [3.5.59] - 2026-04-17

### Added
- **SSL / TLS management page** (Platform → SSL / TLS): cert status cards (Caddy health, validity, days until expiry, issuer, auto-renew), domain + ACME email configuration form, pipeline encryption status panel (web portal, Redis, PostgreSQL, MCP), Force Renewal action, and step-by-step setup guide
- **Caddy reverse proxy** (`ot-caddy` service in `compose.yml`): listens on ports 80/443, auto-obtains Let's Encrypt certificate for `CADDY_DOMAIN`, redirects HTTP→HTTPS, adds HSTS + security headers, gzip compression, access log rotation
- **`config/Caddyfile`**: template using `$CADDY_DOMAIN` / `$ACME_EMAIL` env vars; admin API on port 2019 for status queries; upstream health-check on `/api/ping`
- **API endpoints**: `GET /api/ssl/status` (Caddy health + cert file parse via openssl), `POST /api/ssl/configure` (writes domain/email to .env, reloads Caddy), `POST /api/ssl/renew` (force renewal via caddy reload)
- **`caddy-data` / `caddy-config` volumes** added to compose; `caddy-data` mounted read-only into webui container for cert file inspection; `CADDY_ADMIN_URL` env var wired to webui

## [3.5.58] - 2026-04-17

### Added
- **Topology — Auto Arrange button**: new button next to Reset Layout that automatically positions all nodes using a longest-path layering algorithm. Pipeline nodes (scrapers → aggregator → predictor → traders → gateway/review → chat) are assigned columns by data-flow rank; MCP nodes drop below the column of their primary consumer; scheduler/orchestrator sit at the bottom. SVG viewBox is refit to the computed bounding box. Reset Layout restores the original viewBox as well.

## [3.5.57] - 2026-04-17

### Changed
- **Dividends page — loading banner**: animated spinning indicator appears at the top of the page while broker data is being fetched; hides on completion or error. Added `@keyframes spin` CSS used by the banner icon.

## [3.5.56] - 2026-04-16

### Fixed
- **Options P&L formula** — `options_monitor` was using long-option convention `(exit−entry)` when auto-closing positions not seen in scan; corrected to short-option convention `(entry−exit)`, matching the formula in `webui`
- **Dividend page — exclude paper filter**: paper accounts were visible on initial load and account selection because `loadDividendPage` and `_divSelectAccount` bypassed `_divApplyFilter()`; both now route through the filter
- **Options capital efficiency denominator**: active positions' cost basis was included in ROC denominator, artificially deflating the metric — server SQL and client tree now exclude `status='active'` positions
- **Options P&L milestone fallback** (`webui/main.py`): `(cp−ep)` long convention used when `total_realized_pnl` is NULL in DB; corrected to `(ep−cp)` short convention

### Changed
- **Platform Dashboard enriched**: added Trade Mode (live/sandbox), System status (circuit broken/halted/normal), and Active Directives count as stat cards; all sourced from existing WS push data
- **Recent Events panel rewritten**: now surfaces system alerts, order fills/rejects, active directives, signals, and unhealthy agent heartbeats — sorted by priority and color-coded by event type; max 12 events
- **Topology diagram updated**: added `options-monitor`, `directive-agent`, `scraper-yahoo-sentiment` nodes with correct edges; added Logs dropdown entries for all three; viewBox expanded for new layout
- **ROC % standardized**: "Capital Efficiency" / "Cap Eff" labels unified to "ROC %" across summary card, account table header, and chip tooltip
- **Active Positions table**: added P&L % column; uses Alpaca `unrealized_plpc` (decimal→%) or computes from `pl÷costBasis` for other brokers
- **DB connection pooling**: migrated 6 remaining direct `asyncpg.connect()` calls (scheduler jobs, ovtlyr lists, sentiment trends, breadth history, position signals, sector map) to `_get_db_pool()` pool
- **Review agent**: migrated from Tradier-only fills to `BrokerRegistry.all_records()` — normalises filled order fields across Tradier, Alpaca, and Webull field naming conventions
- **Alpaca positions**: backfill `date_acquired` from order history for paper accounts that omit this field
- **OVTLYR scraper**: reads actual buy/sell signal from per-ticker dashboard page instead of inferring direction from the bull list API; Redis key renamed `scraper:ovtlyr:latest` → `scanner:ovtlyr:latest` (already in use by all consumers)
- **`shared/mcp_client.py`**: added `get_classification()` (Massive → Yahoo fallback), `get_massive_quote()`, `get_massive_daily_bars()`, `get_avg_volume()`, `get_uw_ticker_flow()`, `get_uw_darkpool()`, `get_uw_market_tide()`, `tv_confirms_direction()` helpers

## [3.5.55] - 2026-04-16

### Changed
- **Strategy rule enforcement** — price range and exclusions now live in `strategies.json`, not hard-coded in trader agents
  - Momentum Equity v4: `min_price: 25`, `max_price: 200`, `excluded_tickers: [PLTR, SOFI]`, `excluded_sectors: [Health Care]`, `excluded_industries: [Automotive, Airlines]`
  - `assignments.py` passes all rule fields through to trader at signal time
  - `exclusions.py` `is_excluded()` accepts strategy exclusions merged with user:exclusions
  - `equity_trader.py` price range enforced per-assignment after quote fetch; strategy exclusions merged and checked before order loop

### Fixed
- **Trading Dashboard — options trade count always showed 0**: Webull manual options trades are stored in `option_positions` DB, not the `orders.events` Redis stream. Added `GET /api/trades/options-stats` endpoint and updated chip to query DB for options count (30d window)
- **Win rate excluded all options trades**: now combines equity stream wins + DB options closed winners for combined win rate
- **options_trader published `event_type=submitted`** instead of `fill` — automated options orders were excluded from stream-based counts; changed to `fill` with underlying price recorded

## [3.5.54] - 2026-04-14

### Fixed
- Options Trading Log: filter all three endpoints (`/summary`, `/accounts`, `/ticker/{t}`) to `option_type IN ('call','put')` — excludes 5 Webull non-OCC `unknown`-type entries (WBL: symbols with no strike/expiry) that were appearing alongside proper option positions

## [3.5.53] - 2026-04-14

### Fixed
- Options Trading Log: `webull-live-4` account had 5 old positions stored with stale name "Webull live account 4" — corrected to "Webull IRA 2 Account" in DB

### Changed
- Options Trading Log "All Positions" view replaced with **broker → account → ticker tree**
  - Broker card (top level) with aggregate P&L
  - Account sub-section with per-account P&L
  - Ticker group (collapsible) with total P&L across all positions on that ticker
  - Each position row: entry date, type, strike, expiry, entry price, cost basis, qty, days, status, P&L
  - Milestone chain below each position: colored node per event (Open → Roll 1/2/3 → Closed/Expired) with contract price, cost basis, and per-event realized P&L at each stop
- Summary API now fetches non-scan events in a single batch query and attaches milestones + cost_basis to each position for the tree renderer

## [3.5.52] - 2026-04-14

### Added
- **Options Trading Log** page under the Options nav section — full 18-month trade history with P&L tracking
  - Top-tier summary cards: total P&L, position count, win rate, winners/losers
  - Most Profitable Tickers strip — click any ticker to open full history modal
  - Account-level breakdown table: per-account P&L, win rate, trade counts
  - Full positions table with search/filter by ticker and status (active/closed/rolled/expired)
  - Per-position detail modal: event timeline with risk levels, days between events, per-event P&L
  - **Post-close AI analysis**: "Run AI Analysis" button on closed positions — calls Claude Haiku to evaluate entry/exit timing, strike selection, risk management, and generates actionable improvement suggestions
- New API endpoints: `GET /api/options/log/summary`, `GET /api/options/log/accounts`, `GET /api/options/log/ticker/{ticker}`, `POST /api/options/log/analyze/{position_id}`
- DB migration: added `qty`, `entry_cost`, `exit_cost`, `realized_pnl`, `pnl_pct`, `risk_level` columns to `option_trade_log`; added `total_realized_pnl`, `ai_analysis`, `ai_analyzed_at` to `option_positions`

## [3.5.51] - 2026-04-14

### Fixed
- Options dashboard OVTLYR signal lookup: `_json` (undefined local alias) changed to `json` — silent NameError was causing all OVTLYR lookups to fail and fall through to Yahoo Finance

## [3.5.50] - 2026-04-14

### Fixed
- Options dashboard signal column: OVTLYR position intel (`ovtlyr:position_intel` / `scanner:ovtlyr:latest`) is now consulted before Yahoo Finance for tickers not in the predictor signals stream — previously options positions like SKM would show Yahoo's `underperform` → SELL 60% even when OVTLYR had an active Buy with a 9/9 nine_score
- OVTLYR-sourced confidence is derived from nine_score: `0.55 + (nine_score/9 × 0.40)` — a perfect 9/9 score yields 95% confidence

## [3.5.49] - 2026-04-14

### Fixed
- OVTLYR scraper now enriches open position tickers that are not in the current watchlist — their dashboard data (nine_score, oscillator, fear_greed, signal) is scraped and written into `scanner:ovtlyr:latest` with a seeded baseline entry so the predictor can see them

## [3.5.48] - 2026-04-14

### Added
- Predictor now ingests OVTLYR market breadth (`ovtlyr:market_breadth`) as a market regime filter: breadth < 40% blocks long signals, breadth > 60% blocks short signals; breadth alignment gives a small confidence nudge
- Predictor confidence now blends OVTLYR signal score (70%) with OVTLYR nine-panel score (30%) when nine_score is available from the dashboard scrape
- Scraper enriches `scanner:ovtlyr:latest` with per-ticker dashboard data: `nine_score`, `oscillator`, `fear_greed`, `signal_active`, `signal_date` after each watchlist scrape
- Predictor metadata now includes `nine_score`, `oscillator`, `fear_greed`, `breadth_pct`, `breadth_signal` for each scored ticker
- `scrapers/ovtlyr/main.py`: `_enrich_candidates()` calls `scrape_ticker()` for all watchlist candidates (not just open positions) and merges enrichment back into `scanner:ovtlyr:latest`

## [3.5.47] - 2026-04-14

### Fixed
- `shared/assignments.py`: `_asset_match` now splits comma-separated strategy asset strings (e.g. `"equity, etf"`) before comparing — was doing exact string match, so strategies with multi-asset fields never matched any signal and no trades fired
- `shared/assignments.py`: `max_pos_usd` uses `or 500` fallback instead of `.get("max_pos", 500)` — handles `null` JSON values where `.get()` returns `None` rather than the default

## [3.5.46] - 2026-04-13

### Fixed
- Options report download: removed pre-sort via dashboard sort controls (which could fail outside dashboard context); now goes through same `_optBuildReportHtml` path as email; added try/catch with error toast and success toast with filename
- Options signal column: predictor signals only cover OVTLYR tickers — added Yahoo Finance `recommendationKey` fallback for tickers not in the predictor stream (`strong_buy`→BUY 95%, `buy`→BUY 75%, `underperform`→SELL 60%, `sell`→SELL 80%, `strong_sell`→SELL 95%, `hold`→—)

## [3.5.45] - 2026-04-13

### Changed
- Options email report: removed Type column; sort changed from broker field to account name (account_name) then expiration date — matches user-visible account labels
- Options email report: server-side `_build_options_report_html` sort updated to match

## [3.5.44] - 2026-04-13

### Added
- Options dashboard table: Signal column between ATR and Emergency Exit — shows ▲ BUY / ▼ SELL with confidence % from predictor.signals stream; same signal included in download and email report between DTE and Earnings Date columns

## [3.5.43] - 2026-04-13

### Fixed
- Options schedule toggle state persistence: scheduler container wipes `scheduler:jobs` index set on startup, causing toggle to lose state; added `GET /api/jobs/{id}/state` endpoint that reads the job key directly (bypasses index) with DB fallback; `POST /api/jobs/{id}/toggle` now seeds from DB when key is missing rather than using hardcoded defaults; frontend `_optLoadScheduleState` updated to use the new state endpoint

## [3.5.42] - 2026-04-13

### Added
- Options dashboard: current underlying share price column in dashboard table and report — fetched from `sentiment:latest` Redis cache with yfinance batch download fallback when cache is empty
- Options report: buy/sell signal column (▲ BUY / ▼ SELL + confidence) sourced from predictor.signals stream
- Options report schedule toggle: ⏱ Schedule ON/OFF button to the right of the Email button; state persisted to TimescaleDB `scheduler_jobs` table and restored to Redis on webui startup; scheduler `job_options_report` checks enabled flag before sending
- Scheduler: `job_options_report` added — fires at 13:00 ET on trading days; checks Redis enabled flag; POSTs to `/api/options/report/email/auto` on webui

## [3.5.41] - 2026-04-12

### Fixed
- Trading Dashboard: Options Trades and Equity Trades counts now use a dedicated 30-day rolling fetch (`/api/trades?limit=500`) instead of the 5-entry WebSocket feed — trades made on Friday (or any day) persist across weekends and holidays until they age out of the 30-day window; the WS feed continues to drive the Recent Activity table only

## [3.5.40] - 2026-04-12

### Added
- Library: reader rank banner — 15-tier achievement system based on books-read count, from "Wall-Starer" (0 books) to "Oracle of Page Street" (200+); shows tier icon, title, witty subtitle, books-read badge, and progress bar to next rank

## [3.5.39] - 2026-04-12

### Changed
- Trading Dashboard: "Total Positions" and "Total Trades" stat cards now show equity / options split — each card displays two values side by side (equity in white, options in purple) with labels beneath; Win Rate is computed from equity fills only

## [3.5.38] - 2026-04-12

### Fixed
- Trades: reject reason now always shows a meaningful label — new events use the friendly message from the equity trader; old events with no stored reason derive context from the trade timestamp (weekend/holiday → "Market was closed", otherwise "Reason unknown")
- Trades: `_NYSE_HOLIDAYS` and market-closed check hoisted to `loadTradesPage` scope and reused by both day pills and reject-reason derivation (was re-declared inside the week loop on every iteration)

## [3.5.37] - 2026-04-12

### Added
- Equity trader: `_friendly_error()` maps raw broker error strings to human-readable reject reasons — covers market closed, insufficient buying power, asset not tradable, short selling not allowed, PDT restriction, invalid quantity, auth errors, network errors, and routing failures; unknown errors show the trimmed broker message (up to 80 chars); empty errors fall back to "Rejected"

## [3.5.36] - 2026-04-12

### Fixed
- Equity trader: `reject_reason` now captures Alpaca/broker error text correctly — `r.get("error", default)` was silently returning `""` when the key existed but was empty; changed to `r.get("error") or "gateway error"` so empty strings fall back to the default

## [3.5.35] - 2026-04-12

### Added
- Trades: each weekly section header now shows per-account trade tallies — colored broker dot + account display name + count chips, sorted by most trades; total badge renamed from "Weekly Trades" to "Total"

## [3.5.34] - 2026-04-12

### Added
- Left sidebar navigation reorganized into four sections: **Trading** (Dashboard, Directives, Charts, Broker), **Equities** (Trades, Active Positions, Dividends), **Options** (Options Dashboard), **Trading Plan**, **Resources**, **Platform**

### Changed
- Active Positions: filters out option contracts from position cards and heatmap — only equity/stock positions displayed; options tracked exclusively on Options Dashboard
- Trades: filters out option trades (`asset_class=option/us_option`) from trade history and open orders — equity trades only
- Dividends: backend `_is_equity_position()` helper filters options from holdings and ticker enrichment; options no longer inflate portfolio value totals

## [3.5.33] - 2026-04-12

### Fixed
- Options monitor: DTE now resolves correctly for Webull positions (SAN, NEM, LUNR, SKM were showing wrong expiry)
- Options monitor: chain lookup now scores contracts using bid/ask midpoint instead of stale `lastPrice` — prevents ITM calls from matching wrong expiry when Webull reports `last_price` equal to entry cost
- Options monitor: prefer-earlier-expiry tiebreaker — a later expiry must score 10% better to displace an earlier candidate, so chronologically earlier dates win ties
- Options monitor: removed debug enrichment_check log line

## [3.5.32] - 2026-04-12

### Added
- Webull positions v2 enrichment: broker gateway now calls `/openapi/assets/positions` (x-version: v2) using `WEBULL_APP_KEY`/`WEBULL_APP_SECRET` to extract `strikePrice`, `expiryDate`, and `right` (call/put) from each option position's `legs[]` array; falls back silently to v1 if unconfigured or unavailable
- v2 leg data injected directly into the raw position dict so options monitor can resolve expiry/strike/type without Yahoo Finance chain lookup

## [3.5.31] - 2026-04-11

### Removed
- Webull paper trading account — not supported by the Webull Official API; removed from accounts.toml, accounts.toml.sample, strategies.toml, webui connector config panel, .env.sample, and broker connection-check requirements

## [3.5.30] - 2026-04-11

### Added
- Options dashboard: inline DTE editor — click the ✏ pencil next to any DTE cell to open a date picker directly in the table row; press Enter or click away to save and lock; updates immediately without page reload

## [3.5.29] - 2026-04-11

### Added
- Options dashboard: **Edit & Lock** button in position modal — lets user correct strike, expiry date, and option type; locked values are never overwritten by automated scans
- Options monitor: `expiry_locked` column — when `true`, chain enrichment skips that position entirely

### Fixed
- Options monitor: `entry_date` was referenced before assignment causing all positions to fail enrichment (UnboundLocalError) — initialized before chain lookup
- Options chart: separate Y-axis scales for price history (left panel) and levels (right panel) — extreme strikes/levels no longer compress price history; levels panel shows all 6 levels with even spacing, dollar amount, and name label
- Options chart: level labels no longer overlap (evenly distributed across panel height)

## [3.5.28] - 2026-04-11

### Changed
- Options chart: solid opaque dark background (#0d1117); levels panel slightly lighter (#111820)
- Options chart: Exit/Roll/Emergency/Entry levels now draw only in the right "Levels" panel — price history occupies the left 70%, levels are confined to the right 30%

## [3.5.27] - 2026-04-11

### Fixed
- Options dashboard: expiry resolution now skips expiry dates that would have been < 14 DTE when the position was entered — prevents ITM calls (e.g. SAN $10 call) from matching near-weekly expiries whose price is indistinguishable due to intrinsic-value dominance

### Changed
- Options dashboard: position chart now shows 21 days of underlying price history (canvas line chart with gradient fill) with Exit Alert, Emergency, Entry, and Roll levels overlaid as horizontal lines extending into a "Levels →" target zone on the right

## [3.5.26] - 2026-04-11

### Added
- Options dashboard: 1st-tier and 2nd-tier sort controls — sortable by Ticker, Account, Qty, DTE with Asc/Desc toggle per tier
- Options dashboard: client-side SVG levels chart in position modal — shows Entry/Buy, Roll 1/2/3, Exit Alert, Emergency levels with color-coded zones; no server dependency (falls back from server-side matplotlib chart if unavailable)

### Fixed
- Options dashboard: account filter dropdown now shows friendly account names (e.g. "Webull IRA 1 Account") instead of raw account labels

## [3.5.25] - 2026-04-11

### Fixed
- Options dashboard: DTE now resolves correctly for longer-dated positions — chain lookup now scans up to 16 expiry dates (was 4) and picks the global best price match across all dates without early-exit; prevents deep-ITM calls from matching the nearest-expiry contract

## [3.5.24] - 2026-04-11

### Fixed
- Options dashboard: account name in watchlist chips and account column now uses `{LABEL}_DISPLAY_NAME` env vars (e.g. `WEBULL_LIVE_2_DISPLAY_NAME`) for friendly display names — previously fell through to accounts.toml notes field

## [3.5.23] - 2026-04-11

### Fixed
- Options dashboard: watchlist chip headers and account column subtext now show "Webull", "Alpaca", "Tradier" instead of raw broker IDs
- Options dashboard: Webull non-OCC positions now default to `call` type (env `WEBULL_DEFAULT_OPTION_TYPE`); prevents misidentification as put
- Options monitor: chain lookup now only queries calls when hint is "call" — eliminates cross-side mismatches like NEM showing as PUT

## [3.5.22] - 2026-04-11

### Added
- Options dashboard: **Qty** column showing number of contracts per position
- Options dashboard: **Delta** column — computed via Black-Scholes from Yahoo Finance implied volatility
- Options dashboard: automatic resolution of Webull option contract details (type/strike/expiry/delta) via Yahoo Finance option chain lookup on each scan
- Options dashboard: `delta` column added to `option_positions` DB table

### Fixed
- Options dashboard: type column now shows **UNK** (not PUT) for unresolved Webull contracts
- Options dashboard: strike column now shows **—** (not $0.00) when strike is null
- Options dashboard: Yahoo Finance chain API called with correct `option_type='calls'/'puts'` parameter
- Options monitor: `_normalise_option_position` now extracts option-specific fields (type/strike/expiry) from Webull raw API response fields when present

## [3.5.21] - 2026-04-10

### Added
- Webull MCP server: new `ot-mcp-webull` container (`mcp/webull-mcp/`) using `webull-openapi-mcp==0.1.0` with streamable-HTTP transport; named volume `webull-token` for OAuth token persistence
- Webull MCP: `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ENVIRONMENT`, `WEBULL_REGION_ID` added to `.env.sample` and compose env
- Webull MCP: added to chat-agent `MCP_SERVERS`, topology (node + 3 edges), Agents page, Logs dropdown, Service Connectors config panel
- Active Positions: server-side positions cache (`_positions_cache`, 120s TTL) — serves cached data instantly then refreshes in background; first load from broker takes ~20s, subsequent loads ~26ms
- Active Positions: `?force=true` query param on `/api/broker/positions` to bypass cache and force live fetch
- Active Positions: cache age badge ("cached Xs ago" / "live") and "↻ Refresh" button in page header

### Changed
- Alpaca MCP (`mcp/alpaca-mcp-server/Dockerfile`): now builds from `alpaca-mcp-server==2.0.0` via pip (was pulling external image)
- `get_broker_positions()` refactored: core fetch extracted to `_fetch_positions_from_gateway()`, cache logic in endpoint wrapper

### Fixed
- Agents page: `mcp-unusualwhales` and `mcp-webull` were missing from `KNOWN_AGENTS`, `PODMAN_HEALTH_ONLY`, `CONTAINER_MAP`
- Library page: table/grid view toggle (`_libSetView`) was not calling `_libRender()` — table was empty on switch
- Trading Dashboard breadth indicator: not called on page navigation (only on WS ticks); retry was blocked for 15s on fetch error

## [3.5.20] - 2026-04-09

### Added
- Dividends: four pie charts (Income by Ticker, Income by Sector, Income by Account, Best Dividend Payers by Yield %) moved to very top of page
- Dividends: "Best Dividend Payers" pie chart — top 10 held tickers ranked by forward dividend yield %, legend shows yield % per ticker
- Dividends: forecast API now returns `by_yield` array and `forward_yield_pct` per ticker in `by_ticker`
- Dividends: Received Dividends History — Table / Chart toggle with SVG bar chart of actual received payments
- Dividends: History chart supports three groupings — Month (default), Ticker, Account — via radio button selector
- Dividends: History chart respects account filter dropdown; shows grand total and per-bar dollar labels
- Dividends: History chart account grouping uses friendly display names
- Dividends: Received Dividends History account filter dropdown — filter table and chart by broker account using display name
- Dividends: account column in history table replaced with friendly display name (from broker env vars)

### Changed
- Dividends page layout: pie charts → stats → controls → account cards → 12-month bar chart → holdings → history

## [3.5.19] - 2026-04-08

### Added
- Backtrader engine: real backtesting replacing Monte Carlo simulation, with EMA 10/21 crossover strategy, stop-loss/take-profit management, and trade log
- Backtest exports: PDF and CSV trade reports for both saved version backtests and AI quick backtests
- Backtest chart: custom matplotlib chart with price/EMA/volume/equity curve panels and trade markers
- Backtest results modal: tabbed Summary / Trades / Chart view with inline download buttons
- Unusual Whales MCP: new FastMCP server with 8 tools (options flow, dark pool, market tide, greek exposure, short interest, OI change)
- Aggregator: Unusual Whales options flow and dark pool data integrated into TickerIntelligence (10 new fields, confidence delta ±0.06)
- Strategy Engineer: benchmark ticker field (default SPY) saved per strategy — no more prompt when running a backtest
- Backtest history: version backtests auto-open results modal on completion

### Fixed
- Directive agent: multi-ticker directives now execute all tickers (previously only first)
- Directive agent: direction mapping long→buy, sell→sell, short→sell_short (prevents rejected orders on no-shorting accounts)
- Backtest tab switching: scoped to nearest panel container to prevent ID collisions when inline and modal results coexist
- PDF export: missing Response import causing HTTP 500

### Changed
- Quick backtest endpoint now returns chart PNG (previously stripped)

---

## [3.5.18] - 2026-04-06

### Added
- Risk Controls: slippage % and liquidity (min volume K) filters in shared module, enforced by equity and options traders
- Trade Directives: natural-language trade portal with GTC directives evaluated every 5 min by LLM
- Directive Agent: new container service that evaluates directives, places orders, and sends notifications
- EOD Report: sector breakdown of new positions added
- Strategy Engineer: Risk Controls section in strategy document format
- WebUI: Risk Controls panel in User Settings, Trade Directives nav page

### Changed
- Yahoo Finance MCP: added get_avg_volume tool

---
---

## [3.5.17] - 2026-04-06

### Fixed
- `compose.yml` now mounts `./VERSION:/app/VERSION:ro` into the webui container so `_read_app_version()` can read the version file at `/app/VERSION` in local dev (previously the file was only present in CI-built images, causing the sidebar to show `vdev`)

---

## [3.5.16] - 2026-04-06

### Fixed
- Sidebar version now reads directly from the `VERSION` file on disk (local dev + Docker), falling back to the `APP_VERSION` env var only if the file isn't found — previously only worked in CI-built images

---

## [3.5.15] - 2026-04-06

### Changed
- Strategy Engineer now accepts entry-only, exit-only, or full strategies via a `Type: entry | exit | full` field
- System prompt updated to explain which fields are required per type; entry strategies omit Stop Loss/Take Profit, exit strategies omit Asset Class/Direction/Confidence/Entry Signals
- Strategy parser updated to set inapplicable fields to `null` instead of applying hard fallbacks (e.g. entry strategies no longer silently inherit `stop_pct=1.5`)
- Strategy document placeholder updated to reflect the new format

---

## [3.5.14] - 2026-04-06

### Added
- Sidebar "Command Center" label now shows the live release version (e.g. `v3.5.14`) injected at container build time via `APP_VERSION` build-arg; updates automatically on every release

---

## [3.5.13] - 2026-04-06

### Added
- Broker panel account rows now show a blue indicator dot when a strategy is actively assigned to that account; hovering the dot shows a tooltip with the strategy name

---

## [3.5.12] - 2026-04-06

### Changed
- Equity and options traders no longer contain embedded trading strategies
- Strategy parameters (min confidence, max position size) now come exclusively from the Strategy Assignment workflow via `assignments.json` + `strategies.json`
- Both traders route orders to specific `account_label` from the assignment instead of using `strategy_tag` filtering
- Strategy names in order events are now taken from the assignment (`strategy_name`) not hardcoded strings
- Removed hardcoded `_route_account()` from options trader (was always returning Tradier regardless of assignment)
- Options trader now waits for gateway reply via `blpop` (consistent with equity trader)

### Added
- `python/shared/assignments.py` — `load_active_assignments(asset_class)` joins assignments and strategies, returns per-account execution parameters

---

## [3.5.11] - 2026-04-06

### Added
- Google Books connector card in Configuration panel now shows the Google Books avatar logo

---

## [3.5.10] - 2026-04-06

### Added
- Massive connector card in Configuration panel now shows the Massive avatar logo

---

## [3.5.9] - 2026-04-05

### Fixed
- Category field in the Add/Edit book modal is now a `<select>` dropdown populated from the managed category list, so newly added categories are immediately available when editing an existing book

---

## [3.5.8] - 2026-04-05

### Added
- `library_categories` table stores managed category list in the database
- `GET/POST /api/library/categories` and `DELETE /api/library/categories/{name}` endpoints
- "＋ Add Category…" option always anchored at the bottom of the category filter dropdown
- Small "Add Category" modal opens when the option is selected; saves to DB and selects the new category
- Category field in the Add/Edit book modal uses `<datalist>` for autocomplete from stored categories
- Categories auto-upserted to `library_categories` when a book is saved or edited with a new category

---

## [3.5.7] - 2026-04-05

### Fixed
- Library tile view: cover art now uses `object-fit:contain` (no cropping) with padding, height increased to 260px for proper book cover proportions

---

## [3.5.6] - 2026-04-05

### Fixed
- All `showToast()` calls in Library and Strategy Assignment JS replaced with correct `toast(type, title, msg)` signature — save, delete, exclusion, and assignment actions now show proper feedback toasts

---

## [3.5.5] - 2026-04-05

### Fixed
- Library save broken (token read from localStorage instead of sessionStorage)
- Added Review/Comments field to Library modal with star rating
- Review shown in detail drawer with amber accent

---

## [3.5.4] - 2026-04-05

### Added
- Google Books API connector in Configuration page
- Google Books as tertiary ISBN fallback in Library (after Open Library)
- GOOGLE_BOOKS_API_KEY saved via standard .env connector flow

---

## [3.5.3] - 2026-04-05

### Added
- Resources section in nav with Library page
- Trading book library with ISBN lookup (Open Library API)
- Tile and table view with cover art, star ratings, status tracking
- Filter and sort by title, author, category
- Detail drawer and add/edit modal with ISBN auto-fill

---

## [3.5.2] - 2026-04-05

### Added
- Strategy Assignment dashboard with global exclusions and version tracking
- EMA 10 indicator overlay on Charts page
- Charts moved above Broker in nav
- Release versioning system with GitHub Actions CI/CD

### Fixed
- Strategy Assignment modal placement (was inside display:none parent)
- Assignment modal now fetches data on demand if page not yet loaded

---

## [3.5.1] - 2026-04-05

### Added
- TradingView Charts page with position picker across all broker connectors
- Client-side technical indicators: EMA 10/20/50/200, SMA 20, Bollinger Bands, RSI 14, MACD
- Indicator toggles with localStorage persistence
- Exchange auto-resolution for TradingView (NASDAQ → NYSE → AMEX → NYSE_ARCA → NYSE_MKT)
- OVTLYR market breadth widget: semicircle gauge, sparkline, crossover detection
- Market breadth pipeline: scraper → Redis → TimescaleDB → API → dashboard
- MCP Massive agent added to platform topology diagram
- Broker/paper account filter on Charts position picker
- Fear & Greed trend data now populating correctly

### Fixed
- `/api/sentiment` returning 401 due to erroneous token check (read-only endpoint)
- `pkg_resources` missing in Python 3.12-slim webui container
- `signal.alarm` thread error in tradingview-scraper (switched to ProcessPoolExecutor)
- OHLCV streamer returning generator instead of data (export_result=True)
- CCL and other NYSE tickers rejected as invalid NASDAQ exchange symbols
- MCP Massive topology node rendering off-screen

---
