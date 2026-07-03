"""Backtesting subsystem: event-driven bar-replay engine + reporting.

Public surface:
    BacktestEngine, BacktestResult   (algobot.backtest.engine)
    OptionDataProvider               (algobot.backtest.option_data)
    compute_metrics                  (algobot.backtest.metrics)
    persist_run, equity_figure       (algobot.backtest.report)
    ensure_strategy_deps             (algobot.backtest.compat)
"""
