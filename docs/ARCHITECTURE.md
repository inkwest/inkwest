# Architecture

## Overview

Inkwest is an AI-powered Kubernetes debugging portal. Users interact with an AI assistant via a chat interface that can execute read-only kubectl commands against selected clusters, search documentation, and help diagnose issues.

```
 Browser (React/TS)
        |
        | HTTP REST + WebSocket
        v
  FastAPI Backend
        |
   +---------+---------+----------+
   |         |         |          |
 PostgreSQL  AI     kubectl    Web Search
             Providers          (Brave/SerpAPI)
```

## AI Providers

| Provider | Setting | Use Case | Tested |
|---|---|---|---|
| Anthropic | `AI_PROVIDER=anthropic` | Direct Claude API (with prompt caching) | No |
| AWS Bedrock | `AI_PROVIDER=bedrock` | Enterprise AWS deployments | No |
| Azure OpenAI | `AI_PROVIDER=azure` | GPT models via Azure | No |
| OpenRouter | `AI_PROVIDER=openrouter` | Unified API, multiple providers (with prompt caching for Anthropic models) | Yes |
| OpenAI | `AI_PROVIDER=openai` | Direct OpenAI API (or compatible endpoint) | No |
| Google Gemini | `AI_PROVIDER=google` | Direct Gemini API (implicit caching) | No |
| Claude CLI | `AI_PROVIDER=claude-cli` | Local dev with Claude Max subscription | Yes |

> **Note:** Only **Claude CLI** and **OpenRouter** have been properly end-to-end tested. Other providers have orchestrators implemented but have not been validated against live APIs.

Each provider has its own orchestrator (`orchestrator.py`, `orchestrator_azure.py`, `orchestrator_openai.py`, `orchestrator_openrouter.py`, `orchestrator_google.py`, `orchestrator_cli.py`). The factory `get_orchestrator()` in `ai_client.py` selects the correct one at runtime.

## Authentication

Two modes controlled by `OIDC_ENABLED`:

- **Single-user** (`false`, default): auto-creates `dev@localhost` with admin role. No login needed.
- **OIDC** (`true`): standard OAuth2 authorization code flow. JWT session tokens (HS256, 24h).

Three protection levels:
- **Public**: `/health`, `/health/ready`, `/api/auth/*`
- **Authenticated** (`CurrentUser`): sessions, clusters (read), WebSocket chat
- **Admin** (`AdminUser`): clusters (write), `/api/admin/*` (budgets, users, role mappings, usage)

See [AUTHENTICATION.md](AUTHENTICATION.md) for details.

## Role-Based Cluster Access

`RoleKubeconfigMapping` maps OIDC roles to cluster permissions:

- `allowed_clusters`: list of cluster UUIDs the role can access
- `allowed_namespaces` / `denied_namespaces`: namespace-level restrictions
- `kubeconfig_override`: optional per-role kubeconfig path
- `can_web_search`: enable/disable web search for the role
- `daily_token_budget`: per-role token budget (0 = use global default)

Admins bypass all restrictions. When no mappings exist, all users get unrestricted access.

## Backend Structure

```
backend/app/
  api/
    routes/
      health.py          # /health, /health/ready
      auth.py            # /api/auth/* (OIDC flow)
      sessions.py        # /api/sessions/* (CRUD + stats)
      clusters.py        # /api/clusters/* (read: auth, write: admin)
      admin.py           # /api/admin/* (budgets, users, role mappings, usage)
      chat.py            # /ws/chat/{session_id} (WebSocket)
      sync.py            # /ws/sync (real-time session list sync)
    dependencies.py      # CurrentUser, AdminUser, get_db, cluster access checks
  core/
    config.py            # Pydantic settings (all env vars)
    auth.py              # OIDCClient, JWT create/verify
    database.py          # AsyncSession, engine
  services/
    ai_client.py         # Provider factory: get_orchestrator(), get_ai_client()
    orchestrator.py      # Anthropic/Bedrock tool loop
    orchestrator_azure.py
    orchestrator_openai.py   # Direct OpenAI API
    orchestrator_openrouter.py
    orchestrator_cli.py  # Claude CLI subprocess
    auth_cleanup.py      # Background cleanup of expired OIDC states
    prompts.py           # System prompt generation
    web_search.py        # Brave/SerpAPI with source trust classification
    budget_service.py    # Token/cost budget enforcement
    pricing.py           # Per-model token pricing
    messages.py          # Message persistence helpers
    events.py            # ChatEvent types for WebSocket streaming
    pubsub.py            # PostgreSQL LISTEN/NOTIFY fan-out for real-time sync
  models/
    user.py              # User (OIDC identity, roles)
    session.py           # Chat session (cluster binding, token counters)
    message.py           # Message (role, content blocks, tool calls)
    cluster.py           # Cluster (kubeconfig path, context)
    role_mapping.py      # RoleKubeconfigMapping
    budget.py            # Budget + UsageRecord
    oidc_pending_state.py # Temporary OIDC login CSRF states
    schemas.py           # Pydantic request/response schemas
  tools/
    definitions.py       # Tool schemas for Claude (kubectl_get, _describe, _logs, etc.)
    kubectl.py           # kubectl command builder
    executor.py          # Async subprocess execution with timeout
    validation.py        # Input validation
```

## Key Features

### WebSocket Streaming

`/ws/chat/{session_id}` accepts messages and streams AI responses as events:

| Event | Direction | Description |
|---|---|---|
| `message` | client -> server | User chat message |
| `stop` | client -> server | Cancel current AI processing |
| `ping` / `pong` | bidirectional | Keepalive |
| `text_delta` | server -> client | AI text chunk |
| `tool_start` | server -> client | Tool execution starting |
| `tool_result` | server -> client | Tool output |
| `done` | server -> client | Response complete (metadata includes `stopped: true` if cancelled) |
| `error` | server -> client | Error occurred |

