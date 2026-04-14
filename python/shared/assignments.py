"""
Assignment + Strategy loader for trader agents.

Reads /app/config/assignments.json and /app/config/strategies.json,
joins them, and returns active assignments enriched with the pinned
strategy's parameters (confidence threshold, max position size, etc.).

Traders call load_active_assignments(asset_class) to get the list of
accounts they should place orders against, along with the parameters
defined by the assigned strategy — instead of embedding those values
in the agent code.
"""
import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

ASSIGNMENTS_PATH = os.getenv("ASSIGNMENTS_PATH", "/app/config/assignments.json")
STRATEGIES_PATH  = os.getenv("STRATEGIES_PATH",  "/app/config/strategies.json")


def _read_json(path: str) -> list:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("assignments.file_not_found", path=path)
        return []
    except Exception as e:
        log.error("assignments.read_error", path=path, error=str(e))
        return []


def _asset_match(strat_asset: str, signal_asset: str) -> bool:
    """True if a strategy for strat_asset should execute on signal_asset signals."""
    parts = [p.strip() for p in strat_asset.split(",")]
    if signal_asset in parts:
        return True
    # Equity strategies cover ETFs — same execution path
    if "equity" in parts and signal_asset == "etf":
        return True
    return False


def load_active_assignments(asset_class: str) -> list[dict]:
    """
    Return all active assignments whose pinned strategy covers asset_class.

    Each entry contains:
      account_label      — route orders here via broker gateway
      broker             — for OrderEventPayload
      mode               — for OrderEventPayload
      strategy_name      — human-readable name from the assignment
      strategy_family_id — strategy identifier
      min_confidence     — from strategy.confidence (reject signals below this)
      max_pos_usd        — from strategy.max_pos (position sizing cap)
    """
    assignments = _read_json(ASSIGNMENTS_PATH)
    strategies  = _read_json(STRATEGIES_PATH)

    # Build lookup: (family_id, version) → strategy record
    strat_index: dict[tuple, dict] = {}
    for s in strategies:
        key = (s.get("family_id", ""), int(s.get("version", 1)))
        strat_index[key] = s

    enriched = []
    for a in assignments:
        if a.get("status") != "active":
            continue

        fid = a.get("strategy_family_id", "")
        ver = int(a.get("pinned_version", 1))
        strat = strat_index.get((fid, ver))

        if strat is None:
            log.warning(
                "assignments.strategy_not_found",
                account=a.get("account_label"),
                family_id=fid,
                version=ver,
            )
            continue

        strat_asset = strat.get("asset", "")
        if not _asset_match(strat_asset, asset_class):
            continue

        enriched.append({
            "account_label":      a["account_label"],
            "broker":             a.get("broker", ""),
            "mode":               a.get("mode", ""),
            "strategy_name":      a.get("strategy_name", strat.get("name", "")),
            "strategy_family_id": fid,
            "min_confidence":     float(strat.get("confidence", 0.70)),
            "max_pos_usd":        float(strat.get("max_pos") or 500),
        })

    return enriched
