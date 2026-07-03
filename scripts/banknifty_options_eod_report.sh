#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/finance-db

FYERS_LOG_PATH=/tmp/ uv run python scripts/banknifty_options_paper.py --mode report
