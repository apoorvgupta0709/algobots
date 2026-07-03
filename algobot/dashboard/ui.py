"""Shared Streamlit helpers: cached queries, INR formatting, palette, sidebar.

Pages import this module; all DB access goes through the ``st.cache_data``
wrappers below (ttl=10s) so a Refresh button + short TTL keep the dashboard
fresh without hammering the DB.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from algobot.core import clock
from algobot.dashboard import data_access as da

# ------------------------------------------------------------------- palette
# Validated data-viz palette (categorical slot 1 = blue; status colors are
# reserved for polarity/state and always paired with a label, never color-alone).
BLUE = "#2a78d6"       # primary series
AQUA = "#1baf7a"       # secondary series
VIOLET = "#4a3aa7"
ORANGE = "#eb6834"
POS = "#0ca30c"        # profit  (status: good)
NEG = "#d03b3b"        # loss    (status: critical)
WARN = "#fab219"       # status: warning
MUTED = "#898781"
GRID = "#e1e0d9"
NEUTRAL_MID = "#f0efec"

SERIES = [BLUE, AQUA, "#eda100", "#008300", VIOLET, "#e34948", "#e87ba4", ORANGE]

MODE_BADGES = {
    "live": ("LIVE", "#d03b3b"),
    "paper": ("PAPER", "#2a78d6"),
    "backtest": ("BACKTEST", "#eda100"),
    "off": ("OFF", "#898781"),
}


# ------------------------------------------------------------------ formatting

def fmt_inr(value: float | None, decimals: int = 0, sign: bool = False) -> str:
    """Indian-grouped rupee string: 1234567.8 -> '₹12,34,568'."""
    if value is None or pd.isna(value):
        return "—"
    neg = value < 0
    prefix = "-" if neg else ("+" if sign else "")
    q = round(abs(float(value)), decimals)
    whole = int(q)
    frac = f"{q - whole:.{decimals}f}"[2:] if decimals else ""
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{prefix}₹{s}" + (f".{frac}" if frac else "")


def fmt_ist(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    return pd.Timestamp(ts).strftime("%d %b %H:%M")


def mode_badge(mode: str) -> str:
    label, color = MODE_BADGES.get(str(mode).lower(), (str(mode).upper(), MUTED))
    return (f"<span style='background:{color};color:#fff;padding:2px 8px;"
            f"border-radius:10px;font-size:0.72rem;font-weight:600;"
            f"letter-spacing:.04em'>{label}</span>")


def pnl_styler(df: pd.DataFrame, cols: list[str]):
    """Red/green text on P&L columns; returns a pandas Styler."""
    def _color(v):
        if pd.isna(v):
            return ""
        return f"color: {POS if v > 0 else (NEG if v < 0 else MUTED)}; font-weight: 600"
    styler = df.style.map(_color, subset=[c for c in cols if c in df.columns])
    inr_cols = {c: fmt_inr for c in cols if c in df.columns}
    return styler.format(inr_cols, na_rep="—")


def tight_layout(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        margin=dict(l=10, r=10, t=30, b=10), height=height,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=MUTED)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=MUTED, linecolor=MUTED)
    return fig


# --------------------------------------------------------------- cached reads

@st.cache_data(ttl=10, show_spinner=False)
def q_open_positions(mode: str | None = None) -> pd.DataFrame:
    return da.open_positions(mode)


@st.cache_data(ttl=10, show_spinner=False)
def q_trades(mode: str | None = None, strategy_id: str | None = None,
             days: int | None = 30) -> pd.DataFrame:
    return da.trades(mode=mode, strategy_id=strategy_id, days=days)


@st.cache_data(ttl=10, show_spinner=False)
def q_todays_pnl() -> pd.DataFrame:
    return da.todays_pnl_by_strategy()


@st.cache_data(ttl=10, show_spinner=False)
def q_equity_curves(strategy_id: str | None = None, points: int = 500) -> pd.DataFrame:
    return da.equity_curves(strategy_id=strategy_id, points=points)


@st.cache_data(ttl=10, show_spinner=False)
def q_strategies_overview() -> pd.DataFrame:
    return da.strategies_overview()


@st.cache_data(ttl=10, show_spinner=False)
def q_gate_details() -> pd.DataFrame:
    return da.gate_details()


@st.cache_data(ttl=10, show_spinner=False)
def q_risk_today() -> dict:
    return da.risk_today()


@st.cache_data(ttl=10, show_spinner=False)
def q_backtest_runs(strategy_id: str | None = None) -> pd.DataFrame:
    return da.backtest_runs(strategy_id)


@st.cache_data(ttl=10, show_spinner=False)
def q_events(limit: int = 200) -> pd.DataFrame:
    return da.events(limit)


# -------------------------------------------------------------------- chrome

def sidebar() -> str | None:
    """Common sidebar: mode filter (session_state) + Refresh. Returns the
    selected mode or None for 'all'."""
    st.sidebar.selectbox(
        "Mode filter", ["all", "paper", "live"], key="mode_filter",
        help="Applies to positions / trades / P&L views.")
    if st.sidebar.button("Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Data auto-refreshes every 10 s · IST "
                       f"{clock.now_ist().strftime('%H:%M:%S')}")
    mode = st.session_state.get("mode_filter", "all")
    return None if mode == "all" else mode


def kill_banner() -> None:
    """Red banner on every page while the kill switch is engaged."""
    risk = q_risk_today()
    if risk.get("kill_switch"):
        reason = risk.get("kill_reason") or "no reason recorded"
        st.error(f"KILL SWITCH ENGAGED — all trading halted. Reason: {reason}",
                 icon="🛑")


def show_api_result(ok: bool, payload: dict) -> None:
    if ok:
        st.success(payload.get("message") or payload.get("detail") or "Done.")
        st.cache_data.clear()
    else:
        st.error(payload.get("error") or payload.get("detail") or "API call failed.")


def age_str(ts, now: dt.datetime | None = None) -> str:
    """Human age of a naive timestamp vs IST wall-clock now."""
    if ts is None or pd.isna(ts):
        return "—"
    now = now or clock.now_ist().replace(tzinfo=None)
    secs = max(0, (now - pd.Timestamp(ts).to_pydatetime()).total_seconds())
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{int(secs // 86400)}d"
