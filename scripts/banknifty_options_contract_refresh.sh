#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

FYERS_LOG_PATH=/tmp/ uv run python scripts/banknifty_options_paper.py --mode refresh-contracts
