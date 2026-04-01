"""
Tradier Multi-Account Gateway
Top-level coordinator used by trader hands.
Routes orders to the correct account(s) based on strategy tag and mode.
Handles circuit breaker — halts all accounts simultaneously.
"""
import asyncio
import logging
from typing import Optional
from .accounts import TradierAccountManager, TradierAccount
from .orders   import TradierOrders
from .positions import TradierPositions
from . import market

log = logging.getLogger(__name__)


class TradierGateway:
    """
    The single object trader hands interact with.
    Abstracts away which account(s) an order goes to.
    """

    def __init__(self):
        self.mgr              = TradierAccountManager()
        self._circuit_broken  = False
        log.info(f"TradierGateway ready: {self.mgr.summary()}")

    # ── Circuit breaker ──────────────────────────────────────────────────────

    def trip_circuit_breaker(self):
        """Halt all trading immediately. Called by self-healing watchdog."""
        self._circuit_broken = True
        log.critical("CIRCUIT BREAKER TRIPPED — all Tradier trading halted")

    def reset_circuit_breaker(self):
        self._circuit_broken = False
        log.info("Circuit breaker reset — trading resumed")

    def _check_circuit(self):
        if self._circuit_broken:
            raise RuntimeError("Circuit breaker is open — trading halted")

    # ── Order routing ────────────────────────────────────────────────────────

    def _get_accounts(
        self,
        strategy_tag:  Optional[str] = None,
        mode:          Optional[str] = None,   # "live" | "sandbox" | None (all)
        account_label: Optional[str] = None,   # target specific account
    ) -> list[TradierAccount]:
        """Resolve which accounts to send an order to."""
        if account_label:
            acct = self.mgr.get_by_label(account_label)
            return [acct] if acct else []

        accounts = self.mgr.get_all()

        if mode == "live":
            accounts = [a for a in accounts if a.is_live]
        elif mode == "sandbox":
            accounts = [a for a in accounts if a.is_paper]

        if strategy_tag:
            accounts = [a for a in accounts if strategy_tag in a.strategy_tags]

        return accounts

    async def place_equity_order(
        self,
        symbol:        str,
        side:          str,
        quantity:      int,
        order_type:    str = "market",
        price:         Optional[float] = None,
        stop:          Optional[float] = None,
        duration:      str = "day",
        strategy_tag:  Optional[str] = "equity",
        mode:          Optional[str] = None,
        account_label: Optional[str] = None,
        tag:           Optional[str] = None,
    ) -> list[dict]:
        """
        Place an equity order across matching accounts.
        Returns list of results (one per account the order was sent to).
        """
        self._check_circuit()
        accounts = self._get_accounts(strategy_tag, mode, account_label)
        if not accounts:
            log.warning(f"No matching accounts for equity order: {symbol}")
            return []

        tasks = [
            TradierOrders(acct).place_equity_order(
                symbol=symbol, side=side, quantity=quantity,
                order_type=order_type, price=price, stop=stop,
                duration=duration, tag=tag,
            )
            for acct in accounts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = []
        for acct, result in zip(accounts, results):
            if isinstance(result, Exception):
                log.error(f"[{acct.label}] Order failed: {result}")
            else:
                log.info(f"[{acct.label}] Order placed: {result}")
                output.append({"account": acct.label, "result": result})
        return output

    async def place_option_order(
        self,
        symbol:        str,
        option_symbol: str,
        side:          str,
        quantity:      int,
        order_type:    str = "market",
        price:         Optional[float] = None,
        duration:      str = "day",
        strategy_tag:  Optional[str] = "options",
        mode:          Optional[str] = None,
        account_label: Optional[str] = None,
        tag:           Optional[str] = None,
    ) -> list[dict]:
        self._check_circuit()
        accounts = self._get_accounts(strategy_tag, mode, account_label)
        if not accounts:
            log.warning(f"No matching accounts for options order: {option_symbol}")
            return []

        tasks = [
            TradierOrders(acct).place_option_order(
                symbol=symbol, option_symbol=option_symbol,
                side=side, quantity=quantity, order_type=order_type,
                price=price, duration=duration, tag=tag,
            )
            for acct in accounts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = []
        for acct, result in zip(accounts, results):
            if isinstance(result, Exception):
                log.error(f"[{acct.label}] Option order failed: {result}")
            else:
                output.append({"account": acct.label, "result": result})
        return output

    async def cancel_all_orders(self) -> dict:
        """Cancel all open orders across ALL accounts. Used by circuit breaker."""
        accounts = self.mgr.get_all()
        tasks = [TradierOrders(a).cancel_all_open_orders() for a in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            acct.label: result
            for acct, result in zip(accounts, results)
        }

    # ── Portfolio queries ────────────────────────────────────────────────────

    async def get_all_positions(self) -> list[dict]:
        """Get positions across all accounts."""
        accounts = self.mgr.get_all()
        tasks    = [TradierPositions(a).get_pnl() for a in accounts]
        results  = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]

    async def get_all_balances(self) -> list[dict]:
        accounts = self.mgr.get_all()
        tasks    = [TradierPositions(a).get_balances() for a in accounts]
        results  = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            {"account": acct.label, "balances": r}
            for acct, r in zip(accounts, results)
            if not isinstance(r, Exception)
        ]

    async def get_all_orders(self) -> list[dict]:
        accounts = self.mgr.get_all()
        tasks    = [TradierOrders(a).get_orders() for a in accounts]
        results  = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            {"account": acct.label, "orders": r}
            for acct, r in zip(accounts, results)
            if not isinstance(r, Exception)
        ]

    # ── Market data passthrough ──────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> dict:
        return await market.get_quote(symbol)

    async def get_quotes(self, symbols: list[str]) -> list:
        return await market.get_quotes(symbols)

    async def get_option_chain(self, symbol: str, expiration: str) -> list:
        return await market.get_option_chain(symbol, expiration)

    async def get_option_expirations(self, symbol: str) -> list:
        return await market.get_option_expirations(symbol)

    # ── Status summary ───────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "circuit_broken": self._circuit_broken,
            "accounts":       self.mgr.summary(),
        }
