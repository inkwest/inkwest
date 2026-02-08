#!/bin/bash
# Kubernetes Job entrypoint for database migrations
#
# This script is designed to run as a Kubernetes Job before deploying
# the main application. It ensures the database schema is up to date.
#
# Usage in Kubernetes:
#   Create a Job that runs this entrypoint before the main deployment
#
# Environment variables:
#   DATABASE_URL: PostgreSQL connection string (required)

set -e

echo "=== Inkwest Database Migration ==="
echo "Starting at $(date)"

# Validate environment
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL environment variable is required"
    exit 1
fi

# Wait for database to be ready
echo "Waiting for database connection..."
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if python -c "
import asyncio
import asyncpg
import os

async def check():
    url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(url)
    await conn.close()
    return True

asyncio.run(check())
" 2>/dev/null; then
        echo "Database connection successful"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "Waiting for database... (attempt $RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: Could not connect to database after $MAX_RETRIES attempts"
    exit 1
fi

# Show current status
echo ""
echo "Current migration status:"
python db_migrate.py --status

# Run migrations
echo ""
echo "Running pending migrations..."
python db_migrate.py

# Show final status
echo ""
echo "Final migration status:"
python db_migrate.py --status

echo ""
echo "=== Migration complete at $(date) ==="
