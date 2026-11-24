#!/usr/bin/env bash
# Kubernetes Job entrypoint for database migrations: db_migrate.py owns the whole
# flow (database wait, env validation, migrations) and its structured logs.
#
# Environment variables:
#   DATABASE_URL: PostgreSQL connection string (required)
set -euo pipefail

exec python db_migrate.py "$@"
