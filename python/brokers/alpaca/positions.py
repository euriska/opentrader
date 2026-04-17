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

        positions = [
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
                "date_acquired":   p.get("date_acquired"),
                "raw":             p,
            }
            for p in result
        ]

        # Alpaca paper often omits date_acquired — backfill from order history.
        missing = [p for p in positions if not p["date_acquired"]]
        if missing:
            try:
                orders = await self.client.get(
                    "/orders",
                    params={"status": "closed", "limit": 500, "direction": "asc"},
                )
                if isinstance(orders, list):
                    # earliest buy fill per symbol
                    earliest: dict = {}
                    for o in orders:
                        sym = o.get("symbol", "")
                        side = o.get("side", "")
                        filled_at = o.get("filled_at")
                        if sym and side == "buy" and filled_at and sym not in earliest:
                            earliest[sym] = filled_at
                    for p in missing:
                        sym = p["symbol"] or ""
                        if sym in earliest:
                            p["date_acquired"] = earliest[sym]
            except Exception as e:
                log.warning(f"[alpaca:{self.account_label}] date_acquired backfill failed: {e}")

        return positions

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
