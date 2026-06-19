.PHONY: fresh dev up down logs startup check-dind-connectivity reset-db migrate seed test lint site-dev site-build sandbox-pull

# Full rebuild cycle: tear down, rebuild images, start, migrate, pull sandbox image
fresh: down build up migrate sandbox-pull

# Development
dev:
	./scripts/dev.sh

up:
	docker compose up -d
	@$(MAKE) --no-print-directory startup
	@echo "Inkwest is up → http://localhost:3000/"

down:
	docker compose down

logs:
	docker compose logs -f

# Host-side startup hooks (conf.d-style drop-in dir): run every executable
# startup.d/*.sh in name order, after the stack is up. The dir + its README are the
# committed, generic mechanism; local/site-specific wiring (e.g. VPN routing into dind)
# lives here as git-ignored scripts, so machine specifics never touch the tree.
startup:
	@for f in startup.d/*.sh; do \
		[ -e "$$f" ] || continue; \
		if [ -x "$$f" ]; then echo ">> startup.d: $$f"; "$$f" || exit $$?; \
		else echo ">> startup.d: skip (not executable) $$f"; fi; \
	done

# Sandbox: ECR-login + pull the toolbox image directly inside dind (incremental layers)
sandbox-pull:
	./scripts/sandbox-pull.sh

# Diagnose the sandbox egress chain for a target (host or IP), layer by layer (DNS /
# route / proxy+connect): make check-dind-connectivity TARGET=example.com [ARGS=--http]
check-dind-connectivity:
	@./scripts/check-dind-connectivity.sh $(TARGET) $(ARGS)

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
