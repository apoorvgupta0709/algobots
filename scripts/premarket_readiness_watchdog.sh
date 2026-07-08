#!/usr/bin/env bash
# Read-only BankNifty pre-market data readiness watchdog.
# Manual cron suggestion (do not create from autonomous worker): run around
# 09:15, 09:25 and 09:32 IST on NSE trading weekdays, delivering non-empty
# output only when the watchdog reports NOT READY.
set -euo pipefail

cd "$(dirname "$0")/.."
export FYERS_LOG_PATH="${FYERS_LOG_PATH:-/tmp}"

# Capture output so a NOT READY result (non-zero under --strict) can be pushed
# to Telegram, not just printed to a cron log nobody watches.
rc=0
out="$(uv run python scripts/premarket_readiness_watchdog.py --strict "$@" 2>&1)" || rc=$?
printf '%s\n' "$out"
if [ "$rc" -ne 0 ]; then
  tail="$(printf '%s' "$out" | tail -c 800)"
  ./scripts/notify_telegram.sh "ALERT premarket_readiness_watchdog NOT READY (rc=$rc) at $(TZ=Asia/Kolkata date '+%F %T') on $(hostname):
$tail"
fi
exit "$rc"
