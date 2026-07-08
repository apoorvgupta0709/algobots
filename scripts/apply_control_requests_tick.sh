#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Apply pending dashboard control requests (paper-only control plane).
# Runs every minute, no market-hours gate: strategy toggles and risk-cap edits
# submitted off-hours should still land before the next session.

mkdir -p logs
if ! ./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -Atqc 'select 1' >/dev/null 2>&1; then
  ./scripts/start-postgres.sh >> logs/banknifty_options_postgres_autostart.log 2>&1 || {
    echo "Control request tick skipped: PostgreSQL unavailable on 127.0.0.1:55432"
    ./scripts/notify_telegram.sh "ALERT apply_control_requests_tick: PostgreSQL unavailable on 127.0.0.1:55432 at $(TZ=Asia/Kolkata date '+%F %T') on $(hostname)."
    exit 1
  }
fi

exec 9>/tmp/apply_control_requests.lock
if ! flock -n 9; then
  exit 0
fi

rc=0
timeout 55s uv run python scripts/apply_control_requests.py || rc=$?
if [ "$rc" -ne 0 ]; then
  ./scripts/notify_telegram.sh "ALERT apply_control_requests_tick failed (rc=$rc) at $(TZ=Asia/Kolkata date '+%F %T') on $(hostname). Dashboard control requests may not be applied."
fi
exit "$rc"
