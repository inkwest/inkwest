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
│ model               │       │ max_total_tokens    │
│ provider            │       │ max_cost_usd        │
│ cost_usd            │       │ period              │
│ recorded_at         │       │ is_active           │
└─────────────────────┘       └─────────────────────┘
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

### sessions

Chat sessions, each bound to a specific cluster and user.

```sql
CREATE TABLE inkwest.sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES inkwest.users(id) ON DELETE SET NULL,
    cluster_id UUID REFERENCES inkwest.clusters(id) ON DELETE CASCADE,  -- nullable: session may have no Kube context
    title VARCHAR(255),
    summary TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT false,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
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
    input_tokens INTEGER,
    output_tokens INTEGER,
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
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    model VARCHAR(100) NOT NULL,
    provider VARCHAR(50) NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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
| oidc_pending_states | idx_oidc_pending_states_created | TTL cleanup of expired states |
