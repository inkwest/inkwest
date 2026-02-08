#!/bin/bash
set -e

echo "Starting development environment..."

docker compose up -d db
echo "Waiting for database..."
sleep 5

cd backend
python db_migrate.py
cd ..

docker compose up -d backend frontend

echo "Development environment ready!"
echo "Frontend: http://localhost:3000"
echo "Backend: http://localhost:8000"
echo "API Docs: http://localhost:8000/docs"
