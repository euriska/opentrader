"""
AI Agent — natural language queries via OpenRouter with dynamic MCP tool use.

All tools from all registered MCP servers are automatically available.
Runs an agentic loop: model calls tools until it produces a final text response.
"""
import json
import os
import aiohttp
import structlog
from .mcp_registry import MCPRegistry

log = structlog.get_logger("chat-agent.ai")

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL            = os.getenv("CHAT_AGENT_MODEL", "anthropic/claude-3-5-haiku")
MAX_TOOL_ROUNDS     = 5
MAX_RESPONSE_CHARS  = 1800  # keep within Discord/Telegram limits

SYSTEM_PROMPT = """\
You are a financial data assistant embedded in OpenTrader, an algorithmic trading platform.
You have access to real-time Yahoo Finance tools: quotes, news, financials, options, analyst ratings, and trending tickers.
When users ask about stocks or markets, use the appropriate tools to fetch current data before answering.
Be concise and well-formatted. Use bullet points for lists. Format prices with $ and percentages with %.
Bold the most important numbers. Keep responses under 1500 characters unless detail is specifically requested.
If a ticker is ambiguous, ask for clarification. If data is unavailable, say so clearly.
"""

# Per-channel conversation history (in-memory, bounded)
_history: dict[str, list[dict]] = {}
MAX_HISTORY = 6  # messages to retain per channel (3 exchanges)


def _get_history(channel_id: str) -> list[dict]:
    return _history.get(channel_id, [])


def _push_history(channel_id: str, role: str, content):
    if channel_id not in _history:
        _history[channel_id] = []
    _history[channel_id].append({"role": role, "content": content})
    # Trim to last MAX_HISTORY messages
    if len(_history[channel_id]) > MAX_HISTORY:
        _history[channel_id] = _history[channel_id][-MAX_HISTORY:]


async def handle_ai(message: str, registry: MCPRegistry, channel_id: str = "") -> str:
    """Send a natural language message to the AI with all MCP tools available."""
    if not OPENROUTER_API_KEY:
        return "AI mode not configured — set `OPENROUTER_API_KEY` in your .env."

    tools = registry.get_openai_tools()
    if not tools:
        return "No MCP tools available — check MCP server connections."

    # Build conversation: system + history + new user message
    history = _get_history(channel_id) if channel_id else []
    messages = list(history) + [{"role": "user", "content": message}]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://opentrader.local",
        "X-Title":       "OpenTrader Chat Agent",
    }

    final_text = ""

    for round_num in range(MAX_TOOL_ROUNDS):
        payload = {
            "model":    AI_MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            "tools":    tools,
            "tool_choice": "auto",
            "max_tokens": 1024,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("ai_agent.api_error", status=resp.status, body=body[:200])
                        return f"AI error ({resp.status}) — check OpenRouter API key."
                    data = await resp.json()
        except Exception as e:
            log.error("ai_agent.request_failed", error=str(e))
            return f"AI request failed: {e}"

        choice  = data["choices"][0]
        msg     = choice["message"]
        finish  = choice.get("finish_reason", "")

        # ── Tool calls ────────────────────────────────────────────────────────
        if finish == "tool_calls" or msg.get("tool_calls"):
            messages.append(msg)
            for tc in msg.get("tool_calls", []):
                fn   = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                log.info("ai_agent.tool_call", round=round_num, tool=name, args=args)
                result = await registry.call_tool(name, args)
                messages.append({
                    "role":        "tool",
                    "tool_call_id": tc["id"],
                    "content":     result[:4000],
                })
            continue  # next round with tool results

        # ── Final text response ───────────────────────────────────────────────
        final_text = msg.get("content", "").strip()
        break

    if not final_text:
        final_text = "Reached maximum reasoning depth without a final answer."

    # Persist to history
    if channel_id:
        _push_history(channel_id, "user", message)
        _push_history(channel_id, "assistant", final_text)

    # Truncate if needed
    if len(final_text) > MAX_RESPONSE_CHARS:
        final_text = final_text[:MAX_RESPONSE_CHARS] + "\n…*(response truncated)*"

    return final_text
