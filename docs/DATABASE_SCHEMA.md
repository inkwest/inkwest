# Database Schema

## Overview

Inkwest uses PostgreSQL for persistent storage. All tables reside in the `inkwest` schema. Migrations are managed with `db_migrate.py` using dbmate-style SQL files in `backend/migrations/`.

## Entity Relationship Diagram

```
┌─────────────────────┐       ┌─────────────────────┐
│       users         │       │      clusters       │
├─────────────────────┤       ├─────────────────────┤
│ id (PK)             │       │ id (PK)             │
│ oidc_subject        │       │ name                │
│ oidc_issuer         │       │ display_name        │
│ email               │       │ kubeconfig_path     │
│ display_name        │       │ context_name        │
│ roles (text[])      │       │ description         │
│ is_active           │       │ is_active           │
│ last_login          │       │ created_at          │
│ created_at          │       │ updated_at          │
│ updated_at          │       └─────────────────────┘
└─────────────────────┘                │
         │                             │
         │                             │
         ▼                             │
┌─────────────────────┐                │
│ role_kubeconfig_    │                │
│    mappings         │                │
├─────────────────────┤                │
│ id (PK)             │                │
│ role_name (UNIQUE)  │                │
│ allowed_clusters    │                │
│ allowed_namespaces  │                │
│ denied_namespaces   │                │
│ daily_token_budget  │                │
└─────────────────────┘                │
                                       │
         ┌─────────────────────────────┘
         │
         ▼
┌─────────────────────┐       ┌─────────────────────┐
│      sessions       │       │      messages       │
├─────────────────────┤       ├─────────────────────┤
│ id (PK)             │──────▶│ id (PK)             │
│ user_id (FK)        │       │ session_id (FK)     │
│ cluster_id (FK)     │       │ role                │
│ title               │       │ content (JSONB)     │
│ summary             │       │ tool_calls (JSONB)  │
│ is_archived         │       │ status              │
│ total_input_tokens  │       │ input_tokens        │
│ total_output_tokens │       │ output_tokens       │
│ total_cache_read_…  │       │ cache_read_tokens   │
│ total_cache_write_… │       │ cache_write_tokens  │
│ total_tool_calls    │       │ is_retryable        │
│ total_iterations    │       │ retry_count         │
│ cli_session_id      │       │ created_at          │
│ confirmation_mode   │       └─────────────────────┘
│ created_at          │
│ updated_at          │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐       ┌─────────────────────┐
│    usage_records    │       │      budgets        │
├─────────────────────┤       ├─────────────────────┤
│ id (PK)             │       │ id (PK)             │
│ user_id             │       │ name                │
│ session_id          │       │ budget_type         │
│ input_tokens        │       │ target_id           │
│ output_tokens       │       │ target_role         │
│ cache_read_tokens   │       │ max_total_tokens    │
│ cache_write_tokens  │       │ max_cost_usd        │
│ source              │       │ period              │
│ turn_id             │       │ is_active           │
│ model               │       └─────────────────────┘
│ provider            │
│ cost_usd            │
│ recorded_at         │
└─────────────────────┘
```

## Tables

### clusters

Kubernetes cluster configurations available for debugging.

```sql
CREATE TABLE inkwest.clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    kubeconfig_path VARCHAR(1024) NOT NULL,
    context_name VARCHAR(255),
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### prompt_templates

Reusable, user-editable prompt templates surfaced as a palette in the chat.
Global (no per-user ownership); `content` is plain text and may contain literal
fill-in blanks (e.g. `__________`) edited in the input before sending — there is
no server-side templating or placeholder substitution.

```sql
CREATE TABLE inkwest.prompt_templates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### system_prompts

Named SYSTEM prompts managed by admins — the prompt loaded before any
user interaction. Global (no per-user ownership); exactly one row is the default
applied to non-admins. `content` may contain `{cluster_name}` / `{namespaces}`
blanks filled in at run time. The default is seeded at startup from
`app/services/prompts.py::SYSTEM_PROMPT`.

```sql
CREATE TABLE inkwest.system_prompts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- At most one default at any time.
CREATE UNIQUE INDEX idx_system_prompts_single_default
    ON inkwest.system_prompts(is_default) WHERE is_default = true;
```

### sessions

Chat sessions, each bound to a specific cluster and user. A session also records
the system prompt chosen at creation: `system_prompt_id` (admin
selection) and `system_prompt_custom` (admin free-text). Both null → the global
default is resolved at run time.

