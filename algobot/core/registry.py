"""Auto-discovery of strategy plugins.

Walks ``algobot.strategies.**``, imports every module, and indexes each
``StrategyBase`` subclass by ``meta.strategy_id``. Adding a strategy file is
all that's needed to make it visible to the backtester, engine, API and
dashboard.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from algobot.core.strategy import StrategyBase

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[StrategyBase]] = {}
_LOADED = False


def _discover() -> None:
    global _LOADED
    if _LOADED:
        return
    import algobot.strategies as pkg

    for modinfo in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        if modinfo.ispkg:
            continue
        try:
            module = importlib.import_module(modinfo.name)
        except Exception:  # a broken strategy file must not take down the platform
            log.exception("Failed to import strategy module %s — skipping", modinfo.name)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, StrategyBase) and obj is not StrategyBase
                    and obj.__module__ == module.__name__):
                sid = obj.meta.strategy_id
                if sid in _REGISTRY:
                    raise ValueError(
                        f"Duplicate strategy_id '{sid}' in {module.__name__} "
                        f"(already registered from {_REGISTRY[sid].__module__})")
                expected = module.__name__.rsplit(".", 1)[-1]
                if sid != expected:
                    raise ValueError(
                        f"strategy_id '{sid}' must equal its file name '{expected}'")
                _REGISTRY[sid] = obj
    _LOADED = True
    log.info("Strategy registry loaded: %d strategies", len(_REGISTRY))


def all_strategies() -> dict[str, type[StrategyBase]]:
    _discover()
    return dict(_REGISTRY)


def get_strategy(strategy_id: str) -> type[StrategyBase]:
    _discover()
    try:
        return _REGISTRY[strategy_id]
    except KeyError:
        raise KeyError(f"Unknown strategy '{strategy_id}'. "
                       f"Known: {sorted(_REGISTRY)}") from None


def by_category(category) -> dict[str, type[StrategyBase]]:
    _discover()
    return {sid: cls for sid, cls in _REGISTRY.items()
            if cls.meta.category == category}
