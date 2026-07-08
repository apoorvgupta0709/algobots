"""Live Fyers broker adapter implementing :class:`BrokerInterface`.

Thin translation layer only: Order -> fyers_apiv3 payloads and fyers responses
-> core models. All persistence (orders/positions/trades tables) is done by
the OrderManager, not here — Fyers is the source of truth for live state.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from algobot.broker.base import BrokerInterface
from algobot.core.enums import Mode, OrderStatus, ProductType
from algobot.core.exceptions import BrokerError
from algobot.core.models import Order, Position

log = logging.getLogger(__name__)


class FyersBroker(BrokerInterface):
    """Order routing / quotes / positions / funds via fyers_apiv3.

    The API client is constructed lazily (first use) so importing this module
    never triggers a login. Tests inject a fake ``client``.
    """

    def __init__(self, client: Any | None = None):
        self._client = client

    # ------------------------------------------------------------------ plumbing
    @property
    def name(self) -> str:
        return "fyers"

    @property
    def client(self) -> Any:
        if self._client is None:
            from algobot.broker.fyers.auth import get_fyers_client
            self._client = get_fyers_client()
        return self._client

    @staticmethod
    def _ensure_ok(resp: Any, action: str) -> dict:
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            msg = resp.get("message", "") if isinstance(resp, dict) else str(resp)
            raise BrokerError(f"Fyers {action} failed: {msg or resp}")
        return resp

    # ------------------------------------------------------------------ orders
    def place_order(self, order: Order) -> Order:
        """Place a single order; returns it with broker_order_id and PLACED status."""
        from algobot.core.config import live_orders_enabled
        if not live_orders_enabled():
            order.status = OrderStatus.REJECTED
            raise BrokerError(
                f"live order REFUSED for {order.symbol}: the live_orders_enabled "
                "fuse is closed (config/settings.yaml / ALGOBOT_LIVE_ORDERS_ENABLED)")
        payload = {
            "symbol": order.symbol,
            "qty": int(abs(order.qty)),
            "type": int(order.order_type),          # 1 limit, 2 market, 3 SL-M, 4 SL-L
            "side": int(order.side),                # 1 buy, -1 sell
            "productType": order.product_type.value,
            "limitPrice": float(order.limit_price or 0),
            "stopPrice": float(order.stop_price or 0),
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": order.tag or "",
        }
        resp = self.client.place_order(data=payload)
        try:
            resp = self._ensure_ok(resp, f"place_order {order.symbol}")
        except BrokerError:
            order.status = OrderStatus.REJECTED
            raise
        order.broker_order_id = str(resp.get("id", ""))
        order.status = OrderStatus.PLACED
        log.info("Fyers order placed %s %s x%d -> %s",
                 order.side.name, order.symbol, order.qty, order.broker_order_id)
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        resp = self.client.cancel_order(data={"id": broker_order_id})
        return isinstance(resp, dict) and resp.get("s") == "ok"

    # ------------------------------------------------------------------ market data
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Last traded prices via /quotes; symbols with no ltp are omitted."""
        if not symbols:
            return {}
        resp = self._ensure_ok(self.client.quotes(data={"symbols": ",".join(symbols)}),
                               "quotes")
        out: dict[str, float] = {}
        for item in resp.get("d", []) or []:
            v = item.get("v") or {}
            lp = v.get("lp")
            sym = item.get("n") or v.get("symbol")
            if sym and lp is not None:
                out[sym] = float(lp)
        return out

    # ------------------------------------------------------------------ account
    def get_positions(self) -> list[Position]:
        """Net positions from the broker; zero-qty (closed today) rows skipped."""
        resp = self._ensure_ok(self.client.positions(), "positions")
        out: list[Position] = []
        for p in resp.get("netPositions", []) or []:
            qty = int(p.get("netQty", 0) or 0)
            if qty == 0:
                continue
            try:
                product = ProductType(p.get("productType", "INTRADAY"))
            except ValueError:
                product = ProductType.MARGIN
            out.append(Position(
                strategy_id="external",       # broker book: not attributable to a strategy
                symbol=p.get("symbol", ""),
                qty=qty,
                avg_price=float(p.get("netAvg") or p.get("avgPrice") or 0.0),
                mode=Mode.LIVE,
                opened_at=dt.datetime.now(),
                product_type=product,
            ))
        return out

    def get_funds(self) -> float:
        """Available balance: fund_limit entry with id 10."""
        resp = self._ensure_ok(self.client.funds(), "funds")
        for item in resp.get("fund_limit", []) or []:
            if item.get("id") == 10:
                return float(item.get("equityAmount", 0.0) or 0.0)
        raise BrokerError("Fyers funds response had no fund_limit entry with id 10")

    def __repr__(self) -> str:  # pragma: no cover
        return "<FyersBroker>"


__all__ = ["FyersBroker"]
