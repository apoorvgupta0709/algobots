#!/usr/bin/env python3
"""Create a more visual PDF for the latest 11 AM recommendation report.

The PDF combines:
- latest stored recommendation run from research.signal_runs/research.signals;
- simple score/label/risk-reward graphics;
- summaries from Deep Research markdown reports generated for the run.

Safety: reporting only; no broker/order APIs are called.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import psycopg
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@127.0.0.1:55432/finance_tracker"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TWO = Decimal("0.01")

LABEL_COLORS = {
    "buy_candidate_research": colors.HexColor("#16A34A"),
    "paper_setup": colors.HexColor("#2563EB"),
    "needs_review": colors.HexColor("#F97316"),
    "watch": colors.HexColor("#8B5CF6"),
    "reject": colors.HexColor("#DC2626"),
}


@dataclass
class Signal:
    signal_id: int
    symbol: str
    label: str
    score: Decimal
    technical_score: Decimal
    risk_score: Decimal
    stop_loss: Decimal | None
    target: Decimal | None
    risks: list[str]
    reasons: list[str]
    local_context: dict[str, Any]


def as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO, rounding=ROUND_HALF_UP):,.2f}"


def clean_text(text: str) -> str:
    text = re.sub(r"\[[0-9]+\]", "", text)
    text = text.replace("₹", "Rs. ")
    text = text.replace("INR", "Rs.")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences(text: str, max_sentences: int = 4) -> list[str]:
    text = clean_text(text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if p.strip()][:max_sentences]


def extract_deep_summary(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    topic = re.search(r"^Topic:\s*(.+)$", text, flags=re.M)
    status = re.search(r"^Status:\s*(.+)$", text, flags=re.M)
    usage = re.search(r"^- Usage:\s*(.+)$", text, flags=re.M)
    sources = re.findall(r"^- \d+\. ", text, flags=re.M)

    local_match = re.search(r"## Local FYERS/Postgres facts\n(.*?)(?:\n## |\Z)", text, flags=re.S)
    local_lines = []
    if local_match:
        local_lines = [clean_text(line.lstrip("- ")) for line in local_match.group(1).splitlines() if line.strip()][:3]

    synth_match = re.search(r"## External research synthesis\n(.*?)(?:\n## Sources extracted|\Z)", text, flags=re.S)
    summary_sentences: list[str] = []
    if synth_match:
        synth = synth_match.group(1)
        # Skip headings; use first substantial paragraph from the synthesis.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", synth) if len(p.strip()) > 120 and not p.strip().startswith("#")]
        if paragraphs:
            summary_sentences = split_sentences(paragraphs[0], 5)

    positives: list[str] = []
    risks: list[str] = []
    for sentence in summary_sentences:
        low = sentence.lower()
        if any(k in low for k in ["growth", "profit", "strong", "tailwind", "robust", "net cash", "turnaround", "record"]):
            positives.append(sentence)
        if any(k in low for k in ["risk", "debt", "leverage", "valuation", "concern", "headwind", "loss", "negative"]):
            risks.append(sentence)

    return {
        "topic": topic.group(1).strip() if topic else path.stem,
        "status": status.group(1).strip() if status else "unknown",
        "usage": usage.group(1).strip() if usage else "n/a",
        "source_count": len(sources),
        "local": local_lines,
        "summary": summary_sentences,
        "positives": positives[:3],
        "risks": risks[:3],
        "path": str(path),
    }


def find_deep_reports_for_run(run_report_path: str) -> list[Path]:
    # Current report has references like deep_research_for_SYMBOL_timestamp.md.
    report_text = Path(run_report_path).read_text(encoding="utf-8") if run_report_path else ""
    names = re.findall(r"deep_research_for_[A-Z0-9]+_\d{8}_\d{6}\.md", report_text)
    paths = []
    for name in names:
        p = REPORTS_DIR / name
        if p.exists():
            paths.append(p)
    return paths


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


def load_run(signal_run_id: int | None) -> tuple[dict[str, Any], list[Signal]]:
    with connect_db() as conn:
        with conn.cursor() as cur:
            if signal_run_id is None:
                cur.execute("select signal_run_id from research.signal_runs where status='success' order by signal_run_id desc limit 1")
                row = cur.fetchone()
                if not row:
                    raise SystemExit("No successful signal run found.")
                signal_run_id = int(row[0])
            cur.execute(
                """
                select signal_run_id, generated_at, universe, live_orders_enabled, deep_research_enabled, status, report_path
                from research.signal_runs where signal_run_id=%s
                """,
                (signal_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"Signal run not found: {signal_run_id}")
            run = {
                "signal_run_id": int(row[0]),
                "generated_at": row[1],
                "universe": row[2],
                "live_orders_enabled": row[3],
                "deep_research_enabled": row[4],
                "status": row[5],
                "report_path": row[6],
            }
            cur.execute(
                """
                select signal_id, symbol, label, score, technical_score, risk_score, stop_loss, target,
                       risks, reasons, local_context
                from research.signals
                where signal_run_id=%s
                order by score desc, signal_id asc
                """,
                (signal_run_id,),
            )
            signals = []
            for r in cur.fetchall():
                signals.append(
                    Signal(
                        signal_id=int(r[0]),
                        symbol=str(r[1]),
                        label=str(r[2]),
                        score=Decimal(str(r[3])),
                        technical_score=Decimal(str(r[4])),
                        risk_score=Decimal(str(r[5])),
                        stop_loss=as_decimal(r[6]),
                        target=as_decimal(r[7]),
                        risks=parse_json(r[8], []),
                        reasons=parse_json(r[9], []),
                        local_context=parse_json(r[10], {}),
                    )
                )
            return run, signals


class Pdf:
    def __init__(self, output: Path, title: str):
        pdfmetrics.registerFont(TTFont("DejaVu", FONT_REGULAR))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", FONT_BOLD))
        self.c = canvas.Canvas(str(output), pagesize=A4)
        self.w, self.h = A4
        self.left = 14 * mm
        self.right = 14 * mm
        self.top = 15 * mm
        self.bottom = 15 * mm
        self.y = self.h - self.top
        self.page = 0
        self.title = title
        self.new_page()

    def new_page(self):
        if self.page:
            self.c.showPage()
        self.page += 1
        self.y = self.h - self.top
        self.c.setFillColor(colors.HexColor("#0F172A"))
        self.c.rect(0, self.h - 20 * mm, self.w, 20 * mm, fill=1, stroke=0)
        self.c.setFillColor(colors.white)
        self.c.setFont("DejaVu-Bold", 13)
        self.c.drawString(self.left, self.h - 12 * mm, self.title)
        self.c.setFont("DejaVu", 8)
        self.c.drawRightString(self.w - self.right, self.h - 12 * mm, f"Page {self.page}")
        self.y = self.h - 27 * mm

    def ensure(self, height: float):
        if self.y - height < self.bottom:
            self.new_page()

    def text(self, txt: str, size=8.5, bold=False, color=colors.HexColor("#111827"), leading=4.4 * mm, indent=0, max_chars=105):
        self.c.setFillColor(color)
        self.c.setFont("DejaVu-Bold" if bold else "DejaVu", size)
        wrapped: list[str] = []
        for part in str(txt).splitlines() or [""]:
            if not part:
                wrapped.append("")
            else:
                import textwrap
                wrapped.extend(textwrap.wrap(part, width=max_chars, break_long_words=False) or [""])
        self.ensure(max(leading, len(wrapped) * leading))
        for line in wrapped:
            self.c.drawString(self.left + indent, self.y, line)
            self.y -= leading

    def heading(self, txt: str):
        self.ensure(12 * mm)
        self.y -= 2 * mm
        self.c.setFillColor(colors.HexColor("#1E3A8A"))
        self.c.setFont("DejaVu-Bold", 12)
        self.c.drawString(self.left, self.y, txt)
        self.y -= 6 * mm

    def pill(self, x: float, y: float, text: str, fill, width: float | None = None):
        self.c.setFont("DejaVu-Bold", 7.5)
        width = width or (self.c.stringWidth(text, "DejaVu-Bold", 7.5) + 7 * mm)
        self.c.setFillColor(fill)
        self.c.roundRect(x, y - 4 * mm, width, 6 * mm, 2 * mm, fill=1, stroke=0)
        self.c.setFillColor(colors.white)
        self.c.drawCentredString(x + width / 2, y - 2.6 * mm, text)
        return width

    def finish(self):
        self.c.save()


def draw_bar(pdf: Pdf, x, y, w, h, pct, color, label):
    pdf.c.setFillColor(colors.HexColor("#E5E7EB"))
    pdf.c.roundRect(x, y, w, h, 2, fill=1, stroke=0)
    pdf.c.setFillColor(color)
    pdf.c.roundRect(x, y, w * max(0, min(1, pct)), h, 2, fill=1, stroke=0)
    pdf.c.setFillColor(colors.HexColor("#111827"))
    pdf.c.setFont("DejaVu", 7)
    pdf.c.drawString(x, y + h + 1.5 * mm, label)


def draw_label_distribution(pdf: Pdf, signals: list[Signal]):
    counts = Counter(s.label for s in signals)
    total = sum(counts.values()) or 1
    x = pdf.left
    y = pdf.y
    pdf.ensure(35 * mm)
    pdf.c.setFont("DejaVu-Bold", 9)
    pdf.c.setFillColor(colors.HexColor("#111827"))
    pdf.c.drawString(x, y, "Signal mix")
    y -= 6 * mm
    bar_w = 75 * mm
    for label, count in counts.most_common():
        color = LABEL_COLORS.get(label, colors.grey)
        draw_bar(pdf, x + 32 * mm, y, bar_w, 4 * mm, count / total, color, "")
        pdf.c.setFillColor(color)
        pdf.c.rect(x, y, 4 * mm, 4 * mm, fill=1, stroke=0)
        pdf.c.setFillColor(colors.HexColor("#111827"))
        pdf.c.setFont("DejaVu", 7)
        pdf.c.drawString(x + 6 * mm, y + 0.8 * mm, label.replace("_", " ")[:22])
        pdf.c.drawRightString(x + 32 * mm + bar_w + 9 * mm, y + 0.8 * mm, str(count))
        y -= 6 * mm
    pdf.y = y - 2 * mm


def render_pdf(output: Path, signal_run_id: int | None = None) -> None:
    run, signals = load_run(signal_run_id)
    deep_reports = [extract_deep_summary(p) for p in find_deep_reports_for_run(run.get("report_path"))]
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf = Pdf(output, "11 AM Stock Recommendation — Visual Report")

    generated = run["generated_at"].strftime("%Y-%m-%d %H:%M UTC") if hasattr(run["generated_at"], "strftime") else str(run["generated_at"])
    pdf.c.setFont("DejaVu-Bold", 15)
    pdf.c.setFillColor(colors.HexColor("#0F172A"))
    pdf.c.drawString(pdf.left, pdf.y, "Morning recommendation dashboard")
    pdf.y -= 8 * mm
    pdf.text(f"Generated: {generated} | Run ID: {run['signal_run_id']} | Universe: {run['universe']}", size=8.5)
    pdf.pill(pdf.left, pdf.y, "RESEARCH ONLY", colors.HexColor("#2563EB"))
    pdf.pill(pdf.left + 37 * mm, pdf.y, "NO ORDERS PLACED", colors.HexColor("#DC2626"))
    pdf.pill(pdf.left + 82 * mm, pdf.y, "LIVE EXECUTION OFF", colors.HexColor("#475569"))
    pdf.y -= 10 * mm

    draw_label_distribution(pdf, signals)

    pdf.heading("Ranked candidates — score and risk/reward")
    top = signals[:8]
    for idx, s in enumerate(top, 1):
        pdf.ensure(24 * mm)
        color = LABEL_COLORS.get(s.label, colors.grey)
        y = pdf.y
        pdf.c.setFillColor(colors.HexColor("#F8FAFC"))
        pdf.c.roundRect(pdf.left, y - 19 * mm, pdf.w - pdf.left - pdf.right, 20 * mm, 3 * mm, fill=1, stroke=0)
        pdf.c.setFillColor(color)
        pdf.c.roundRect(pdf.left, y - 19 * mm, 3 * mm, 20 * mm, 1 * mm, fill=1, stroke=0)
        pdf.c.setFillColor(colors.HexColor("#111827"))
        pdf.c.setFont("DejaVu-Bold", 9.5)
        pdf.c.drawString(pdf.left + 6 * mm, y - 4 * mm, f"{idx}. {s.symbol}")
        pdf.pill(pdf.left + 56 * mm, y - 1 * mm, s.label.replace("_", " ").upper(), color, width=42 * mm)
        draw_bar(pdf, pdf.left + 105 * mm, y - 8 * mm, 55 * mm, 4 * mm, float(s.score / Decimal("100")), color, f"Score {s.score}/100")
        ltp = s.local_context.get("ltp", "n/a")
        change = s.local_context.get("change", "n/a")
        trend = s.local_context.get("trend", "n/a")
        rsi = s.local_context.get("rsi_14", "n/a")
        relvol = s.local_context.get("relative_volume_20", "n/a")
        pdf.c.setFont("DejaVu", 7.5)
        pdf.c.setFillColor(colors.HexColor("#334155"))
        pdf.c.drawString(pdf.left + 6 * mm, y - 10 * mm, f"LTP {ltp} | Change {change} | Trend {trend} | RSI {rsi} | RelVol {relvol}")
        pdf.c.drawString(pdf.left + 6 * mm, y - 15 * mm, f"SL {money(s.stop_loss)} | 2R Target {money(s.target)} | Key risk: {(s.risks[0] if s.risks else 'None flagged')[:95]}")
        pdf.y -= 23 * mm

    pdf.heading("Deep Research summaries")
    if not deep_reports:
        pdf.text("No Deep Research markdown report was referenced by this recommendation run.")
    for d in deep_reports:
        pdf.ensure(48 * mm)
        pdf.c.setFillColor(colors.HexColor("#EFF6FF"))
        pdf.c.roundRect(pdf.left, pdf.y - 8 * mm, pdf.w - pdf.left - pdf.right, 9 * mm, 2 * mm, fill=1, stroke=0)
        pdf.c.setFillColor(colors.HexColor("#1E3A8A"))
        pdf.c.setFont("DejaVu-Bold", 10)
        pdf.c.drawString(pdf.left + 3 * mm, pdf.y - 5 * mm, f"{d['topic']} — Status: {d['status']} | Sources: {d['source_count']} | {d['usage'][:70]}")
        pdf.y -= 12 * mm
        if d["local"]:
            pdf.text("Local snapshot:", bold=True, size=8)
            for item in d["local"][:2]:
                pdf.text(f"• {item}", size=7.6, indent=4 * mm, max_chars=100)
        if d["summary"]:
            pdf.text("Research synthesis:", bold=True, size=8)
            for sentence in d["summary"][:4]:
                pdf.text(f"• {sentence}", size=7.6, indent=4 * mm, max_chars=100)
        if d["positives"] or d["risks"]:
            pdf.text("Bull/Bear extraction:", bold=True, size=8)
            for sentence in d["positives"][:2]:
                pdf.text(f"+ {sentence}", size=7.4, indent=4 * mm, color=colors.HexColor("#166534"), max_chars=100)
            for sentence in d["risks"][:2]:
                pdf.text(f"- {sentence}", size=7.4, indent=4 * mm, color=colors.HexColor("#991B1B"), max_chars=100)
        pdf.y -= 3 * mm

    pdf.heading("Way forward — algobot path")
    bullets = [
        "Phase 2A remains paper-only: create/monitor paper trades from buy_candidate_research signals.",
        "Next enhancement: EOD paper P&L report, weekly win-rate/expectancy summary, and rule-compliance dashboard.",
        "Before live orders: require explicit live_order gate, max-loss controls, kill switch, and per-order confirmation.",
    ]
    for b in bullets:
        pdf.text(f"• {b}", size=8.2, indent=3 * mm)
    pdf.text("Disclaimer: This is research context, not investment advice. Verify with your own judgment / qualified professional before material decisions.", size=7.2, color=colors.HexColor("#64748B"))
    pdf.finish()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-run-id", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (REPORTS_DIR / f"morning_stock_recommendations_visual_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.pdf")
    render_pdf(output, args.signal_run_id)
    print(output)


if __name__ == "__main__":
    main()
