"""
AgentMail Notifier
Each agent gets its own inbox. Sends emails via AgentMail REST API.
Also handles Telegram and Discord webhook delivery.
"""
import asyncio
import os
import logging
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

AGENTMAIL_BASE_URL = os.getenv("AGENTMAIL_BASE_URL", "https://api.agentmail.to")
AGENTMAIL_API_KEY  = os.getenv("AGENTMAIL_API_KEY", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")
REPORT_RECIPIENT   = os.getenv("REPORT_RECIPIENT_EMAIL", "")

INBOX_MAP = {
    "orchestrator": os.getenv("AGENTMAIL_ORCHESTRATOR_INBOX", "orchestrator"),
    "review":       os.getenv("AGENTMAIL_REVIEW_INBOX",       "review-agent"),
    "eod":          os.getenv("AGENTMAIL_EOD_INBOX",          "eod-reports"),
    "alerts":       os.getenv("AGENTMAIL_ALERTS_INBOX",       "alerts"),
}


class Notifier:
    """
    Multi-channel notifier for an OpenTrader agent.
    Supports AgentMail, Telegram, and Discord.
    """

    def __init__(self, agent: str):
        self.agent    = agent
        _inbox_name   = INBOX_MAP.get(agent, "alerts")
        # AgentMail requires full email format as inbox_id in API paths
        self.inbox_id = _inbox_name if "@" in _inbox_name else f"{_inbox_name}@agentmail.to"
        self.headers  = {
            "Authorization": f"Bearer {AGENTMAIL_API_KEY}",
            "Content-Type":  "application/json",
        }

    # ── AgentMail ────────────────────────────────────────────────────────────

    async def ensure_inbox(self) -> dict:
        """Create the agent inbox if it doesn't exist yet."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{AGENTMAIL_BASE_URL}/v0/inboxes",
                headers=self.headers,
                json={"username": self.inbox_id.split("@")[0]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if resp.status in (200, 201, 409):  # 409 = already exists
                    addr = data.get("email", f"{self.inbox_id}@agentmail.to")
                    log.info(f"[{self.agent}] AgentMail inbox ready: {addr}")
                    return data
                else:
                    log.error(f"[{self.agent}] AgentMail inbox error {resp.status}: {data}")
                    return {}

    async def send_email(
        self,
        to:      str,
        subject: str,
        body:    str,
        html:    Optional[str] = None,
    ) -> bool:
        """Send email from this agent's inbox via AgentMail."""
        payload = {
            "to":      [to],
            "subject": subject,
            "text":    body,
        }
        if html:
            payload["html"] = html

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{AGENTMAIL_BASE_URL}/v0/inboxes/{self.inbox_id}/messages/send",
                headers=self.headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 201):
                    log.info(f"[{self.agent}] Email sent to {to}: {subject}")
                    return True
                else:
                    body_err = await resp.text()
                    log.error(f"[{self.agent}] AgentMail send failed {resp.status}: {body_err}")
                    return False

    # ── Telegram ─────────────────────────────────────────────────────────────

    async def telegram(self, message: str, parse_mode: str = "Markdown") -> bool:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning(f"[{self.agent}] Telegram not configured")
            return False

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       message,
                    "parse_mode": parse_mode,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ok = resp.status == 200
                if not ok:
                    log.error(f"[{self.agent}] Telegram error {resp.status}")
                return ok

    # ── Discord ──────────────────────────────────────────────────────────────

    async def discord(self, message: str, webhook_type: str = "alerts") -> bool:
        if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
            log.warning(f"[{self.agent}] Discord not configured (DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID)")
            return False

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                json={"content": message[:2000]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ok = resp.status in (200, 201)
                if not ok:
                    log.error(f"[{self.agent}] Discord error {resp.status}")
                return ok

    # ── Convenience broadcast methods ────────────────────────────────────────

    async def alert(self, subject: str, body: str) -> None:
        """Broadcast a system alert to all channels."""
        await asyncio.gather(
            self.telegram(f"*[ALERT]* {subject}\n\n{body}"),
            self.discord(f"**[ALERT]** {subject}\n\n{body}", webhook_type="general"),
            self.send_email(REPORT_RECIPIENT, f"[OpenTrader Alert] {subject}", body),
            return_exceptions=True,
        )

    async def trade_fill(self, fill: dict) -> None:
        """Notify all channels of a trade fill or pending order submission."""
        event_type = fill.get("event_type", "fill")
        label = "Order Pending" if event_type == "pending" else "Trade Fill"
        side  = (fill.get("direction") or "").upper()
        msg = (
            f"*{label}* — {fill.get('ticker')} {side}\n"
            f"Qty: {fill.get('qty')} @ ${fill.get('price')}\n"
            f"Account: {fill.get('account_id')} ({fill.get('mode')})\n"
            f"Strategy: {fill.get('strategy', '—')}"
        )
        await asyncio.gather(
            self.telegram(msg),
            self.discord(msg, webhook_type="general"),
            return_exceptions=True,
        )

    async def trade_reject(self, fill: dict) -> None:
        """Notify all channels of a broker order rejection."""
        side = (fill.get("direction") or "").upper()
        reason = fill.get("reject_reason") or "Unknown reason"
        msg = (
            f"*Order Rejected* ❌ — {fill.get('ticker')} {side}\n"
            f"Qty: {fill.get('qty')} @ ${fill.get('price')}\n"
            f"Account: {fill.get('account_id')} ({fill.get('mode')})\n"
            f"Reason: {reason}"
        )
        await asyncio.gather(
            self.telegram(msg),
            self.discord(msg, webhook_type="general"),
            return_exceptions=True,
        )

    async def eod_report(self, subject: str, body: str, html: Optional[str] = None) -> None:
        """Deliver EOD report to all channels."""
        await asyncio.gather(
            self.telegram(f"*EOD Report*\n\n{body[:4000]}"),
            self.discord(f"**EOD Report**\n\n{body[:2000]}", webhook_type="general"),
            self.send_email(REPORT_RECIPIENT, subject, body, html),
            return_exceptions=True,
        )

    async def review_findings(self, findings: str) -> None:
        """Deliver review agent findings."""
        subject = "[OpenTrader] Strategy Review — 50 Trade Analysis"
        await asyncio.gather(
            self.telegram(f"*Strategy Review*\n\n{findings[:4000]}"),
            self.discord(f"**Strategy Review**\n\n{findings[:2000]}", webhook_type="general"),
            self.send_email(REPORT_RECIPIENT, subject, findings),
            return_exceptions=True,
        )

    async def directive_executed(self, directive_text: str, result: str) -> None:
        """Notify all channels when a trade directive is executed."""
        subject = "[OpenTrader] Trade Directive Executed"
        msg = (
            f"*Trade Directive Executed*\n\n"
            f"Directive: {directive_text[:200]}\n\n"
            f"Result: {result[:500]}"
        )
        await asyncio.gather(
            self.telegram(msg),
            self.discord(msg, webhook_type="general"),
            self.send_email(REPORT_RECIPIENT, subject, f"Directive: {directive_text}\n\nResult: {result}"),
            return_exceptions=True,
        )
