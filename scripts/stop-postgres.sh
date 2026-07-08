#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/pg-env.sh"
"$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast
