"""
Broker Router
Receives a parsed command dict from the gateway, resolves which connector(s)
to use, executes the operation, and returns a list of result dicts.

Command fields (all string-encoded for Redis stream compatibility):
  command        — place_order | cancel_order | cancel_all | get_positions |
                   get_balances | get_orders | get_quote | get_quotes
  request_id     — caller-supplied correlation ID
  account_label  — route to this specific account (takes priority)
  broker         — filter by broker name (tradier|alpaca|webull)
  mode           — filter by mode (live|sandbox|paper)
  strategy_tag   — filter by strategy tag
  asset_class    — equity | option  (for place_order)
  symbol         — ticker symbol
  option_symbol  — full option symbol (for option orders)
  side           — buy|sell|sell_short|buy_to_cover|buy_to_open|...
  quantity       — integer shares/contracts
  order_type     — market|limit|stop|stop_limit
  price          — limit price (float string or empty)
  stop           — stop price (float string or empty)
  duration       — day|gtc|pre|post
  tag            — client order tag
  order_id       — for cancel_order
  symbols        — comma-separated list (for get_quotes)
  status         — all|open|filled  (for get_orders)
"""
import asyncio
import logging
from typing import Optional

from .registry import BrokerRegistry, AccountRecord

log = logging.getLogger(__name__)


def _flt(value: str) -> Optional[float]:
    """Parse float from string, return None if empty or invalid."""
    try:
        return float(value) if value else None
    except (ValueError, TypeError):
        return None


def _int(value: str) -> int:
    try:
        return int(value) if value else 0
    except (ValueError, TypeError):
        return 0


