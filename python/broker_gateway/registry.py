"""
Broker Registry
Loads all accounts from config/accounts.toml and instantiates the
appropriate BrokerConnector for each enabled account.

Provides lookup by:
  - account_label  (exact)
  - broker + mode  (list)
  - strategy_tag   (list)
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import toml

from brokers.base import BrokerConnector

log = logging.getLogger(__name__)

ACCOUNTS_CONFIG = os.getenv("ACCOUNTS_CONFIG", "/app/config/accounts.toml")

# Env vars that must be non-empty for each broker+mode to be considered credentialed.
# Account ID resolution is checked separately.
_BROKER_CREDS: dict[tuple, list[str]] = {
    ("tradier", "sandbox"):  ["TRADIER_SANDBOX_API_KEY"],
    ("tradier", "live"):     ["TRADIER_PRODUCTION_API_KEY"],
    ("alpaca",  "paper"):    ["ALPACA_API_KEY", "ALPACA_API_SECRET"],
    ("alpaca",  "live"):     ["ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET"],
    ("webull",  "paper"):    ["WEBULL_API_KEY", "WEBULL_SECRET_KEY"],
    ("webull",  "live"):     ["WEBULL_API_KEY", "WEBULL_SECRET_KEY"],
}

_BROKER_FACTORIES: dict = {}


def _register_broker(name: str):
    """Decorator — register a connector factory for a broker name."""
    def decorator(fn):
        _BROKER_FACTORIES[name] = fn
        return fn
    return decorator


@_register_broker("tradier")
def _make_tradier(account_label, account_id, mode, **_) -> BrokerConnector:
    from brokers.tradier.connector import TradierConnector
    return TradierConnector(account_label=account_label, account_id=account_id, mode=mode)


@_register_broker("alpaca")
def _make_alpaca(account_label, account_id, mode, **_) -> BrokerConnector:
    from brokers.alpaca.connector import AlpacaConnector
    return AlpacaConnector(account_label=account_label, account_id=account_id, mode=mode)


@_register_broker("webull")
def _make_webull(account_label, account_id, mode, **_) -> BrokerConnector:
    from brokers.webull.connector import WebullConnector
    return WebullConnector(account_label=account_label, account_id=account_id, mode=mode)


@dataclass
class AccountRecord:
    label:            str
    broker:           str
    mode:             str
    strategy_tags:    list = field(default_factory=list)
    max_position_usd: float = 1000
    connector:        Optional[BrokerConnector] = field(default=None, repr=False)


class BrokerRegistry:
    """
    Single source of truth for all broker accounts.
    Instantiated once by the gateway agent on startup.
    """

    def __init__(self):
        self._accounts: dict[str, AccountRecord] = {}   # label → record
        self._load()

    # ── Loading ───────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_env(value: str) -> str:
        def replacer(m):
            key = m.group(1)
            val = os.getenv(key, "")
            if not val:
                log.warning(f"[registry] Env var not set: {key}")
            return val
        return re.sub(r'\$\{(\w+)\}', replacer, str(value))

    def _load(self):
        try:
            raw = toml.load(ACCOUNTS_CONFIG)
        except FileNotFoundError:
            log.warning(f"[registry] accounts.toml not found at {ACCOUNTS_CONFIG}")
            return
        except Exception as e:
            log.error(f"[registry] Failed to load accounts.toml: {e}")
            return

        for entry in raw.get("accounts", []):
            broker = entry.get("broker", "")
            label  = entry.get("label", "")
            mode   = entry.get("mode", "sandbox")

            # enabled=false is an explicit user opt-out; skip immediately
            if entry.get("enabled") is False:
                log.debug(f"[registry] Skipping explicitly disabled account: {label}")
                continue

            if broker not in _BROKER_FACTORIES:
                log.warning(f"[registry] No connector for broker '{broker}' — skipping {label}")
                continue

            # Auto-enable: account ID must resolve and broker API key(s) must be set
            account_id = self._resolve_env(entry.get("id", ""))
            if not account_id:
                log.debug(f"[registry] Account ID not set for {label} — skipping (add credentials to enable)")
                continue

            required_creds = _BROKER_CREDS.get((broker, mode), [])
            missing = [k for k in required_creds if not os.getenv(k, "").strip()]
            if missing:
                log.debug(f"[registry] Missing credentials for {label}: {missing} — skipping")
                continue

            try:
                connector = _BROKER_FACTORIES[broker](
                    account_label=label,
                    account_id=account_id,
                    mode=mode,
                )
            except Exception as e:
                log.error(f"[registry] Failed to instantiate connector for {label}: {e}")
                continue

            self._accounts[label] = AccountRecord(
                label=label,
                broker=broker,
                mode=mode,
                strategy_tags=entry.get("strategy_tags", []),
                max_position_usd=entry.get("max_position_usd", 1000),
                connector=connector,
            )
            log.info(f"[registry] Loaded: {label} ({broker}, {mode})")

        log.info(
            f"[registry] {len(self._accounts)} accounts loaded: "
            f"{list(self._accounts.keys())}"
        )

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get(self, label: str) -> Optional[BrokerConnector]:
        rec = self._accounts.get(label)
        return rec.connector if rec else None

    def get_record(self, label: str) -> Optional[AccountRecord]:
        return self._accounts.get(label)

    def find(
        self,
        broker:       Optional[str] = None,
        mode:         Optional[str] = None,
        strategy_tag: Optional[str] = None,
    ) -> list[AccountRecord]:
        results = list(self._accounts.values())
        if broker:
            results = [r for r in results if r.broker == broker]
        if mode:
            results = [r for r in results if r.mode == mode]
        if strategy_tag:
            results = [r for r in results if strategy_tag in r.strategy_tags]
        return results

    def all_records(self) -> list[AccountRecord]:
        return list(self._accounts.values())

    def summary(self) -> dict:
        return {
            label: {"broker": r.broker, "mode": r.mode, "tags": r.strategy_tags}
            for label, r in self._accounts.items()
        }
