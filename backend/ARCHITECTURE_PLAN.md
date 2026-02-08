# Inkwest Enhancement Architecture Plan

## Overview

This document outlines the architecture for major feature additions:
1. OIDC Authentication with Role-to-Kubeconfig Mapping
2. Budget Management System with Admin Interface
3. Interactive Flow with User Confirmation (Claude Code-like experience)
4. Web Search for Documentation with Source Validation
5. MCP (Model Context Protocol) Integration for Extended Context

## Implementation Status

| Feature             | Status | Files Created                                                                                  |
| ------------------- | ------ | ---------------------------------------------------------------------------------------------- |
| OIDC Auth           | ✅ Done | `core/auth.py`, `api/routes/auth.py`, `models/user.py`, `models/role_mapping.py`               |
| Budget System       | ✅ Done | `models/budget.py`, `services/budget_service.py`, `services/pricing.py`, `api/routes/admin.py` |
| Interactive Flow    | ✅ Done | `services/events.py` (extended), `models/session.py` (ConfirmationMode)                        |
| Web Search          | ✅ Done | `services/web_search.py`                                                                       |
| MCP Integration     | ✅ Done | `services/mcp_client.py`                                                                       |
| Admin Frontend      | ✅ Done | `frontend/src/pages/admin/*`, `frontend/src/store/admin.ts`                                    |
| OpenRouter Provider | ✅ Done | `services/orchestrator_openrouter.py`                                                          |
| Tests               | ✅ Done | 234 tests passing (77 new tests for enterprise features)                                       |

---

## 1. OIDC Authentication System

### Components

```
app/
├── core/
│   └── auth.py              # OIDC client, token validation, role extraction
├── models/
│   ├── user.py              # User model with OIDC subject, roles
│   └── role_kubeconfig.py   # Role-to-kubeconfig mapping
├── api/
│   ├── middleware/
│   │   └── auth.py          # Authentication middleware
│   └── routes/
│       └── auth.py          # Login callback, user info
└── services/
    └── user_service.py      # User CRUD, role management
```

### Database Models

```python
# User
- id: UUID (PK)
- oidc_subject: str (unique)  # sub claim from OIDC
- email: str
- display_name: str
- roles: list[str]            # ["admin", "user", "viewer"]
- is_active: bool
- last_login: datetime
- created_at, updated_at

# RoleKubeconfigMapping
- id: UUID (PK)
- role_name: str (unique)
- allowed_clusters: list[UUID]  # FK to clusters
- kubeconfig_override: str | None  # Optional override path
- namespaces: list[str] | None    # Namespace restrictions
- created_at, updated_at
```

### OIDC Flow

1. Frontend redirects to `/api/auth/login`
2. Backend redirects to OIDC provider
3. Provider redirects to `/api/auth/callback` with code
4. Backend exchanges code for tokens
5. Backend extracts roles from token claims (configurable claim path)
6. Backend creates/updates user, returns JWT for session

### Role-Based Access

| Role     | Permissions                                 |
| -------- | ------------------------------------------- |
| admin    | Full access, manage budgets, view all users |
| operator | Access all clusters, view usage stats       |
| user     | Access mapped clusters only                 |
| viewer   | Read-only access to assigned clusters       |

---

## 2. Budget Management System

### Components

```
app/
├── models/
│   ├── budget.py            # Global and per-user budgets
│   └── usage.py             # Usage tracking records
├── api/routes/
│   └── admin.py             # Admin-only budget management
├── services/
│   ├── budget_service.py    # Budget CRUD, enforcement
│   └── pricing.py           # Model pricing calculations
```

### Database Models

