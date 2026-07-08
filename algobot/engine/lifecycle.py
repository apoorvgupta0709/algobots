"""Strategy lifecycle: config -> DB seeding, active instantiation, mode moves.

The ``strategies`` table is the single runtime source of truth for each
strategy's mode/params/capital. This module seeds it from
``config/strategies.yaml`` (insert-only — the API's promote/demote owns rows
after the seed), instantiates the active set for the runner, and performs
gate-checked mode transitions (paper -> live requires an eligible gate row).
"""
from __future__ import annotations

import datetime as dt
import logging

from algobot.core import config, registry
from algobot.core.enums import Mode
from algobot.core.exceptions import GateError
from algobot.core.strategy import StrategyBase
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import EventLogRow, GateStatusRow, StrategyRow

log = logging.getLogger(__name__)

#: Modes the engine actually trades. OFF/BACKTEST strategies are never scanned.
DEFAULT_ACTIVE_MODES: frozenset[Mode] = frozenset({Mode.PAPER, Mode.LIVE})


def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


def sync_config_to_db() -> list[str]:
    """Insert a StrategyRow for every registered strategy missing from the DB.

    Precedence for the seeded values: ``config/strategies.yaml`` per-strategy
    entry > yaml ``defaults`` > the strategy's ``meta``. Existing rows are
    NEVER touched — after the first seed the API (promote/demote/params)
    owns them.

    Returns the list of strategy_ids that were inserted.
    """
    init_db()
    yaml_cfg = config.strategies_config()
    defaults = config.strategies_defaults()
    inserted: list[str] = []

    with session_scope() as s:
        existing = {row.strategy_id for row in s.query(StrategyRow.strategy_id)}
        for sid, cls in registry.all_strategies().items():
            if sid in existing:
                continue
            entry = yaml_cfg.get(sid) or {}
            mode_raw = entry.get("mode") or defaults.get("mode") or Mode.PAPER.value
            try:
                mode = Mode(str(mode_raw))
            except ValueError:
                log.warning("invalid mode %r for %s in config; defaulting to paper",
                            mode_raw, sid)
                mode = Mode.PAPER
            params = entry.get("params") or defaults.get("params") \
                or dict(cls.meta.params)
            capital = entry.get("capital") or defaults.get("capital") \
                or cls.meta.capital_required
            s.add(StrategyRow(
                strategy_id=sid,
                category=cls.meta.category.value,
                mode=mode.value,
                params_json=dict(params),
                capital_alloc=float(capital),
                enabled=True,
            ))
            inserted.append(sid)
        if inserted:
            s.add(EventLogRow(
                source="lifecycle", level="info",
                message=f"seeded {len(inserted)} strategies from config",
                detail_json={"strategy_ids": inserted}))
    if inserted:
        log.info("lifecycle seed: inserted %d strategy rows: %s",
                 len(inserted), inserted)
    return inserted


def get_active(mode_filter: set[Mode] | None = None
               ) -> list[tuple[StrategyBase, StrategyRow]]:
    """Instantiate every enabled registered strategy whose mode is in the filter.

    ``params_json`` from the DB is merged over the strategy's meta defaults
    (``StrategyBase.__init__`` semantics). Rows for strategies no longer in
    the registry are skipped with a warning. Default filter: {PAPER, LIVE}.
    """
    modes = {m.value for m in (mode_filter or DEFAULT_ACTIVE_MODES)}
    known = registry.all_strategies()
    out: list[tuple[StrategyBase, StrategyRow]] = []
    with session_scope() as s:
        rows = (s.query(StrategyRow)
                .filter(StrategyRow.enabled.is_(True),
                        StrategyRow.mode.in_(modes))
                .order_by(StrategyRow.strategy_id)
                .all())
    for row in rows:
        cls = known.get(row.strategy_id)
        if cls is None:
            log.warning("strategy %s present in DB but not in registry — skipping",
                        row.strategy_id)
            continue
        try:
            out.append((cls(row.params_json or {}), row))
        except Exception:
            log.exception("failed to instantiate strategy %s", row.strategy_id)
    return out


def set_mode(strategy_id: str, mode: Mode, actor: str = "api",
             force: bool = False) -> StrategyRow:
    """Move a strategy to ``mode``, gate-checked for LIVE promotions.

    Promotion to LIVE requires an eligible :class:`GateStatusRow` unless
    ``force`` is True. Every transition is journalled; a live promotion
    stamps ``promoted_at``/``promoted_by`` on the gate row.

    Raises:
        KeyError: unknown strategy_id (no DB row).
        GateError: LIVE promotion without a passing gate and ``force=False``.
    """
    init_db()
    with session_scope() as s:
        row = s.get(StrategyRow, strategy_id)
        if row is None:
            raise KeyError(f"Unknown strategy '{strategy_id}' — run "
                           "sync_config_to_db() first or check the id")
        gate = s.get(GateStatusRow, strategy_id)
        if mode == Mode.LIVE and not force:
            if gate is None or not gate.eligible:
                raise GateError(
                    f"{strategy_id}: promotion to LIVE blocked — gate "
                    f"{'not evaluated yet' if gate is None else 'not eligible'} "
                    f"(use force=True to override at your own risk)")
        if mode == Mode.LIVE and not config.live_orders_enabled():
            raise GateError(
                f"{strategy_id}: promotion to LIVE refused — the "
                "live_orders_enabled fuse is closed (config/settings.yaml or "
                "ALGOBOT_LIVE_ORDERS_ENABLED); force cannot override the fuse")
        old_mode = row.mode
        row.mode = mode.value
        if mode == Mode.LIVE:
            if gate is None:
                gate = GateStatusRow(strategy_id=strategy_id)
                s.add(gate)
            gate.promoted_at = _utcnow()
            gate.promoted_by = actor
        s.add(EventLogRow(
            source="lifecycle",
            level="warn" if mode == Mode.LIVE else "info",
            message=(f"{strategy_id}: mode {old_mode} -> {mode.value} by {actor}"
                     + (" (FORCED past gate)" if mode == Mode.LIVE and force else "")),
            detail_json={"strategy_id": strategy_id, "from": old_mode,
                         "to": mode.value, "actor": actor, "force": force}))
        s.flush()
        log.info("%s: mode %s -> %s (actor=%s force=%s)",
                 strategy_id, old_mode, mode.value, actor, force)
        return row
