"""
Webull API Client (Official Developer API)
Authentication uses HMAC-SHA1 signed requests per the official SDK spec.

Set env vars:
  WEBULL_API_KEY    — App Key from Webull developer portal
  WEBULL_SECRET_KEY — App Secret from Webull developer portal
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

API_BASE    = "https://api.webull.com"
API_KEY     = os.getenv("WEBULL_API_KEY", "")
SECRET_KEY  = os.getenv("WEBULL_SECRET_KEY", "")
APP_KEY     = os.getenv("WEBULL_APP_KEY", "")
APP_SECRET  = os.getenv("WEBULL_APP_SECRET", "")
MAX_RETRIES = int(os.getenv("WEBULL_MAX_RETRIES", "3"))

# Module-level cache: account_number → internal account_id
_ACCT_ID_CACHE: dict = {}


def _build_auth_headers(api_key: str, secret: str, method: str, path: str,
                        host: str = "api.webull.com",
                        params: Optional[dict] = None,
                        body: Optional[dict] = None) -> dict:
    """
    Build Webull signed request headers using the official SDK algorithm:
      string_to_sign = URI + "&" + sorted(sign_params as k=v) [+ "&" + MD5(body)]
      signature = base64(HMAC-SHA1(secret + "&", quote(string_to_sign, safe='')))
    """
    nonce     = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Sign params: auth headers + host + any query params, sorted by key
    sign_params: dict = {
        "x-app-key":             api_key,
        "x-timestamp":           timestamp,
        "x-signature-version":   "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     nonce,
        "host":                  host,
    }
    if params:
        for k, v in params.items():
            lk = k.lower()
            sign_params[lk] = f"{sign_params[lk]}&{v}" if lk in sign_params else str(v)

    sorted_pairs = "&".join(f"{k}={v}" for k, v in sorted(sign_params.items()))
    string_to_sign = path + "&" + sorted_pairs

    if body is not None:
        body_md5 = hashlib.md5(
            json.dumps(body, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        string_to_sign += "&" + body_md5

    encoded    = quote(string_to_sign, safe="")
    key_bytes  = (secret + "&").encode("utf-8")
    sig        = base64.b64encode(
        hmac.new(key_bytes, encoded.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")

    return {
        "x-app-key":             api_key,
        "x-timestamp":           timestamp,
        "x-signature-version":   "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     nonce,
        "x-signature":           sig,
        "Content-Type":          "application/json",
        "Accept":                "application/json",
    }


class WebullClient:
    """
    Low-level Webull HTTP client (Official Developer API).
    One instance per account (live or paper).
    """

    def __init__(self, mode: str = "paper"):
        self.mode = mode

    async def resolve_account_id(self, account_number: str) -> str:
        """
        Translate a human-readable account number (e.g. 'CVU66ZC3') to the
        internal account_id required by the developer API (e.g. 'HOJARI7B...').
        Results are cached module-wide after the first call.
        """
        if account_number in _ACCT_ID_CACHE:
            return _ACCT_ID_CACHE[account_number]
        try:
            subs = await self.get("/app/subscriptions/list")
            for sub in (subs if isinstance(subs, list) else []):
                num = sub.get("account_number", "")
                aid = sub.get("account_id", "")
                if num and aid:
                    _ACCT_ID_CACHE[num] = aid
        except Exception as e:
            log.warning(f"[webull] Could not fetch subscriptions: {e}")
        return _ACCT_ID_CACHE.get(account_number, account_number)

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        return await self._request("GET", path, params=params)

    async def get_v2(self, path: str, params: Optional[dict] = None) -> dict:
        """Call the v2 OpenAPI endpoint using APP_KEY/APP_SECRET with x-version: v2 header."""
        return await self._request("GET", path, params=params, use_app_key=True, api_version="v2")

    async def post(self, path: str, body: Optional[dict] = None) -> dict:
        return await self._request("POST", path, body=body)

    async def delete(self, path: str) -> dict:
        return await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path:   str,
        params: Optional[dict] = None,
        body:   Optional[dict] = None,
        use_app_key:  bool = False,
        api_version:  Optional[str] = None,
    ) -> dict:
        url = f"{API_BASE}{path}"
        key    = APP_KEY    if use_app_key and APP_KEY    else API_KEY
        secret = APP_SECRET if use_app_key and APP_SECRET else SECRET_KEY

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = _build_auth_headers(key, secret, method, path,
                                              params=params, body=body)
                if api_version:
                    headers["x-version"] = api_version
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url,
                        headers=headers,
                        params=params,
                        json=body,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 204:
                            return {}
                        resp_body = await resp.json(content_type=None)

                        if resp.status == 200:
                            return resp_body
                        elif resp.status == 401:
                            log.error("[webull] Auth error — check API key and secret")
                            raise PermissionError("Webull auth failed: 401")
                        elif resp.status == 429:
                            wait = 2 ** attempt
                            log.warning(f"[webull] Rate limited — sleeping {wait}s")
                            await asyncio.sleep(wait)
                        else:
                            log.warning(
                                f"[webull] {method} {path} → {resp.status} "
                                f"attempt {attempt}: {resp_body}"
                            )
                            await asyncio.sleep(2 ** attempt)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning(f"[webull] Network error attempt {attempt}: {e}")
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"[webull] Request failed after {MAX_RETRIES} attempts: {method} {path}"
        )
