#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Run only during the intraday BankNifty options window. Paper only.
# Adaptive cadence:
# - before entry: evaluate entry/status only on configured 5-minute boundaries
# - after a paper entry exists: monitor the open trade every 15 seconds
dow="$(TZ=Asia/Kolkata date +%u)"
hhmm="$(TZ=Asia/Kolkata date +%H%M)"
if [[ "$dow" -gt 5 || "$hhmm" < "0920" || "$hhmm" > "1520" ]]; then
  exit 0
fi

mkdir -p logs
if ! ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atqc 'select 1' >/dev/null 2>&1; then
  ./scripts/start-postgres.sh >> logs/banknifty_options_postgres_autostart.log 2>&1 || {
    echo "BankNifty options paper tick skipped: PostgreSQL unavailable on 127.0.0.1:55432"
    exit 1
  }
fi

exec 9>/tmp/banknifty_options_paper_tick.lock
if ! flock -n 9; then
  exit 0
fi

FYERS_LOG_PATH=/tmp/ timeout 58s uv run python scripts/banknifty_options_paper.py \
  --mode tick \
  --refresh-quotes \
  --quiet-no-change \
  --loop-seconds 55
