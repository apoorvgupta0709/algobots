"""Backtest run browser: metrics table + per-run detail expanders."""
from __future__ import annotations

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AlgoBot · Backtests", page_icon="🧪", layout="wide")

from algobot.dashboard import ui  # noqa: E402

ui.sidebar()
st.title("Backtest runs")
ui.kill_banner()

runs = ui.q_backtest_runs()
if runs.empty:
    st.info("No backtest runs recorded yet.")
    st.stop()

strats = ["all"] + sorted(runs["strategy_id"].unique())
pick = st.selectbox("Strategy", strats)
if pick != "all":
    runs = runs[runs["strategy_id"] == pick]

SOURCE_ICON = {"real": "🟢 real", "synthetic": "🟠 synthetic", "mixed": "🟡 mixed"}
table = runs.assign(
    period=runs["start"].astype(str) + " → " + runs["end"].astype(str),
    source=runs["data_source"].map(lambda s: SOURCE_ICON.get(s, s)),
)[["id", "strategy_id", "period", "source", "profit_factor", "max_drawdown_pct",
   "sharpe", "trades", "net_pnl", "cost_model_version", "created_at"]]

st.dataframe(
    ui.pnl_styler(table, ["net_pnl"]),
    use_container_width=True, hide_index=True,
    column_config={
        "id": "Run", "strategy_id": "Strategy", "period": "Period",
        "source": "Data source",
        "profit_factor": st.column_config.NumberColumn("PF", format="%.2f"),
        "max_drawdown_pct": st.column_config.NumberColumn("Max DD %", format="%.1f"),
        "sharpe": st.column_config.NumberColumn("Sharpe", format="%.2f"),
        "trades": "Trades", "net_pnl": "Net P&L",
        "cost_model_version": "Cost model",
        "created_at": st.column_config.DatetimeColumn("Run at", format="DD MMM YY HH:mm"),
    })

st.subheader("Run details")
for _, r in runs.iterrows():
    pf = r["profit_factor"]
    label = (f"Run {r['id']} · {r['strategy_id']} · {r['start']} → {r['end']} · "
             f"{SOURCE_ICON.get(r['data_source'], r['data_source'])}"
             + (f" · PF {pf:.2f}" if pd.notna(pf) else ""))
    with st.expander(label):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Metrics**")
            st.json(r["metrics_json"] or {})
        with c2:
            st.markdown("**Parameters**")
            st.json(r["params_json"] or {})
