#!/bin/bash
set -e

echo "Resetting database..."
docker compose down -v
docker compose up -d db
echo "Waiting for database..."
sleep 5
docker compose up -d backend
sleep 3
docker compose exec backend python db_migrate.py
docker compose exec backend python seed.py
echo "Database reset complete!"
