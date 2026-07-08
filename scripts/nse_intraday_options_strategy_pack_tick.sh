#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Script-only paper/proxy monitor. No LLM. No live broker orders.
# Exits quietly outside NSE market window or weekends.
DOW=$(TZ=Asia/Kolkata date +%u)
HM=$(TZ=Asia/Kolkata date +%H%M)
if [ "$DOW" -gt 5 ]; then
  exit 0
fi
if [ "$HM" -lt 0930 ] || [ "$HM" -gt 1525 ]; then
  exit 0
fi
exec 9>/tmp/nse_intraday_options_strategy_pack.lock
if ! flock -n 9; then
  exit 0
fi

rc=0
uv run python scripts/run_nse_intraday_options_strategy_pack.py \
    --config config/nse_intraday_options_strategy_pack.json \
    --mode tick \
    --refresh || rc=$?
if [ "$rc" -ne 0 ]; then
  ./scripts/notify_telegram.sh "ALERT nse_intraday_options_strategy_pack_tick failed (rc=$rc) at $(TZ=Asia/Kolkata date '+%F %T') on $(hostname). Open paper positions may not be getting stop/exit checks."
fi
exit "$rc"
