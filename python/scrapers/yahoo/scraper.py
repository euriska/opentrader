"""
Yahoo Finance Sentiment Scraper — MCP-backed implementation.
All data fetched via the ot-mcp-yahoo MCP service (yfinance-based).
Eliminates direct Yahoo Finance HTTP calls that were 429-rate-limited.
"""
import json
import os
import time
from typing import List

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log = structlog.get_logger("scraper.yahoo")

MCP_URL = os.getenv("YAHOO_MCP_URL", "http://ot-mcp-yahoo:8000/mcp")


async def scrape_yahoo(max_tickers: int = 25) -> List[dict]:
    """
    Fetch trending tickers and their news sentiment via the Yahoo Finance MCP service.
    Returns list of dicts: ticker, mention_count, sentiment_score,
    sentiment_label, headlines, ts_utc, trending.
    """
    vader = SentimentIntensityAnalyzer()
    ts    = int(time.time() * 1000)
    results = []

    try:
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Step 1: Get trending tickers via MCP
                trend_result = await session.call_tool(
                    "get_trending_tickers", {"count": max_tickers}
                )
                trend_text = trend_result.content[0].text if trend_result.content else "[]"
                try:
                    parsed = json.loads(trend_text)
                    trending = parsed if isinstance(parsed, list) else []
                except Exception:
                    trending = []

                if not trending:
                    log.warning("yahoo.no_trending_tickers")
                    return []

                log.info("yahoo.trending_fetched", count=len(trending))

                # Step 2: Fetch news + sentiment per ticker
                for ticker in trending:
                    try:
                        news_result = await session.call_tool(
                            "get_yahoo_finance_news", {"ticker": ticker}
                        )
                        news_text  = news_result.content[0].text if news_result.content else ""
                        headlines  = _parse_headlines(news_text)
                    except Exception as e:
                        log.warning("yahoo.mcp_news_error", ticker=ticker, error=str(e))
                        headlines = []

                    if headlines:
                        scores = [vader.polarity_scores(h)["compound"] for h in headlines]
                        avg    = sum(scores) / len(scores)
                        label  = ("positive" if avg > 0.05 else
                                  "negative" if avg < -0.05 else "neutral")
                    else:
                        avg   = 0.0
                        label = "neutral"

                    results.append({
                        "ticker":          ticker,
                        "mention_count":   len(headlines),
                        "sentiment_score": round(avg, 4),
                        "sentiment_label": label,
                        "headlines":       headlines[:3],
                        "ts_utc":          ts,
                        "trending":        True,
                    })

    except Exception as e:
        log.error("yahoo.mcp_session_error", error=str(e))
        return results

    log.info("yahoo.scraped", tickers=len(results))
    return results


def _parse_headlines(news_text: str) -> List[str]:
    """Extract headline titles from MCP get_yahoo_finance_news response."""
    headlines = []
    for line in news_text.splitlines():
        if line.startswith("Title:"):
            title = line[6:].strip()
            if title:
                headlines.append(title)
    return headlines
