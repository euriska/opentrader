"""
Discord Bot for OpenTrader Chat Agent.

Responds to:
  - !commands / /commands in any channel
  - @mentions with natural language (AI mode)
  - Direct messages (both modes)

Prerequisites (Discord Developer Portal):
  - Bot > "Message Content Intent" must be ENABLED
  - Bot > "Server Members Intent" is optional
"""
import asyncio
import structlog
import discord

from .mcp_registry import MCPRegistry
from .commands import handle_command, is_command, HELP_TEXT
from .ai_agent import handle_ai

log = structlog.get_logger("chat-agent.discord")

DISCORD_CHUNK = 1900  # stay under 2000 char limit


def _split_message(text: str, limit: int = DISCORD_CHUNK) -> list[str]:
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


class OpenTraderDiscordBot(discord.Client):
    def __init__(self, registry: MCPRegistry):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in dev portal
        super().__init__(intents=intents)
        self.registry = registry

    async def on_ready(self):
        log.info("discord.ready", user=str(self.user), guilds=len(self.guilds))

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content = message.content.strip()
        is_dm   = isinstance(message.channel, discord.DMChannel)

        # User mention: @Bot (direct)
        user_mentioned = self.user in message.mentions

        # Role mention: bot's assigned role was @mentioned instead of the user
        bot_member    = message.guild.get_member(self.user.id) if message.guild else None
        role_mentioned = bool(
            bot_member and message.role_mentions and
            set(bot_member.roles) & set(message.role_mentions)
        )

        mentioned = user_mentioned or role_mentioned

        log.info("discord.message_received",
                 author=str(message.author),
                 is_dm=is_dm,
                 user_mentioned=user_mentioned,
                 role_mentioned=role_mentioned,
                 content_preview=content[:80])

        # Strip the mention prefix from content
        if user_mentioned:
            content = content.replace(f"<@{self.user.id}>", "") \
                             .replace(f"<@!{self.user.id}>", "").strip()
        if role_mentioned and bot_member:
            for role in message.role_mentions:
                if role in bot_member.roles:
                    content = content.replace(f"<@&{role.id}>", "").strip()

        # Determine response mode
        if is_command(content):
            mode = "command"
        elif is_dm or mentioned:
            mode = "ai"
        else:
            return  # ignore plain channel messages

        channel_id = str(message.channel.id)

        async with message.channel.typing():
            try:
                if mode == "command":
                    response = await handle_command(content, self.registry)
                else:
                    response = (await handle_ai(content, self.registry, channel_id)
                                if content else HELP_TEXT)
            except Exception as e:
                log.error("discord.handle_error", error=str(e))
                response = f"Something went wrong: {e}"

        for chunk in _split_message(response):
            await message.channel.send(chunk)


async def start_discord(token: str, registry: MCPRegistry):
    bot = OpenTraderDiscordBot(registry)
    try:
        await bot.start(token)
    except discord.LoginFailure:
        log.error("discord.invalid_token")
    except Exception as e:
        log.error("discord.start_failed", error=str(e))
