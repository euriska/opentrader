"""
Tradier API Client
Handles authentication, rate limiting, and base HTTP for all Tradier endpoints.
Single access token covers all accounts (live + sandbox).
"""
import asyncio
import os
import logging
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

# Tradier endpoints
LIVE_BASE    = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"

ACCESS_TOKEN    = os.getenv("TRADIER_SANDBOX_API_KEY") or os.getenv("TRADIER_PRODUCTION_API_KEY", "")
MAX_RETRIES     = int(os.getenv("TRADIER_MAX_RETRIES", "3"))
RATE_LIMIT_SEC  = float(os.getenv("TRADIER_RATE_LIMIT_PER_SEC", "2"))


class TradierClient:
    """
    Low-level Tradier HTTP client.
    One instance per account — selects live or sandbox base URL automatically.
    Rate-limited to respect Tradier API limits.
    """

    def __init__(self, account_id: str, mode: str = "live"):
        self.account_id = account_id
        self.mode       = mode  # "live" or "sandbox"
        self.base_url   = SANDBOX_BASE if mode == "sandbox" else LIVE_BASE
        self.headers    = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Accept":        "application/json",
        }
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _rate_limit(self):
        """Enforce per-second rate limit across calls."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = (1.0 / RATE_LIMIT_SEC) - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, data: Optional[dict] = None) -> dict:
        return await self._request("POST", path, data=data)

    async def put(self, path: str, data: Optional[dict] = None) -> dict:
        return await self._request("PUT", path, data=data)

    async def delete(self, path: str) -> dict:
        return await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path:   str,
        params: Optional[dict] = None,
        data:   Optional[dict] = None,
    ) -> dict:
        await self._rate_limit()
        url = f"{self.base_url}{path}"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method,
                        url,
                        headers=self.headers,
                        params=params,
                        data=data,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        body = await resp.json(content_type=None)

                        if resp.status == 200:
                            return body
                        elif resp.status == 400:
                            # Order rejection / bad request — don't retry, surface the error immediately
                            err = body.get("errors", {}).get("error") or body.get("fault", {}).get("faultstring") or str(body)
                            if isinstance(err, list):
                                err = "; ".join(str(e) for e in err)
                            log.error(f"[tradier:{self.account_id}] Order rejected ({resp.status}): {err}")
                            raise ValueError(f"Tradier rejected order: {err}")
                        elif resp.status == 429:
                            wait = 2 ** attempt
                            log.warning(f"[tradier:{self.account_id}] Rate limited — sleeping {wait}s")
                            await asyncio.sleep(wait)
                        elif resp.status in (401, 403):
                            log.error(f"[tradier:{self.account_id}] Auth error {resp.status}")
                            raise PermissionError(f"Tradier auth failed: {resp.status}")
                        else:
                            log.warning(
                                f"[tradier:{self.account_id}] {method} {path} → {resp.status} attempt {attempt}"
                            )
                            await asyncio.sleep(2 ** attempt)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning(f"[tradier:{self.account_id}] Network error attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"[tradier:{self.account_id}] Request failed after {MAX_RETRIES} attempts: {method} {path}"
        )