```sql
CREATE TABLE inkwest.sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    cluster_id UUID REFERENCES inkwest.clusters(id) ON DELETE CASCADE,  -- nullable: session may have no Kube context
    system_prompt_id UUID REFERENCES inkwest.system_prompts(id) ON DELETE SET NULL,  -- admin-selected prompt
    system_prompt_custom TEXT,  -- admin free-text prompt for this session
    title VARCHAR(255),
    summary TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT false,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,   -- FRESH (uncached) input
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,   -- prompt-cache reads (input-side)
    total_cache_write_tokens INTEGER NOT NULL DEFAULT 0,  -- prompt-cache writes (input-side)
    total_tool_calls INTEGER NOT NULL DEFAULT 0,
    total_iterations INTEGER NOT NULL DEFAULT 0,
    cli_session_id VARCHAR(50),
    confirmation_mode VARCHAR(50) NOT NULL DEFAULT 'expensive',
    cost_confirmation_threshold INTEGER NOT NULL DEFAULT 10,
    iteration_confirmation_interval INTEGER NOT NULL DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### messages

Individual messages within a session (user inputs, AI responses, tool results).

```sql
CREATE TABLE inkwest.messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES inkwest.sessions(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL,
    content JSONB NOT NULL,
    tool_calls JSONB,
    tool_call_id VARCHAR(255),
    token_count INTEGER,
    input_tokens INTEGER,      -- FRESH (uncached) input for the turn
    output_tokens INTEGER,
    cache_read_tokens INTEGER,   -- prompt-cache reads (NULL on rows predating the column)
    cache_write_tokens INTEGER,  -- prompt-cache writes (NULL on rows predating the column)
    thinking_content TEXT,
    latency_ms INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    error_message TEXT,
    is_retryable BOOLEAN NOT NULL DEFAULT false,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### users

User accounts linked to OIDC identity provider.

```sql
CREATE TABLE inkwest.users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    oidc_subject VARCHAR(255) NOT NULL UNIQUE,
    oidc_issuer VARCHAR(512) NOT NULL,
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    roles TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### budgets

Token/cost limits per user, role, or global.

```sql
CREATE TABLE inkwest.budgets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    description VARCHAR(1000),
    budget_type VARCHAR(50) NOT NULL,  -- global, user, role
    target_id UUID,                     -- user ID for user budgets
    target_role VARCHAR(100),           -- role name for role budgets
    max_input_tokens INTEGER,
    max_output_tokens INTEGER,
    max_total_tokens INTEGER,
    max_cost_usd NUMERIC(10, 4),
    period VARCHAR(50) NOT NULL DEFAULT 'daily',  -- hourly, daily, weekly, monthly
    soft_limit_percent INTEGER NOT NULL DEFAULT 80,
    hard_limit_action VARCHAR(50) NOT NULL DEFAULT 'block',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### usage_records

Per-request usage tracking for budget enforcement and reporting.

```sql
CREATE TABLE inkwest.usage_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID,
    session_id UUID NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,          -- FRESH (uncached) input
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,      -- prompt-cache reads (billed ~0.1x)
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,     -- prompt-cache writes (billed ~1.25x)
    source VARCHAR(16) NOT NULL DEFAULT 'engine',      -- accounting authority: engine | broker
    turn_id UUID,                                      -- per-turn rollup key (indexed; NULL pre-cutover)
    model VARCHAR(100) NOT NULL,
    provider VARCHAR(50) NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_usage_records_turn_id ON inkwest.usage_records (turn_id);
-- One engine-side budget row per turn (DB backstop for the ambiguous-commit retry race;
-- broker rows are N per turn and pre-cutover rows have turn_id NULL — both excluded).
CREATE UNIQUE INDEX uq_usage_records_engine_turn ON inkwest.usage_records (turn_id)
    WHERE source = 'engine' AND turn_id IS NOT NULL;
```

### role_kubeconfig_mappings

Maps OIDC roles to cluster access and namespace restrictions.

```sql
CREATE TABLE inkwest.role_kubeconfig_mappings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500),
    allowed_clusters UUID[] NOT NULL DEFAULT '{}',
    kubeconfig_override VARCHAR(1024),
    allowed_namespaces TEXT[] NOT NULL DEFAULT '{}',
    denied_namespaces TEXT[] NOT NULL DEFAULT '{}',
    can_web_search BOOLEAN NOT NULL DEFAULT false,
    daily_token_budget INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### oidc_pending_states

Temporary OIDC login states for CSRF protection. Rows are consumed atomically on callback and expired rows are cleaned up by a background task every 60s.

```sql
CREATE TABLE inkwest.oidc_pending_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_oidc_pending_states_created ON inkwest.oidc_pending_states(created_at);
```

### app_settings

Runtime-tunable scalar settings managed from the admin console (e.g. `bedrock_include_open`). Simple key/value store; loaded into memory at startup and refreshed on write. See `app.services.app_settings`.

```sql
CREATE TABLE inkwest.app_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### model_registry

Admin-managed catalog of selectable models: which models users may pick, the default, per-model metadata (endpoint/arn/options), and pricing overrides. Discovered provider models are upserted as `source='discovered'`; operators add `source='custom'` rows for models the discovery filter doesn't surface (e.g. non-Anthropic Bedrock vendor models). Replaces the former `anthropic_model` / `enabled_models` keys in `app_settings`. See `app.services.model_registry`.

```sql
CREATE TABLE inkwest.model_registry (
    model_id VARCHAR(512) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,           -- per-vendor: anthropic|amazon|meta|mistral|cohere|ai21|deepseek|stability|claude-cli (+ legacy bedrock|bedrock-open)
    display_name VARCHAR(255),               -- NULL = derived from the ID
    endpoint VARCHAR(1024),                  -- optional custom inference base URL
    arn VARCHAR(1024),                       -- optional Bedrock inference-profile / imported-model ARN
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_price NUMERIC(12, 4),              -- per-Mtok override; NULL = fall back to the pricing table
    output_price NUMERIC(12, 4),
    enabled BOOLEAN NOT NULL DEFAULT false,
    is_default BOOLEAN NOT NULL DEFAULT false,
    source VARCHAR(16) NOT NULL DEFAULT 'discovered',  -- discovered | custom
    legacy BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT model_registry_source_check CHECK (source IN ('discovered', 'custom')),
    CONSTRAINT model_registry_price_pair CHECK ((input_price IS NULL) = (output_price IS NULL))
);
-- At most one default at a time; fast lookup of the enabled subset.
CREATE UNIQUE INDEX idx_model_registry_default ON inkwest.model_registry (is_default) WHERE is_default;
CREATE INDEX idx_model_registry_enabled ON inkwest.model_registry (enabled) WHERE enabled;
```

### OAuth2 MCP brokering

See `docs/MCP.md` and `docs/mcp-oauth2-proxy.md`. Token MATERIAL is never stored in these
tables: per-user tokens are AES-256-GCM ciphertext (`mcp_user_tokens.blob`/`blob_iv`) under a
per-user DEK whose **browser-wrapped** form is the only key-related data at rest
(`mcp_user_keys.wrapped_dek`) — the unwrapping key never leaves the browser.

```sql
-- DCR results + discovered endpoints, one row per oauth catalog key
CREATE TABLE inkwest.mcp_oauth_clients (
    mcp_key             VARCHAR(255) PRIMARY KEY,
    issuer              TEXT NOT NULL,
    token_endpoint      TEXT NOT NULL,
    revocation_endpoint TEXT,
    client_id           TEXT NOT NULL,
    client_secret_ref   TEXT,           -- nullable (public/PKCE client)
    client_expires_at   TIMESTAMPTZ,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- per-user grant (status: active | expired | revoked); blob = encrypted token bundle
CREATE TABLE inkwest.mcp_user_tokens (
    user_id    UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    mcp_key    VARCHAR(255) NOT NULL,
    vault_ref  TEXT NOT NULL,
    blob       BYTEA,
    blob_iv    BYTEA,
    expires_at TIMESTAMPTZ,
    scopes     TEXT,
    status     VARCHAR(20) NOT NULL DEFAULT 'active',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, mcp_key)
);

-- single-use OAuth handshake states (mirrors connector_pending_states)
CREATE TABLE inkwest.mcp_oauth_pending_states (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state         VARCHAR(255) NOT NULL UNIQUE,
    code_verifier VARCHAR(128) NOT NULL,
    mcp_key       VARCHAR(255) NOT NULL,
    user_id       UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_mcp_oauth_pending_created ON inkwest.mcp_oauth_pending_states(created_at);

-- browser-wrapped per-user DEK (audit-doc Phase C key custody)
CREATE TABLE inkwest.mcp_user_keys (
    user_id     UUID PRIMARY KEY REFERENCES inkwest.users(id) ON DELETE CASCADE,
    wrapped_dek BYTEA NOT NULL,
    wrap_alg    VARCHAR(32) NOT NULL,
    wrap_iv     BYTEA,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MCP grant events are user-level (no conversation): session_id relaxed to nullable.
ALTER TABLE inkwest.connector_audit_log ALTER COLUMN session_id DROP NOT NULL;
```

## JSONB Content Examples

### User Message Content

Simple text:
```json
"Why is my nginx pod crashing?"
```

### Assistant Message Content

Text with tool calls:
```json
[
    {
        "type": "text",
        "text": "I'll check the pod status and recent events."
    },
    {
        "type": "tool_use",
        "id": "toolu_01ABC123",
        "name": "kubectl_get",
        "input": {
            "resource": "pods",
            "namespace": "default",
            "selector": "app=nginx"
        }
    }
]
```

### Tool Result Content

```json
[
    {
        "type": "tool_result",
        "tool_use_id": "toolu_01ABC123",
        "content": "NAME                     READY   STATUS             RESTARTS   AGE\nnginx-7d9b8c8b4-x2k3j   0/1     CrashLoopBackOff   5          10m"
    }
]
```

## Migrations

Migrations use dbmate-style SQL files in `backend/migrations/`:

```bash
# Apply all pending migrations
cd backend && python db_migrate.py

# Or via Docker
docker compose exec backend python db_migrate.py
```

> The list below is illustrative and partly historical (some early filenames were
> renamed/reorganized). The authoritative, ordered set is the contents of
> `backend/migrations/`.

| Migration | Purpose |
|-----------|---------|
| `20260127000001_initial_schema.sql` | Base tables: clusters, sessions, messages |
| `20260128000001_enterprise_features.sql` | Users, budgets, usage tracking, session stats |
| `20260128000002_messages_columns.sql` | Message token tracking, thinking, latency |
| `20260128000003_message_retry.sql` | Message status, retry support |
| `20260128000004_cli_session_id.sql` | Claude CLI session resumption |
| `20260129000001_users_oidc_columns.sql` | OIDC subject/issuer, roles as text[] |
| `20260129000002_align_admin_models.sql` | Budget/role mapping redesign |
| `20260130000001_align_constraints.sql` | Constraint tightening, wider kubeconfig_path |
| `20260130000002_add_hourly_budget_period.sql` | Hourly budget period support |
| `20260205000001_oidc_pending_states.sql` | DB-backed OIDC login state storage |
| _… intervening migrations (connectors, skills, system prompts, app_settings, session model) — see `backend/migrations/`_ | |
| `20260612140000_mcp_oauth.sql` | OAuth2 MCP brokering: `mcp_oauth_clients`, `mcp_user_tokens`, `mcp_oauth_pending_states`, `mcp_user_keys`; relaxes `connector_audit_log.session_id` to nullable (user-level MCP grant events). **Down-path deletes all `session_id IS NULL` audit rows** |
| `20260619000000_model_registry.sql` | Admin-managed model registry (replaces the model keys in app_settings) |
| `20260704000000_usage_cache_tokens.sql` | usage_records: prompt-cache columns, source, indexed turn_id (budget accuracy) |
| `20260704010000_message_session_cache_tokens.sql` | messages/sessions: prompt-cache columns for per-bubble/badge surfacing |

## Key Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| clusters | idx_clusters_active | Fast lookup of active clusters |
| sessions | idx_sessions_user | List sessions by user |
| sessions | idx_sessions_cluster | List sessions by cluster |
| sessions | idx_sessions_updated | Recent sessions first |
| messages | ix_messages_session_created | Load conversation in order |
| messages | ix_messages_status | Failed message lookup |
| users | idx_users_oidc_subject | OIDC login lookup |
| usage_records | idx_usage_recorded_at | Usage reporting queries |
| usage_records | ix_usage_records_turn_id | Per-turn rollup (GROUP BY turn_id) |
| usage_records | uq_usage_records_engine_turn | Unique engine row per turn (partial; double-persist backstop) |
| oidc_pending_states | idx_oidc_pending_states_created | TTL cleanup of expired states |
