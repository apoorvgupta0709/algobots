"""Open positions grouped per strategy, with totals and P&L styling."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="AlgoBot · Positions", page_icon="📌", layout="wide")

from algobot.core import clock  # noqa: E402
from algobot.dashboard import ui  # noqa: E402

mode = ui.sidebar()
st.title("Open positions")
ui.kill_banner()

df = ui.q_open_positions(mode)
if df.empty:
    st.info("No open positions" + (f" in {mode} mode." if mode else "."))
    st.stop()

now = clock.now_ist().replace(tzinfo=None)
df = df.assign(age=[ui.age_str(t, now=now) for t in df["opened_at"]])

total_unrl = float(df["unrealized_pnl"].fillna(0).sum())
exposure = float((df["avg_price"] * df["qty"].abs()).sum())
c1, c2, c3 = st.columns(3)
c1.metric("Positions", len(df))
c2.metric("Unrealized P&L", ui.fmt_inr(total_unrl, sign=True),
          delta=ui.fmt_inr(total_unrl, sign=True),
          delta_color="normal" if total_unrl >= 0 else "inverse")
c3.metric("Gross exposure", ui.fmt_inr(exposure))

COLS = ["symbol", "mode", "qty", "avg_price", "last_price", "unrealized_pnl",
        "stop_loss", "take_profit", "product_type", "age"]
COLUMN_CONFIG = {
    "symbol": "Symbol", "mode": "Mode", "qty": "Qty",
    "avg_price": st.column_config.NumberColumn("Avg price", format="%.2f"),
    "last_price": st.column_config.NumberColumn("LTP", format="%.2f"),
    "unrealized_pnl": "Unrealized P&L",
    "stop_loss": st.column_config.NumberColumn("SL", format="%.2f"),
    "take_profit": st.column_config.NumberColumn("TP", format="%.2f"),
    "product_type": "Product", "age": "Age",
}

for sid, group in df.groupby("strategy_id", sort=True):
    sub_unrl = float(group["unrealized_pnl"].fillna(0).sum())
    color = ui.POS if sub_unrl >= 0 else ui.NEG
    st.markdown(
        f"#### {sid} &nbsp; {ui.mode_badge(group['mode'].iloc[0])} &nbsp; "
        f"<span style='color:{color};font-weight:700'>"
        f"{ui.fmt_inr(sub_unrl, sign=True)}</span>",
        unsafe_allow_html=True)
    st.dataframe(ui.pnl_styler(group[COLS], ["unrealized_pnl"]),
                 use_container_width=True, hide_index=True,
                 column_config=COLUMN_CONFIG)