```python
# Budget
- id: UUID (PK)
- name: str
- budget_type: enum("global", "user", "role")
- target_id: UUID | None      # user_id or null for global
- max_tokens_daily: int | None
- max_tokens_monthly: int | None
- max_cost_daily: Decimal | None
- max_cost_monthly: Decimal | None
- soft_limit_percentage: int  # Warn at this % (default 80)
- is_active: bool
- created_at, updated_at

# UsageRecord
- id: UUID (PK)
- user_id: UUID (FK)
- session_id: UUID (FK)
- input_tokens: int
- output_tokens: int
- model: str
- cost_usd: Decimal
- recorded_at: datetime
```

### Model Pricing (Configurable)

```python
MODEL_PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},  # per 1M tokens
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
    "gpt-5-codex": {"input": 5.00, "output": 15.00},
}
```

### Admin API Endpoints

```
POST   /api/admin/budgets           # Create budget
GET    /api/admin/budgets           # List all budgets
GET    /api/admin/budgets/{id}      # Get budget details
PUT    /api/admin/budgets/{id}      # Update budget
DELETE /api/admin/budgets/{id}      # Delete budget
GET    /api/admin/usage             # Usage report (filterable)
GET    /api/admin/usage/summary     # Aggregated usage stats
```

### Budget Enforcement

1. **Pre-check**: Before API call, check remaining budget
2. **Soft limit**: Emit warning event at 80% (configurable)
3. **Hard limit**: Reject with error at 100%
4. **Grace period**: Allow current request to complete

---

## 3. Interactive Flow with User Confirmation

### New Event Types

```python
# Existing
text_delta, tool_start, tool_result, done, error, thinking

# New
reasoning        # AI's reasoning process (why it's choosing actions)
tool_proposal    # Proposed tool call awaiting user approval
confirm_request  # Request user confirmation (cost, continue, source)
status_update    # Progress update (iteration count, tokens used)
```

### WebSocket Protocol Enhancement

```python
# Server -> Client
{
    "type": "tool_proposal",
    "proposal_id": "uuid",
    "tool_name": "kubectl_get",
    "tool_input": {"resource": "pods"},
    "reasoning": "I need to check pod status to diagnose the issue",
    "estimated_cost": 0.002,
    "awaiting_approval": true
}

# Client -> Server
{
    "type": "user_response",
    "proposal_id": "uuid",
    "action": "approve" | "reject" | "modify",
    "modified_input": {...}  # Optional
}

# Server -> Client (after approval)
{
    "type": "tool_start",
    "tool_name": "kubectl_get",
    ...
}
```

### Confirmation Modes

```python
class ConfirmationMode(Enum):
    NONE = "none"           # Fully automatic (current behavior)
    TOOLS_ONLY = "tools"    # Confirm each tool call
    EXPENSIVE = "expensive" # Confirm when cost > threshold
    ITERATIONS = "iterations" # Confirm every N iterations
    ALL = "all"             # Confirm everything
```

### Session Settings

```python
# Per-session configuration (stored in session model)
- confirmation_mode: ConfirmationMode
- cost_confirmation_threshold: Decimal  # e.g., $0.10
- iteration_confirmation_interval: int  # e.g., every 5 iterations
- auto_approve_tools: list[str]        # Tools that don't need approval
```

---

## 4. Web Search for Documentation

### Components

```
app/
├── tools/
│   └── web_search.py        # Web search tool definition
├── services/
│   └── web_search_service.py # Search execution, source validation
```

### Tool Definition

```python
{
    "name": "web_search",
    "description": "Search the web for Kubernetes documentation and troubleshooting guides",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "source_filter": {
                "type": "string",
                "enum": ["official", "community", "all"],
                "description": "Filter by source type"
            }
        },
        "required": ["query"]
    }
}
```

### Trusted Sources (Configurable)

```python
OFFICIAL_SOURCES = [
    "kubernetes.io",
    "docs.docker.com",
    "helm.sh",
    "prometheus.io",
    "grafana.com/docs",
]

COMMUNITY_SOURCES = [
    "stackoverflow.com",
    "github.com",
    "medium.com",
    "dev.to",
]

BLOCKED_SOURCES = [
    # Configurable blocklist
]
```

