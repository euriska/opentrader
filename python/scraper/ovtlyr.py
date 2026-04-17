"""
OVTLYR Playwright Scraper
Logs into ovtlyr.com and reads buy/sell signals from the per-ticker dashboard.

OVTLYR is a momentum/breakout screener. The scraper:
  1. Logs in once and keeps the session warm
  2. Gets the ticker list from the watchlist API
  3. For each ticker, navigates to dashboard/<ticker> and reads the OVTLYR signal panel
  4. Returns list of OvtlyrTicker with the actual buy/sell direction from the dashboard

Set env vars:
  OVTLYR_EMAIL
  OVTLYR_PASSWORD
  OVTLYR_DEBUG=1   — saves screenshots to /app/logs/ for debugging
"""
import os
import asyncio
import re
from typing import List, Optional

import structlog
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from .models import OvtlyrTicker

log = structlog.get_logger("scraper.ovtlyr")

LOGIN_URL     = "https://console.ovtlyr.com/login"
DASHBOARD_URL = "https://console.ovtlyr.com/dashboard"
WATCHLIST_URL = "https://console.ovtlyr.com/watchlist"
SCREENER_URL  = "https://console.ovtlyr.com/screener"
# Fallback: SPY dashboard shows top movers / market overview
MARKET_URL    = "https://console.ovtlyr.com/dashboard/SPY"


