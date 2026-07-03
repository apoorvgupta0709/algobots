"""AlgoBot dashboard — Overview page.

Run with: ``streamlit run algobot/dashboard/app.py``
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="AlgoBot · Overview", page_icon="📈", layout="wide")

from algobot.core import clock  # noqa: E402
from algobot.dashboard import ui  # noqa: E402

mode = ui.sidebar()

# ------------------------------------------------------------------- header
now = clock.now_ist()
open_now = clock.is_market_open(now)
events = ui.q_events(limit=200)
engine_events = events[events["source"].astype(str).str.contains("engine", case=False)] \
    if not events.empty else events
last_engine_ts = engine_events["ts"].iloc[0] if not engine_events.empty else None
# event_log.ts defaults to utcnow — compare against UTC wall time
heartbeat = ui.age_str(last_engine_ts, now=dt.datetime.utcnow())

c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
with c1:
    st.title("AlgoBot")
with c2:
    st.metric("IST", now.strftime("%H:%M:%S"), now.strftime("%a %d %b %Y"),
              delta_color="off")
with c3:
    color, label = (ui.POS, "MARKET OPEN") if open_now else (ui.MUTED, "MARKET CLOSED")
    st.markdown(
        f"<div style='margin-top:1.4rem'><span style='background:{color};color:#fff;"
        f"padding:4px 12px;border-radius:12px;font-weight:700'>{label}</span></div>",
        unsafe_allow_html=True)
with c4:
    st.metric("Engine heartbeat",
              "no events" if last_engine_ts is None else f"{heartbeat} ago",
              delta_color="off")

ui.kill_banner()

# ------------------------------------------------------------------- tiles
pnl_today = ui.q_todays_pnl()
positions = ui.q_open_positions(mode)
strategies = ui.q_strategies_overview()
week_trades = ui.q_trades(mode=mode, days=None)
if not week_trades.empty:
    wk_start = pd.Timestamp(dt.datetime.combine(clock.week_start(), dt.time.min))
    week_trades = week_trades[pd.to_datetime(week_trades["exit_time"]) >= wk_start]

def _sum_mode(df: pd.DataFrame, m: str) -> float:
    if df.empty:
        return 0.0
    return float(df.loc[df["mode"] == m, "net_pnl"].sum())

live_pnl, paper_pnl = _sum_mode(pnl_today, "live"), _sum_mode(pnl_today, "paper")
week_pnl = float(week_trades["net_pnl"].sum()) if not week_trades.empty else 0.0
active = strategies[strategies["mode"].isin(["paper", "live"]) & strategies["enabled"]] \
    if not strategies.empty else strategies
n_live = int((active["mode"] == "live").sum()) if not active.empty else 0
n_paper = int((active["mode"] == "paper").sum()) if not active.empty else 0
unrl = float(positions["unrealized_pnl"].fillna(0).sum()) if not positions.empty else 0.0

t1, t2, t3, t4, t5 = st.columns(5)
t1.metric("Today · LIVE", ui.fmt_inr(live_pnl, sign=True),
          delta=ui.fmt_inr(live_pnl, sign=True) if live_pnl else None)
t2.metric("Today · PAPER", ui.fmt_inr(paper_pnl, sign=True),
          delta=ui.fmt_inr(paper_pnl, sign=True) if paper_pnl else None)
t3.metric("Week P&L", ui.fmt_inr(week_pnl, sign=True),
          delta=ui.fmt_inr(week_pnl, sign=True) if week_pnl else None)
t4.metric("Open positions", len(positions),
          delta=f"{ui.fmt_inr(unrl, sign=True)} unrealized" if len(positions) else None,
          delta_color="normal" if unrl >= 0 else "inverse")
t5.metric("Active strategies", f"{n_live} live · {n_paper} paper")

st.divider()

# ------------------------------------------------------------ equity + today's bar
left, right = st.columns([3, 2])

with left:
    st.subheader("Total equity")
    eq_mode = st.radio("Equity mode", ["paper", "live"], horizontal=True,
                       label_visibility="collapsed", key="eq_mode")
    eq = ui.q_equity_curves()
    eq = eq[eq["mode"] == eq_mode] if not eq.empty else eq
    if eq.empty:
        st.info(f"No {eq_mode} equity snapshots yet.")
    else:
        pivot = (eq.pivot_table(index="ts", columns="strategy_id",
                                values="equity", aggfunc="last")
                   .sort_index().ffill())
        total = pivot.sum(axis=1)
        fig = go.Figure(go.Scatter(
            x=total.index, y=total.values, mode="lines", name="Total equity",
            line=dict(color=ui.BLUE, width=2),
            hovertemplate="%{x|%d %b %H:%M}<br>₹%{y:,.0f}<extra></extra>"))
        ui.tight_layout(fig, height=340)
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, width="stretch")

with right:
    st.subheader("Today's P&L by strategy")
    dfp = pnl_today if mode is None else pnl_today[pnl_today["mode"] == mode] \
        if not pnl_today.empty else pnl_today
    if dfp.empty:
        st.info("No closed trades today.")
    else:
        agg = dfp.groupby("strategy_id", as_index=False)["net_pnl"].sum() \
                 .sort_values("net_pnl")
        fig = go.Figure(go.Bar(
            x=agg["net_pnl"], y=agg["strategy_id"], orientation="h",
            marker=dict(color=[ui.POS if v >= 0 else ui.NEG for v in agg["net_pnl"]],
                        cornerradius=4),
            text=[ui.fmt_inr(v, sign=True) for v in agg["net_pnl"]],
            textposition="outside", cliponaxis=False,
            hovertemplate="%{y}: ₹%{x:,.0f}<extra></extra>"))
        ui.tight_layout(fig, height=340)
        fig.update_layout(hovermode="closest", showlegend=False)
        st.plotly_chart(fig, width="stretch")

# ------------------------------------------------------------------- recent trades
st.subheader("Recent trades")
recent = ui.q_trades(mode=mode, days=None).head(20)
if recent.empty:
    st.info("No trades recorded yet.")
else:
    show = recent[["exit_time", "strategy_id", "mode", "symbol", "direction",
                   "qty", "entry_price", "exit_price", "net_pnl", "exit_reason"]]
    st.dataframe(
        ui.pnl_styler(show, ["net_pnl"]),
        width="stretch", hide_index=True,
        column_config={
            "exit_time": st.column_config.DatetimeColumn("Exit", format="DD MMM HH:mm"),
            "strategy_id": "Strategy", "mode": "Mode", "symbol": "Symbol",
            "direction": "Dir", "qty": "Qty",
            "entry_price": st.column_config.NumberColumn("Entry", format="%.2f"),
            "exit_price": st.column_config.NumberColumn("Exit px", format="%.2f"),
            "net_pnl": "Net P&L", "exit_reason": "Reason",
        })
