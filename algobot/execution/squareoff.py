"""End-of-day intraday square-off (15:15 IST window).

Flattens every open position that is either an INTRADAY product or belongs to
a strategy whose meta requests ``intraday_squareoff``. Idempotent: positions
already closed are skipped; safe to call every monitor tick.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Mapping, Optional

from algobot.core.clock import SQUAREOFF_START, now_ist
from algobot.core.enums import ExitReason, ProductType
from algobot.core.models import Order
from algobot.persistence.db import session_scope
from algobot.persistence.schema import PositionRow

log = logging.getLogger(__name__)


def squareoff_intraday(order_manager, registry_metas: Optional[Mapping] = None,
                       now: Optional[dt.datetime] = None) -> list[Order]:
    """Flatten intraday positions after 15:15 IST. No-op before the window.

    ``registry_metas`` maps strategy_id -> StrategyMeta (or anything with an
    ``intraday_squareoff`` attribute) so positional products carried by
    intraday strategies are flattened too.
    """
    ts = now or now_ist()
    if ts.time() < SQUAREOFF_START:
        return []

    metas = registry_metas or {}
    with session_scope() as s:
        rows = s.query(PositionRow).filter_by(status="open").all()
        targets = []
        for row in rows:
            meta = metas.get(row.strategy_id)
            if (row.product_type == ProductType.INTRADAY.value
                    or bool(getattr(meta, "intraday_squareoff", False))):
                targets.append(row.id)

    orders: list[Order] = []
    for pid in targets:
        try:
            order = order_manager.close_position(pid, ExitReason.SQUAREOFF)
            if order is not None:
                orders.append(order)
        except Exception:
            log.exception("squareoff failed for position %d", pid)
    if orders:
        log.info("squared off %d intraday positions", len(orders))
    return orders
