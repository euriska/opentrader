"""Yahoo Finance Sentiment Scraper Agent"""
import asyncio
import json
import structlog

from scrapers.base import BaseScraper
from .scraper import scrape_yahoo

log = structlog.get_logger("scraper-yahoo")

MAX_TICKERS = 25


class YahooScraperAgent(BaseScraper):
    SOURCE      = "yahoo"
    TRIGGER_JOB = "scrape_yahoo"
    GROUP_KEY   = "scraper-yahoo"

    def __init__(self):
        super().__init__("scraper-yahoo")

    async def scrape(self):
        log.info("scraper-yahoo.scrape_start")
        try:
            results = await scrape_yahoo(max_tickers=MAX_TICKERS)
        except Exception as e:
            log.error("scraper-yahoo.scrape_failed", error=str(e))
            return

        published = 0
        for r in results:
            await self.publish(r["ticker"], {
                "mention_count":   str(r["mention_count"]),
                "sentiment_score": str(r["sentiment_score"]),
                "sentiment_label": r["sentiment_label"],
                "headlines":       json.dumps(r["headlines"]),
                "trending":        str(r.get("trending", False)),
                "ts_utc":          str(r["ts_utc"]),
            })
            published += 1

        log.info("scraper-yahoo.published", count=published)


async def main():
    agent = YahooScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