The WS handler uses a dual-task pattern: a receive loop (always listening for `stop`/`ping`/messages) and a process loop (orchestrating AI responses). This allows `stop` messages to arrive during streaming. Each orchestration gets a fresh `asyncio.Event` that the receive loop can set to signal cancellation.

### Tool Execution

Read-only kubectl tools via async subprocess:
- `kubectl_get` -- list/get resources (pods, deployments, services, etc.)
- `kubectl_describe` -- detailed resource info with events
- `kubectl_logs` -- container logs (with tail, previous, since)
- `kubectl_events` -- cluster events with field selectors
- `kubectl_top` -- CPU/memory metrics (requires metrics-server)
- `kubectl_explain` -- resource field documentation
- `kubectl_api_resources` -- list available API resources

All commands: validated input, timeout enforcement, output truncation at 50K chars.

### Web Search

Optional documentation lookup (Brave Search or SerpAPI). Sources classified by trust level: official (kubernetes.io, helm.sh, ...), trusted (github.com, stackoverflow.com, ...), community, unknown, blocked. Controlled per-role via `can_web_search`.

### Session Management

Sessions are bound to a user and **optionally** a cluster. Track:
- Message history with content blocks (text, tool_use, tool_result)
- Token usage counters (input, output, tool calls, iterations)
- Retry support for failed messages
- Archive (soft delete)

#### Sessions without a configured cluster

A session can start with **no cluster** (`cluster_id` is nullable). This is the
default when no cluster is configured — Kube access in our model comes from AWS
(EKS kubeconfigs minted per session), so a session must not require a
pre-configured cluster to open.

- **New-session flow** skips cluster selection when none exist and creates the
  session without a `cluster_id`; the cluster picker only appears when ≥ 1
  cluster is configured.
- **WebSocket chat + orchestrators** run without a kubeconfig. `kubectl_*` tools
  stay advertised to the model (cluster access can arrive mid-session via AWS),
  but invoking one without a kubeconfig returns a clean error instead of
  failing the turn. The system prompt notes that no cluster is configured.
- Connector tools (AWS, GitLab, Coralogix) are unaffected and fully usable.

### Quick-Start Links

Shareable URLs to create a session on a specific cluster with an optional pre-filled prompt:

```
/start?cluster=production&prompt=Why are pods crashlooping in namespace monitoring?
```

Matches cluster by name, display name, or UUID. Creates a session and redirects to chat.

### Admin UI

Admin-only endpoints under `/api/admin/`:
- **Budgets**: CRUD for token/cost budgets (per-user, per-role, global). Daily/monthly periods, soft/hard limits.
- **Users**: list, view, update roles, enable/disable accounts.
- **Role Mappings**: CRUD for `RoleKubeconfigMapping` (cluster access, namespace restrictions, web search, token budgets).
- **Usage Reports**: summary by period, breakdown by user, breakdown by model/provider.

### Real-Time Session Sync

`/ws/sync` WebSocket (authenticated) broadcasts session lifecycle events via PostgreSQL `LISTEN/NOTIFY`:

- **Pub/sub layer** (`pubsub.py`): single background listener task fans events to per-WS `asyncio.Queue`s
- **Events**: `session_created`, `session_updated`, `session_deleted`
- Keeps multiple browser tabs in sync without polling

### Multi-Provider Orchestration

The `get_orchestrator()` factory in `ai_client.py` returns an async generator function based on `AI_PROVIDER`:

| Provider | Orchestrator | Notes |
|---|---|---|
| `anthropic` / `bedrock` | `orchestrator.py` | Native Anthropic SDK, streaming tool loop |
| `azure` | `orchestrator_azure.py` | OpenAI SDK, function calling |
| `openai` | `orchestrator_openai.py` | Direct OpenAI API (or compatible) |
| `openrouter` | `orchestrator_openrouter.py` | OpenAI-compatible API |
| `claude-cli` | `orchestrator_cli.py` | Local subprocess, no API costs |

All orchestrators yield `ChatEvent` objects for WebSocket streaming. They accept an optional `cancel_event: asyncio.Event` for mid-stream cancellation, checked at loop boundaries (before API call, after response, before/during tool execution).

## Database

PostgreSQL with SQLAlchemy 2.0 async. Key tables:
- `users` -- OIDC identity, roles, status
- `sessions` -- cluster binding, token counters, CLI session ID (for claude-cli)
- `messages` -- role, content (JSON blocks), tool calls, status, retry
- `clusters` -- name, kubeconfig path, context, active flag
- `role_kubeconfig_mappings` -- role -> cluster/namespace permissions
- `budgets` + `usage_records` -- budget enforcement and tracking
- `oidc_pending_states` -- temporary OIDC login CSRF states (auto-cleaned)

## Configuration

All settings via environment variables (see `app/core/config.py`):

| Category | Key Variables |
|---|---|
| Database | `DATABASE_URL` |
| AI Provider | `AI_PROVIDER`, `ANTHROPIC_API_KEY`, `AWS_REGION`, `AZURE_OPENAI_*`, `OPENROUTER_*`, `CLAUDE_CLI_PATH` |
| Auth | `OIDC_ENABLED`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SECRET_KEY` |
| Kubernetes | `KUBECONFIG_DIR`, `KUBECTL_TIMEOUT` |
| Budget | `BUDGET_ENABLED`, `DEFAULT_DAILY_TOKEN_LIMIT`, `DEFAULT_MONTHLY_TOKEN_LIMIT` |
| Web Search | `WEB_SEARCH_ENABLED`, `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_API_KEY` |
| App | `APP_ENV`, `FRONTEND_URL` |
