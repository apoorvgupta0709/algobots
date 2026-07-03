#!/usr/bin/env bash
set -euo pipefail
source /opt/data/finance-db/scripts/pg-env.sh
exec "$PGBIN/psql" "$@"
