"""Paper-to-live gate board with promote/demote controls (via the API)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AlgoBot · Gates", page_icon="🚦", layout="wide")

from algobot.core.config import gate_config  # noqa: E402
from algobot.dashboard import api_client, ui  # noqa: E402

ui.sidebar()
st.title("Promotion gates")
ui.kill_banner()

cfg = gate_config()

top_l, top_r = st.columns([3, 1])
with top_r:
    if st.button("Re-evaluate gates", use_container_width=True):
        ui.show_api_result(*api_client.evaluate_gates())
with top_l:
    st.caption(
        f"Requirements: ≥{cfg['min_paper_trades']} paper trades · "
        f"PF ≥ {cfg['min_profit_factor']} · max DD ≤ {cfg['max_drawdown_pct']}% · "
        f"stop-fire fidelity ≤ {cfg['stop_fire_tolerance_pct']}% · "
        f"≥{cfg['min_oos_backtest_months']} OOS backtest months")

gates = ui.q_gate_details()
if gates.empty:
    st.info("No gate evaluations yet — run the gate evaluator or press "
            "'Re-evaluate gates'.")
    st.stop()


def _check(ok: bool) -> str:
    return "✅" if ok else "❌"


for _, g in gates.iterrows():
    sid = g["strategy_id"]
    with st.container(border=True):
        h1, h2, h3 = st.columns([3, 1, 2])
        with h1:
            st.markdown(f"### {sid} &nbsp; {ui.mode_badge(g['mode'])}",
                        unsafe_allow_html=True)
            st.caption(f"Evaluated {ui.fmt_ist(g['evaluated_at'])}"
                       + (f" · promoted {ui.fmt_ist(g['promoted_at'])}"
                          f" by {g['promoted_by']}" if pd.notna(g["promoted_at"]) else ""))
        with h2:
            st.markdown(f"## {_check(bool(g['eligible']))}")
            st.caption("eligible" if g["eligible"] else "not eligible")

        pf = g["profit_factor"]
        dd = g["max_drawdown_pct"]
        fid = g["stop_fire_fidelity_pct"]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Paper trades",
                  f"{int(g['paper_trades_count'])} / {cfg['min_paper_trades']}",
                  delta=_check(g["paper_trades_count"] >= cfg["min_paper_trades"]),
                  delta_color="off")
        c2.metric("Profit factor",
                  "—" if pd.isna(pf) else f"{pf:.2f} / {cfg['min_profit_factor']}",
                  delta=_check(pd.notna(pf) and pf >= cfg["min_profit_factor"]),
                  delta_color="off")
        c3.metric("Max drawdown",
                  "—" if pd.isna(dd) else f"{dd:.1f}% / {cfg['max_drawdown_pct']}%",
                  delta=_check(pd.notna(dd) and dd <= cfg["max_drawdown_pct"]),
                  delta_color="off")
        c4.metric("Stop-fire fidelity",
                  "—" if pd.isna(fid) else f"{fid:.2f}% / {cfg['stop_fire_tolerance_pct']}%",
                  delta=_check(pd.notna(fid) and fid <= cfg["stop_fire_tolerance_pct"]),
                  delta_color="off")
        c5.metric("OOS backtest",
                  f"{g['oos_backtest_months']:.1f}m / {cfg['min_oos_backtest_months']}m",
                  delta=_check(g["oos_backtest_months"] >= cfg["min_oos_backtest_months"]),
                  delta_color="off")

        with st.expander("Gate detail"):
            st.json(g["detail_json"] or {})

        a1, a2 = st.columns([3, 1])
        with a1:
            if g["mode"] != "live":
                ack = st.checkbox(
                    "I understand this trades real money",
                    key=f"ack_{sid}")
                if st.button("Promote to LIVE", key=f"promote_{sid}",
                             type="primary", disabled=not ack):
                    ui.show_api_result(*api_client.promote(sid, "live"))
        with a2:
            if g["mode"] == "live":
                if st.button("Demote to paper", key=f"demote_{sid}"):
                    ui.show_api_result(*api_client.demote(sid, "paper"))
