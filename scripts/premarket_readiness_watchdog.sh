#!/usr/bin/env bash
# Read-only BankNifty pre-market data readiness watchdog.
# Manual cron suggestion (do not create from autonomous worker): run around
# 09:15, 09:25 and 09:32 IST on NSE trading weekdays, delivering non-empty
# output only when the watchdog reports NOT READY.
set -euo pipefail

cd "$(dirname "$0")/.."
export FYERS_LOG_PATH="${FYERS_LOG_PATH:-/tmp}"
exec uv run python scripts/premarket_readiness_watchdog.py --strict "$@"
