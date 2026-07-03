#!/usr/bin/env bash
# Idempotent pgvector install for the extracted-pgroot PostgreSQL 17 deploy.
# Run ON THE VPS as a user with write access to $PGROOT (hermes or root). Read-only against the DB
# except for making the extension available; migration 014 actually creates it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pg-env.sh
source "$SCRIPT_DIR/pg-env.sh"

available() {
  "$PGBIN/psql" -tAc "select count(*) from pg_available_extensions where name = 'vector'" 2>/dev/null | grep -q '^1$'
}

if available; then
  echo "pgvector already available — nothing to do"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

# Requires the PGDG apt source (https://wiki.postgresql.org/wiki/Apt). If the
# download fails, configure PGDG, run 'apt-get update', and retry.
echo "Downloading postgresql-17-pgvector..."
if ! apt-get download postgresql-17-pgvector; then
  echo "ERROR: apt-get download failed. Is the PGDG apt source configured and 'apt-get update' run?" >&2
  exit 1
fi

dpkg-deb -x postgresql-17-pgvector_*.deb extracted

install -d "$PGROOT/usr/lib/postgresql/17/lib" "$PGROOT/usr/share/postgresql/17/extension"
cp extracted/usr/lib/postgresql/17/lib/vector*.so "$PGROOT/usr/lib/postgresql/17/lib/"
cp extracted/usr/share/postgresql/17/extension/vector* "$PGROOT/usr/share/postgresql/17/extension/"
# No postgres restart needed: .control files are read on demand and vector.so is
# loaded lazily by CREATE EXTENSION. Apply migration 014 next.

if "$PGBIN/psql" -tAc "select count(*) from pg_available_extensions where name = 'vector'" | grep -q '^1$'; then
  echo "pgvector installed into $PGROOT"
else
  echo "ERROR: pgvector not visible after copy — confirm postgres is running at $PGHOST:$PGPORT as $PGUSER, then check $PGROOT layout" >&2
  exit 1
fi
