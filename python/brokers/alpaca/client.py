"""
Alpaca REST API Client
Handles authentication, rate limiting, and base HTTP for all Alpaca endpoints.
Supports both paper and live environments via base URL switching.
Docs: https://docs.alpaca.markets/reference
"""
import asyncio
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

DATA_BASE = "https://data.alpaca.markets/v2"

# Endpoints are configurable so paper/live URLs can be set from the broker config UI
PAPER_BASE = os.getenv("ALPACA_PAPER_ENDPOINT", "https://paper-api.alpaca.markets/v2")
LIVE_BASE  = os.getenv("ALPACA_LIVE_ENDPOINT",  "https://api.alpaca.markets/v2")

PAPER_API_KEY    = os.getenv("ALPACA_API_KEY", "")
PAPER_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
LIVE_API_KEY     = os.getenv("ALPACA_LIVE_API_KEY", "") or PAPER_API_KEY
LIVE_API_SECRET  = os.getenv("ALPACA_LIVE_API_SECRET", "") or PAPER_API_SECRET
MAX_RETRIES      = int(os.getenv("ALPACA_MAX_RETRIES", "3"))


class AlpacaClient:
    """
    Low-level Alpaca HTTP client.
    Authenticates with a single API key via the APCA-API-KEY-ID header.
    """

    def __init__(self, mode: str = "paper"):
        self.mode     = mode
        self.base_url = PAPER_BASE if mode == "paper" else LIVE_BASE
        api_key    = PAPER_API_KEY    if mode == "paper" else LIVE_API_KEY
        api_secret = PAPER_API_SECRET if mode == "paper" else LIVE_API_SECRET
        self.headers  = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Content-Type":        "application/json",
            "Accept":              "application/json",
        }

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: Optional[dict] = None) -> dict:
        return await self._request("POST", path, json=json)

    async def delete(self, path: str, params: Optional[dict] = None) -> dict:
        return await self._request("DELETE", path, params=params)

    async def patch(self, path: str, json: Optional[dict] = None) -> dict:
        return await self._request("PATCH", path, json=json)

    async def get_market_data(self, path: str, params: Optional[dict] = None) -> dict:
        """Use data.alpaca.markets for market data endpoints."""
        return await self._request("GET", path, params=params, base=DATA_BASE)

    async def _request(
        self,
        method: str,
        path:   str,
        params: Optional[dict] = None,
        json:   Optional[dict] = None,
        base:   Optional[str]  = None,
    ) -> dict:
        url = f"{base or self.base_url}{path}"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method,
                        url,
                        headers=self.headers,
                        params=params,
                        json=json,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 204:
                            return {}
                        body = await resp.json(content_type=None)

                        if resp.status in (200, 201):
                            return body
                        elif resp.status == 429:
                            wait = 2 ** attempt
                            log.warning(f"[alpaca] Rate limited — sleeping {wait}s")
                            await asyncio.sleep(wait)
                        elif resp.status in (401, 403):
                            log.error(f"[alpaca] Auth error {resp.status}: {body}")
                            raise PermissionError(f"Alpaca auth failed: {resp.status}")
                        elif resp.status == 422:
                            # Unprocessable — don't retry
                            msg = body.get("message", str(body)) if isinstance(body, dict) else str(body)
                            raise ValueError(f"Alpaca rejected order: {msg}")
                        else:
                            log.warning(f"[alpaca] {method} {path} → {resp.status} attempt {attempt}: {body}")
                            await asyncio.sleep(2 ** attempt)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning(f"[alpaca] Network error attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"[alpaca] Request failed after {MAX_RETRIES} attempts: {method} {path}")
