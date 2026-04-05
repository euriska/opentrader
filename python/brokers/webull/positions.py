"""
Webull Positions + Account Data (Official Developer API)
Query balances and positions for a single Webull account.
"""
import logging
from .client import WebullClient

log = logging.getLogger(__name__)


class WebullPositions:

    def __init__(self, client: WebullClient, account_id: str, account_label: str, mode: str):
        self.client        = client
        self.account_id    = account_id
        self.account_label = account_label
        self.mode          = mode

    async def get_balances(self) -> dict:
        internal_id = await self.client.resolve_account_id(self.account_id)
        result = await self.client.get(
            "/account/balance",
            params={"account_id": internal_id},
        )
        assets = {}
        if isinstance(result, dict):
            # Top-level fields
            assets = result
            # Prefer per-currency breakdown if present
            for entry in result.get("account_currency_assets", []):
                if entry.get("currency", "").upper() in ("USD", ""):
                    assets = {**result, **entry}
                    break
        return {
            "cash":         float(assets.get("cash_balance",          assets.get("total_cash_balance", 0)) or 0),
            "net_value":    float(assets.get("net_liquidation_value",  assets.get("total_asset", 0))        or 0),
            "buying_power": float(assets.get("cash_power",             assets.get("margin_power", 0))        or 0),
            "raw":          result,
        }

    async def get_positions(self) -> list[dict]:
        internal_id = await self.client.resolve_account_id(self.account_id)
        items: list = []
        last_id: str = ""
        while True:
            params: dict = {"account_id": internal_id, "page_size": 100}
            if last_id:
                params["last_instrument_id"] = last_id
            result = await self.client.get("/account/positions", params=params)
            page = result.get("holdings", result.get("items", result.get("data", [])))
            if isinstance(result, list):
                page = result
            items.extend(page)
            if not result.get("has_next") or not page:
                break
            last_id = page[-1].get("instrument_id", "")
            if not last_id:
                break

        return [
            {
                "symbol":          p.get("symbol", ""),
                "qty":             float(p.get("qty", 0) or 0),
                "avg_entry_price": float(p.get("unit_cost", 0) or 0),
                "current_price":   float(p.get("last_price", 0) or 0),
                "market_value":    float(p.get("market_value", 0) or 0),
                "unrealized_pl":   float(p.get("unrealized_profit_loss", 0) or 0),
                "date_acquired":   p.get("open_date") or p.get("date_acquired"),
                "raw":             p,
            }
            for p in items
        ]
