"""
Broker Gateway Agent
The single broker communication hub for the entire platform.

Listens on: broker.commands  (Redis stream)
Publishes to: broker.fills   (Redis stream)
Reply channel: broker:reply:{request_id}  (Redis list, blpop pattern)

Command protocol — all fields are string-encoded for Redis compatibility.
See broker_gateway/router.py for the full field reference.
"""
import asyncio
import json
import logging
import os

import structlog

from shared.base_agent   import BaseAgent
from shared.redis_client import STREAMS, GROUPS, get_redis
from .registry           import BrokerRegistry
from .router             import BrokerRouter

log = structlog.get_logger("broker-gateway")

CONSUMER_NAME  = os.getenv("HOSTNAME", "broker-gateway-0")
REPLY_TTL      = int(os.getenv("BROKER_REPLY_TTL_SEC", "60"))


class BrokerGatewayAgent(BaseAgent):
    """
    Consumes broker.commands, routes to the correct broker connector,
    and writes results to both broker.fills and the reply key.
    """

    def __init__(self):
        super().__init__("broker-gateway")
        self.registry = BrokerRegistry()
        self.router   = BrokerRouter(self.registry)

    async def start(self):
        await self.setup()
        await self._ensure_consumer_group()

        log.info(
            "broker-gateway.started",
            accounts=list(self.registry.summary().keys()),
        )

        await asyncio.gather(
            self.heartbeat_loop(),
            self._command_loop(),
            self._order_poll_loop(),
        )

    async def _ensure_consumer_group(self):
        stream = STREAMS["broker_commands"]
        group  = GROUPS["broker_gateway"]
        try:
            await self.redis.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                log.warning("broker-gateway.group_create", error=str(e))

    async def _command_loop(self):
        stream = STREAMS["broker_commands"]
        group  = GROUPS["broker_gateway"]
        log.info("broker-gateway.command_loop_start")

        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname    = group,
                    consumername = CONSUMER_NAME,
                    streams      = {stream: ">"},
                    count        = 10,
                    block        = 5000,
                )
                if not messages:
                    continue

                for _stream, entries in messages:
                    for msg_id, data in entries:
                        await self._handle_command(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                err = str(e)
                log.error("broker-gateway.command_loop_error", error=err)
                if "NOGROUP" in err:
                    await self._ensure_consumer_group()
                # Redis loading its dataset after restart — wait longer
                wait = 10 if "loading" in err.lower() else 3
                await asyncio.sleep(wait)
                try:
                    await self.redis.ping()
                except Exception:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass
                    self.redis = await get_redis()

    async def _handle_command(self, msg_id: str, cmd: dict):
        command    = cmd.get("command", "unknown")
        request_id = cmd.get("request_id", "")
        issued_by  = cmd.get("issued_by", "unknown")

        log.info(
            "broker-gateway.command",
            command=command,
            request_id=request_id,
            issued_by=issued_by,
            account_label=cmd.get("account_label", ""),
        )

        try:
            results = await self.router.route(cmd)
        except Exception as e:
            log.error("broker-gateway.route_error", command=command, error=str(e))
            results = [{
                "request_id":    request_id,
                "command":       command,
                "account_label": "",
                "broker":        "",
                "mode":          "",
                "status":        "error",
                "data":          {},
                "error":         str(e),
            }]

        # Publish each result to the fills stream
        fills_stream = STREAMS["broker_fills"]
        for r in results:
            await self.redis.xadd(
                fills_stream,
                {
                    "request_id":    r["request_id"],
                    "command":       r["command"],
                    "account_label": r["account_label"],
                    "broker":        r["broker"],
                    "mode":          r["mode"],
                    "status":        r["status"],
                    "data":          json.dumps(r["data"]),
                    "error":         r["error"],
                    "issued_by":     issued_by,
                },
                maxlen=50_000,
            )

        # Also write first result to reply key so callers can blpop
        if request_id:
            reply_key = f"broker:reply:{request_id}"
            reply_payload = json.dumps(results[0] if len(results) == 1 else results)
            await self.redis.lpush(reply_key, reply_payload)
            await self.redis.expire(reply_key, REPLY_TTL)

        # Acknowledge message
        await self.redis.xack(STREAMS["broker_commands"], GROUPS["broker_gateway"], msg_id)

        log.info(
            "broker-gateway.command_done",
            command=command,
            request_id=request_id,
            results=len(results),
            statuses=[r["status"] for r in results],
        )

    async def _order_poll_loop(self):
        """Every 60 s, fetch open orders from all brokers and emit fill events for any that closed."""
        POLL_INTERVAL = int(os.getenv("ORDER_POLL_INTERVAL_SEC", "60"))
        known_open: dict[str, set] = {}  # account_label → set of order_ids

        await asyncio.sleep(15)  # let the command loop start first
        log.info("broker-gateway.order_poll_start", interval=POLL_INTERVAL)

        while self._running:
            try:
                for rec in self.registry.all_records():
                    try:
                        orders = await rec.connector.get_orders(status="all")
                    except Exception as e:
                        log.warning("broker-gateway.order_poll_fetch_error",
                                    account=rec.label, error=str(e))
                        continue

                    prev_open = known_open.get(rec.label, set())
                    curr_open: set[str] = set()
                    newly_filled = []

                    for o in orders:
                        if not isinstance(o, dict):
                            continue
                        oid = str(o.get("id") or o.get("order_id") or o.get("clientOrderId") or "")
                        if not oid:
                            continue
                        status = str(o.get("status", "")).lower()
                        if status in ("open", "partially_filled", "pending_new", "new", "accepted", "held"):
                            curr_open.add(oid)
                        elif oid in prev_open and status in ("filled", "complete", "completed"):
                            newly_filled.append(o)

                    known_open[rec.label] = curr_open

                    for o in newly_filled:
                        try:
                            await self.redis.xadd(
                                STREAMS.get("orders", "orders.events"),
                                {
                                    "event_type":    "fill",
                                    "account_label": rec.label,
                                    "broker":        rec.broker,
                                    "mode":          rec.mode,
                                    "ticker":        o.get("symbol", ""),
                                    "order_id":      str(o.get("id") or ""),
                                    "qty":           str(o.get("quantity") or o.get("qty") or ""),
                                    "fill_price":    str(o.get("avg_fill_price") or o.get("filledPrice") or ""),
                                    "side":          str(o.get("side") or ""),
                                    "source":        "order_poll",
                                },
                                maxlen=10_000,
                            )
                            log.info("broker-gateway.order_fill_detected",
                                     account=rec.label, order_id=o.get("id"), symbol=o.get("symbol"))
                        except Exception as e:
                            log.warning("broker-gateway.order_poll_emit_error", error=str(e))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("broker-gateway.order_poll_error", error=str(e))

            await asyncio.sleep(POLL_INTERVAL)

    async def shutdown(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()


async def main():
    logging.basicConfig(level=logging.INFO)
    agent = BrokerGatewayAgent()
    try:
        await agent.start()
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
