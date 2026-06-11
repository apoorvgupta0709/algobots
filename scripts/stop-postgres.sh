#!/usr/bin/env bash
set -euo pipefail
source /opt/data/finance-db/scripts/pg-env.sh
"$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast
