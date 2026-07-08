#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Read-only dashboard. Bind to loopback by default; use SSH tunnel/VPN/reverse proxy with auth.
# Example SSH tunnel from your laptop:
#   ssh -L 8501:127.0.0.1:8501 <vps-user>@<vps-host>
# Then open http://127.0.0.1:8501 locally.

HOST="${BANKNIFTY_DASHBOARD_HOST:-127.0.0.1}"
PORT="${BANKNIFTY_DASHBOARD_PORT:-8501}"
ALLOW_EXTERNAL="${BANKNIFTY_DASHBOARD_ALLOW_EXTERNAL:-no}"

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  if [[ "$ALLOW_EXTERNAL" != "yes" ]]; then
    echo "Refusing external dashboard bind ($HOST): use SSH tunnel/VPN, or set BANKNIFTY_DASHBOARD_ALLOW_EXTERNAL=yes only behind authenticated reverse proxy." >&2
    exit 2
  fi
fi

# Local pg_hba uses trust auth; dashboard_ro is created by migration 009 as SELECT-only.
export DASHBOARD_DATABASE_URL="${DASHBOARD_DATABASE_URL:-postgresql://dashboard_ro@127.0.0.1:55432/finance_tracker}"

if [[ "${BANKNIFTY_DASHBOARD_VALIDATE_ONLY:-0}" == "1" ]]; then
  echo "dashboard config validated: host=$HOST port=$PORT"
  exit 0
fi

exec uv run streamlit run dashboard/banknifty_options_dashboard.py \
  --server.address "$HOST" \
  --server.port "$PORT" \
  --server.headless true
