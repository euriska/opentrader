# Changelog

All notable changes to OpenTrader will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — versioning follows [Semantic Versioning](https://semver.org/).

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
