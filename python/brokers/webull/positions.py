"""
Webull Positions + Account Data (Official Developer API)
Query balances and positions for a single Webull account.

v1 endpoint (/account/positions)         — minimal fields, no option contract details
v2 endpoint (/openapi/assets/positions)  — returns legs[] with strikePrice, expiryDate, right
"""
import logging
from .client import WebullClient, APP_KEY

log = logging.getLogger(__name__)


def _parse_v2_legs(raw_pos: dict) -> dict:
    """
    Extract option contract details from a v2 position's legs array.
    Returns dict with keys: option_type, strike_price, expiry_date (or empty dict).
    """
    legs = raw_pos.get("legs") or []
    if not legs:
        return {}
    leg = legs[0]  # single-leg options have one entry
    raw_type = str(leg.get("option_type") or leg.get("right") or "").upper()
    if raw_type in ("CALL", "C"):
        opt_type = "call"
    elif raw_type in ("PUT", "P"):
        opt_type = "put"
    else:
        opt_type = None

    raw_strike = leg.get("option_exercise_price") or leg.get("strikePrice") or leg.get("strike_price")
    try:
        strike = float(raw_strike) if raw_strike else None
    except (ValueError, TypeError):
        strike = None

    raw_expiry = (
        leg.get("option_expire_date") or leg.get("expiryDate") or
        leg.get("expiry_date") or leg.get("expiration_date")
    )
    expiry = str(raw_expiry)[:10] if raw_expiry else None

    return {"option_type": opt_type, "strike_price": strike, "expiry_date": expiry}


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

    async def _fetch_v2_positions(self, account_id: str) -> dict:
        """
        Fetch positions from the v2 OpenAPI endpoint using WEBULL_APP_KEY/APP_SECRET.
        Returns a dict mapping instrument_id → leg details (option_type, strike, expiry).
        Returns empty dict if APP_KEY is not configured or the call fails.
        """
        if not APP_KEY:
            return {}
        try:
            # v2 may use the account_number directly or the resolved internal_id;
            # try both — the first successful non-empty response wins
            items_v2: list = []
            for acct_id in dict.fromkeys([self.account_id, account_id]):
                result = await self.client.get_v2(
                    "/openapi/assets/positions",
                    params={"account_id": acct_id},
                )
                items_v2 = result if isinstance(result, list) else result.get("data", result.get("items", []))
                if items_v2:
                    break
            out: dict = {}
            items = items_v2
            for pos in items:
                iid = str(pos.get("instrument_id") or "")
                if iid and pos.get("instrument_type", "").upper() == "OPTION":
                    details = _parse_v2_legs(pos)
                    if details:
                        out[iid] = details
            log.info(f"[webull-v2] fetched {len(out)} option leg details for {account_id}")
            return out
        except Exception as e:
            log.warning(f"[webull-v2] positions call failed (will use v1 only): {e}")
            return {}

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

        # Try v2 enrichment for option positions (adds strike, expiry, type from legs)
        option_items = [p for p in items if str(p.get("instrument_type", "")).upper() == "OPTION"]
        v2_details: dict = {}
        if option_items:
            v2_details = await self._fetch_v2_positions(internal_id)

        out = []
        for p in items:
            iid = str(p.get("instrument_id") or "")
            raw = dict(p)
            # Inject v2 leg data directly into raw so _normalise_option_position can use it
            if iid and iid in v2_details:
                raw.update(v2_details[iid])
            out.append({
                "symbol":          p.get("symbol", ""),
                "qty":             float(p.get("qty", 0) or 0),
                "avg_entry_price": float(p.get("unit_cost", 0) or 0),
                "current_price":   float(p.get("last_price", 0) or 0),
                "market_value":    float(p.get("market_value", 0) or 0),
                "unrealized_pl":   float(p.get("unrealized_profit_loss", 0) or 0),
                "date_acquired":   p.get("open_date") or p.get("date_acquired"),
                "raw":             raw,
            })
        return out
