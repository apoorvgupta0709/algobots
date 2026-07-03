"""Per-strategy deep dive: equity curve, trade stats, monthly heatmap, histogram."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="AlgoBot · Strategy P&L", page_icon="📊", layout="wide")

from algobot.dashboard import ui  # noqa: E402

mode = ui.sidebar()
st.title("Strategy P&L")
ui.kill_banner()

overview = ui.q_strategies_overview()
if overview.empty:
    st.info("No strategies found.")
    st.stop()

names = dict(zip(overview["strategy_id"], overview["name"]))
sid = st.selectbox("Strategy", list(overview["strategy_id"]),
                   format_func=lambda s: f"{s} — {names.get(s, s)}")
meta_row = overview[overview["strategy_id"] == sid].iloc[0]
st.markdown(f"{ui.mode_badge(meta_row['mode'])} &nbsp; "
            f"*{meta_row['category']}* · alloc {ui.fmt_inr(meta_row['capital_alloc'])}"
            + (f" — {meta_row['description']}" if meta_row["description"] else ""),
            unsafe_allow_html=True)

lookback = st.radio("Trade window", ["30d", "90d", "365d", "All"], index=3,
                    horizontal=True)
days = {"30d": 30, "90d": 90, "365d": 365, "All": None}[lookback]
tdf = ui.q_trades(mode=mode, strategy_id=sid, days=days)

# --------------------------------------------------------------- equity curve
st.subheader("Equity curve")
eq = ui.q_equity_curves(strategy_id=sid)
if not eq.empty and mode:
    eq = eq[eq["mode"] == mode]
if eq.empty:
    st.info("No equity snapshots for this strategy yet.")
else:
    fig = go.Figure()
    for i, (m, g) in enumerate(eq.groupby("mode", sort=True)):
        fig.add_trace(go.Scatter(
            x=g["ts"], y=g["equity"], mode="lines", name=m,
            line=dict(color=ui.SERIES[i % len(ui.SERIES)], width=2),
            hovertemplate="%{x|%d %b %H:%M}<br>₹%{y:,.0f}<extra>" + m + "</extra>"))
    ui.tight_layout(fig, height=320)
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- trade stats
st.subheader("Trade statistics")
if tdf.empty:
    st.info("No closed trades in this window.")
    st.stop()

pnl = tdf.sort_values("exit_time")["net_pnl"].astype(float)
wins, losses = pnl[pnl > 0], pnl[pnl < 0]
win_rate = len(wins) / len(pnl) * 100 if len(pnl) else 0.0
pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
expectancy = float(pnl.mean())
cum = pnl.cumsum()
max_dd = float((cum.cummax() - cum).max()) if len(cum) else 0.0

s1, s2, s3, s4, s5, s6 = st.columns(6)
s1.metric("Trades", len(pnl))
s2.metric("Win rate", f"{win_rate:.1f}%")
s3.metric("Profit factor", "∞" if np.isinf(pf) else f"{pf:.2f}")
s4.metric("Expectancy / trade", ui.fmt_inr(expectancy, sign=True))
s5.metric("Net P&L", ui.fmt_inr(float(pnl.sum()), sign=True))
s6.metric("Max drawdown", ui.fmt_inr(max_dd))

left, right = st.columns(2)

# ------------------------------------------------------------ monthly heatmap
with left:
    st.subheader("Monthly P&L")
    m = tdf.assign(exit_time=pd.to_datetime(tdf["exit_time"]))
    m["year"] = m["exit_time"].dt.year
    m["month"] = m["exit_time"].dt.month
    pivot = m.pivot_table(index="year", columns="month", values="net_pnl",
                          aggfunc="sum")
    pivot = pivot.reindex(columns=range(1, 13))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    zmax = float(np.nanmax(np.abs(pivot.values))) or 1.0
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=months, y=[str(y) for y in pivot.index],
        zmin=-zmax, zmax=zmax,
        colorscale=[[0, ui.NEG], [0.5, ui.NEUTRAL_MID], [1, ui.POS]],
        text=[[ui.fmt_inr(v, sign=True) if pd.notna(v) else ""
               for v in row] for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=10),
        hovertemplate="%{y} %{x}: ₹%{z:,.0f}<extra></extra>",
        xgap=2, ygap=2, showscale=False))
    ui.tight_layout(fig, height=max(180, 60 + 48 * len(pivot)))
    fig.update_layout(hovermode="closest")
    fig.update_yaxes(autorange="reversed")
    st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------- P&L histogram
with right:
    st.subheader("Per-trade P&L distribution")
    fig = go.Figure(go.Histogram(
        x=pnl, nbinsx=min(40, max(10, len(pnl) // 3)),
        marker=dict(color=ui.BLUE, cornerradius=4,
                    line=dict(color="rgba(0,0,0,0)", width=1)),
        hovertemplate="₹%{x}<br>%{y} trades<extra></extra>"))
    fig.add_vline(x=0, line_color=ui.MUTED, line_dash="dot")
    ui.tight_layout(fig, height=max(180, 60 + 48 * max(1, len(pivot))))
    fig.update_layout(hovermode="closest", bargap=0.05, showlegend=False)
    fig.update_xaxes(title_text="Net P&L per trade (₹)")
    st.plotly_chart(fig, use_container_width=True)
