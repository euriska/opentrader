"""
Tradier Positions + Account Data
Query balances, positions, P&L, and history across all accounts.
"""
import logging
from .accounts import TradierAccount

log = logging.getLogger(__name__)


class TradierPositions:

    def __init__(self, account: TradierAccount):
        self.account = account
        self.client  = account.client
        self.acct_id = account.id

    async def get_balances(self) -> dict:
        result = await self.client.get(f"/accounts/{self.acct_id}/balances")
        return result.get("balances", result)

    async def get_positions(self) -> list:
        result = await self.client.get(f"/accounts/{self.acct_id}/positions")
        positions = result.get("positions", {})
        if positions == "null" or positions is None:
            return []
        raw = positions.get("position", [])
        return raw if isinstance(raw, list) else [raw]

    async def get_history(self, limit: int = 50) -> list:
        result = await self.client.get(
            f"/accounts/{self.acct_id}/history",
            params={"limit": limit},
        )
        history = result.get("history", {})
        if history == "null" or history is None:
            return []
        raw = history.get("event", [])
        return raw if isinstance(raw, list) else [raw]

    async def get_pnl(self) -> dict:
        """Calculate unrealized P&L from current positions."""
        positions = await self.get_positions()
        total_cost  = sum(float(p.get("cost_basis", 0)) for p in positions)
        total_value = sum(
            float(p.get("quantity", 0)) * float(p.get("last_price", 0))
            for p in positions
        )
        return {
            "account":         self.account.label,
            "mode":            self.account.mode,
            "position_count":  len(positions),
            "total_cost":      round(total_cost, 2),
            "total_value":     round(total_value, 2),
            "unrealized_pnl":  round(total_value - total_cost, 2),
            "positions":       positions,
        }
