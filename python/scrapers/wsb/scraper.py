"""
WSB Sentiment Scraper
Fetches r/wallstreetbets via old.reddit.com public JSON API (no auth required).
Extracts ticker mentions and scores sentiment with VADER.
"""
import re
import time
from typing import List, Tuple
import aiohttp
import structlog
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log = structlog.get_logger("scraper.wsb")

WSB_URL = "https://old.reddit.com/r/wallstreetbets/hot.json"

HEADERS = {
    "User-Agent": "OpenTrader/1.0 (market research bot; contact: admin@opentrader.local)",
    "Accept": "application/json",
}

BLOCKLIST = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HER",
    "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW",
    "ITS", "LET", "MAN", "MAY", "NEW", "NOW", "OLD", "SEE", "TWO", "WAY",
    "WHO", "BOY", "DID", "PUT", "SAY", "SHE", "TOO", "USE", "YOLO", "TLDR",
    "EDIT", "FOMO", "GAIN", "LOSS", "MOON", "PUTS", "CALL", "LMAO", "FWIW",
    "IMO", "LOL", "OMG", "WTF", "ATH", "EPS", "IPO", "WSB", "SEC", "CEO",
    "CFO", "CTO", "IRA", "GDP", "CPI", "FED", "USD", "USA", "NYSE", "NASDAQ",
    "BUY", "SELL", "HOLD", "LONG", "PUT", "ITM", "OTM", "ATM", "DTE",
    "DD", "BE", "IS", "IT", "IN", "AT", "TO", "OF", "OR", "IF", "AN",
    "MY", "DO", "GO", "SO", "UP", "NO", "VS", "PM", "AM", "TV", "AI",
    "US", "UK", "EU", "EV", "PE", "IV", "RH", "TA", "MA", "SMA", "EMA",
    "SAME", "MOST", "MUCH", "MANY", "MORE", "LESS", "HIGH", "HITS", "HOLD",
    "BEEN", "BEST", "LAST", "NEXT", "JUST", "LIKE", "MAKE", "MADE", "TAKE",
    "TOOK", "COME", "CAME", "WANT", "SAID", "HAVE", "ALSO", "FROM", "THAT",
    "THIS", "WITH", "THEY", "WILL", "BEEN", "WHEN", "TIME", "SOME", "WHAT",
    "BACK", "GOOD", "WELL", "EVEN", "DOWN", "OVER", "JUST", "THEN", "THAN",
    "WERE", "VERY", "EACH", "BOTH", "LONG", "DAYS", "YEAR", "WEEK", "HATE",
    "LOVE", "NICE", "REAL", "FREE", "EASY", "HARD", "WORK", "FEEL", "SHIT",
    "FUCK", "CASH", "BANK", "FUND", "DEBT", "LOAN", "RISK", "SAFE", "FAST",
    "SLOW", "HUGE", "TINY", "ONLY", "EVER", "ONCE", "SOON", "BULL", "BEAR",
}

TICKER_RE = re.compile(r'\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b')


async def scrape_wsb(post_limit: int = 100, min_mentions: int = 2) -> List[dict]:
    """
    Fetch WSB hot posts and extract ticker sentiment.
    Returns list of dicts: ticker, mention_count, sentiment_score,
    sentiment_label, headlines, ts_utc
    """
    vader = SentimentIntensityAnalyzer()

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                WSB_URL,
                params={"limit": post_limit, "raw_json": 1},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    log.warning("wsb.fetch_failed", status=resp.status)
                    return []
                data = await resp.json()
    except Exception as e:
        log.error("wsb.fetch_error", error=str(e))
        return []

    posts = data.get("data", {}).get("children", [])
    if not posts:
        log.warning("wsb.no_posts")
        return []

    # ticker → {count, sentiments, headlines}
    mentions: dict = {}

    for post in posts:
        p = post.get("data", {})
        title = p.get("title", "")
        selftext = p.get("selftext", "")
        text = f"{title} {selftext[:300]}"

        sentiment = vader.polarity_scores(title)["compound"]

        for m in TICKER_RE.finditer(text):
            ticker = (m.group(1) or m.group(2)).upper()
            if ticker in BLOCKLIST or len(ticker) < 2:
                continue
            if ticker not in mentions:
                mentions[ticker] = {"count": 0, "sentiments": [], "headlines": []}
            mentions[ticker]["count"] += 1
            mentions[ticker]["sentiments"].append(sentiment)
            if title not in mentions[ticker]["headlines"]:
                mentions[ticker]["headlines"].append(title)

    results = []
    ts = int(time.time() * 1000)
    for ticker, d in mentions.items():
        if d["count"] < min_mentions:
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
    log.info("wsb.scraped", tickers=len(results), posts=len(posts))
    return results
