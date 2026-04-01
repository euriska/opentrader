"""
Alpaca Positions + Account Data
Query balances, positions, and P&L for a single Alpaca account.
"""
import logging
from .client import AlpacaClient

log = logging.getLogger(__name__)


class AlpacaPositions:

    def __init__(self, client: AlpacaClient, account_label: str):
        self.client        = client
        self.account_label = account_label

    async def get_account(self) -> dict:
        """Full Alpaca account object — includes equity, cash, buying_power, etc."""
        return await self.client.get("/account")

    async def get_balances(self) -> dict:
        account = await self.get_account()
        return {
            "cash":            float(account.get("cash", 0)),
            "buying_power":    float(account.get("buying_power", 0)),
            "equity":          float(account.get("equity", 0)),
            "portfolio_value": float(account.get("portfolio_value", 0)),
            "daytrade_count":  account.get("daytrade_count", 0),
            "account_status":  account.get("status", ""),
            "raw":             account,
        }

    async def get_positions(self) -> list[dict]:
        result = await self.client.get("/positions")
        if not isinstance(result, list):
            return []
        return [
            {
                "symbol":          p.get("symbol"),
                "qty":             float(p.get("qty", 0)),
                "avg_entry_price": float(p.get("avg_entry_price", 0)),
                "current_price":   float(p.get("current_price", 0)),
                "market_value":    float(p.get("market_value", 0)),
                "cost_basis":      float(p.get("cost_basis", 0)),
                "unrealized_pl":   float(p.get("unrealized_pl", 0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                "side":            p.get("side", "long"),
                "raw":             p,
            }
            for p in result
        ]

    async def get_pnl(self) -> dict:
        positions = await self.get_positions()
        total_cost  = sum(p["cost_basis"] for p in positions)
        total_value = sum(p["market_value"] for p in positions)
        total_pl    = sum(p["unrealized_pl"] for p in positions)
        return {
            "account":        self.account_label,
            "position_count": len(positions),
            "total_cost":     round(total_cost, 2),
            "total_value":    round(total_value, 2),
            "unrealized_pnl": round(total_pl, 2),
            "positions":      positions,
        }
