"""
OpenRouter LLM Connector
Unified interface for all agent LLM calls.
Supports model routing, fallback, and retries.
"""
import asyncio
import os
import logging
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")

# Model assignments loaded from env (overrides system.toml defaults)
MODELS = {
    "predictor":    os.getenv("LLM_PREDICTOR_MODEL",    "anthropic/claude-sonnet-4-5"),
    "review":       os.getenv("LLM_REVIEW_MODEL",       "anthropic/claude-opus-4-5"),
    "eod":          os.getenv("LLM_EOD_MODEL",          "anthropic/claude-sonnet-4-5"),
    "orchestrator": os.getenv("LLM_ORCHESTRATOR_MODEL", "anthropic/claude-haiku-4-5"),
    "fallback":     os.getenv("LLM_FALLBACK_MODEL",     "openai/gpt-4o"),
}


class LLMConnector:
    """
    OpenRouter-backed LLM connector.
    Drop-in for any agent — call complete() with your agent name and prompt.
    """

    def __init__(self, agent: str, max_retries: int = 3):
        self.agent       = agent
        self.model       = MODELS.get(agent, MODELS["fallback"])
        self.max_retries = max_retries
        self.headers = {
            "Authorization":  f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":   "application/json",
            "HTTP-Referer":   "https://opentrader.local",
            "X-Title":        f"OpenTrader/{agent}",
        }

    async def complete(
        self,
        prompt:      str,
        system:      Optional[str] = None,
        max_tokens:  int = 1000,
        temperature: float = 0.2,
        model:       Optional[str] = None,  # override per-call if needed
    ) -> str:
        """
        Send a prompt to OpenRouter and return the text response.
        Automatically retries on transient errors and falls back to
        the fallback model after max_retries on the primary.
        """
        target_model = model or self.model
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model":       target_model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }

        wait_after = 0
        for attempt in range(1, self.max_retries + 1):
            if wait_after:
                await asyncio.sleep(wait_after)
                wait_after = 0
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{OPENROUTER_BASE_URL}/chat/completions",
                        headers=self.headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            text = data["choices"][0]["message"]["content"]
                            log.info(
                                "LLM call success",
                                extra={
                                    "agent":  self.agent,
                                    "model":  target_model,
                                    "tokens": data.get("usage", {}).get("total_tokens", 0),
                                }
                            )
                            return text

                        elif resp.status == 429:
                            log.warning(f"[{self.agent}] Rate limited — waiting {2**attempt}s")
                            await resp.read()
                            wait_after = 2 ** attempt

                        elif resp.status >= 500:
                            log.warning(f"[{self.agent}] Server error {resp.status} attempt {attempt}")
                            await resp.read()
                            wait_after = 2 ** attempt

                        else:
                            body = await resp.text()
                            log.error(f"[{self.agent}] LLM error {resp.status}: {body}")
                            break

            except asyncio.TimeoutError:
                log.warning(f"[{self.agent}] LLM timeout attempt {attempt}")
                wait_after = 2 ** attempt

            except Exception as e:
                log.error(f"[{self.agent}] LLM exception: {e}")
                wait_after = 2 ** attempt

        # All retries on primary model failed — try fallback
        if target_model != MODELS["fallback"]:
            log.warning(f"[{self.agent}] Falling back to {MODELS['fallback']}")
            return await self.complete(
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                model=MODELS["fallback"],
            )

        raise RuntimeError(f"[{self.agent}] LLM unavailable after {self.max_retries} retries")


    async def complete_json(
        self,
        prompt:     str,
        system:     Optional[str] = None,
        max_tokens: int = 1000,
    ) -> dict:
        """
        Like complete() but instructs the model to return JSON
        and parses the response automatically.
        """
        import json, re

        json_system = (system or "") + "\nRespond ONLY with valid JSON. No markdown, no backticks, no explanation."
        raw = await self.complete(
            prompt=prompt,
            system=json_system.strip(),
            max_tokens=max_tokens,
            temperature=0.1,
        )
        # Strip any accidental markdown fences
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            log.error(f"[{self.agent}] JSON parse failed: {e}\nRaw: {raw}")
            raise
