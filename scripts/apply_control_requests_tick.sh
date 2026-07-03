#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/finance-db

# Apply pending dashboard control requests (paper-only control plane).
# Runs every minute, no market-hours gate: strategy toggles and risk-cap edits
# submitted off-hours should still land before the next session.

mkdir -p logs
if ! ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atqc 'select 1' >/dev/null 2>&1; then
  ./scripts/start-postgres.sh >> logs/banknifty_options_postgres_autostart.log 2>&1 || {
    echo "Control request tick skipped: PostgreSQL unavailable on 127.0.0.1:55432"
    exit 1
  }
fi

exec 9>/tmp/apply_control_requests.lock
if ! flock -n 9; then
  exit 0
fi

timeout 55s uv run python scripts/apply_control_requests.py
