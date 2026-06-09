.PHONY: dev up down logs reset-db migrate seed test lint site-dev site-build sandbox-pull

# Development
dev:
	./scripts/dev.sh

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

# Sandbox: ECR-login + pull the toolbox image directly inside dind (incremental layers)
sandbox-pull:
	./scripts/sandbox-pull.sh

# Database
reset-db:
	./scripts/reset-db.sh

migrate:
	docker compose exec backend python db_migrate.py

seed:
	docker compose exec backend python seed.py

# Testing
test:
	cd backend && pytest
	cd frontend && npm test

# Linting
lint:
	cd backend && ruff check .
	cd frontend && npm run lint

# Build
build:
	docker compose build

# Production
prod-build:
	docker compose -f docker-compose.prod.yml build

# Site
site-dev:
	cd site && hugo server --buildDrafts

site-build:
	cd site && hugo --minify
