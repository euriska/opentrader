"""
Telegram Bot for OpenTrader Chat Agent.

Responds to:
  - /commands or !commands in any chat (groups + private)
  - @mentions in group chats (AI mode)
  - Private messages (both modes)
"""
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import CommandStart

from .mcp_registry import MCPRegistry
from .commands import handle_command, is_command, HELP_TEXT
from .ai_agent import handle_ai
from .formatter import discord_to_telegram_html

log = structlog.get_logger("chat-agent.telegram")

TG_CHUNK = 4000  # under 4096 Telegram limit


def _split_message(text: str, limit: int = TG_CHUNK) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line).lstrip("\n")
    if buf:
        chunks.append(buf)
    return chunks or [text[:limit]]


async def start_telegram(token: str, registry: MCPRegistry):
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    _bot_username_cache = {}

    async def _get_username() -> str:
        if "name" not in _bot_username_cache:
            me = await bot.get_me()
            _bot_username_cache["name"] = me.username or ""
        return _bot_username_cache["name"]

    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        await message.answer(discord_to_telegram_html(HELP_TEXT))

    @dp.message()
    async def handle_message(message: Message):
        text = (message.text or "").strip()
        if not text:
            return

        is_private  = message.chat.type == "private"
        bot_username = await _get_username()
        mentioned   = bot_username and f"@{bot_username}" in text

        if mentioned:
            text = text.replace(f"@{bot_username}", "").strip()

        # Determine mode
        if is_command(text):
            mode = "command"
        elif is_private or mentioned:
            mode = "ai"
        else:
            return  # ignore plain group messages

        channel_id = str(message.chat.id)

        try:
            if mode == "command":
                response = await handle_command(text, registry)
            else:
                response = (await handle_ai(text, registry, channel_id)
                            if text else HELP_TEXT)
        except Exception as e:
            log.error("telegram.handle_error", error=str(e))
            response = f"Something went wrong: {e}"

        html = discord_to_telegram_html(response)
        for chunk in _split_message(html):
            try:
                await message.answer(chunk, parse_mode=ParseMode.HTML)
            except Exception as e:
                # Fall back to plain text if HTML parse fails
                log.warning("telegram.html_parse_failed", error=str(e))
                await message.answer(chunk, parse_mode=None)

    log.info("telegram.starting")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        log.error("telegram.start_failed", error=str(e))
    finally:
        await bot.session.close()
