# Inkwest

AI-powered Kubernetes debugging assistant — chat interface backed by Claude that executes read-only kubectl commands.

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy async + PostgreSQL + asyncpg
- **Frontend**: React + TypeScript + Vite + Zustand + Tailwind CSS
- **AI**: Anthropic Claude (direct, Bedrock, Azure, OpenRouter, CLI)
- **Auth**: OIDC (optional) with JWT session tokens, dev mode auto-login

## Dev Setup

```bash
# Backend
cd backend && uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -v

# Frontend
cd frontend && npm install
npm test -- --run
npm run dev

# Pre-commit
pre-commit run --all-files
```

## Architecture

- **Orchestrator pattern**: each AI provider has its own orchestrator yielding `ChatEvent` for WS streaming
- **Cancellation**: orchestrators accept `cancel_event: asyncio.Event` checked at loop boundaries (before API call, after response, before/during tool execution)
- **Dependency injection**: FastAPI `Depends()` for `get_current_user`, `require_admin`, `get_db`, `check_cluster_access`
- **Real-time sync**: PostgreSQL `LISTEN/NOTIFY` → `pubsub.py` → per-WS `asyncio.Queue` fan-out
- **Tool execution**: async subprocess kubectl with validation, timeout, output truncation
- **WS dual-task pattern**: `chat.py` runs receive loop + process loop as concurrent tasks so `stop` messages arrive during streaming
- **Combined prod image**: root `Dockerfile` builds the SPA (`VITE_API_URL=""` → same-origin) and bakes it into `/app/static`; `mount_spa()` in `app/main.py` serves it with index.html fallback (404-guarded for `api/`, `assets/`, `ws/`), `Cache-Control: no-cache` on index, gzip + security headers. Dev unchanged (no `static/` dir → JSON root endpoint). CI builds it, no variants

## Key Directories

```
backend/app/api/routes/     # FastAPI route handlers
backend/app/api/dependencies.py  # Auth + DB injection
backend/app/services/       # Orchestrators, AI client, tools, budget
backend/app/models/         # SQLAlchemy models
backend/app/core/           # Config, auth, database, logging
backend/app/tools/          # kubectl definitions, executor, validation
frontend/src/components/    # React components (chat/, session/, layout/)
frontend/src/store/         # Zustand stores (chat, sessions, settings)
frontend/src/services/      # API client, WebSocket service
frontend/src/hooks/         # useWebSocket, etc.
```

## Testing

- **Backend**: pytest + pytest-asyncio. Mock DB via `AsyncMock(spec=AsyncSession)`, mock auth via `app.dependency_overrides`. API tests use `httpx.AsyncClient` + `ASGITransport`.
- **Frontend**: Vitest + React Testing Library. Mock `fetch` for API tests, Zustand `setState` for store tests.

## Auth Modes

| Mode | `OIDC_ENABLED` | Behavior |
|------|----------------|----------|
| Dev | `false` | Auto-creates `dev@localhost` admin user |
| OIDC | `true` | Standard OAuth2 code flow + JWT sessions |
