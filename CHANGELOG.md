# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

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
