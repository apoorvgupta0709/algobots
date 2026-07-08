#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/pg-env.sh"
exec "$PGBIN/psql" "$@"
