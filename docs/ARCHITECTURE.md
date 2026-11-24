# Architecture

## Overview

Inkwest is an AI-powered investigation portal. Users interact with an AI assistant via a chat interface that brokers scoped, read-only access across connectors — Kubernetes (kubectl), AWS, and GitLab — plus Coralogix observability per conversation via MCP, to investigate and diagnose issues, search documentation, and correlate findings across systems. Kubernetes is one connector among several, not the portal's sole scope.

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

## Engines

A turn is driven by an *engine* (the agentic loop), not by an inference-provider
setting — there is no `AI_PROVIDER`. Enabled engines come from `AGENTIC_ENGINES`
(CSV, default `claude-cli`):

| Engine | Orchestrator | Use Case |
|---|---|---|
| `claude-cli` | `orchestrator_cli.py` | Claude Code CLI subprocess (Claude models); dev-local needs no sandbox |
| `inkwest-agent` | `orchestrator_inkwest_agent.py` | Sandboxed Bedrock Converse loop (all vendors) |

The inference backend is whatever the CLI / sandbox is configured to talk to (an
Anthropic API key, or AWS Bedrock via bearer/creds). `get_orchestrator()` in
`ai_client.py` returns a single per-turn dispatcher that picks the engine from the
resolved model and any session pin.

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
    ai_client.py                    # get_orchestrator(): per-turn engine dispatcher
    orchestrator_cli.py             # Claude CLI subprocess engine
    orchestrator_inkwest_agent.py   # Sandboxed Bedrock Converse engine
    orchestrator_opencode.py        # Sandboxed opencode CLI engine
    compaction.py        # Context compaction: archive, summary turn, boundary row
    context_window.py    # Per-model context-window table (the gauge's denominator)
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
| `retry` | client -> server | Re-run the last user message (replaces its failed answer) |
| `compact` | client -> server | Summarize the conversation so far and start a new context boundary (**owner only**) |
| `stop` | client -> server | Cancel current AI processing |
| `typing` | client -> server | Typing indicator, fanned out to the other participants |
| `ping` / `pong` | bidirectional | Keepalive |
| `text_delta` | server -> client | AI text chunk |
| `tool_start` | server -> client | Tool execution starting |
| `tool_result` | server -> client | Tool output |
| `context_compacted` | server -> client | A compaction boundary was written (row id, timestamp, and the persisted row content) |
| `done` | server -> client | Response complete (metadata carries the turn's cumulative token counts and `context_tokens`, the last model call's prompt for the context gauge, when the engine reported any (a stopped inkwest-agent turn and the no-terminal fallback carry only `stopped`); `stopped: true` if cancelled) |
| `error` | server -> client | Error occurred |

`message`, `retry` and `compact` share one gate (`start_gated_turn`): turn-rate limit → budget check → one-turn-per-session slot. Role gating (`session_access.turn_refusal` — viewers may not drive a turn, and only the owner may compact) happens earlier, in the receive loop. Compaction (`services/compaction.py`) runs a normal billed turn through the session's resolved engine, dumps the pre-boundary transcript to `/workdir/.inkwest/history-<n>.md`, and commits a `role='system'` boundary row; from there `get_session_messages` replays only what follows it, and the summary rides the system prompt. The UI still renders the whole history, with a divider at the boundary.

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
- Connector tools (AWS, GitLab) are unaffected and fully usable.

### Prompt prefill links

```
/#prompt=<form-encoded text>
```

- Opens the new-conversation composer with the text typed in. Never sends: the user reviews, edits
  and presses send; a "Prefilled from a link" notice shows above the composer.
- Encoding: `application/x-www-form-urlencoded`, produced by `encodeURIComponent` (JS),
  `urllib.parse.quote(text, safe='')` (Python), `url.QueryEscape` (Go). A raw `+` is a space; a
  literal plus is `%2B`.
- Survives the OIDC login round-trip (same tab, up to 10 minutes). Only on `/`; other fragment keys
  are kept.
- The fragment never reaches the server: no server/ALB logs, no request-line size limit.

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

### Engine-Dispatched Orchestration

The `get_orchestrator()` factory in `ai_client.py` returns a single async generator that resolves the engine per turn (from the session's model + any pin, against `AGENTIC_ENGINES`):

| Engine | Orchestrator | Notes |
|---|---|---|
| `claude-cli` | `orchestrator_cli.py` | Claude Code CLI subprocess; also the dev-local fallback (no sandbox) |
| `inkwest-agent` | `orchestrator_inkwest_agent.py` | Sandboxed Bedrock Converse loop for all vendors |

Both orchestrators yield `ChatEvent` objects for WebSocket streaming. `orchestrator_cli.py` accepts an optional `cancel_event: asyncio.Event` for mid-stream cancellation, checked at loop boundaries; the sandboxed agent cancels by-kill (the broker terminates the in-container process).

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
| Inference / Engines | `AGENTIC_ENGINES`, `ANTHROPIC_API_KEY`, `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`, `CLAUDE_CLI_PATH`, `CLAUDE_CLI_MODELS` |
| Auth | `OIDC_ENABLED`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SECRET_KEY` |
| Kubernetes | `KUBECONFIG_DIR`, `KUBECTL_TIMEOUT` |
| Budget | `BUDGET_ENABLED`, `DEFAULT_DAILY_TOKEN_LIMIT`, `DEFAULT_MONTHLY_TOKEN_LIMIT` |
| Web Search | `WEB_SEARCH_ENABLED`, `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_API_KEY` |
| App | `APP_ENV`, `FRONTEND_URL` |
