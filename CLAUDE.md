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
- **Model selection**: admin-managed registry (`model_registry` table + `model_registry.py`) over a live-discovered catalog. `model_catalog.py` is pure discovery (Anthropic API, Bedrock via aws CLI incl. open-weight models when the `bedrock_include_open` flag is set; for the `claude-cli` provider the backend is resolved from **backend settings** — `aws_bearer_token_bedrock`/AWS creds ⇒ Bedrock, else `anthropic_api_key` ⇒ Anthropic — never `sandbox.env`, so the LLM broker can strip the key from the sandbox without breaking discovery) with pricing resolved per turn as **live Bedrock Price-List → static table → default** (`pricing.get_model_pricing`): `pricing_live` fetches the public, unauthenticated AWS bulk Price List at catalog-refresh and maps it to model IDs via `pricing.price_match_keys`; the static table covers Anthropic (absent from the Bedrock Price List) and is the offline fallback. Vendor grouping/routing tags come from `model_catalog.bedrock_vendor` (the namespace, verbatim — no allowlist). The registry owns the admin-facing layer: which models are enabled, the default, per-model metadata (endpoint/arn/options), and pricing overrides, plus operator-added `source='custom'` rows the discovery filter doesn't surface. Discovered models with no row are synthesized on read (disabled; provider re-derived from the ID, priced when the live list or static table knows them). Resolution per turn: session pin → registry default → newest enabled → newest available, each validated against the current provider catalog so `AI_PROVIDER` switches degrade gracefully. Admin endpoints: `/api/admin/settings/model` (GET/PUT) + `/api/admin/settings/model/refresh`, and the sibling `/api/admin/models` CRUD (`POST`, `PUT|DELETE /{model_id}`). Registry + catalog cached in-process (mirrored in memory post-write, like `app_settings.py`): replicas pick up changes on restart/TTL only. Switching a session's model invalidates its Anthropic prompt cache (first turn after re-pays full input)
- **Non-Anthropic Bedrock models in chat**: non-Anthropic Bedrock models (Llama/Mistral/Nova/DeepSeek) can't go through the Anthropic SDK, so `orchestrator_converse.py` runs the same `ChatEvent` tool-use loop over the Bedrock **Converse** API (boto3 via `asyncio.to_thread`, translating Anthropic content blocks ↔ Converse blocks at the API boundary only). `get_orchestrator()` returns a per-turn dispatcher on Bedrock that routes by the resolved model's registry provider tag: `provider_speaks_messages()` (anthropic/legacy-bedrock → `orchestrator.py`, every other per-vendor tag → `orchestrator_converse.py`), falling back to the `is_anthropic_model` ID heuristic only for models with no registry row. Registry provider tags are per-vendor (`anthropic`/`amazon`/`meta`/`mistral`/`cohere`/`ai21`/`deepseek`/`stability`, derived via `model_catalog.bedrock_vendor`; legacy `bedrock`/`bedrock-open` still accepted). The whole-Bedrock catalog (all vendors) and the admin all-vendors toggle are gated on the `inkwest-agent` engine being enabled (`AGENTIC_ENGINES`, see `app/services/engines.py`)
- **LLM auth broker** (`llm_broker_enabled`, sandboxed `claude-cli` provider): zero real Bedrock secret in the sandbox. The sandbox holds only a short-lived, opaque **per-turn grant** in `AWS_BEARER_TOKEN_BEDROCK` (in-process store `llm_proxy_token.py`, minted at turn start, BURNED in the orchestrator `finally`). The broker route `app/api/routes/llm_proxy.py` has two legs: data plane (`POST /api/llm-proxy/model/{id}/{op}`) verifies the grant, restricts to enabled registry models/ARNs (+ their inference-profile ids) + allowed Bedrock ops — **fail-closed**: no enabled rows ⇒ nothing invocable (unlike the default-open picker; the broker is a credential gate) — swaps in the backend-held real bearer (`settings.aws_bearer_token_bedrock`), streams to `bedrock-runtime.<region>` under a total wall-clock cap; control plane (`GET /api/llm-proxy/{inference-profiles|foundation-models}`, dot-segments rejected) forwards to `bedrock.<region>` so Claude Code can resolve a model to its regional inference profile (required in regions like eu-west-1 — without it the bare-id invoke 403s). Pure header swap, no SigV4 (both the CLI's `ANTHROPIC_BEDROCK_BASE_URL` and the agent's boto3 `endpoint_url` send `Authorization: Bearer …`; `x-amz-*` SigV4/credential headers from the sandbox are stripped); shared streaming-proxy primitives (body cap, hop-by-hop strip, teardown) live in `app/api/routes/_proxy_stream.py`, reused by `mcp_proxy.py`. Wiring (`llm_broker.py`): both engines inject the grant + base URL per turn via `docker exec -e` (overriding `sandbox_env_file`); the agent also carries `bedrock_endpoint_url` on the TurnSpec (protocol v5; the orchestrator omits unset/None scalars from the wire spec so an older agent image still parses a broker-off turn). With the broker on, the CLI is forced onto the registry default when a session has no model pin (else it'd invoke its built-in default and 403). Reuses the `mcp_proxy_base_url` host (squid allowlist + `--add-host`) — no new egress rule. Grant store is in-process + hard-capped (single-replica assumption; `main.py` fail-fasts on `WEB_CONCURRENCY>1`). Off by default ⇒ today's behavior (real bearer in `sandbox_env_file`). See `docs/llm-auth-broker.md`
- **Combined prod image**: root `Dockerfile` builds the SPA (`VITE_API_URL=""` → same-origin) and bakes it into `/app/static`; `mount_spa()` in `app/main.py` serves it with index.html fallback (404-guarded for `api/`, `assets/`, `ws/`), `Cache-Control: no-cache` on index, gzip + security headers. Dev unchanged (no `static/` dir → JSON root endpoint). CI builds it, no variants
- **Frontend backend-down resilience**: AWS/ALB answers 502/503/504 (or the network fails) while the backend restarts/redeploys. `services/api.ts::fetchWithRetry` (every `fetchAPI` caller + the raw attachment fetches route through it) retries those + network errors for **all verbs** with jittered capped-exponential backoff (`lib/backoff.ts::jitteredBackoff`, ~12 attempts ≈ up to a minute). Client timeouts split by idempotency: **GET/HEAD** get a 30s abort-**and-retry** timeout (a hung read is safe to replay); **mutations** get a 5min timeout-to-**fail** (`MUTATION_TIMEOUT_MS` — reject, never replay) so a slow AWS enumerate/STS-mint isn't killed but a truly hung POST still surfaces. Attachment transfers opt out of timeouts entirely. The three WS reconnect loops (`ChatWebSocket`, `useSyncSocket`, `useEgressSocket`) reconnect **uncapped** (delay capped 30s + jitter) until a 10min wall-clock budget (`lib/backoff.ts::createReconnectBudget`, reset on a successful open / tab-foreground) is spent; each stops on its own terminal/clean close codes (1000 app-close; 4001 auth→redirect; chat 4004/4010/4011; egress 4003) and aborts its in-flight `fetchWsTicket` on disconnect/unmount. A refcounted `store/backend.ts::useBackendStore` drives `BackendBanner`: amber "reconnecting…" while any HTTP call backs off (cleared on the first reachable app response or WS `onopen`), and — only when the **sync** socket (the app-wide heartbeat) exhausts its budget — a red **"reload"** prompt (`gaveUp`, sticky until page reload; chat/egress give up quietly). **Trade-off**: a retried non-idempotent call (502/504/network) can rarely double-apply server-side — accepted; durable fix is server idempotency keys on the AWS mint/grant routes

## Key Directories

```
backend/app/api/routes/     # FastAPI route handlers
backend/app/api/dependencies.py  # Auth + DB injection
backend/app/services/       # Orchestrators, AI client, tools, budget; agent_mcp_config.py (P1.7: broker materializes a conversation's enabled MCP server defs into a sandbox file, removed on turn end as it can embed creds). MCP credential brokering: static catalog HTTP/SSE servers (`mcp_catalog.py`) are rewritten to `/api/mcp-proxy/{key}/` + an internal JWT (like OAuth2) by `mcp_config.build_session_static_servers`, so their real headers/apikey never enter the sandbox — `mcp_proxy.py` injects them server-side; stdio servers can't be brokered (local process) and stay verbatim. Needs `mcp_proxy_base_url`; unset ⇒ verbatim fallback
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
