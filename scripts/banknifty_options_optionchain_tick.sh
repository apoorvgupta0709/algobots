#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/finance-db

# Ingest BankNifty + Nifty option-chain snapshots during market hours. Read-only
# market data; no order placement. Runs alongside the paper monitor so greeks/IV/OI
# and PCR/max-pain are fresh when the engine evaluates an entry.
dow="$(TZ=Asia/Kolkata date +%u)"
hhmm="$(TZ=Asia/Kolkata date +%H%M)"
if [[ "$dow" -gt 5 || "$hhmm" < "0915" || "$hhmm" > "1530" ]]; then
  exit 0
fi

mkdir -p logs
if ! ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atqc 'select 1' >/dev/null 2>&1; then
  ./scripts/start-postgres.sh >> logs/banknifty_options_postgres_autostart.log 2>&1 || {
    echo "Option-chain ingest skipped: PostgreSQL unavailable on 127.0.0.1:55432"
    exit 1
  }
fi

exec 9>/tmp/banknifty_options_optionchain_tick.lock
if ! flock -n 9; then
  exit 0
fi

FYERS_LOG_PATH=/tmp/ timeout 58s uv run python scripts/ingest_fyers_optionchain.py \
  --config config/options_chain.json