class BrokerRouter:
    """
    Stateless routing layer — holds a reference to the registry,
    executes commands, normalises results.
    """

    def __init__(self, registry: BrokerRegistry):
        self.registry = registry

    # ── Entry point ───────────────────────────────────────────────────────────

    async def route(self, cmd: dict) -> list[dict]:
        """
        Route a command to one or more connectors.
        Returns a list of result dicts (one per account targeted).
        """
        command = cmd.get("command", "")
        handler = {
            "place_order":   self._place_order,
            "cancel_order":  self._cancel_order,
            "cancel_all":    self._cancel_all,
            "get_positions": self._get_positions,
            "get_balances":  self._get_balances,
            "get_orders":    self._get_orders,
            "get_quote":     self._get_quote,
            "get_quotes":    self._get_quotes,
        }.get(command)

        if not handler:
            return [self._error(cmd, f"Unknown command: {command}")]

        try:
            return await handler(cmd)
        except Exception as e:
            log.error(f"[router] Unhandled error in {command}: {e}", exc_info=True)
            return [self._error(cmd, str(e))]

    # ── Account resolution ────────────────────────────────────────────────────

    def _resolve_accounts(self, cmd: dict) -> list[AccountRecord]:
        label = cmd.get("account_label", "")
        if label:
            rec = self.registry.get_record(label)
            if not rec:
                log.warning(f"[router] Unknown account_label: {label}")
                return []
            return [rec]

        return self.registry.find(
            broker=cmd.get("broker") or None,
            mode=cmd.get("mode") or None,
            strategy_tag=cmd.get("strategy_tag") or None,
        )

    def _result(self, cmd: dict, account: AccountRecord, data: dict) -> dict:
        return {
            "request_id":    cmd.get("request_id", ""),
            "command":       cmd.get("command", ""),
            "account_label": account.label,
            "broker":        account.broker,
            "mode":          account.mode,
            "status":        "ok",
            "data":          data,
            "error":         "",
        }

    def _error(self, cmd: dict, message: str, account_label: str = "") -> dict:
        return {
            "request_id":    cmd.get("request_id", ""),
            "command":       cmd.get("command", ""),
            "account_label": account_label,
            "broker":        "",
            "mode":          "",
            "status":        "error",
            "data":          {},
            "error":         message,
        }

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _place_order(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        if not accounts:
            return [self._error(cmd, "No matching accounts for place_order")]

        asset_class = cmd.get("asset_class", "equity")
        symbol      = cmd.get("symbol", "")
        side        = cmd.get("side", "buy")
        quantity    = _int(cmd.get("quantity", "1"))
        order_type  = cmd.get("order_type", "market")
        price       = _flt(cmd.get("price", ""))
        stop        = _flt(cmd.get("stop", ""))
        duration    = cmd.get("duration", "day")
        tag         = cmd.get("tag", "") or None
        opt_sym     = cmd.get("option_symbol", "")

        tasks = []
        for rec in accounts:
            if asset_class == "option" and opt_sym:
                tasks.append((rec, rec.connector.place_option_order(
                    symbol=symbol, option_symbol=opt_sym,
                    side=side, quantity=quantity,
                    order_type=order_type, price=price,
                    duration=duration, tag=tag,
                )))
            else:
                tasks.append((rec, rec.connector.place_equity_order(
                    symbol=symbol, side=side, quantity=quantity,
                    order_type=order_type, price=price, stop=stop,
                    duration=duration, tag=tag,
                )))

        return await self._gather(cmd, tasks)

    async def _cancel_order(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        order_id = cmd.get("order_id", "")
        if not order_id:
            return [self._error(cmd, "cancel_order requires order_id")]

        tasks = [(rec, rec.connector.cancel_order(order_id)) for rec in accounts]
        return await self._gather(cmd, tasks)

    async def _cancel_all(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        tasks    = [(rec, rec.connector.cancel_all_orders()) for rec in accounts]
        return await self._gather(cmd, tasks)

    async def _get_positions(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        tasks    = [(rec, rec.connector.get_positions()) for rec in accounts]
        return await self._gather(cmd, tasks)

    async def _get_balances(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        tasks    = [(rec, rec.connector.get_balances()) for rec in accounts]
        return await self._gather(cmd, tasks)

    async def _get_orders(self, cmd: dict) -> list[dict]:
        accounts = self._resolve_accounts(cmd)
        status   = cmd.get("status", "all")
        tasks    = [(rec, rec.connector.get_orders(status=status)) for rec in accounts]
        return await self._gather(cmd, tasks)

    async def _get_quote(self, cmd: dict) -> list[dict]:
        symbol = cmd.get("symbol", "")
        # Use first matching connector for market data
        accounts = self._resolve_accounts(cmd)
        if not accounts:
            accounts = self.registry.all_records()
        if not accounts:
            return [self._error(cmd, "No connectors available for get_quote")]
        rec = accounts[0]
        try:
            data = await rec.connector.get_quote(symbol)
            return [self._result(cmd, rec, data)]
        except Exception as e:
            return [self._error(cmd, str(e), rec.label)]

    async def _get_quotes(self, cmd: dict) -> list[dict]:
        symbols_raw = cmd.get("symbols", "")
        symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
        accounts = self._resolve_accounts(cmd)
        if not accounts:
            accounts = self.registry.all_records()
        if not accounts:
            return [self._error(cmd, "No connectors available for get_quotes")]
        rec = accounts[0]
        try:
            data = await rec.connector.get_quotes(symbols)
            return [self._result(cmd, rec, {"quotes": data})]
        except Exception as e:
            return [self._error(cmd, str(e), rec.label)]

    # ── Gather helper ─────────────────────────────────────────────────────────

    async def _gather(
        self,
        cmd:   dict,
        tasks: list[tuple],
    ) -> list[dict]:
        """Run all (record, coroutine) pairs concurrently, collect results."""
        records  = [t[0] for t in tasks]
        coros    = [t[1] for t in tasks]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        results = []
        for rec, outcome in zip(records, outcomes):
            if isinstance(outcome, Exception):
                log.error(f"[router] {cmd.get('command')} failed for {rec.label}: {outcome}")
                results.append(self._error(cmd, str(outcome), rec.label))
            else:
                results.append(self._result(cmd, rec, outcome if isinstance(outcome, dict) else {"items": outcome}))
        return results
