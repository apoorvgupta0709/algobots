#!/usr/bin/env python3
"""Render a Telegram-friendly Markdown report into a simple downloadable PDF.

This is intentionally dependency-light: it uses ReportLab only at runtime and keeps
finance reports auditable by preserving the source Markdown path in the footer.
"""
from __future__ import annotations

import argparse
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def clean_markdown(line: str) -> tuple[str, str]:
    stripped = line.rstrip("\n")
    if stripped.startswith("## "):
        return "h2", stripped[3:].strip()
    if stripped.startswith("# "):
        return "h1", stripped[2:].strip()
    # Keep bullets readable but strip markdown emphasis.
    stripped = stripped.replace("**", "").replace("__", "")
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
    return "body", stripped


def draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, width_chars: int, font: str, size: int, leading: int) -> float:
    if not text:
        return y - leading
    lines = []
    for raw in text.splitlines():
        indent = len(raw) - len(raw.lstrip(" "))
        prefix = " " * min(indent, 8)
        clean = raw.strip()
        wrap_width = max(24, width_chars - len(prefix))
        wrapped = textwrap.wrap(clean, width=wrap_width, break_long_words=False, replace_whitespace=False) or [""]
        for i, part in enumerate(wrapped):
            lines.append((prefix if i == 0 else prefix + "  ") + part)
    c.setFont(font, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def render_pdf(markdown_path: Path, output_path: Path, title: str | None = None) -> None:
    pdfmetrics.registerFont(TTFont("DejaVu", FONT_REGULAR))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", FONT_BOLD))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    page_width, page_height = A4
    left = 16 * mm
    right = 16 * mm
    top = 18 * mm
    bottom = 16 * mm
    usable_width = page_width - left - right
    width_chars = 94

    source_text = markdown_path.read_text(encoding="utf-8")
    c = canvas.Canvas(str(output_path), pagesize=A4)

    def new_page(page_no: int) -> float:
        c.setFont("DejaVu-Bold", 12)
        c.drawString(left, page_height - 11 * mm, title or "Finance Report")
        c.setFont("DejaVu", 8)
        c.drawRightString(page_width - right, page_height - 11 * mm, f"Page {page_no}")
        c.line(left, page_height - 14 * mm, page_width - right, page_height - 14 * mm)
        c.setFont("DejaVu", 7)
        footer = f"Source: {markdown_path} | Rendered: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        c.drawString(left, bottom - 5 * mm, footer[:150])
        return page_height - top

    page_no = 1
    y = new_page(page_no)
    for raw_line in source_text.splitlines():
        style, text = clean_markdown(raw_line)
        if y < bottom + 16 * mm:
            c.showPage()
            page_no += 1
            y = new_page(page_no)
        if style == "h1":
            y -= 4
            y = draw_wrapped(c, text, left, y, width_chars, "DejaVu-Bold", 15, 18)
            y -= 3
        elif style == "h2":
            y -= 5
            y = draw_wrapped(c, text, left, y, width_chars, "DejaVu-Bold", 13, 16)
            y -= 2
        else:
            y = draw_wrapped(c, text, left, y, width_chars, "DejaVu", 9, 12)
    c.save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", default="11 AM Morning Stock Recommendations")
    args = parser.parse_args()
    render_pdf(args.input, args.output, args.title)
    print(args.output)


if __name__ == "__main__":
    main()
