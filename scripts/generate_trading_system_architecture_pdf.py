#!/usr/bin/env python3
"""Generate Apoorv's trading system architecture PDF with embedded Excalidraw-derived diagrams.

The script creates:
- .excalidraw source files for the diagrams
- PNG exports rendered from the same diagram specs
- a PDF architecture document embedding the PNG diagrams

It avoids printing secrets and uses only local DB/file metadata.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer

ROOT = Path("/opt/data/finance-db")
OUT_DIR = ROOT / "reports" / "trading_system_architecture"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PDF_PATH = OUT_DIR / "apoorv_trading_system_architecture.pdf"

# Semantic colors aligned with Excalidraw skill palette.
PALETTE = {
    "input": "#a5d8ff",
    "output": "#b2f2bb",
    "external": "#ffd8a8",
    "process": "#d0bfff",
    "storage": "#c3fae8",
    "warning": "#fff3bf",
    "critical": "#ffc9c9",
    "white": "#ffffff",
    "stroke": "#1e1e1e",
    "muted": "#495057",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return completed.stdout.strip()
    except Exception as exc:  # pragma: no cover - defensive doc generation fallback
        return f"unavailable: {type(exc).__name__}: {exc}"


def psql(query: str) -> str:
    return run([
        "./scripts/psql.sh",
        "-h", "127.0.0.1",
        "-p", "55432",
        "-d", "finance_tracker",
        "-A",
        "-F", "\t",
        "-c", query,
    ], timeout=60)


def list_files(path: Path, pattern: str) -> list[str]:
    return [str(p.relative_to(ROOT)) for p in sorted(path.glob(pattern))]


@dataclass
class Box:
    id: str
    title: str
    subtitle: str
    x: int
    y: int
    w: int
    h: int
    kind: str = "process"
    note: str | None = None


@dataclass
class Arrow:
    id: str
    start: str
    end: str
    label: str = ""
    dashed: bool = False
    color: str = PALETTE["stroke"]


@dataclass
class Diagram:
    name: str
    title: str
    subtitle: str
    width: int
    height: int
    boxes: list[Box] = field(default_factory=list)
    arrows: list[Arrow] = field(default_factory=list)
    notes: list[tuple[int, int, str, str]] = field(default_factory=list)  # x, y, text, color key

    def box(self, box_id: str) -> Box:
        for box in self.boxes:
            if box.id == box_id:
                return box
        raise KeyError(box_id)


def text_element(elem_id: str, x: int, y: int, text: str, font_size: int = 18, width: int | None = None, height: int | None = None, container_id: str | None = None, align: str = "center") -> dict:
    return {
        "type": "text",
        "id": elem_id,
        "x": x,
        "y": y,
        "width": width or max(120, len(max(text.splitlines(), key=len, default="")) * font_size * 0.55),
        "height": height or max(24, (text.count("\n") + 1) * int(font_size * 1.35)),
        "angle": 0,
        "strokeColor": PALETTE["stroke"],
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": abs(hash(elem_id)) % 2147483647,
        "version": 1,
        "versionNonce": abs(hash(elem_id + "nonce")) % 2147483647,
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
        "text": text,
        "fontSize": font_size,
        "fontFamily": 1,
        "textAlign": align,
        "verticalAlign": "middle",
        "containerId": container_id,
        "originalText": text,
        "autoResize": True,
        "lineHeight": 1.25,
    }


def rect_element(box: Box) -> dict:
    return {
        "type": "rectangle",
        "id": box.id,
        "x": box.x,
        "y": box.y,
        "width": box.w,
        "height": box.h,
        "angle": 0,
        "strokeColor": PALETTE["stroke"],
        "backgroundColor": PALETTE[box.kind],
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3},
        "seed": abs(hash(box.id)) % 2147483647,
        "version": 1,
        "versionNonce": abs(hash(box.id + "nonce")) % 2147483647,
        "isDeleted": False,
        "boundElements": [{"id": f"t_{box.id}", "type": "text"}],
        "updated": 1,
        "link": None,
        "locked": False,
    }


def anchor(box: Box, other: Box) -> tuple[int, int, tuple[float, float]]:
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    ocx, ocy = other.x + other.w / 2, other.y + other.h / 2
    dx, dy = ocx - cx, ocy - cy
    if abs(dx) >= abs(dy):
        return (box.x + (box.w if dx > 0 else 0), int(cy), (1 if dx > 0 else 0, 0.5))
    return (int(cx), box.y + (box.h if dy > 0 else 0), (0.5, 1 if dy > 0 else 0))


def arrow_element(diagram: Diagram, arrow: Arrow) -> list[dict]:
    start_box, end_box = diagram.box(arrow.start), diagram.box(arrow.end)
    sx, sy, sfp = anchor(start_box, end_box)
    ex, ey, efp = anchor(end_box, start_box)
    elem = {
        "type": "arrow",
        "id": arrow.id,
        "x": sx,
        "y": sy,
        "width": ex - sx,
        "height": ey - sy,
        "angle": 0,
        "strokeColor": arrow.color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "dashed" if arrow.dashed else "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 2},
        "seed": abs(hash(arrow.id)) % 2147483647,
        "version": 1,
        "versionNonce": abs(hash(arrow.id + "nonce")) % 2147483647,
        "isDeleted": False,
        "boundElements": [{"id": f"t_{arrow.id}", "type": "text"}] if arrow.label else None,
        "updated": 1,
        "link": None,
        "locked": False,
        "points": [[0, 0], [ex - sx, ey - sy]],
        "lastCommittedPoint": None,
        "startBinding": {"elementId": start_box.id, "focus": 0, "gap": 4, "fixedPoint": list(sfp)},
        "endBinding": {"elementId": end_box.id, "focus": 0, "gap": 4, "fixedPoint": list(efp)},
        "startArrowhead": None,
        "endArrowhead": "arrow",
        "elbowed": False,
    }
    out = [elem]
    if arrow.label:
        mx, my = sx + (ex - sx) / 2, sy + (ey - sy) / 2
        out.append(text_element(f"t_{arrow.id}", int(mx - 75), int(my - 22), arrow.label, 14, 150, 28, arrow.id))
    return out


def diagram_to_excalidraw(diagram: Diagram) -> dict:
    elements: list[dict] = []
    # Title annotation inside the canvas.
    elements.append(text_element("title", 30, 20, diagram.title, 28, diagram.width - 60, 42, align="left"))
    elements.append(text_element("subtitle", 32, 62, diagram.subtitle, 16, diagram.width - 60, 28, align="left"))
    # Arrows first so boxes/text sit visually above connections.
    for ar in diagram.arrows:
        elements.extend(arrow_element(diagram, ar))
    for box in diagram.boxes:
        elements.append(rect_element(box))
        label = f"{box.title}\n{box.subtitle}" if box.subtitle else box.title
        elements.append(text_element(f"t_{box.id}", box.x + 8, box.y + 8, label, 17, box.w - 16, box.h - 16, box.id))
    for idx, (x, y, text, color_key) in enumerate(diagram.notes):
        note_id = f"note_{idx}"
        elements.append({**rect_element(Box(note_id, "", "", x, y, 300, 58, color_key)), "boundElements": [{"id": f"t_{note_id}", "type": "text"}]})
        elements.append(text_element(f"t_{note_id}", x + 8, y + 7, text, 15, 284, 44, note_id))
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "hermes-agent finance profile",
        "elements": elements,
        "appState": {"viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        cur = ""
        for word in words:
            trial = (cur + " " + word).strip()
            if draw.textbbox((0, 0), trial, font=fnt)[2] <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def draw_arrow(draw: ImageDraw.ImageDraw, sx: int, sy: int, ex: int, ey: int, fill: str, dashed: bool = False) -> None:
    color = hex_to_rgb(fill)
    if dashed:
        length = math.hypot(ex - sx, ey - sy)
        if length == 0:
            return
        dash, gap = 12, 8
        ux, uy = (ex - sx) / length, (ey - sy) / length
        t = 0
        while t < length:
            t2 = min(length, t + dash)
            draw.line((sx + ux * t, sy + uy * t, sx + ux * t2, sy + uy * t2), fill=color, width=3)
            t += dash + gap
    else:
        draw.line((sx, sy, ex, ey), fill=color, width=3)
    angle = math.atan2(ey - sy, ex - sx)
    size = 13
    left = (ex - size * math.cos(angle - math.pi / 6), ey - size * math.sin(angle - math.pi / 6))
    right = (ex - size * math.cos(angle + math.pi / 6), ey - size * math.sin(angle + math.pi / 6))
    draw.polygon([(ex, ey), left, right], fill=color)


def render_diagram(diagram: Diagram, png_path: Path) -> None:
    img = Image.new("RGB", (diagram.width, diagram.height), "white")
    draw = ImageDraw.Draw(img)
    # subtle grid
    for x in range(0, diagram.width, 40):
        draw.line((x, 0, x, diagram.height), fill=(240, 240, 240), width=1)
    for y in range(0, diagram.height, 40):
        draw.line((0, y, diagram.width, y), fill=(240, 240, 240), width=1)
    draw.text((30, 20), diagram.title, fill=hex_to_rgb(PALETTE["stroke"]), font=font(28, True))
    draw.text((32, 62), diagram.subtitle, fill=hex_to_rgb(PALETTE["muted"]), font=font(16))
    for ar in diagram.arrows:
        sb, eb = diagram.box(ar.start), diagram.box(ar.end)
        sx, sy, _ = anchor(sb, eb)
        ex, ey, _ = anchor(eb, sb)
        draw_arrow(draw, sx, sy, ex, ey, ar.color, ar.dashed)
        if ar.label:
            f = font(13)
            mx, my = (sx + ex) / 2, (sy + ey) / 2
            tw = draw.textbbox((0, 0), ar.label, font=f)[2]
            draw.rectangle((mx - tw / 2 - 6, my - 16, mx + tw / 2 + 6, my + 4), fill=(255, 255, 255), outline=(220, 220, 220))
            draw.text((mx - tw / 2, my - 14), ar.label, fill=hex_to_rgb(PALETTE["stroke"]), font=f)
    for box in diagram.boxes:
        fill = hex_to_rgb(PALETTE[box.kind])
        outline = hex_to_rgb(PALETTE["stroke"])
        draw.rounded_rectangle((box.x, box.y, box.x + box.w, box.y + box.h), radius=14, fill=fill, outline=outline, width=3)
        title_f = font(17, True)
        sub_f = font(13)
        title_lines = wrap_text(draw, box.title, title_f, box.w - 18)
        sub_lines = wrap_text(draw, box.subtitle, sub_f, box.w - 18) if box.subtitle else []
        line_h = 21
        sub_h = 17
        total_h = len(title_lines) * line_h + len(sub_lines) * sub_h
        y = box.y + max(8, (box.h - total_h) / 2)
        for line in title_lines:
            tw = draw.textbbox((0, 0), line, font=title_f)[2]
            draw.text((box.x + (box.w - tw) / 2, y), line, fill=outline, font=title_f)
            y += line_h
        for line in sub_lines:
            tw = draw.textbbox((0, 0), line, font=sub_f)[2]
            draw.text((box.x + (box.w - tw) / 2, y), line, fill=hex_to_rgb(PALETTE["muted"]), font=sub_f)
            y += sub_h
    for x, y, text, color_key in diagram.notes:
        draw.rounded_rectangle((x, y, x + 300, y + 58), radius=12, fill=hex_to_rgb(PALETTE[color_key]), outline=hex_to_rgb(PALETTE["stroke"]), width=2)
        lines = wrap_text(draw, text, font(14), 280)
        yy = y + 8
        for line in lines[:3]:
            draw.text((x + 10, yy), line, fill=hex_to_rgb(PALETTE["stroke"]), font=font(14))
            yy += 16
    img.save(png_path)


def build_diagrams() -> list[tuple[Diagram, Path, Path]]:
    diagrams: list[Diagram] = []
    diagrams.append(Diagram(
        name="01_deployment_runtime",
        title="Deployment and Runtime Architecture",
        subtitle="Hostinger VPS, Docker/Hermes finance profile, rootless PostgreSQL, broker/API integrations",
        width=1500,
        height=950,
        boxes=[
            Box("user", "Apoorv", "Telegram DM control plane", 55, 180, 180, 86, "input"),
            Box("gateway", "Telegram Gateway", "message delivery and cron outputs", 285, 180, 230, 86, "process"),
            Box("hermes", "Hermes Finance Agent", "profile: finance; orchestrates reports, research, paper bot", 590, 150, 300, 120, "process"),
            Box("scripts", "Profile Cron Scripts", "watchdog, watchlist report, 11am recs, paper monitor", 980, 120, 330, 110, "warning"),
            Box("project", "/opt/data/finance-db", "Python/uv project, migrations, tests, reports", 590, 355, 300, 110, "storage"),
            Box("pg", "Rootless PostgreSQL 17", "finance_tracker on 127.0.0.1:55432", 980, 330, 330, 120, "storage"),
            Box("fyers", "FYERS v3", "read-only quotes, candles, account snapshots", 980, 555, 320, 105, "external"),
            Box("sonar", "OpenRouter / Sonar", "cited deep research for F/S context", 590, 580, 300, 105, "external"),
            Box("library", "Trading Library", "/opt/data/trading-library + Obsidian vault", 250, 555, 280, 105, "storage"),
            Box("reports", "Artifacts", "Markdown, PDF, strategy cards, Telegram summaries", 590, 760, 310, 105, "output"),
            Box("gate", "Live Order Gate", "disabled by default; explicit approval required", 990, 755, 320, 105, "critical"),
        ],
        arrows=[
            Arrow("a_user_gateway", "user", "gateway", "messages"),
            Arrow("a_gateway_hermes", "gateway", "hermes", "tasks/results"),
            Arrow("a_hermes_scripts", "hermes", "scripts", "cron/no_agent"),
            Arrow("a_hermes_project", "hermes", "project", "runs code"),
            Arrow("a_project_pg", "project", "pg", "SQL"),
            Arrow("a_project_fyers", "project", "fyers", "read-only API"),
            Arrow("a_project_sonar", "project", "sonar", "external research"),
            Arrow("a_project_library", "project", "library", "strategy docs"),
            Arrow("a_project_reports", "project", "reports", "writes"),
            Arrow("a_reports_gateway", "reports", "gateway", "deliver", dashed=True),
            Arrow("a_pg_gate", "pg", "gate", "approvals/logs", dashed=True, color="#d9480f"),
            Arrow("a_gate_fyers", "gate", "fyers", "NO live calls now", dashed=True, color="#c92a2a"),
        ],
        notes=[(55, 815, "Safety: research/paper-only. No live broker orders from current reporting or paper-bot paths.", "critical")],
    ))

    diagrams.append(Diagram(
        name="02_data_research_flow",
        title="Market Data, Research, Signal, and Backtest Flow",
        subtitle="FYERS/Postgres is source of truth for market facts; Sonar adds cited F/S context",
        width=1550,
        height=1020,
        boxes=[
            Box("watchlist", "Watchlist Universe", "200 NSE instruments", 60, 175, 240, 85, "input"),
            Box("auth", "Token Watchdog", "08:45 IST check; auth link if expired", 60, 335, 240, 85, "warning"),
            Box("quotes", "Quote Ingestion", "ingest_fyers_quotes.py", 385, 150, 250, 80, "process"),
            Box("history", "Candle Ingestion", "ingest_fyers_history.py", 385, 275, 250, 80, "process"),
            Box("snap", "Broker Snapshots", "positions/orderbook/holdings/funds read-only", 385, 400, 250, 90, "process"),
            Box("marketdb", "market schema", "instruments, candles, quotes, ingestion_runs", 720, 210, 285, 110, "storage"),
            Box("tradingdb", "trading schema", "read-only account snapshots, ideas, approvals, log", 720, 390, 285, 110, "storage"),
            Box("factors", "Technical Factors", "SMA, EMA, RSI, MACD, ROC, Donchian, ATR, relvol", 1110, 185, 320, 110, "process"),
            Box("deep", "Deep Research", "OpenRouter/Sonar reports + citations", 1110, 365, 320, 90, "external"),
            Box("evidence", "Structured F/S Evidence", "numeric F + S snapshots for strategies", 1110, 520, 320, 90, "process"),
            Box("signals", "Signal Engine", "11am recommendations; score/label/reasons", 720, 610, 285, 100, "process"),
            Box("backtest", "FTS_SWING_V1 Backtests", "stored runs/trades; reports and strategy card", 1110, 690, 320, 95, "process"),
            Box("paper", "Paper Algobot", "paper trades, stops, targets, weekly risk", 720, 795, 285, 100, "output"),
            Box("reports", "Reports", "daily watchlist, morning recs, backtests, PDFs", 1110, 845, 320, 95, "output"),
        ],
        arrows=[
            Arrow("b1", "watchlist", "quotes", "symbols"),
            Arrow("b2", "watchlist", "history", "symbols"),
            Arrow("b3", "auth", "quotes", "valid token"),
            Arrow("b4", "quotes", "marketdb", "upsert"),
            Arrow("b5", "history", "marketdb", "upsert"),
            Arrow("b6", "snap", "tradingdb", "raw JSONB"),
            Arrow("b7", "marketdb", "factors", "OHLCV"),
            Arrow("b8", "factors", "signals", "technical score"),
            Arrow("b9", "deep", "evidence", "citations"),
            Arrow("b10", "evidence", "signals", "F/S score"),
            Arrow("b11", "signals", "paper", "paper setup"),
            Arrow("b12", "signals", "reports", "Telegram MD"),
            Arrow("b13", "factors", "backtest", "T"),
            Arrow("b14", "evidence", "backtest", "F + S"),
            Arrow("b15", "backtest", "reports", "metrics"),
        ],
        notes=[(60, 840, "Data integrity: raw broker/provider payloads are preserved in JSONB; ingestion is idempotent with upserts.", "storage")],
    ))

    diagrams.append(Diagram(
        name="03_trading_lifecycle_safety",
        title="Trading Lifecycle and Safety Gates",
        subtitle="Signals can create paper trades; live execution remains blocked until explicit confirmed approval",
        width=1500,
        height=920,
        boxes=[
            Box("signal", "Stored Signal", "research.signals; no order permission", 80, 145, 260, 90, "input"),
            Box("risk", "Risk Engine", "capital, max risk/trade, loss lockout", 430, 145, 270, 90, "process"),
            Box("papercreate", "Create Paper Trade", "research.paper_trades only", 790, 145, 270, 90, "output"),
            Box("monitor", "Paper Monitor", "entry/stop/target/trailing/time-stop", 1120, 145, 290, 90, "process"),
            Box("review", "Human Review", "weekly metrics + rule compliance", 80, 370, 260, 90, "warning"),
            Box("idea", "Trade Idea", "optional promotion to trading.trade_ideas", 430, 370, 270, 90, "warning"),
            Box("gate", "Live Gate State", "default live_orders_enabled=false", 790, 370, 270, 90, "critical"),
            Box("approval", "Exact Approval", "symbol, side, qty, order type, max loss, exit plan", 1120, 370, 290, 100, "critical"),
            Box("dryrun", "Execution Log Dry Run", "trading.execution_log; no FYERS order call", 790, 620, 270, 95, "storage"),
            Box("livefuture", "Future Live Adapter", "only if separately approved and coded", 1120, 620, 290, 95, "critical"),
            Box("killswitch", "Kill Switch", "manual disable, max loss, no new signals", 430, 620, 270, 95, "critical"),
        ],
        arrows=[
            Arrow("c1", "signal", "risk", "validate"),
            Arrow("c2", "risk", "papercreate", "paper only"),
            Arrow("c3", "papercreate", "monitor", "state updates"),
            Arrow("c4", "monitor", "review", "P&L/events"),
            Arrow("c5", "review", "idea", "manual promote"),
            Arrow("c6", "idea", "gate", "check"),
            Arrow("c7", "gate", "approval", "requires enabled + exact confirmation", dashed=True, color="#c92a2a"),
            Arrow("c8", "approval", "dryrun", "current behavior", dashed=True),
            Arrow("c9", "approval", "livefuture", "not implemented now", dashed=True, color="#c92a2a"),
            Arrow("c10", "killswitch", "gate", "disable", dashed=True, color="#c92a2a"),
            Arrow("c11", "risk", "killswitch", "breach"),
        ],
        notes=[(80, 680, "No live orders: current scripts intentionally do not call place_order, modify_order, cancel_order, or exit_positions.", "critical")],
    ))

    outputs: list[tuple[Diagram, Path, Path]] = []
    for diagram in diagrams:
        excalidraw_path = OUT_DIR / f"{diagram.name}.excalidraw"
        png_path = OUT_DIR / f"{diagram.name}.png"
        excalidraw_path.write_text(json.dumps(diagram_to_excalidraw(diagram), indent=2), encoding="utf-8")
        render_diagram(diagram, png_path)
        outputs.append((diagram, excalidraw_path, png_path))
    return outputs


class NumberedCanvas:
    def __init__(self, canvas, doc):
        self.canvas = canvas
        self.doc = doc


def add_header_footer(canvas, doc):
    canvas.saveState()
    width, height = doc.pagesize
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#495057"))
    canvas.drawString(0.55 * inch, 0.35 * inch, "Apoorv Trading System Architecture | finance profile | research/paper-only baseline")
    canvas.drawRightString(width - 0.55 * inch, 0.35 * inch, f"Page {doc.page}")
    canvas.restoreState()


def bullet_list(items: Iterable[str], style: ParagraphStyle) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(item, style), bulletColor=colors.HexColor("#1e1e1e")) for item in items], bulletType="bullet", leftIndent=18)


def code_block(text: str, style: ParagraphStyle) -> Preformatted:
    return Preformatted(textwrap.dedent(text).strip(), style)


def split_count_rows(raw: str) -> list[str]:
    rows: list[str] = []
    for line in raw.splitlines()[1:]:
        if not line or line.startswith("("):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append(f"{parts[0]}: {parts[1]}")
    return rows


def build_pdf(diagram_outputs: list[tuple[Diagram, Path, Path]]) -> None:
    generated = now_utc()
    counts_raw = psql("select 'market.instruments' as table_name, count(*) from market.instruments union all select 'market.candles', count(*) from market.candles union all select 'market.quotes', count(*) from market.quotes union all select 'research.factor_snapshots', count(*) from research.factor_snapshots union all select 'research.deep_research_runs', count(*) from research.deep_research_runs union all select 'research.symbol_evidence_snapshots', count(*) from research.symbol_evidence_snapshots union all select 'research.signal_runs', count(*) from research.signal_runs union all select 'research.signals', count(*) from research.signals union all select 'research.backtest_runs', count(*) from research.backtest_runs union all select 'research.backtest_trades', count(*) from research.backtest_trades union all select 'research.paper_trades', count(*) from research.paper_trades union all select 'trading.trade_ideas', count(*) from trading.trade_ideas union all select 'trading.approvals', count(*) from trading.approvals union all select 'trading.execution_log', count(*) from trading.execution_log;")
    schemas_raw = psql("select schema_name from information_schema.schemata where schema_name in ('finance','market','knowledge','research','trading') order by schema_name;")
    tables_raw = psql("select table_schema||'.'||table_name from information_schema.tables where table_schema in ('finance','market','knowledge','research','trading') and table_type='BASE TABLE' order by table_schema, table_name;")
    version_raw = psql("select current_database(), inet_server_addr(), inet_server_port();")
    migrations = list_files(ROOT / "migrations", "*.sql")
    scripts = list_files(ROOT / "scripts", "*.py")

    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.6 * inch,
        title="Apoorv Trading System Architecture",
        author="Hermes Finance Agent",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleCenter", parent=styles["Title"], alignment=TA_CENTER, fontSize=24, leading=30, spaceAfter=10))
    styles.add(ParagraphStyle(name="SubTitle", parent=styles["BodyText"], alignment=TA_CENTER, fontSize=11, leading=15, textColor=colors.HexColor("#495057"), spaceAfter=18))
    styles.add(ParagraphStyle(name="H1x", parent=styles["Heading1"], fontSize=18, leading=22, spaceBefore=12, spaceAfter=8, textColor=colors.HexColor("#1e1e1e")))
    styles.add(ParagraphStyle(name="H2x", parent=styles["Heading2"], fontSize=13, leading=17, spaceBefore=10, spaceAfter=5, textColor=colors.HexColor("#343a40")))
    styles.add(ParagraphStyle(name="Bodyx", parent=styles["BodyText"], fontSize=9.3, leading=12.5, spaceAfter=5))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#495057")))
    styles.add(ParagraphStyle(name="CodeBlock", parent=styles["Code"], fontName="Courier", fontSize=7.2, leading=9, backColor=colors.HexColor("#f8f9fa"), borderColor=colors.HexColor("#dee2e6"), borderWidth=0.5, borderPadding=5, spaceBefore=4, spaceAfter=6))

    story: list = []
    story.append(Paragraph("Apoorv Trading System Architecture", styles["TitleCenter"]))
    story.append(Paragraph(f"Generated {generated} from the live finance profile. This document describes the current research/paper-only trading system, setup steps, operational flows, database layout, cron jobs, and safety gates.", styles["SubTitle"]))
    story.append(Paragraph("Executive summary", styles["H1x"]))
    story.append(bullet_list([
        "The system runs inside the Hermes finance profile on the Hostinger VPS/Docker environment and uses /opt/data/finance-db as the trading-system project root.",
        "Local FYERS/PostgreSQL data is the market-data source of truth: quotes, OHLCV candles, technical factors, read-only account snapshots, signals, backtests, and paper trades are stored auditable in PostgreSQL.",
        "OpenRouter/Perplexity Sonar is used only for cited external fundamental/sentiment research. It does not replace FYERS/Postgres market facts.",
        "The current execution posture is research/paper-only. Reporting, recommendations, backtests, and the paper algobot do not place, modify, cancel, or exit live FYERS orders.",
        "Any later live execution requires a separate live-order adapter plus explicit approval scope: symbol, side, quantity, order type, price/trigger, product, account, max loss, and exit plan.",
    ], styles["Bodyx"]))

    story.append(Paragraph("Current verified runtime setup", styles["H1x"]))
    story.append(bullet_list([
        "Project root: /opt/data/finance-db",
        "Hermes profile scripts: /opt/data/profiles/finance/scripts",
        "Database: finance_tracker on 127.0.0.1:55432 using local rootless PostgreSQL 17",
        "Trading library: /opt/data/trading-library with Obsidian-style vault and strategy research artifacts",
        "Reports directory: /opt/data/finance-db/reports",
        "Environment variables are stored in .env; this document intentionally lists key names only and never includes secret values.",
    ], styles["Bodyx"]))
    story.append(code_block(version_raw, styles["CodeBlock"]))

    story.append(Paragraph("Diagram 1 — Deployment and runtime architecture", styles["H1x"]))
    story.append(RLImage(str(diagram_outputs[0][2]), width=7.5 * inch, height=4.75 * inch))
    story.append(Paragraph(f"Excalidraw source: {diagram_outputs[0][1]}", styles["Small"]))
    story.append(PageBreak())

    story.append(Paragraph("Diagram 2 — Data, research, signal, and backtest flow", styles["H1x"]))
    story.append(RLImage(str(diagram_outputs[1][2]), width=7.55 * inch, height=4.98 * inch))
    story.append(Paragraph(f"Excalidraw source: {diagram_outputs[1][1]}", styles["Small"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Diagram 3 — Trading lifecycle and safety gates", styles["H1x"]))
    story.append(RLImage(str(diagram_outputs[2][2]), width=7.55 * inch, height=4.63 * inch))
    story.append(Paragraph(f"Excalidraw source: {diagram_outputs[2][1]}", styles["Small"]))
    story.append(PageBreak())

    story.append(Paragraph("Logical architecture", styles["H1x"]))
    layers = [
        ("Control plane", "Telegram DM + Hermes finance profile receive instructions, run tools, schedule jobs, and deliver reports."),
        ("Broker/data ingress", "FYERS v3 auth helper, token watchdog, quote ingestion, historical candle ingestion, and read-only trading snapshots."),
        ("Storage", "Rootless PostgreSQL schemas: finance, market, knowledge, research, and trading. Raw payloads are kept in JSONB for auditability."),
        ("Research engine", "Technical factor computation, deep-research context, F/S evidence structuring, strategy hypotheses, and backtest runners."),
        ("Decision layer", "Morning recommendations and FTS_SWING_V1 scoring combine technical, fundamental, sentiment, risk, and freshness evidence."),
        ("Paper trading", "Paper algobot creates and monitors paper trades with entries, stops, targets, trailing rules, expiry, time stops, and weekly risk lockout."),
        ("Safety/execution boundary", "Live order gate exists as a scaffold/dry-run audit layer. Actual FYERS live-order adapter is intentionally absent."),
        ("Artifacts", "Markdown reports, strategy cards, PDF reports, Telegram summaries, and Excalidraw/PDF architecture docs."),
    ]
    for title, desc in layers:
        story.append(Paragraph(f"<b>{title}</b>: {desc}", styles["Bodyx"]))

    story.append(Paragraph("Database schemas and current table inventory", styles["H1x"]))
    schema_lines = [line for line in schemas_raw.splitlines() if line and not line.startswith("schema_name") and not line.startswith("(")]
    story.append(Paragraph("Active schemas: " + ", ".join(schema_lines), styles["Bodyx"]))
    table_lines = [line for line in tables_raw.splitlines() if line and not line.startswith("?") and not line.startswith("(")]
    story.append(bullet_list(table_lines, styles["Small"]))
    story.append(Paragraph("Selected current row counts", styles["H2x"]))
    story.append(bullet_list(split_count_rows(counts_raw), styles["Small"]))

    story.append(Paragraph("Core scripts", styles["H1x"]))
    story.append(bullet_list(scripts, styles["Small"]))
    story.append(Paragraph("Migrations", styles["H2x"]))
    story.append(bullet_list(migrations, styles["Small"]))

    story.append(Paragraph("Scheduled jobs", styles["H1x"]))
    story.append(bullet_list([
        "FYERS token watchdog: weekdays 08:45 IST; silent if valid; sends auth link if token/API check fails.",
        "11am IST morning stock recommendations: weekdays 11:00 IST / 05:30 UTC; read-only recommendation report plus paper-trade handoff.",
        "Weekday watchlist market report: 10:15 UTC; reads stored history, refreshes quotes, computes factors, produces daily market report; soft-fails to stale-data report if FYERS has issues.",
        "Paper algobot monitor Phase 2A: every 30 minutes on weekdays; monitors paper trades and stays quiet when no meaningful change occurs.",
    ], styles["Bodyx"]))

    story.append(PageBreak())
    story.append(Paragraph("Full setup / rebuild guide", styles["H1x"]))
    story.append(Paragraph("Use this as the reference sequence to recreate or audit the system. Commands intentionally avoid printing secrets.", styles["Bodyx"]))
    story.append(Paragraph("1. Prepare project and Python environment", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
uv sync
uv run python -m compileall scripts tests
""", styles["CodeBlock"]))
    story.append(Paragraph("2. Start/verify rootless PostgreSQL", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
./scripts/pg_isready.sh  # if present in your local helper set, or use pg_isready with PGBIN/LD_LIBRARY_PATH
./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atc 'select version();'
""", styles["CodeBlock"]))
    story.append(Paragraph("3. Configure .env key names", styles["H2x"]))
    story.append(bullet_list([
        "DATABASE_URL",
        "FYERS_CLIENT_ID",
        "FYERS_SECRET_KEY",
        "FYERS_REDIRECT_URI",
        "FYERS_ACCESS_TOKEN",
        "OPENROUTER_API_KEY for Sonar deep research",
    ], styles["Bodyx"]))
    story.append(Paragraph("4. Refresh FYERS token safely", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
FYERS_LOG_PATH=/tmp/ uv run python scripts/fyers_auth.py auth-url
# Open the URL outside Telegram. After login, paste only auth_code/code or the full 127.0.0.1 redirect URL into stdin:
FYERS_LOG_PATH=/tmp/ uv run python scripts/fyers_auth.py token --auth-code-stdin --write-env
""", styles["CodeBlock"]))
    story.append(Paragraph("5. Apply migrations", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
for f in migrations/*.sql; do ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -f "$f"; done
""", styles["CodeBlock"]))
    story.append(Paragraph("6. Ingest and compute read-only market data", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_quotes.py --symbols NSE:SBIN-EQ
FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_history.py --symbols NSE:SBIN-EQ --resolution D --range-from 2025-01-01 --range-to 2026-06-07
uv run python scripts/compute_technical_factors.py --symbols NSE:SBIN-EQ --resolution D
""", styles["CodeBlock"]))
    story.append(Paragraph("7. Generate reports and recommendations", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
uv run python scripts/run_watchlist_daily_report.py --skip-history --limit 25 --print
uv run python scripts/run_morning_stock_recommendations.py --limit 25 --deep-research-top 1 --print
uv run python scripts/run_fts_swing_backtest.py --strategy FTS_SWING_V1 --print
""", styles["CodeBlock"]))
    story.append(Paragraph("8. Verify safety and tests", styles["H2x"]))
    story.append(code_block("""
cd /opt/data/finance-db
uv run pytest tests -q
python - <<'PY'
from pathlib import Path
forbidden = ['place_order', 'modify_order', 'cancel_order', 'exit_positions']
for f in Path('scripts').glob('*.py'):
    txt = f.read_text(errors='ignore')
    for name in forbidden:
        needle = name + '('
        if needle in txt:
            print('FORBIDDEN', name, f)
PY
""", styles["CodeBlock"]))

    story.append(PageBreak())
    story.append(Paragraph("Safety model", styles["H1x"]))
    story.append(bullet_list([
        "No live order placement is allowed from current reporting, research, backtest, or paper-trading paths.",
        "Read-only FYERS endpoints are allowed for market data and account snapshots; raw responses are stored for audit.",
        "Signals are not permission to trade. They must include evidence, entry condition, stop/invalidation, target, risk, and freshness status.",
        "Paper trades simulate state transitions only. They can create alerts and review records but cannot call a broker order endpoint.",
        "Live order gate default is disabled. A later live adapter must require exact confirmation and must write a complete approval/execution audit trail.",
        "Secrets remain in .env. Architecture docs, logs, reports, and Telegram messages should not expose tokens, OTPs, account numbers, or full identifiers.",
    ], styles["Bodyx"]))

    story.append(Paragraph("Operational runbooks", styles["H1x"]))
    story.append(Paragraph("FYERS token expired", styles["H2x"]))
    story.append(bullet_list([
        "The watchdog sends a fresh auth link. Open outside Telegram, complete login, and paste the auth_code/code or full final redirect URL back to Hermes.",
        "Hermes exchanges the code with --auth-code-stdin --write-env, then verifies with a read-only quote pull.",
        "The process is semi-automatic because the available FYERS SDK flow requires interactive login/auth-code generation and does not expose a reusable refresh-token method here.",
    ], styles["Bodyx"]))
    story.append(Paragraph("Daily report failed", styles["H2x"]))
    story.append(bullet_list([
        "Run /opt/data/profiles/finance/scripts/daily_watchlist_report.sh manually.",
        "If FYERS fails, the wrapper should generate a stale-data report rather than failing silently.",
        "Check data freshness lines in the report before using any setup as a trading idea.",
    ], styles["Bodyx"]))
    story.append(Paragraph("Paper bot alert", styles["H2x"]))
    story.append(bullet_list([
        "Treat as paper-only. Review entry/stop/target, reason, risk, and whether weekly loss lockout is active.",
        "Do not place a live order from the alert unless a separate exact live-order confirmation flow is explicitly invoked.",
    ], styles["Bodyx"]))

    story.append(Paragraph("Near-term roadmap", styles["H1x"]))
    story.append(bullet_list([
        "Populate more symbol_evidence_snapshots so FTS_SWING_V1 backtests and daily signals are more evidence-backed and less placeholder-driven.",
        "Wire run_fts_swing_backtest.py fully to structured F/S snapshots for all candidates where evidence exists.",
        "Add sector/index context and liquidity/slippage fields to signal scoring.",
        "Create weekly paper-performance review: expectancy, drawdown, rule compliance, false positives, and missed setups.",
        "Only after several weeks of paper validation, design a separately approved live-order adapter with hard capital/risk limits and kill switch.",
    ], styles["Bodyx"]))

    story.append(Paragraph("Generated source files", styles["H1x"]))
    story.append(bullet_list([str(p.relative_to(ROOT)) for _, p, _ in diagram_outputs] + [str(PDF_PATH.relative_to(ROOT))], styles["Small"]))

    doc.build(story, onFirstPage=add_header_footer, onLaterPages=add_header_footer)


def main() -> None:
    diagram_outputs = build_diagrams()
    build_pdf(diagram_outputs)
    print(f"PDF: {PDF_PATH}")
    for _, excalidraw_path, png_path in diagram_outputs:
        print(f"EXCALIDRAW: {excalidraw_path}")
        print(f"PNG: {png_path}")


if __name__ == "__main__":
    main()