class OvtlyrScraper:
    """
    Persistent Playwright session for OVTLYR.
    Call start() once, then scrape() as many times as needed.
    """

    def __init__(self):
        self._pw:      Optional[Playwright]    = None
        self._browser: Optional[Browser]       = None
        self._ctx:     Optional[BrowserContext] = None
        self._page:    Optional[Page]          = None
        self._logged_in = False

    async def start(self):
        """Launch browser and log in."""
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._ctx  = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._ctx.new_page()
        await self._login()

    async def _login(self):
        email    = os.environ.get("OVTLYR_EMAIL", "")
        password = os.environ.get("OVTLYR_PASSWORD", "")

        if not email or not password:
            log.warning("ovtlyr.no_credentials")
            return

        try:
            await self._page.goto(LOGIN_URL, timeout=30_000)
            await self._page.wait_for_load_state("domcontentloaded", timeout=20_000)
            await asyncio.sleep(3)  # let SPA JS render

            # Debug: log page title and URL to help diagnose selector mismatches
            title = await self._page.title()
            log.info("ovtlyr.login_page", url=self._page.url, title=title)

            # Save screenshot for debugging if OVTLYR_DEBUG=1
            if os.environ.get("OVTLYR_DEBUG") == "1":
                await self._page.screenshot(path="/app/logs/ovtlyr_login.png")

            # Try multiple email selectors in order
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="user" i]',
                'input[autocomplete="email"]',
                'input[autocomplete="username"]',
                'input:not([type="password"]):not([type="hidden"]):not([type="submit"])',
            ]
            pw_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="password" i]',
            ]
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Log")',
                'button:has-text("Sign")',
                '[class*="login" i] button',
                '[class*="submit" i]',
            ]

            filled_email = False
            for sel in email_selectors:
                try:
                    locator = self._page.locator(sel).first
                    await locator.wait_for(state="visible", timeout=5_000)
                    await locator.fill(email)
                    filled_email = True
                    log.info("ovtlyr.email_filled", selector=sel)
                    break
                except Exception:
                    pass

            if not filled_email:
                log.error("ovtlyr.no_email_input")
                return

            filled_pw = False
            for sel in pw_selectors:
                try:
                    locator = self._page.locator(sel).first
                    await locator.wait_for(state="visible", timeout=5_000)
                    await locator.fill(password)
                    filled_pw = True
                    log.info("ovtlyr.password_filled", selector=sel)
                    break
                except Exception:
                    pass

            if not filled_pw:
                log.error("ovtlyr.no_password_input")
                return

            for sel in submit_selectors:
                try:
                    locator = self._page.locator(sel).first
                    await locator.wait_for(state="visible", timeout=3_000)
                    await locator.click()
                    log.info("ovtlyr.submitted", selector=sel)
                    break
                except Exception:
                    pass

            await self._page.wait_for_load_state("networkidle", timeout=20_000)

            # Confirm login succeeded — check we're not still on login page
            if "login" not in self._page.url and "signin" not in self._page.url:
                self._logged_in = True
                log.info("ovtlyr.logged_in", url=self._page.url)
            else:
                log.error("ovtlyr.login_failed", url=self._page.url)
                if os.environ.get("OVTLYR_DEBUG") == "1":
                    await self._page.screenshot(path="/app/logs/ovtlyr_login_failed.png")

        except Exception as e:
            log.error("ovtlyr.login_error", error=str(e))

    # Watchlist API handlers used only for getting the ticker list
    _WATCHLIST_HANDLERS = [
        "GetWatchList",
        "GetBullsList_Stocks",
        "GetBullsList_ETFs",
        "AjaxGetHighCoverage_Stocks",
    ]

    async def scrape(self) -> List[OvtlyrTicker]:
        """
        Main entry point:
          1. Pull the ticker list from the watchlist API handlers
          2. For each ticker, navigate to dashboard/<ticker> and read the actual
             OVTLYR buy/sell signal from the signal panel
        Returns OvtlyrTicker objects with direction sourced from the dashboard,
        not inferred from the bull list.
        """
        if not self._logged_in:
            log.warning("ovtlyr.not_logged_in_retry")
            await self._login()
            if not self._logged_in:
                return []

        tickers = await self._get_watchlist_tickers()
        if not tickers:
            log.warning("ovtlyr.no_tickers_from_watchlist")
            return []

        log.info("ovtlyr.watchlist_tickers", count=len(tickers))
        return await self.scrape_dashboards(tickers)

    async def _get_watchlist_tickers(self) -> List[str]:
        """
        Hit the watchlist Razor Page handlers to collect the ticker list.
        Returns a deduplicated list of ticker symbols.
        """
        # Ensure session is on the watchlist page
        try:
            if "watchlist" not in self._page.url:
                await self._page.goto(WATCHLIST_URL, timeout=30_000)
                await self._page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(2)
        except Exception as e:
            log.warning("ovtlyr.goto_watchlist_error", error=str(e))

        seen: set = set()
        tickers: List[str] = []

        for handler in self._WATCHLIST_HANDLERS:
            url = f"{WATCHLIST_URL}?handler={handler}"
            try:
                result = await self._page.evaluate(f"""
                    async () => {{
                        const r = await fetch("{url}", {{
                            headers: {{
                                "Accept": "application/json",
                                "X-Requested-With": "XMLHttpRequest"
                            }},
                            credentials: "include"
                        }});
                        if (!r.ok) return null;
                        const ct = r.headers.get("content-type") || "";
                        if (!ct.includes("json")) return null;
                        return await r.json();
                    }}
                """)
                if not result:
                    continue

                batch = self._extract_tickers_from_response(result)
                added = 0
                for t in batch:
                    if t not in seen:
                        seen.add(t)
                        tickers.append(t)
                        added += 1
                log.info("ovtlyr.watchlist_handler", handler=handler, added=added)

            except Exception as e:
                log.warning("ovtlyr.watchlist_handler_error", handler=handler, error=str(e))

        return tickers

    async def scrape_dashboards(self, tickers: List[str]) -> List[OvtlyrTicker]:
        """
        For each ticker, navigate to dashboard/<ticker> and extract the OVTLYR
        buy/sell signal from the signal panel.

        Throttled to avoid hammering the server — 1 page at a time with a brief
        delay between requests.
        """
        results: List[OvtlyrTicker] = []
        debug = os.environ.get("OVTLYR_DEBUG") == "1"

        for ticker in tickers:
            url = f"{DASHBOARD_URL}/{ticker}"
            try:
                await self._page.goto(url, timeout=30_000)
                await self._page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(1.5)  # let SPA render signal panel

                if debug:
                    safe = ticker.replace("/", "_")
                    await self._page.screenshot(
                        path=f"/app/logs/ovtlyr_dashboard_{safe}.png",
                        full_page=True,
                    )

                signal = await self._extract_dashboard_signal(ticker)
                if signal:
                    direction, score = signal
                    results.append(OvtlyrTicker(
                        ticker    = ticker,
                        direction = direction,
                        score     = score,
                        metadata  = {"source": "dashboard", "url": url},
                    ))
                    log.info("ovtlyr.dashboard_signal",
                             ticker=ticker, direction=direction, score=score)
                else:
                    log.warning("ovtlyr.dashboard_no_signal", ticker=ticker)

            except Exception as e:
                log.warning("ovtlyr.dashboard_error", ticker=ticker, error=str(e))

        log.info("ovtlyr.dashboards_scraped",
                 total=len(tickers), signals=len(results))
        return results

    async def _extract_dashboard_signal(
        self, ticker: str
    ) -> Optional[tuple]:
        """
        Extract the OVTLYR buy/sell signal from a dashboard page.

        Tries multiple strategies in order:
          1. Signal badge/chip elements (class names containing 'signal', 'badge', etc.)
          2. Text proximity — finds "Buy"/"Sell" near "OVTLYR" or "Signal" headings
          3. OVTLYR 9-panel — reads the 9 indicator chips and derives consensus
          4. Raw buy/sell word count across the visible page text
        """
        result = await self._page.evaluate("""
            (ticker) => {
                const norm = s => (s || '').trim().toUpperCase();

                // ── Strategy 1: Signal/recommendation badge elements ─────────
                const badgeSels = [
                    '[class*="signal" i]',
                    '[class*="recommendation" i]',
                    '[class*="badge" i]',
                    '[class*="status" i]',
                    '[class*="label" i]',
                    '[class*="chip" i]',
                    '[class*="tag" i]',
                    '[class*="indicator" i]',
                    '[class*="action" i]',
                ];
                for (const sel of badgeSels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = norm(el.innerText);
                        if (t === 'BUY' || t === 'STRONG BUY')   return {direction: 'long',  score: t === 'STRONG BUY' ? 90 : 75, strategy: 'badge', text: t};
                        if (t === 'SELL' || t === 'STRONG SELL')  return {direction: 'short', score: t === 'STRONG SELL' ? 85 : 70, strategy: 'badge', text: t};
                    }
                }

                // ── Strategy 2: Text proximity to OVTLYR/Signal headings ─────
                const allEls = Array.from(document.querySelectorAll('*'));
                for (let i = 0; i < allEls.length; i++) {
                    const el = allEls[i];
                    if (el.children.length > 5) continue;
                    const t = norm(el.innerText);
                    if (!t.includes('OVTLYR') && !t.includes('SIGNAL') && !t.includes('CURRENT SIGNAL'))
                        continue;
                    // Check next 10 siblings/children for Buy/Sell
                    const parent = el.parentElement;
                    if (!parent) continue;
                    const siblings = Array.from(parent.querySelectorAll('*')).slice(0, 20);
                    for (const s of siblings) {
                        const st = norm(s.innerText);
                        if (st === 'BUY' || st === 'STRONG BUY')   return {direction: 'long',  score: st === 'STRONG BUY' ? 90 : 75, strategy: 'proximity', text: st};
                        if (st === 'SELL' || st === 'STRONG SELL')  return {direction: 'short', score: st === 'STRONG SELL' ? 85 : 70, strategy: 'proximity', text: st};
                    }
                }

                // ── Strategy 3: OVTLYR 9-panel — count indicator buy/sell chips
                // The 9-panel shows individual indicator states; majority rules.
                const allText = document.body.innerText || '';
                const lines = allText.split(/[\\n\\r]+/).map(l => l.trim()).filter(Boolean);
                let panelBuy = 0, panelSell = 0, inPanel = false;
                for (let i = 0; i < lines.length; i++) {
                    const u = lines[i].toUpperCase();
                    if (u.includes('OVTLYR') || u.includes('9') || u.includes('PANEL')) inPanel = true;
                    if (!inPanel) continue;
                    if (u === 'BUY' || u === 'STRONG BUY' || u === 'BULLISH') panelBuy++;
                    if (u === 'SELL' || u === 'STRONG SELL' || u === 'BEARISH') panelSell++;
                    // Stop counting after 20 lines past the panel start
                    if (panelBuy + panelSell >= 9) break;
                }
                if (panelBuy + panelSell > 0) {
                    const pct = panelBuy / (panelBuy + panelSell);
                    return {
                        direction: pct >= 0.5 ? 'long' : 'short',
                        score: Math.round(pct >= 0.5 ? 55 + pct * 35 : 55 + (1 - pct) * 35),
                        strategy: 'panel9',
                        text: `buy=${panelBuy} sell=${panelSell}`,
                    };
                }

                // ── Strategy 4: Raw buy/sell word count (last resort) ─────────
                const buyCount  = (allText.match(/\\bBUY\\b/gi)  || []).length;
                const sellCount = (allText.match(/\\bSELL\\b/gi) || []).length;
                if (buyCount + sellCount > 0) {
                    const pct = buyCount / (buyCount + sellCount);
                    return {
                        direction: pct >= 0.5 ? 'long' : 'short',
                        score: Math.round(pct >= 0.5 ? 50 + pct * 30 : 50 + (1 - pct) * 30),
                        strategy: 'wordcount',
                        text: `buy=${buyCount} sell=${sellCount}`,
                    };
                }

                return null;
            }
        """, ticker)

        if not result:
            return None

        log.debug("ovtlyr.signal_extracted",
                  ticker=ticker,
                  strategy=result.get("strategy"),
                  text=result.get("text"),
                  direction=result.get("direction"))

        return result.get("direction", "long"), float(result.get("score", 70.0))

    def _parse_handler_response(
        self,
        data,
        default_direction: Optional[str],
        source_url: str,
    ) -> List[OvtlyrTicker]:
        """Parse a single API handler JSON response into OvtlyrTicker list."""
        results: List[OvtlyrTicker] = []

        # Unwrap common envelope shapes
        entries = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            for key in ("data", "results", "stocks", "etfs", "watchlist", "items", "list", "tickers"):
                if key in data and isinstance(data[key], list):
                    entries = data[key]
                    break
            if not entries and "symbol" in data:
                entries = [data]  # single-item response

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # Find ticker
            ticker = None
            for k in ("symbol", "ticker", "stock", "sym", "Symbol", "Ticker"):
                v = entry.get(k, "")
                if v and re.match(r'^[A-Z]{1,5}$', str(v).strip().upper()):
                    ticker = str(v).strip().upper()
                    break

            if not ticker:
                continue

            # Find direction
            direction = default_direction or "long"
            for k in ("signal", "direction", "recommendation", "action",
                      "Signal", "Direction", "currentSignal", "current_signal"):
                v = entry.get(k, "")
                if v:
                    s = str(v).lower()
                    if any(w in s for w in ("sell", "short", "bear")):
                        direction = "short"
                    elif any(w in s for w in ("buy", "long", "bull")):
                        direction = "long"
                    break

            # Find score
            score = 75.0 if default_direction == "long" else 60.0
            for k in ("score", "confidence", "strength", "rank", "rating",
                      "Score", "Confidence", "signalStrength"):
                v = entry.get(k)
                if v is not None:
                    try:
                        f = float(v)
                        score = f * 100 if f <= 1.0 else f
                        score = min(score, 100.0)
                        break
                    except (ValueError, TypeError):
                        pass

            # Optional fields
            price      = entry.get("price") or entry.get("Price") or entry.get("lastPrice")
            change_pct = entry.get("change_pct") or entry.get("changePct") or entry.get("changePercent")
            sector     = entry.get("sector") or entry.get("Sector")

            results.append(OvtlyrTicker(
                ticker     = ticker,
                direction  = direction,
                score      = score,
                price      = float(price)      if price      else None,
                change_pct = float(change_pct) if change_pct else None,
                sector     = str(sector)       if sector     else None,
                metadata   = {"source_url": source_url},
            ))

        return results

    def _extract_tickers_from_response(self, data) -> List[str]:
        """
        Extract just ticker symbols from a watchlist API response.
        Used by _get_watchlist_tickers() to build the ticker list for dashboard scraping.
        """
        entries = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            for key in ("data", "results", "stocks", "etfs", "watchlist", "items", "list", "tickers"):
                if key in data and isinstance(data[key], list):
                    entries = data[key]
                    break
            if not entries and "symbol" in data:
                entries = [data]

        tickers = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for k in ("symbol", "ticker", "stock", "sym", "Symbol", "Ticker"):
                v = entry.get(k, "")
                if v and re.match(r'^[A-Z]{1,5}$', str(v).strip().upper()):
                    tickers.append(str(v).strip().upper())
                    break
        return tickers

    async def _extract_tickers(self) -> List[OvtlyrTicker]:
        """
        DOM extraction strategies for OVTLYR watchlist.
        The watchlist has two panels: Favorites (left) and Bull List (right).
        Each row shows: Symbol chip | Current Signal (Buy/Sell) | Last Signal Date
        """
        results: List[OvtlyrTicker] = []
        seen: set = set()

        # Strategy 1 — table rows (most reliable for structured data)
        rows = await self._page.query_selector_all(
            "table tbody tr, [class*='Row'], [class*='row'], [role='row']"
        )
        for row in rows[:100]:
            try:
                t = await self._parse_row(row)
                if t and t.ticker not in seen:
                    results.append(t)
                    seen.add(t.ticker)
            except Exception:
                pass

        if results:
            return results

        # Strategy 2 — look for elements containing ticker + signal text
        # OVTLYR watchlist shows ticker chips next to Buy/Sell badges
        # Try to find parent containers that have both a ticker and a signal
        containers = await self._page.query_selector_all(
            "[class*='watchlist' i] *[class*='item' i], "
            "[class*='list' i] *[class*='item' i], "
            "[class*='card'], [class*='stock'], [data-symbol], [data-ticker]"
        )
        for el in containers[:100]:
            try:
                t = await self._parse_card(el)
                if t and t.ticker not in seen:
                    results.append(t)
                    seen.add(t.ticker)
            except Exception:
                pass

        if results:
            return results

        # Strategy 3 — JS evaluate: find all text nodes matching ticker pattern
        # near Buy/Sell text
        try:
            js_tickers = await self._page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    // Walk all elements, find ones that look like tickers (2-5 uppercase)
                    document.querySelectorAll('*').forEach(el => {
                        const text = el.innerText || '';
                        if (!text || text.length > 20 || el.children.length > 3) return;
                        const m = text.trim().match(/^([A-Z]{2,5})$/);
                        if (!m) return;
                        const ticker = m[1];
                        if (seen.has(ticker)) return;
                        seen.add(ticker);
                        // Look for Buy/Sell in siblings/parent
                        const parent = el.closest('[class]');
                        const parentText = parent ? parent.innerText : '';
                        const direction = /sell/i.test(parentText) ? 'short' :
                                          /buy/i.test(parentText)  ? 'long'  : 'long';
                        results.push({ticker, direction, score: 75.0});
                    });
                    return results.slice(0, 60);
                }
            """)
            for item in (js_tickers or []):
                t_str = item.get("ticker", "")
                if t_str and t_str not in seen:
                    results.append(OvtlyrTicker(
                        ticker    = t_str,
                        direction = item.get("direction", "long"),
                        score     = item.get("score", 75.0),
                    ))
                    seen.add(t_str)
        except Exception as e:
            log.warning("ovtlyr.js_extract_error", error=str(e))

        if results:
            return results

        # Strategy 4 — regex over full page HTML (last resort)
        content = await self._page.content()
        return self._parse_page_text(content)

    async def _parse_row(self, row) -> Optional[OvtlyrTicker]:
        text = (await row.inner_text()).strip()
        ticker = self._find_ticker(text)
        if not ticker:
            return None

        direction = self._direction_from_text(text)
        score     = 80.0 if direction == "long" else 65.0
        return OvtlyrTicker(ticker=ticker, direction=direction, score=score)

    async def _parse_card(self, card) -> Optional[OvtlyrTicker]:
        text = (await card.inner_text()).strip()

        # Try data attribute first for ticker
        data_ticker = await card.get_attribute("data-ticker") or \
                      await card.get_attribute("data-symbol")
        if data_ticker and re.match(r'^[A-Z]{1,5}$', data_ticker.strip().upper()):
            ticker = data_ticker.strip().upper()
        else:
            ticker = self._find_ticker(text)

        if not ticker:
            return None

        direction = self._direction_from_text(text)
        score     = 80.0 if direction == "long" else 65.0
        return OvtlyrTicker(ticker=ticker, direction=direction, score=score)

    @staticmethod
    def _direction_from_text(text: str) -> str:
        t = text.upper()
        if any(w in t for w in ("SELL", "SHORT", "BEAR")):
            return "short"
        if any(w in t for w in ("BUY", "LONG", "BULL")):
            return "long"
        return "long"  # default: assume bullish from OVTLYR

    def _parse_page_text(self, html: str) -> List[OvtlyrTicker]:
        """Pull ticker symbols out of raw HTML as a fallback."""
        import re
        # Look for patterns like: AAPL, TSLA, NVDA in context
        ticker_re = re.compile(r'\b([A-Z]{2,5})\b')
        common_words = {
            "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
            "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM",
            "HIS", "HOW", "ITS", "LET", "MAN", "MAY", "NEW", "NOW", "OLD",
            "SEE", "TWO", "WAY", "WHO", "BOY", "DID", "ITS", "LET", "PUT",
            "SAY", "SHE", "TOO", "USE", "HTML", "BODY", "HEAD", "SPAN", "DIV",
            "HREF", "CLASS", "STYLE", "TYPE", "DATA", "ARIA", "ROLE", "LINK",
            "META", "TITLE", "SCRIPT", "REACT", "NULL", "TRUE", "FALSE",
        }
        seen = set()
        results = []
        for m in ticker_re.finditer(html):
            t = m.group(1)
            if t in seen or t in common_words or len(t) < 2:
                continue
            seen.add(t)
            results.append(OvtlyrTicker(ticker=t, direction="long", score=50.0))
            if len(results) >= 30:
                break
        return results

    @staticmethod
    def _find_ticker(text: str) -> Optional[str]:
        """Extract a stock ticker from a row/card text snippet."""
        m = re.search(r'\b([A-Z]{1,5})\b', text)
        if m and len(m.group(1)) >= 2:
            return m.group(1)
        return None

    @staticmethod
    def _find_score(text: str) -> float:
        """Try to extract a numeric confidence/score from text."""
        m = re.search(r'(\d{1,3})(?:\.\d+)?%?', text)
        if m:
            v = float(m.group(1))
            return min(v, 100.0)
        return 50.0

    async def warmup(self):
        """Pre-market warmup — just refresh the session."""
        if not self._logged_in:
            await self._login()
        else:
            try:
                await self._page.reload(timeout=15_000)
                log.info("ovtlyr.warmed_up")
            except Exception as e:
                log.warning("ovtlyr.warmup_failed", error=str(e))
                await self._login()

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._logged_in = False