### Source Validation Flow

1. AI proposes web search
2. User sees proposed query and source filter
3. Results returned with source classification
4. User can approve/reject specific sources
5. AI only sees approved content

---

---

## 5. MCP (Model Context Protocol) Integration

### Overview

MCP integration allows Inkwest to connect to external context providers,
significantly expanding the debugging capabilities beyond just kubectl.

### Supported MCP Servers

| Server             | Use Case                      | Key Tools                                                                    |
| ------------------ | ----------------------------- | ---------------------------------------------------------------------------- |
| **AWS MCP**        | EKS, CloudWatch, ECR          | `eks_describe_cluster`, `cloudwatch_get_log_events`, `ecr_get_scan_findings` |
| **GCP MCP**        | GKE, Cloud Logging            | `gke_describe_cluster`, `logging_list_entries`                               |
| **Azure MCP**      | AKS, Monitor                  | `aks_describe_cluster`, `monitor_query_logs`                                 |
| **GitHub MCP**     | Repos, Actions, PRs           | `github_get_file_contents`, `github_list_workflow_runs`                      |
| **GitLab MCP**     | Projects, Pipelines, Registry | `gitlab_get_pipeline`, `gitlab_get_job_log`, `gitlab_list_registry_tags`     |
| **Prometheus MCP** | Metrics, Alerts               | `prometheus_query`, `prometheus_list_alerts`                                 |
| **Datadog MCP**    | APM, Logs, Traces             | `datadog_search_logs`, `datadog_list_traces`                                 |
| **Vault MCP**      | Secret metadata               | `vault_list_secrets`, `vault_get_secret_metadata`                            |

### Debug Synergy Examples

**1. Pod CrashLoopBackOff Investigation**
```
kubectl (events, logs) + Datadog MCP (traces) + GitLab MCP (recent commits) + ECR MCP (image scan)
```

**2. Ingress/DNS Issues**
```
kubectl (ingress, service) + AWS MCP (Route53, ALB health) + Prometheus MCP (latency metrics)
```

**3. Deployment Rollback Analysis**
```
kubectl (rollout history) + GitHub MCP (PR that caused issue) + Datadog MCP (error spike correlation)
```

### Configuration

```python
# Environment variable: MCP_SERVERS (JSON)
[
  {
    "name": "aws",
    "type": "aws",
    "endpoint": "http://localhost:3000",
    "api_key": "...",
    "read_only": true
  },
  {
    "name": "gitlab",
    "type": "gitlab",
    "endpoint": "http://localhost:3001",
    "api_key": "glpat-...",
    "read_only": true
  }
]
```

### Security: Read-Only by Default

All MCP servers are configured as **read-only** by default:
- Write operations (create, update, delete) are blocked
- Allowed tools can be explicitly whitelisted
- Admin can enable write access per-server if needed

---

## Implementation Order

1. **Phase 1: OIDC Foundation** (enables user identification) ✅
   - User model
   - Basic OIDC flow
   - Role extraction

2. **Phase 2: Budget System** (requires user identification) ✅
   - Budget models
   - Usage tracking
   - Admin API

3. **Phase 3: Interactive Flow** (core experience enhancement) ✅
   - New event types
   - WebSocket protocol update
   - Orchestrator modifications

4. **Phase 4: Web Search** (extends tool capabilities) ✅
   - Search tool
   - Source validation
   - Integration

5. **Phase 5: MCP Integration** (extended context) ✅
   - MCP client
   - Server templates
   - Tool routing

6. **Phase 6: Admin UI** (frontend) ✅
   - Budget management page
   - User management page
   - Role mappings page
   - Usage reports page
   - Dashboard with stats

7. **Phase 7: OpenRouter Provider** ✅
   - OpenRouter orchestrator
   - Unified API for multiple AI providers
   - Model pricing support

8. **Phase 8: Tests & Documentation** ✅
   - 234 tests (77 new for enterprise features)
   - Architecture documentation
   - .env.example updated

