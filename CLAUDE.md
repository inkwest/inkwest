# Inkwest

AI-powered investigation assistant — chat interface backed by Claude that brokers scoped, read-only access across connectors (Kubernetes/kubectl, AWS, GitLab) plus Coralogix observability via MCP, to investigate and correlate issues across systems.

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy async + PostgreSQL + asyncpg
- **Frontend**: React + TypeScript + Vite + Zustand + Tailwind CSS
- **AI**: Anthropic Claude (direct, Bedrock, CLI)
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
- **Model selection**: admin-managed registry (`model_registry` table + `model_registry.py`) over a live-discovered catalog. `model_catalog.py` is pure discovery (Anthropic API, Bedrock via aws CLI incl. open-weight models when the `bedrock_include_open` flag is set, or the sandbox's backend from `sandbox.env`) with pricing resolved per turn as **live Bedrock Price-List → static table → default** (`pricing.get_model_pricing`): `pricing_live` fetches the public, unauthenticated AWS bulk Price List at catalog-refresh and maps it to model IDs via `pricing.price_match_keys`; the static table covers Anthropic (absent from the Bedrock Price List) and is the offline fallback. Vendor grouping/routing tags come from `model_catalog.bedrock_vendor` (the namespace, verbatim — no allowlist). The registry owns the admin-facing layer: which models are enabled, the default, per-model metadata (endpoint/arn/options), and pricing overrides, plus operator-added `source='custom'` rows the discovery filter doesn't surface. Discovered models with no row are synthesized on read (disabled; provider re-derived from the ID, priced when the live list or static table knows them). Resolution per turn: session pin → registry default → newest enabled → newest available, each validated against the current provider catalog so `AI_PROVIDER` switches degrade gracefully. Admin endpoints: `/api/admin/settings/model` (GET/PUT) + `/api/admin/settings/model/refresh`, and the sibling `/api/admin/models` CRUD (`POST`, `PUT|DELETE /{model_id}`). Registry + catalog cached in-process (mirrored in memory post-write, like `app_settings.py`): replicas pick up changes on restart/TTL only. Switching a session's model invalidates its Anthropic prompt cache (first turn after re-pays full input)
- **Non-Anthropic Bedrock models in chat**: non-Anthropic Bedrock models (Llama/Mistral/Nova/DeepSeek) can't go through the Anthropic SDK, so `orchestrator_converse.py` runs the same `ChatEvent` tool-use loop over the Bedrock **Converse** API (boto3 via `asyncio.to_thread`, translating Anthropic content blocks ↔ Converse blocks at the API boundary only). `get_orchestrator()` returns a per-turn dispatcher on Bedrock that routes by the resolved model's registry provider tag: `provider_speaks_messages()` (anthropic/legacy-bedrock → `orchestrator.py`, every other per-vendor tag → `orchestrator_converse.py`), falling back to the `is_anthropic_model` ID heuristic only for models with no registry row. Registry provider tags are per-vendor (`anthropic`/`amazon`/`meta`/`mistral`/`cohere`/`ai21`/`deepseek`/`stability`, derived via `model_catalog.bedrock_vendor`; legacy `bedrock`/`bedrock-open` still accepted). The whole-Bedrock catalog (all vendors) and the admin all-vendors toggle are gated on the `inkwest-agent` engine being enabled (`AGENTIC_ENGINES`, see `app/services/engines.py`)
- **Combined prod image**: root `Dockerfile` builds the SPA (`VITE_API_URL=""` → same-origin) and bakes it into `/app/static`; `mount_spa()` in `app/main.py` serves it with index.html fallback (404-guarded for `api/`, `assets/`, `ws/`), `Cache-Control: no-cache` on index, gzip + security headers. Dev unchanged (no `static/` dir → JSON root endpoint). CI builds it, no variants

## Key Directories

```
backend/app/api/routes/     # FastAPI route handlers
backend/app/api/dependencies.py  # Auth + DB injection
backend/app/services/       # Orchestrators, AI client, tools, budget; agent_mcp_config.py (P1.7: broker materializes a conversation's enabled MCP server defs into a sandbox file, removed on turn end as it can embed creds)
backend/app/models/         # SQLAlchemy models
backend/app/core/           # Config, auth, database, logging
backend/app/tools/          # kubectl definitions, executor, validation
backend/inkwest_agent/      # standalone agent worker (P1.1): DB-free Converse loop + stdin turn-spec -> stdout NDJSON ChatEvent (`python -m inkwest_agent`). P1.7: skills-as-tools (skills.py — synthetic `read_skill` progressive disclosure) + a lazy in-sandbox MCP client (mcp.py — connects enabled servers, merges their tools as `mcp__<server>__<tool>`; optional `mcp` SDK, degrades if absent)
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
