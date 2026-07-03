#!/usr/bin/env bash
set -euo pipefail
source /opt/data/finance-db/scripts/pg-env.sh
mkdir -p "$FINANCE_DB_BASE/run" "$FINANCE_DB_BASE/logs"
if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "PostgreSQL already running on 127.0.0.1:55432"
else
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$FINANCE_DB_BASE/logs/pg_ctl.log" start
fi
"$PGBIN/pg_isready" -h "$PGHOST" -p "$PGPORT" -U "$PGUSER"
