# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

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
