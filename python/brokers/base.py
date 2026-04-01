"""
BrokerConnector — abstract base class.
Every broker module must implement this interface.
The broker gateway routes all platform commands through it.
"""
from abc import ABC, abstractmethod
from typing import Optional


class BrokerConnector(ABC):
    """
    One instance per account.
    broker_name and account_label are set by the registry at construction time.
    """

    broker_name:   str  # "tradier" | "webull" | "alpaca"
    account_label: str  # human label from accounts.toml
    account_id:    str  # broker-side account ID
    mode:          str  # "live" | "sandbox" | "paper"

    # ── Orders ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_equity_order(
        self,
        symbol:     str,
        side:       str,           # buy | sell | sell_short | buy_to_cover
        quantity:   int,
        order_type: str = "market",
        price:      Optional[float] = None,
        stop:       Optional[float] = None,
        duration:   str = "day",
        tag:        Optional[str] = None,
    ) -> dict:
        """Place an equity or ETF order. Returns broker order dict."""

    @abstractmethod
    async def place_option_order(
        self,
        symbol:        str,        # underlying
        option_symbol: str,        # full OCC / broker option symbol
        side:          str,        # buy_to_open | sell_to_open | buy_to_close | sell_to_close
        quantity:      int,
        order_type:    str = "market",
        price:         Optional[float] = None,
        duration:      str = "day",
        tag:           Optional[str] = None,
    ) -> dict:
        """Place a single-leg options order."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a specific open order."""

    @abstractmethod
    async def cancel_all_orders(self) -> list[dict]:
        """Cancel all open orders. Used by circuit breaker."""

    # ── Portfolio ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[dict]:
        """Return all open positions."""

    @abstractmethod
    async def get_balances(self) -> dict:
        """Return account balances / buying power."""

    @abstractmethod
    async def get_orders(self, status: str = "all") -> list[dict]:
        """Return orders. status: all | open | closed | filled."""

    # ── Market data ───────────────────────────────────────────────────────────

    @abstractmethod
    async def get_quote(self, symbol: str) -> dict:
        """Return latest quote for a symbol."""

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> list[dict]:
        """Return latest quotes for multiple symbols."""

    # ── Status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "broker":        self.broker_name,
            "account_label": self.account_label,
            "account_id":    self.account_id,
            "mode":          self.mode,
        }
