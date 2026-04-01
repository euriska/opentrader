"""WSB Sentiment Scraper Agent"""
import asyncio
import json
import structlog

from scrapers.base import BaseScraper
from .scraper import scrape_wsb

log = structlog.get_logger("scraper-wsb")

MAX_TICKERS = 20


class WSBScraperAgent(BaseScraper):
    SOURCE      = "wsb"
    TRIGGER_JOB = "scrape_wsb"
    GROUP_KEY   = "scraper-wsb"

    def __init__(self):
        super().__init__("scraper-wsb")

    async def scrape(self):
        log.info("scraper-wsb.scrape_start")
        try:
            results = await scrape_wsb(post_limit=100, min_mentions=2)
        except Exception as e:
            log.error("scraper-wsb.scrape_failed", error=str(e))
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

        log.info("scraper-wsb.published", count=published)


async def main():
    agent = WSBScraperAgent()
    try:
        await agent.run()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
