#!/usr/bin/env python3
"""
DhanHQ Historical Options Data Fetcher
========================================
Fetches minute-level rolling option data (expired contracts) via DhanHQ API
and stores it in the local database for backtesting from 2020 onwards.

Endpoint: POST /charts/rollingoption
Returns:  OHLC + IV + Volume + OI + Spot for CALL and PUT at 1/5/15/25/60 min intervals.

The API returns ALL data for the given date range, with strikes that follow
the ATM price as it moves. One call per option-type per month is sufficient.

Usage:
  # Test connectivity
  python3 scripts/dhan_option_data.py --mode test

  # Backfill BankNifty for 2024
  python3 scripts/dhan_option_data.py --mode backfill --underlying BANKNIFTY \
      --from 2024-01-01 --to 2024-12-31 --interval 5

  # Backfill Nifty for Jan-Jun 2024
  python3 scripts/dhan_option_data.py --mode backfill --underlying NIFTY \
      --from 2024-01-01 --to 2024-06-30

Security IDs (from Dhan scrip master):
  Nifty 50       = 13  (SYMBOL=NIFTY)
  BankNifty      = 25  (SYMBOL=BANKNIFTY)
  Finnifty       = 27  (SYMBOL=FINNIFTY)

Dhan rate limit: be conservative - 1 request per 250ms (max 4/sec).
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

import psycopg

# -- Constants ------------------------------------------------------------------
API_BASE = "https://api.dhan.co/v2"
UNDERLYING_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27}
EXCHANGE_SEGMENT = "NSE_FNO"
INSTRUMENT_OPTIDX = "OPTIDX"
VALID_INTERVALS = ["1", "5", "15", "25", "60"]
# The special chars in the actual API values cause encoding issues in file writes.
# We build them at runtime in fetch_rolling_option().
REQUIRED_DATA = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
REQUEST_INTERVAL = 0.25
# Token expires 2026-07-08 16:39 UTC
TOKEN_EXPIRY_TS = 1783528770


def get_token() -> str:
    """Read Dhan API token from environment or .env file."""
    token = os.environ.get("DHAN_API_TOKEN")
    if not token:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DHAN_API_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        break
    if not token:
        print("ERROR: DHAN_API_TOKEN not found", file=sys.stderr)
        sys.exit(1)
    return token


def check_token_expiry() -> None:
    """Warn if token is close to expiry."""
    now = time.time()
    remaining = TOKEN_EXPIRY_TS - now
    if remaining < 0:
        print("WARNING: Dhan API token has EXPIRED.", file=sys.stderr)
    elif remaining < 3600:
        print(f"WARNING: Dhan API token expires in {remaining/60:.0f} minutes.", file=sys.stderr)
    elif remaining < 86400:
        print(f"INFO: Dhan API token expires in {remaining/3600:.1f} hours.", file=sys.stderr)


def make_db_conn() -> psycopg.Connection:
    """Create database connection."""
    url = os.environ.get("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")
    return psycopg.connect(url)


def ensure_table(conn: psycopg.Connection) -> None:
    """Create the options candle table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market.dhan_option_candles (
                security_id      INTEGER NOT NULL,
                interval_minutes INTEGER NOT NULL,
                expiry_date      DATE NOT NULL,
                strike           NUMERIC NOT NULL,
                option_type      TEXT NOT NULL,
                ts               TIMESTAMPTZ NOT NULL,
                open             NUMERIC NOT NULL,
                high             NUMERIC NOT NULL,
                low              NUMERIC NOT NULL,
                close            NUMERIC NOT NULL,
                iv               NUMERIC,
                volume           BIGINT NOT NULL DEFAULT 0,
                oi               BIGINT NOT NULL DEFAULT 0,
                spot             NUMERIC,
                fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (security_id, expiry_date, strike, option_type, ts)
            );
        """)
        conn.commit()


# -- API ------------------------------------------------------------------------

# Build strike range values with proper special chars at call site
_STRIKE_CHARS = {
    "atm": "ATM",
    "atm3": "ATM" + chr(177) + "3~3",   # ATM +/-3
    "atm10": "ATM" + chr(177) + "10~10", # ATM +/-10
}


def fetch_rolling_option(
    token: str,
    security_id: int,
    option_type: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    interval: str,
    from_date: str,
    to_date: str,
) -> dict:
    """Fetch rolling option data for a single option type (CALL or PUT)."""
    payload = {
        "exchangeSegment": EXCHANGE_SEGMENT,
        "interval": interval,
        "securityId": security_id,
        "instrument": INSTRUMENT_OPTIDX,
        "expiryFlag": expiry_flag,
        "expiryCode": expiry_code,
        "strike": _STRIKE_CHARS.get(strike, strike),
        "drvOptionType": option_type,
        "requiredData": REQUIRED_DATA,
        "fromDate": from_date,
        "toDate": to_date,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/charts/rollingoption",
        data=body,
        headers={"access-token": token, "Content-Type": "application/json"},
        method="POST",
    )
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_text = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                print(f"  Rate limited, retry {attempt+1}/{max_retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                # Rebuild request (body already consumed, re-create)
                req = urllib.request.Request(
                    f"{API_BASE}/charts/rollingoption",
                    data=body,
                    headers={"access-token": token, "Content-Type": "application/json"},
                    method="POST",
                )
                continue
            print(f"  HTTP {e.code}: {error_text[:300]}", file=sys.stderr)
            return {}
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  Network error, retry {attempt+1}/{max_retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                req = urllib.request.Request(
                    f"{API_BASE}/charts/rollingoption",
                    data=body,
                    headers={"access-token": token, "Content-Type": "application/json"},
                    method="POST",
                )
                continue
            print(f"  Network error: {e.reason}", file=sys.stderr)
            return {}
    return {}


# -- Helpers ---------------------------------------------------------------------

def next_thursday_after(d: datetime.date) -> datetime.date:
    """Return the Thursday on or after date d (weekly expiry)."""
    # Thursday = weekday 3
    days_ahead = (3 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + datetime.timedelta(days=days_ahead)


def month_chunks(from_date: datetime.date, to_date: datetime.date) -> list:
    """Split a date range into monthly chunks."""
    chunks = []
    current = from_date
    while current <= to_date:
        # Last day of current month
        if current.month == 12:
            month_end = current.replace(day=31)
        else:
            month_end = current.replace(month=current.month + 1, day=1) - datetime.timedelta(days=1)
        chunk_end = min(month_end, to_date)
        chunks.append((current, chunk_end))
        current = chunk_end + datetime.timedelta(days=1)
    return chunks


# -- Processing ------------------------------------------------------------------

def process_payload(
    conn: psycopg.Connection,
    security_id: int,
    interval_minutes: int,
    payload: dict,
    opt_type: str,
) -> int:
    """Process one API response and insert into DB. Returns row count."""
    if not payload or "data" not in payload:
        return 0

    side_key = "ce" if opt_type == "CALL" else "pe"
    side = payload["data"].get(side_key)
    if not side or not side.get("timestamp"):
        return 0

    timestamps = side["timestamp"]
    strikes = side.get("strike", [])
    opens = side.get("open", [])
    highs = side.get("high", [])
    lows = side.get("low", [])
    closes = side.get("close", [])
    ivs = side.get("iv", [])
    volumes = side.get("volume", [])
    ois = side.get("oi", [])
    spots = side.get("spot", [])

    opt_code = "CE" if opt_type == "CALL" else "PE"

    with conn.cursor() as cur:
        inserted = 0
        for i in range(len(timestamps)):
            strike_val = round(strikes[i]) if i < len(strikes) else 0
            ts_unix = timestamps[i]
            candle_ts = datetime.datetime.fromtimestamp(ts_unix, tz=datetime.timezone.utc)
            candle_date = candle_ts.date()
            expiry_d = next_thursday_after(candle_date)

            try:
                cur.execute(
                    """
                    INSERT INTO market.dhan_option_candles
                        (security_id, interval_minutes, expiry_date, strike, option_type,
                         ts, open, high, low, close, iv, volume, oi, spot)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (security_id, expiry_date, strike, option_type, ts)
                    DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high,
                        low = EXCLUDED.low, close = EXCLUDED.close,
                        iv = EXCLUDED.iv, volume = EXCLUDED.volume,
                        oi = EXCLUDED.oi, spot = EXCLUDED.spot
                    """,
                    (
                        security_id, interval_minutes, expiry_d, strike_val, opt_code,
                        candle_ts,
                        opens[i] if i < len(opens) else 0,
                        highs[i] if i < len(highs) else 0,
                        lows[i] if i < len(lows) else 0,
                        closes[i] if i < len(closes) else 0,
                        ivs[i] if i < len(ivs) else None,
                        int(volumes[i]) if i < len(volumes) else 0,
                        int(ois[i]) if i < len(ois) else 0,
                        spots[i] if i < len(spots) else None,
                    ),
                )
                inserted += 1
            except Exception:
                pass
        conn.commit()
    return inserted


# -- Main -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="DhanHQ Historical Options Data Fetcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["test", "backfill", "spot"], default="test")
    parser.add_argument("--underlying", choices=list(UNDERLYING_IDS.keys()), default="BANKNIFTY")
    parser.add_argument("--from", dest="from_date", default="", help="Start YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default="", help="End YYYY-MM-DD")
    parser.add_argument("--interval", choices=VALID_INTERVALS, default="5")
    parser.add_argument("--strike-range", choices=list(_STRIKE_CHARS.keys()), default="atm10")
    parser.add_argument("--expiry-flag", choices=["WEEK", "MONTH"], default="WEEK")
    parser.add_argument("--expiry-code", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--option-type", choices=["CALL", "PUT", "BOTH"], default="BOTH")
    parser.add_argument("--yes", action="store_true", help="Skip confirm prompt")
    args = parser.parse_args()

    token = get_token()
    check_token_expiry()

    security_id = UNDERLYING_IDS[args.underlying]
    option_types = ["CALL", "PUT"] if args.option_type == "BOTH" else [args.option_type]

    # -- Mode: test ------------------------------------------------------------
    if args.mode == "test":
        today = datetime.date.today()
        last_month = today - datetime.timedelta(days=35)
        print(f"Testing DhanHQ API (underlying={args.underlying} sec_id={security_id})...")
        print(f"  Date range: {last_month.isoformat()} -> {today.isoformat()}")

        resp = fetch_rolling_option(
            token, security_id, "CALL",
            args.expiry_flag, args.expiry_code, args.strike_range,
            args.interval, last_month.isoformat(), today.isoformat(),
        )
        ce = resp.get("data", {}).get("ce", {}) if resp.get("data") else {}
        pe = resp.get("data", {}).get("pe", {}) if resp.get("data") else {}
        ce_n = len(ce.get("timestamp", [])) if ce else 0
        pe_n = len(pe.get("timestamp", [])) if pe else 0
        if ce_n or pe_n:
            strikes = sorted(set(ce.get("strike", []))) if ce else []
            print(f"  OK - CE: {ce_n} candles, PE: {pe_n}")
            if strikes:
                print(f"  Strikes: {len(strikes)} [{min(strikes):.0f}-{max(strikes):.0f}]")
        else:
            print(f"  No data. Resp keys: {list(resp.keys())}")
        return

    # -- Mode: spot -------------------------------------------------------------
    if args.mode == "spot":
        print(f"Fetching spot data ({args.from_date} -> {args.to_date})...")
        resp = fetch_rolling_option(
            token, security_id, "CALL",
            args.expiry_flag, args.expiry_code, args.strike_range,
            args.interval, args.from_date, args.to_date,
        )
        ce = resp.get("data", {}).get("ce", {}) if resp.get("data") else {}
        if ce and ce.get("spot"):
            spots = list(dict.fromkeys(zip(ce["timestamp"], ce["spot"])))
            vals = [s for _, s in spots]
            print(f"  Unique spot samples: {len(spots)}")
            print(f"  Range: {min(vals):.0f} - {max(vals):.0f}")
        else:
            print("  No spot data.")
        return

    # -- Mode: backfill ----------------------------------------------------------
    if not args.from_date or not args.to_date:
        print("ERROR: --from and --to required for backfill mode", file=sys.stderr)
        sys.exit(1)

    from_dt = datetime.date.fromisoformat(args.from_date)
    to_dt = datetime.date.fromisoformat(args.to_date)
    chunks = month_chunks(from_dt, to_dt)
    n_chunks = len(chunks)
    n_calls = n_chunks * len(option_types)
    est_time = n_calls * REQUEST_INTERVAL

    print(f"\n{'='*60}")
    print(f"DHAN OPTION DATA BACKFILL")
    print(f"{'='*60}")
    print(f"  Underlying:   {args.underlying} (sec_id={security_id})")
    print(f"  Date range:   {args.from_date} -> {args.to_date}")
    print(f"  Expiry:       {args.expiry_flag} code={args.expiry_code}")
    print(f"  Strike:       {args.strike_range}")
    print(f"  Interval:     {args.interval} min")
    print(f"  Option types: {args.option_type}")
    print(f"  Monthly chunks: {n_chunks}")
    print(f"  API calls:    {n_calls} (~{est_time:.0f}s)")

    if not args.yes:
        confirm = input("Continue? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    conn = make_db_conn()
    ensure_table(conn)

    total_rows = 0
    total_calls = 0
    total_errors = 0

    for chunk_start, chunk_end in chunks:
        from_str = chunk_start.isoformat()
        to_str = chunk_end.isoformat()
        print(f"\n  Chunk: {from_str} -> {to_str}")

        for opt_type in option_types:
            time.sleep(REQUEST_INTERVAL)
            total_calls += 1
            print(f"    {opt_type:5s}...", end=" ", flush=True)

            resp = fetch_rolling_option(
                token, security_id, opt_type,
                args.expiry_flag, args.expiry_code, args.strike_range,
                args.interval, from_str, to_str,
            )

            rows = process_payload(conn, security_id, int(args.interval), resp, opt_type)
            if rows > 0:
                total_rows += rows
                print(f"OK ({rows} rows)")
            else:
                print("Empty")
                if not resp:
                    total_errors += 1

    conn.close()
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Chunks:       {n_chunks}")
    print(f"  API calls:    {total_calls}")
    print(f"  Rows stored:  {total_rows:,}")
    print(f"  Errors:       {total_errors}")

    if total_rows > 0:
        with make_db_conn() as vconn:
            with vconn.cursor() as cur:
                cur.execute(
                    "SELECT MIN(ts), MAX(ts), COUNT(*) FROM market.dhan_option_candles"
                    " WHERE security_id = %s", (security_id,)
                )
                r = cur.fetchone()
                print(f"  DB: {r[2]:,} rows ({r[0]} -> {r[1]})")


if __name__ == "__main__":
    main()