---

## Configuration Additions

```python
# OIDC
oidc_enabled: bool = False
oidc_issuer_url: str | None = None
oidc_client_id: str | None = None
oidc_client_secret: str | None = None
oidc_roles_claim: str = "roles"  # Path in token to extract roles
oidc_redirect_uri: str | None = None

# Budget
budget_enabled: bool = False
default_daily_token_limit: int = 100000
default_monthly_token_limit: int = 2000000

# Interactive
default_confirmation_mode: str = "expensive"
default_cost_threshold: float = 0.10

# Web Search
web_search_enabled: bool = False
web_search_api_key: str | None = None  # e.g., SerpAPI, Brave Search
trusted_sources: list[str] = []
blocked_sources: list[str] = []

# OpenRouter (unified API for multiple AI providers)
openrouter_api_key: str | None = None
openrouter_model: str = "anthropic/claude-sonnet-4"
openrouter_base_url: str = "https://openrouter.ai/api/v1"
```

---

## 6. OpenRouter Integration

### Overview

OpenRouter provides a unified API compatible with OpenAI's format, allowing access
to multiple AI providers through a single endpoint. This eliminates the need to
maintain separate integrations for each provider.

### Supported Providers via OpenRouter

| Provider      | Models                                            | Use Case                             |
| ------------- | ------------------------------------------------- | ------------------------------------ |
| **Anthropic** | claude-opus-4, claude-sonnet-4, claude-3.5-sonnet | Best for complex reasoning, tool use |
| **OpenAI**    | gpt-4o, gpt-4-turbo, gpt-4o-mini                  | Good general purpose, fast           |
| **Google**    | gemini-pro-1.5, gemini-flash-1.5                  | Long context windows                 |
| **Meta**      | llama-3.1-405b, llama-3.1-70b                     | Open-source, cost-effective          |
| **Mistral**   | mistral-large                                     | European provider, fast              |

### Configuration

```python
# .env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4

# Optional
OPENROUTER_SITE_URL=https://your-site.com  # For rankings
OPENROUTER_APP_NAME=Inkwest
```

### Model Selection

You can switch models by changing `OPENROUTER_MODEL`:

```bash
# Anthropic Claude
OPENROUTER_MODEL=anthropic/claude-sonnet-4
OPENROUTER_MODEL=anthropic/claude-opus-4

# OpenAI GPT
OPENROUTER_MODEL=openai/gpt-4o
OPENROUTER_MODEL=openai/gpt-4o-mini

# Google Gemini
OPENROUTER_MODEL=google/gemini-pro-1.5
OPENROUTER_MODEL=google/gemini-flash-1.5

# Meta Llama
OPENROUTER_MODEL=meta-llama/llama-3.1-405b-instruct

# Mistral
OPENROUTER_MODEL=mistralai/mistral-large
```

### Pricing

OpenRouter passes through provider pricing with a small markup. Inkwest's
pricing service includes OpenRouter-format model names for accurate cost tracking.

---

## 7. Admin Frontend

### Pages

| Page              | Path             | Features                                    |
| ----------------- | ---------------- | ------------------------------------------- |
| **Dashboard**     | `/admin`         | Usage stats, active users, budget overview  |
| **Budgets**       | `/admin/budgets` | CRUD for budgets, enable/disable            |
| **Users**         | `/admin/users`   | View users, edit roles, activate/deactivate |
| **Role Mappings** | `/admin/roles`   | Map OIDC roles to cluster access            |
| **Usage Reports** | `/admin/usage`   | Token/cost breakdown by user, model, time   |

### Access Control

Admin routes require the `admin` role (configurable via `OIDC_ADMIN_ROLE`).
The sidebar shows an "Admin Panel" link only for admin users.

### Components

- Reusable UI components: Button, Input, Select, Card, Table, Badge, Modal
- Zustand stores for state management
- TypeScript types for admin entities
