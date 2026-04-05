"""
OVTLYR Playwright Scraper
Logs into ovtlyr.com and scrapes the watchlist / screener page.

OVTLYR is a momentum/breakout screener. The scraper:
  1. Logs in once and keeps the session warm
  2. On each trigger, navigates to the dashboard and extracts tickers
  3. Returns list of OvtlyrTicker with direction + score

Set env vars:
  OVTLYR_EMAIL
  OVTLYR_PASSWORD
"""
import os
import asyncio
import re
from typing import List, Optional

import structlog
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from scraper.models import OvtlyrTicker

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

    # Known OVTLYR API handler endpoints (discovered via network interception)
    _API_HANDLERS = [
        ("GetBullsList_Stocks", "long"),   # main bull list — stocks
        ("GetBullsList_ETFs",   "long"),   # bull list — ETFs
        ("GetWatchList",        None),     # user's personal watchlist (has own signal field)
        ("AjaxGetHighCoverage_Stocks", "long"),
    ]

    async def scrape(self) -> List[OvtlyrTicker]:
        """
        Fetch tickers via direct API calls to OVTLYR's Razor Page handlers.
        Falls back to DOM extraction if API calls don't return usable JSON.
        """
        if not self._logged_in:
            log.warning("ovtlyr.not_logged_in_retry")
            await self._login()
            if not self._logged_in:
                return []

        # Ensure we're on the watchlist page (session cookies are scoped here)
        try:
            if "watchlist" not in self._page.url:
                await self._page.goto(WATCHLIST_URL, timeout=30_000)
                await self._page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(2)
        except Exception as e:
            log.warning("ovtlyr.goto_watchlist_error", error=str(e))

        tickers: List[OvtlyrTicker] = []
        seen: set = set()

        # Call each handler via browser fetch() — reuses session cookies
        for handler, default_direction in self._API_HANDLERS:
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

                batch = self._parse_handler_response(result, default_direction, url)
                for t in batch:
                    if t.ticker not in seen:
                        tickers.append(t)
                        seen.add(t.ticker)

                log.info("ovtlyr.api_handler", handler=handler, count=len(batch))

            except Exception as e:
                log.warning("ovtlyr.api_handler_error", handler=handler, error=str(e))

        if tickers:
            log.info("ovtlyr.scraped_api", count=len(tickers))
            return tickers

        # Fall back to DOM extraction
        log.info("ovtlyr.dom_fallback")
        try:
            if os.environ.get("OVTLYR_DEBUG") == "1":
                await self._page.screenshot(
                    path="/app/logs/ovtlyr_console.ovtlyr.com_watchlist.png",
                    full_page=True,
                )
            dom_tickers = await self._extract_tickers()
            if dom_tickers:
                log.info("ovtlyr.scraped", url=self._page.url, count=len(dom_tickers))
            return dom_tickers
        except Exception as e:
            log.error("ovtlyr.dom_fallback_error", error=str(e))
            return []

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

    async def scrape_ticker(self, ticker: str) -> dict:
        """
        Navigate to /dashboard/{ticker} and extract key OVTLYR metrics.
        Returns a dict with signal, nine_score, oscillator, fear_greed, last_close, avg_vol_30d.
        Must be called after start() — reuses the logged-in session.
        """
        if not self._logged_in:
            await self._login()
            if not self._logged_in:
                return {}

        url = f"https://console.ovtlyr.com/dashboard/{ticker.upper()}"
        try:
            await self._page.goto(url, timeout=30_000)
            await self._page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(4)
        except Exception as e:
            log.warning("ovtlyr.scrape_ticker_nav_error", ticker=ticker, error=str(e))
            return {}

        try:
            text = await self._page.evaluate("() => document.body.innerText || ''")
        except Exception as e:
            log.warning("ovtlyr.scrape_ticker_eval_error", ticker=ticker, error=str(e))
            return {}

        result: dict = {}

        # Signal: "Sell" or "Buy" near "Current Signal"
        m = re.search(r'Current Signal[^\n]*\n+\s*(Buy|Sell)', text, re.IGNORECASE)
        if m:
            result["signal"] = m.group(1).capitalize()

        # Active status
        result["signal_active"] = bool(re.search(r'\bActive\b', text))

        # Signal date — e.g. "(Apr 02, 2026)"
        m = re.search(r'Current Signal.*?\(([A-Za-z]+ \d+,\s*\d{4})\)', text, re.DOTALL)
        if not m:
            m = re.search(r'Active Signal.*?\(([A-Za-z]+ \d+,\s*\d{4})\)', text, re.DOTALL)
        if m:
            result["signal_date_str"] = m.group(1).strip()

        # OVTLYR NINE score — "(5 / 9)"
        m = re.search(r'\(\s*(\d+)\s*/\s*9\s*\)', text)
        if m:
            result["nine_score"] = int(m.group(1))

        # Oscillator direction
        m = re.search(r'Oscillator direction\s*\n+\s*([^\n]+)', text)
        if m:
            result["oscillator"] = m.group(1).strip()

        # Fear & Greed score — standalone decimal like "32.71"
        m = re.search(r'\b(\d{1,3}\.\d{2})\s*\n*\s*arrow_outward', text)
        if m:
            try:
                result["fear_greed"] = float(m.group(1))
            except ValueError:
                pass

        # Last close — "$655.24" near "Last Day Close"
        m = re.search(r'Last Day Close\s*\n+\s*\$?([\d,]+\.?\d*)', text)
        if m:
            try:
                result["last_close"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

        # 30-day avg volume
        m = re.search(r'30-Day Avg\. Vol\.\s*\n+\s*([\d,]+)', text)
        if m:
            try:
                result["avg_vol_30d"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

        result["ticker"] = ticker.upper()
        log.info("ovtlyr.scrape_ticker", ticker=ticker, signal=result.get("signal"),
                 nine_score=result.get("nine_score"), fear_greed=result.get("fear_greed"))
        return result

    @staticmethod
    def _entry_to_signal(e: dict) -> dict:
        from datetime import datetime as _dt
        sig_date = None
        raw_date = e.get("BuySellDate", "")
        if raw_date:
            try:
                sig_date = _dt.fromisoformat(raw_date.replace("Z", "")).date().isoformat()
            except Exception:
                pass
        return {
            "ticker":      (e.get("Symbol") or "").upper(),
            "name":        e.get("Name", ""),
            "sector":      e.get("gics_Sector", ""),
            "signal":      e.get("BuySellStatus", ""),
            "signal_date": sig_date,
            "last_price":  e.get("LastPrice"),
            "avg_vol_30d": int(e.get("averageVol30Days", 0)) if e.get("averageVol30Days") else None,
        }

    async def scrape_lists(self) -> dict:
        """
        Fetch all OVTLYR list types via paginated POST calls (using the page's CSRF token).
        Lists: bull, bear, market_leaders, alpha_picks
        Returns dict: { list_type: [{ ticker, name, sector, signal, signal_date, last_price, avg_vol_30d }] }
        """
        if not self._logged_in:
            await self._login()
            if not self._logged_in:
                return {}

        # Navigate to watchlist to get a valid CSRF token
        try:
            await self._page.goto(WATCHLIST_URL, timeout=30_000)
            await self._page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("ovtlyr.scrape_lists_nav_error", error=str(e))
            return {}

        csrf = await self._page.evaluate(
            "() => document.querySelector('[name=__RequestVerificationToken]')?.value || ''"
        )
        if not csrf:
            log.warning("ovtlyr.scrape_lists_no_csrf")
            return {}

        # (handler, list_type, base_payload)
        LISTS = [
            ("GetBullsList_Stocks", "bull", {
                "page_size": 500, "page_index": 0, "stockTypeId": 1, "status": "buy",
                "filter_sectorIds": None, "filter_industryNames": None,
                "filter_minMarkerCap": None, "filter_maxMarkerCap": None,
                "sortOrder": "0", "sortBy": "0",
                "filter_BuySellFinalRegion": None, "filter_UnaTmt": None,
            }),
            ("GetBearsList_Stocks", "bear", {
                "page_size": 500, "page_index": 0, "stockTypeId": 1, "status": "sell",
                "filter_sectorIds": None, "filter_industryNames": None,
                "filter_minMarkerCap": None, "filter_maxMarkerCap": None,
                "sortOrder": "0", "sortBy": "0",
                "filter_BuySellFinalRegion": None, "filter_UnaTmt": None,
            }),
            ("AjaxGetHighCoverage_Stocks", "market_leaders", {
                "page_size": 500, "page_index": 0, "stockTypeId": 1, "status": "",
                "filter_sectorIds": None, "filter_industryNames": None,
                "filter_minMarkerCap": None, "filter_maxMarkerCap": None,
                "sortOrder": "0", "sortBy": "0",
                "filter_BuySellFinalRegion": None, "filter_IsHighNewsCoverage": True,
                "filter_UnaTmt": None, "filter_CurrentSentiment": None,
            }),
        ]

        result: dict = {"bull": [], "bear": [], "market_leaders": [], "alpha_picks": []}

        import json as _json
        for handler, list_type, base_payload in LISTS:
            all_entries: list = []
            page_index = 0
            while True:
                payload = {**base_payload, "page_index": page_index}
                payload_str = _json.dumps(payload).replace("'", "\\'")
                resp = await self._page.evaluate(f"""
                    async () => {{
                        try {{
                            const r = await fetch('/watchlist?handler={handler}', {{
                                method: 'POST',
                                headers: {{
                                    'RequestVerificationToken': '{csrf}',
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Content-Type': 'application/json;charset=UTF-8'
                                }},
                                body: '{payload_str}',
                                credentials: 'include'
                            }});
                            if (!r.ok) return null;
                            return await r.json();
                        }} catch(e) {{ return null; }}
                    }}
                """)
                if not resp:
                    break
                lst = resp.get("lst_stk") or []
                if not lst:
                    break
                all_entries.extend(lst)
                count_total = resp.get("count_total", 0)
                if len(all_entries) >= count_total:
                    break
                page_index += 1
                if page_index > 30:   # safety cap (~15 000 tickers max)
                    break

            result[list_type] = [self._entry_to_signal(e) for e in all_entries if e.get("Symbol")]
            log.info("ovtlyr.list_fetched", list_type=list_type, count=len(result[list_type]))

        # Try Alpha Picks — attempt several likely handler names
        for alpha_handler in ("GetAlphaPicks_Stocks", "AjaxGetAlphaPicks_Stocks",
                              "GetAlphaPicksList", "GetAlphaPicks"):
            payload_str = _json.dumps({"page_size": 500, "page_index": 0, "stockTypeId": 1,
                                       "sortOrder": "0", "sortBy": "0"})
            payload_str = payload_str.replace("'", "\\'")
            resp = await self._page.evaluate(f"""
                async () => {{
                    try {{
                        const r = await fetch('/watchlist?handler={alpha_handler}', {{
                            method: 'POST',
                            headers: {{
                                'RequestVerificationToken': '{csrf}',
                                'X-Requested-With': 'XMLHttpRequest',
                                'Content-Type': 'application/json;charset=UTF-8'
                            }},
                            body: '{payload_str}',
                            credentials: 'include'
                        }});
                        if (!r.ok) return null;
                        const data = await r.json();
                        return data.count_total > 0 ? data : null;
                    }} catch(e) {{ return null; }}
                }}
            """)
            if resp and resp.get("lst_stk"):
                result["alpha_picks"] = [
                    self._entry_to_signal(e) for e in resp["lst_stk"] if e.get("Symbol")
                ]
                log.info("ovtlyr.alpha_fetched", handler=alpha_handler,
                         count=len(result["alpha_picks"]))
                break

        log.info("ovtlyr.lists_scraped",
                 bull=len(result["bull"]), bear=len(result["bear"]),
                 market_leaders=len(result["market_leaders"]),
                 alpha_picks=len(result["alpha_picks"]))
        return result

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
