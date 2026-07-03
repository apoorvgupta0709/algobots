"""Risk console: cap gauges, kill switch control, event log tail."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="AlgoBot · Risk", page_icon="🛡️", layout="wide")

from algobot.dashboard import api_client, ui  # noqa: E402

ui.sidebar()
st.title("Risk")
ui.kill_banner()

risk = ui.q_risk_today()


def _gauge(title: str, value: float, lo: float, hi: float, danger_from: float,
           prefix: str = "", suffix: str = "") -> go.Figure:
    """Compact gauge: recessive track, status-red danger band, thin value bar."""
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        number=dict(prefix=prefix, suffix=suffix,
                    valueformat=",.0f", font=dict(size=30)),
        title=dict(text=title, font=dict(size=14, color=ui.MUTED)),
        gauge=dict(
            axis=dict(range=[lo, hi], tickcolor=ui.MUTED,
                      tickfont=dict(size=10, color=ui.MUTED)),
            bar=dict(color=ui.NEG if value <= danger_from else ui.BLUE,
                     thickness=0.55),
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            steps=[dict(range=[lo, danger_from], color="rgba(208,59,59,0.18)")]
            if danger_from > lo else [],
            threshold=dict(line=dict(color=ui.NEG, width=2),
                           thickness=0.8, value=danger_from),
        )))
    fig.update_layout(margin=dict(l=25, r=25, t=40, b=10), height=210,
                      paper_bgcolor="rgba(0,0,0,0)")
    return fig


day_cap = risk["daily_loss_cap"] or 1.0
week_cap = risk["weekly_loss_cap"] or 1.0
max_pos = max(risk["max_concurrent_positions"], 1)
max_trd = max(risk["max_trades_per_day"], 1)

g1, g2, g3, g4 = st.columns(4)
with g1:
    st.plotly_chart(_gauge("Day P&L vs cap", risk["realized_day_pnl"],
                           -day_cap, day_cap, -day_cap * 0.999, prefix="₹"),
                    use_container_width=True)
    st.caption(f"Daily loss cap {ui.fmt_inr(day_cap)}")
with g2:
    st.plotly_chart(_gauge("Week P&L vs cap", risk["realized_week_pnl"],
                           -week_cap, week_cap, -week_cap * 0.999, prefix="₹"),
                    use_container_width=True)
    st.caption(f"Weekly loss cap {ui.fmt_inr(week_cap)}")
with g3:
    fig = _gauge("Open positions", risk["open_position_count"], 0, max_pos, 0)
    fig.update_traces(gauge_bar_color=ui.NEG
                      if risk["open_position_count"] >= max_pos else ui.BLUE)
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Max concurrent {max_pos}")
with g4:
    fig = _gauge("Trades today", risk["trades_today"], 0, max_trd, 0)
    fig.update_traces(gauge_bar_color=ui.NEG
                      if risk["trades_today"] >= max_trd else ui.BLUE)
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Max per day {max_trd}")

st.divider()

# ------------------------------------------------------------------ kill switch
st.subheader("Kill switch")
engaged = bool(risk["kill_switch"])
if engaged:
    st.warning(f"Kill switch is ON — reason: {risk['kill_reason'] or 'n/a'}")
else:
    st.caption("Kill switch is off. Engaging it flattens/halts all trading "
               "until manually released.")

reason = st.text_input(
    "Reason (required)", key="kill_reason_input",
    placeholder="e.g. broker connectivity flapping / manual halt for review")
c1, c2 = st.columns(2)
with c1:
    if st.button("ENGAGE KILL SWITCH", type="primary", disabled=engaged or not reason.strip()):
        ui.show_api_result(*api_client.killswitch(True, reason.strip()))
with c2:
    if st.button("Release kill switch", disabled=not engaged or not reason.strip()):
        ui.show_api_result(*api_client.killswitch(False, reason.strip()))
if not reason.strip():
    st.caption("Type a reason above to enable the kill-switch buttons.")

st.divider()

# -------------------------------------------------------------------- event log
st.subheader("Event log")
events = ui.q_events(limit=200)
if events.empty:
    st.info("No events logged yet.")
else:
    levels = st.multiselect("Level", sorted(events["level"].unique()))
    show = events[events["level"].isin(levels)] if levels else events
    st.dataframe(
        show[["ts", "level", "source", "message"]],
        use_container_width=True, hide_index=True, height=380,
        column_config={
            "ts": st.column_config.DatetimeColumn("Time (UTC)", format="DD MMM HH:mm:ss"),
            "level": "Level", "source": "Source", "message": "Message",
        })
