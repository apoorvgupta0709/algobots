#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/finance-db

# After-market BankNifty day-pattern builder + report. Research / paper-only:
# no FYERS order placement, no execution-log writes. Builds today's session
# features+classification then renders the Markdown/JSON report.
#
# NOT scheduled by default. Wire into Hermes cron (~16:00 IST, Mon-Fri) only
# after a historical backfill + sample report have been verified.

DATE="${1:-$(TZ=Asia/Kolkata date +%F)}"

mkdir -p logs
if ! ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atqc 'select 1' >/dev/null 2>&1; then
  ./scripts/start-postgres.sh >> logs/banknifty_options_postgres_autostart.log 2>&1 || {
    echo "BankNifty trend-pattern report skipped: PostgreSQL unavailable on 127.0.0.1:55432"
    exit 1
  }
fi

exec 9>/tmp/banknifty_trend_pattern_report.lock
if ! flock -n 9; then
  exit 0
fi

FYERS_LOG_PATH=/tmp/ uv run python scripts/build_banknifty_trend_pattern_library.py \
  --date "$DATE" --resolution 5

FYERS_LOG_PATH=/tmp/ uv run python scripts/generate_banknifty_trend_pattern_report.py \
  --date "$DATE"
