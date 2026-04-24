# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

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
