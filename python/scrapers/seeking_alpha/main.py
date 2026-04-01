"""Seeking Alpha Sentiment Scraper Agent"""
import asyncio
import json
import structlog

from scrapers.base import BaseScraper
from .scraper import scrape_seeking_alpha

log = structlog.get_logger("scraper-seekalpha")

MAX_TICKERS = 25


class SeekingAlphaScraperAgent(BaseScraper):
    SOURCE      = "seekalpha"
    TRIGGER_JOB = "scrape_seekalpha"
    GROUP_KEY   = "scraper-seekalpha"

    def __init__(self):
        super().__init__("scraper-seekalpha")

    async def scrape(self):
        log.info("scraper-seekalpha.scrape_start")
        try:
            results = await scrape_seeking_alpha(max_articles=50)
        except Exception as e:
            log.error("scraper-seekalpha.scrape_failed", error=str(e))
            return

        published = 0
        for r in results[:MAX_TICKERS]:
            await self.publish(r["ticker"], {
                "mention_count":   str(r["mention_count"]),
                "sentiment_score": str(r["sentiment_score"]),
                "sentiment_label": r["sentiment_label"],
                "headlines":       json.dumps(r["headlines"]),
                "ts_utc":          str(r["ts_utc"]),
            })
            published += 1

        log.info("scraper-seekalpha.published", count=published)


async def main():
    agent = SeekingAlphaScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
