"""
Seeking Alpha Sentiment Scraper
Uses SeekingAlpha's public news API to pull market-moving headlines,
extracts ticker mentions, and scores sentiment with VADER.
Falls back gracefully if the API is unavailable.
"""
import re
import time
from typing import List
import aiohttp
import structlog
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log = structlog.get_logger("scraper.seekalpha")

# SeekingAlpha public news API — no auth for market-news headlines
SA_NEWS_URL = "https://seekingalpha.com/api/v3/news"
SA_TRENDING_URL = "https://seekingalpha.com/api/v3/symbols/suggested"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://seekingalpha.com/",
}

TICKER_RE = re.compile(r'\b([A-Z]{1,5})\b')
BLOCKLIST = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HAS",
    "ITS", "MAY", "NEW", "NOW", "OLD", "SEE", "TWO", "WAY", "WHO", "DID",
    "SAY", "TOO", "USE", "CEO", "CFO", "CTO", "IPO", "EPS", "GDP", "CPI",
    "FED", "USD", "USA", "NYSE", "SEC", "IRS", "ETF", "IRA", "EV", "AI",
    "BUY", "SELL", "HOLD", "SAYS", "SETS", "CUTS", "TOPS", "HITS", "SEES",
    "ADDS", "GETS", "FLAT", "RISE", "FELL", "GAIN", "LOSS", "HIGH", "LOWS",
    "RATE", "BANK", "CASH", "DEBT", "FUND", "RISK", "FAST", "SLOW", "BULL",
    "BEAR", "JUST", "THAN", "THAT", "THIS", "WITH", "WILL", "BEEN", "FROM",
    "MOST", "MUCH", "MANY", "MORE", "LESS", "BEST", "LAST", "NEXT", "ALSO",
    "EVEN", "OVER", "DOWN", "BACK", "GOOD", "WELL", "THEN", "WERE", "VERY",
    "EACH", "BOTH", "YEAR", "WEEK", "DAYS", "TIME", "WEEK", "ONLY", "EVER",
    "REAL", "HUGE", "FREE", "ONCE", "SOON", "SOME", "CAME", "CAME", "TOOK",
    "MADE", "MAKE", "TAKE", "COME", "WANT", "HAVE", "LIKE", "BEEN", "SAID",
    "WHAT", "WHEN", "THEY", "AFTER", "BEFORE",
}


async def scrape_seeking_alpha(max_articles: int = 50) -> List[dict]:
    """
    Fetch SA market news headlines, extract ticker mentions + sentiment.
    Returns list of dicts: ticker, mention_count, sentiment_score,
    sentiment_label, headlines, ts_utc
    """
    vader = SentimentIntensityAnalyzer()
    headlines = await _fetch_headlines(max_articles)

    if not headlines:
        return []

    mentions: dict = {}
    ts = int(time.time() * 1000)

    for item in headlines:
        title = item.get("title", "")
        tickers_tagged = item.get("tickers", [])  # SA often includes tagged tickers

        sentiment = vader.polarity_scores(title)["compound"]

        # Use SA-tagged tickers first
        found_tickers = set(tickers_tagged)

        # Also extract from headline text
        for m in TICKER_RE.finditer(title):
            t = m.group(1)
            if t not in BLOCKLIST and len(t) >= 2:
                found_tickers.add(t)

        for ticker in found_tickers:
            if ticker not in mentions:
                mentions[ticker] = {"count": 0, "sentiments": [], "headlines": []}
            mentions[ticker]["count"] += 1
            mentions[ticker]["sentiments"].append(sentiment)
            if title not in mentions[ticker]["headlines"]:
                mentions[ticker]["headlines"].append(title)

    results = []
    for ticker, d in mentions.items():
        if d["count"] < 1:
            continue
        avg_sentiment = sum(d["sentiments"]) / len(d["sentiments"])
        label = "positive" if avg_sentiment > 0.05 else \
                "negative" if avg_sentiment < -0.05 else "neutral"
        results.append({
            "ticker":          ticker,
            "mention_count":   d["count"],
            "sentiment_score": round(avg_sentiment, 4),
            "sentiment_label": label,
            "headlines":       d["headlines"][:3],
            "ts_utc":          ts,
        })

    results.sort(key=lambda x: x["mention_count"], reverse=True)
    log.info("seekalpha.scraped", tickers=len(results), headlines=len(headlines))
    return results


async def _fetch_headlines(limit: int) -> List[dict]:
    """Try SA news API endpoints — return list of {title, tickers}."""
    params = {
        "filter[category]": "market-news::all",
        "include":          "author,primaryTickers",
        "page[size]":       str(limit),
    }
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                SA_NEWS_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("seekalpha.api_failed", status=resp.status)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("seekalpha.fetch_error", error=str(e))
        return []

    items = data.get("data", [])
    results = []
    included = {i.get("id"): i for i in data.get("included", [])}

    for item in items:
        attrs = item.get("attributes", {})
        title = attrs.get("title", attrs.get("headline", ""))
        if not title:
            continue

        # Extract primary tickers from included relationships
        tickers = []
        rels = item.get("relationships", {})
        primary = rels.get("primaryTickers", {}).get("data", [])
        for ref in primary:
            inc = included.get(ref.get("id"), {})
            slug = inc.get("attributes", {}).get("slug", "")
            if slug and re.match(r'^[A-Z]{1,5}$', slug.upper()):
                tickers.append(slug.upper())

        results.append({"title": title, "tickers": tickers})

    return results
