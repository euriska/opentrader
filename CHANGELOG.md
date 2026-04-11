# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

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
