#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/finance-db
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
exec flock -n /tmp/nse_intraday_options_strategy_pack.lock \
  uv run python scripts/run_nse_intraday_options_strategy_pack.py \
    --config config/nse_intraday_options_strategy_pack.json \
    --mode tick \
    --refresh
