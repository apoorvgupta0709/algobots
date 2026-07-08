#!/usr/bin/env bash
set -euo pipefail
export FINANCE_DB_BASE="${FINANCE_DB_BASE:-/opt/data/finance-db}"   # Postgres deploy dir (NOT the repo); override via env
export PGROOT="$FINANCE_DB_BASE/pgroot"
export PGDATA="$FINANCE_DB_BASE/pgdata"
export PGBIN="$PGROOT/usr/lib/postgresql/17/bin"
export PGLIB="$PGROOT/usr/lib/x86_64-linux-gnu"
export LD_LIBRARY_PATH="$PGLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PGHOST=127.0.0.1
export PGPORT=55432
export PGUSER=hermes
export PGDATABASE=finance_tracker
export DATABASE_URL="postgresql://hermes@127.0.0.1:55432/finance_tracker"
