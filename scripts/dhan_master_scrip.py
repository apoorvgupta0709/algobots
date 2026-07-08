#!/usr/bin/env python3
"""
DhanHQ Scrip Master Downloader
================================
Downloads and caches the DhanHQ scrip master CSV that maps
exchange symbols to Dhan securityId values.

The CSV is published at:
  https://images.dhan.co/api-data/api-scrip-master-detailed.csv

It is large (~300MB) and changes daily (new contracts added, expired
ones removed).  We cache it locally under data/dhan_scrip_master.csv
and refresh on demand.

Usage:
    python3 scripts/dhan_master_scrip.py                # download/refresh
    python3 scripts/dhan_master_scrip.py --lookup NIFTY  # lookup by symbol
    python3 scripts/dhan_master_scrip.py --sec-id 25    # reverse lookup
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# -- Config -------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "dhan_scrip_master.csv"

CSV_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Dhan CSV column names (verified Jan 2026)
COLS = {
    "exchange": "EXCH_ID",
    "segment": "SEGMENT",
    "security_id": "SECURITY_ID",
    "symbol": "SYMBOL_NAME",
    "underlying_symbol": "UNDERLYING_SYMBOL",
    "instrument": "INSTRUMENT",
    "instrument_type": "INSTRUMENT_TYPE",
    "series": "SERIES",
    "expiry": "SM_EXPIRY_DATE",
    "expiry_flag": "EXPIRY_FLAG",
    "lot_size": "LOT_SIZE",
    "strike": "STRIKE_PRICE",
    "option_type": "OPTION_TYPE",
    "tick_size": "TICK_SIZE",
    "isin": "ISIN",
    "display_name": "DISPLAY_NAME",
}


# -- Download -----------------------------------------------------------------


def download_csv(force: bool = False) -> Path:
    """Download the scrip master CSV if missing or stale. Returns path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Refresh if older than 1 day
    if CSV_PATH.exists() and not force:
        mtime = datetime.fromtimestamp(CSV_PATH.stat().st_mtime)
        age_hours = (datetime.now() - mtime).total_seconds() / 3600
        if age_hours < 24:
            print(f"Scrip master cached ({age_hours:.1f}h old): {CSV_PATH}")
            return CSV_PATH

    print(f"Downloading scrip master from {CSV_URL}...")
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()

    # The CSV may be gzip — Dhan usually serves it plain, but check
    if data[:2] == b"\x1f\x8b":
        import gzip

        data = gzip.decompress(data)

    CSV_PATH.write_bytes(data)
    size_mb = CSV_PATH.stat().st_size / (1024 * 1024)
    print(f"  Saved: {CSV_PATH} ({size_mb:.1f} MB)")
    return CSV_PATH


# -- Lookup -------------------------------------------------------------------


def load_rows(path: Path) -> list[dict]:
    """Load CSV as list of dict rows."""
    rows = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def lookup_symbol(rows: list[dict], symbol: str, exchange: str | None = None) -> list[dict]:
    """Find all contracts matching a trading symbol."""
    results = []
    for r in rows:
        if r.get(COLS["symbol"], "").upper() == symbol.upper():
            if exchange is None or r.get(COLS["exchange"], "").upper() == exchange.upper():
                results.append(r)
    return results


def lookup_sec_id(rows: list[dict], sec_id: int) -> list[dict]:
    """Reverse lookup: find row(s) matching securityId."""
    return [r for r in rows if r.get(COLS["security_id"], "") == str(sec_id)]


# -- CLI ----------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="DhanHQ scrip master downloader")
    ap.add_argument("--force", action="store_true", help="Force re-download even if cached")
    ap.add_argument("--lookup", type=str, help="Lookup symbol (exchange:symbol or symbol)")
    ap.add_argument("--sec-id", type=int, help="Reverse lookup by securityId")
    ap.add_argument("--list-indices", action="store_true", help="Show all index security IDs")
    args = ap.parse_args()

    path = download_csv(force=args.force)

    if args.lookup:
        rows = load_rows(path)
        # Parse exchange:symbol
        if ":" in args.lookup:
            exch, sym = args.lookup.split(":", 1)
            matches = lookup_symbol(rows, sym, exch)
        else:
            matches = lookup_symbol(rows, args.lookup)
        if not matches:
            print(f"No matches for {args.lookup}")
            sys.exit(1)
        for m in matches[:20]:
            print(
                f"  sec_id={m.get(COLS['security_id'], '?'):>10} "
                f"exch={m.get(COLS['exchange'], '?'):10} "
                f"seg={m.get(COLS['segment'], '?'):10} "
                f"inst={m.get(COLS['instrument'], '?'):20} "
                f"symbol={m.get(COLS['symbol'], '?'):20} "
                f"exp={m.get(COLS['expiry'], '?')} "
                f"lot={m.get(COLS['lot_size'], '?')}"
            )
        return

    if args.sec_id:
        rows = load_rows(path)
        matches = lookup_sec_id(rows, args.sec_id)
        if not matches:
            print(f"No match for securityId={args.sec_id}")
            sys.exit(1)
        for m in matches:
            for k, col in COLS.items():
                print(f"  {k:18s}: {m.get(col, '')}")
        return

    if args.list_indices:
        rows = load_rows(path)
        # Filter for IDX instruments
        idx_rows = [r for r in rows if "IDX" in (r.get(COLS["instrument"], "") or "").upper()]
        # Also catch common indices by instruments
        seen = {}
        for r in idx_rows:
            sid = r.get(COLS["security_id"], "")
            if sid and sid not in seen:
                seen[sid] = r
        print(f"Found {len(seen)} unique index security IDs:")
        for sid, r in sorted(seen.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            print(
                f"  sec_id={sid:>10} "
                f"symbol={r.get(COLS['symbol'], '?'):20} "
                f"name={r.get(COLS['display_name'], '?')}"
            )
        return

    # Default: just verify file
    if path.exists():
        size_mb = path.stat().st_size / (1024 * 1024)
        row_count = sum(1 for _ in open(path))
        print(f"OK: {path} ({size_mb:.1f} MB, ~{row_count:,} rows)")


if __name__ == "__main__":
    main()