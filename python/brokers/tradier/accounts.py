"""
Tradier Account Manager
Loads all configured accounts from accounts.toml + env vars.
Provides account selection by label, mode, and strategy tag.
"""
import os
import logging
import toml
from typing import Optional
from dataclasses import dataclass, field
from .client import TradierClient

log = logging.getLogger(__name__)

ACCOUNTS_CONFIG = os.getenv("ACCOUNTS_CONFIG", "/app/config/accounts.toml")


@dataclass
class TradierAccount:
    id:               str
    broker:           str
    mode:             str          # live | sandbox
    label:            str
    enabled:          bool
    strategy_tags:    list
    max_position_usd: float
    notes:            str = ""
    client:           Optional[TradierClient] = field(default=None, repr=False)

    def __post_init__(self):
        if self.enabled and self.broker == "tradier":
            self.client = TradierClient(
                account_id=self.id,
                mode=self.mode,
            )

    @property
    def is_paper(self) -> bool:
        return self.mode == "sandbox"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


class TradierAccountManager:
    """
    Manages all Tradier accounts.
    Resolves env var placeholders in accounts.toml automatically.
    """

    def __init__(self):
        self.accounts: list[TradierAccount] = []
        self._load()

    def _resolve_env(self, value: str) -> str:
        """Replace ${ENV_VAR} placeholders with actual env values."""
        import re
        def replacer(m):
            key = m.group(1)
            val = os.getenv(key, "")
            if not val:
                log.warning(f"Env var not set: {key}")
            return val
        return re.sub(r'\$\{(\w+)\}', replacer, str(value))

    def _load(self):
        try:
            raw = toml.load(ACCOUNTS_CONFIG)
        except FileNotFoundError:
            log.warning(f"accounts.toml not found at {ACCOUNTS_CONFIG} — no accounts loaded")
            return

        for entry in raw.get("accounts", []):
            if entry.get("broker") != "tradier":
                continue

            resolved_id = self._resolve_env(entry["id"])
            if not resolved_id:
                log.warning(f"Skipping account with unresolved id: {entry.get('label')}")
                continue

            account = TradierAccount(
                id=resolved_id,
                broker=entry["broker"],
                mode=entry["mode"],
                label=entry["label"],
                enabled=entry.get("enabled", False),
                strategy_tags=entry.get("strategy_tags", []),
                max_position_usd=entry.get("max_position_usd", 1000),
                notes=entry.get("notes", ""),
            )

            if account.enabled:
                self.accounts.append(account)
                log.info(
                    f"Loaded Tradier account: {account.label} "
                    f"[{account.mode}] id={account.id}"
                )

        log.info(
            f"TradierAccountManager: {len(self.accounts)} accounts loaded "
            f"({sum(1 for a in self.accounts if a.is_live)} live, "
            f"{sum(1 for a in self.accounts if a.is_paper)} sandbox)"
        )

    # ── Account selection ────────────────────────────────────────────────────

    def get_by_label(self, label: str) -> Optional[TradierAccount]:
        return next((a for a in self.accounts if a.label == label), None)

    def get_by_id(self, account_id: str) -> Optional[TradierAccount]:
        return next((a for a in self.accounts if a.id == account_id), None)

    def get_live_accounts(self) -> list[TradierAccount]:
        return [a for a in self.accounts if a.is_live]

    def get_sandbox_accounts(self) -> list[TradierAccount]:
        return [a for a in self.accounts if a.is_paper]

    def get_by_strategy(self, strategy_tag: str) -> list[TradierAccount]:
        """Return all enabled accounts that support a given strategy."""
        return [
            a for a in self.accounts
            if strategy_tag in a.strategy_tags
        ]

    def get_all(self) -> list[TradierAccount]:
        return self.accounts

    def summary(self) -> dict:
        return {
            "total":   len(self.accounts),
            "live":    [a.label for a in self.get_live_accounts()],
            "sandbox": [a.label for a in self.get_sandbox_accounts()],
        }
