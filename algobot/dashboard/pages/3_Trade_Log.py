"""Filterable trade journal with CSV export and cumulative P&L of the filter set."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="AlgoBot · Trade Log", page_icon="📒", layout="wide")

from algobot.core import clock  # noqa: E402
from algobot.dashboard import ui  # noqa: E402

sidebar_mode = ui.sidebar()
st.title("Trade log")
ui.kill_banner()

df = ui.q_trades(mode=None, days=None)
if df.empty:
    st.info("No trades recorded yet.")
    st.stop()
df["exit_time"] = pd.to_datetime(df["exit_time"])
df["entry_time"] = pd.to_datetime(df["entry_time"])

# --------------------------------------------------------------------- filters
f1, f2, f3, f4 = st.columns([1, 2, 2, 2])
with f1:
    modes = st.multiselect("Mode", sorted(df["mode"].unique()),
                           default=[sidebar_mode] if sidebar_mode else [])
with f2:
    strats = st.multiselect("Strategy", sorted(df["strategy_id"].unique()))
with f3:
    today = clock.now_ist().date()
    min_day = df["exit_time"].min().date()
    date_range = st.date_input("Exit date range", (min_day, today),
                               min_value=min_day, max_value=today)
with f4:
    reasons = st.multiselect("Exit reason", sorted(df["exit_reason"].unique()))

flt = df
if modes:
    flt = flt[flt["mode"].isin(modes)]
if strats:
    flt = flt[flt["strategy_id"].isin(strats)]
if isinstance(date_range, tuple) and len(date_range) == 2:
    lo = pd.Timestamp(dt.datetime.combine(date_range[0], dt.time.min))
    hi = pd.Timestamp(dt.datetime.combine(date_range[1], dt.time.max))
    flt = flt[(flt["exit_time"] >= lo) & (flt["exit_time"] <= hi)]
if reasons:
    flt = flt[flt["exit_reason"].isin(reasons)]

net = float(flt["net_pnl"].sum())
costs = float(flt["costs"].sum())
m1, m2, m3 = st.columns(3)
m1.metric("Trades in filter", len(flt))
m2.metric("Net P&L", ui.fmt_inr(net, sign=True),
          delta=ui.fmt_inr(net, sign=True),
          delta_color="normal" if net >= 0 else "inverse")
m3.metric("Costs paid", ui.fmt_inr(costs))

# ------------------------------------------------------------- cumulative P&L
if not flt.empty:
    cum = flt.sort_values("exit_time")[["exit_time", "net_pnl"]]
    cum["cum_pnl"] = cum["net_pnl"].cumsum()
    fig = go.Figure(go.Scatter(
        x=cum["exit_time"], y=cum["cum_pnl"], mode="lines",
        line=dict(color=ui.BLUE, width=2), name="Cumulative net P&L",
        hovertemplate="%{x|%d %b %H:%M}<br>₹%{y:,.0f}<extra></extra>"))
    fig.add_hline(y=0, line_color=ui.MUTED, line_dash="dot")
    ui.tight_layout(fig, height=260)
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------------- table
show = flt[["exit_time", "strategy_id", "mode", "symbol", "direction", "qty",
            "entry_time", "entry_price", "exit_price", "gross_pnl", "costs",
            "net_pnl", "exit_reason"]]
st.dataframe(
    ui.pnl_styler(show, ["gross_pnl", "net_pnl"]),
    width="stretch", hide_index=True, height=420,
    column_config={
        "exit_time": st.column_config.DatetimeColumn("Exit", format="DD MMM YY HH:mm"),
        "entry_time": st.column_config.DatetimeColumn("Entry", format="DD MMM YY HH:mm"),
        "strategy_id": "Strategy", "mode": "Mode", "symbol": "Symbol",
        "direction": "Dir", "qty": "Qty",
        "entry_price": st.column_config.NumberColumn("Entry px", format="%.2f"),
        "exit_price": st.column_config.NumberColumn("Exit px", format="%.2f"),
        "gross_pnl": "Gross", "costs": st.column_config.NumberColumn("Costs", format="%.0f"),
        "net_pnl": "Net P&L", "exit_reason": "Reason",
    })

st.download_button(
    "Download CSV", flt.to_csv(index=False).encode("utf-8"),
    file_name=f"trades_{clock.now_ist():%Y%m%d_%H%M}.csv", mime="text/csv")
