# Inkwest

AI-powered investigation assistant. Chat with an AI that brokers scoped, read-only access across Kubernetes, AWS, and GitLab — plus Coralogix observability via MCP — to help diagnose issues, explain configurations, and trace problems end to end.

## Why Inkwest?

Investigating a production issue rarely stays inside one system: it spans the Kubernetes workload, the AWS resources behind it, the GitLab pipeline that shipped it, and the logs in Coralogix. Getting hands-on access to each — VPN, kubectl, the right kubeconfig, AWS profiles, tokens — is real overhead, and per-developer local setup (MCP servers, CLI plugins) only multiplies it.

Inkwest removes that friction. A shared web interface that's always connected, already authenticated, and scoped to the right systems. Developers ask questions in plain language; the AI runs the right read-only queries across the granted connectors and explains what's going on. Kubernetes is one of those connectors, not the whole story.

It also solves the permissions problem. Instead of handing out broad access "just in case", Inkwest acts as a controlled read-only proxy. The AI only runs approved read operations, and access is managed through OIDC and role mappings — not by distributing kubeconfigs and credentials.

## Features

- **Conversational investigation**: Describe your problem in natural language
- **Cross-system access**: Kubernetes, AWS, and GitLab connectors plus Coralogix observability (logs/metrics/traces) via MCP — all scoped read-only per session
- **Intelligent tool use**: AI automatically runs relevant queries across the granted connectors
- **Session persistence**: Resume previous investigation sessions with rename/delete support
- **Multi-cluster support**: Switch between different Kubernetes contexts
- **Read-only safety**: No destructive operations allowed
- **Multiple AI providers**: Claude CLI (default), Anthropic API, AWS Bedrock
- **Admin dashboard**: Manage clusters, users, budgets, and usage reports
- **Enterprise features**: OIDC authentication, role-based access, budget management

### Chat UI Features

- **Inline tool execution**: Commands appear inline with reasoning text as they execute
- **Stop mid-stream**: Cancel AI processing at any point; partial response preserved with interruption notice
- **Smart tool cards**: Collapsible with description in title, auto-expand on run, auto-collapse on complete
- **Copy to clipboard**: One-click copy for commands and code blocks
- **Todo list display**: Task progress with visual counter (e.g., "3/6") and status icons (interrupted state shown in yellow)
- **Syntax highlighting**: Code blocks with language detection and copy button
- **Smart auto-scroll**: Scrolls with content unless user scrolled up to read history

## Architecture

```
┌─────────────────┐     ┌─────────────────────────────────┐     ┌─────────────────┐
│    React UI     │────▶│    Python Backend (FastAPI)     │────▶│   AI Provider   │
│  - Chat View    │     │    - AI Orchestration           │     │  - Claude CLI   │
│  - Admin Panel  │     │    - Tool Execution             │     │  - Anthropic    │
│  - Sessions     │     │    - Session Management         │     │  - Bedrock      │
└─────────────────┘     │    - Budget Tracking            │     └─────────────────┘
        │               └─────────────────────────────────┘
        │                           │          │
        │    WebSocket              │          │
        └───────────────────────────┘          │
                                               ▼
                                        ┌─────────────┐
                                        │   kubectl   │
                                        │   (tools)   │
                                        └─────────────┘
                                               │
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                   ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
                   │  PostgreSQL │      │  Kubernetes │      │   Web/MCP   │
                   │  (storage)  │      │  (clusters) │      │  (search)   │
                   └─────────────┘      └─────────────┘      └─────────────┘
```

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A Kubernetes cluster with kubectl access
- (Optional) Claude CLI for local AI or API keys for cloud providers

### Development Setup

```bash
# Clone the repository
git clone https://github.com/kubepanion/kubepanion.git
cd kubepanion

# Copy environment templates
cp .env.example .env
cp backend/.env.example backend/.env  # sandbox knobs; the sandbox stays off until you set it up
# Edit .env with your configuration (Claude CLI works by default!)

# Start all services
docker compose up -d

# Run database migrations
docker compose exec backend python db_migrate.py

# Access the UI
open http://localhost:3000

# Access admin panel (as admin user)
open http://localhost:3000/admin
```

### Configuration

Create a `.env` file:

```bash
# Inference runs through the Claude Code CLI (default) and, when enabled, the
# sandboxed inkwest-agent. There is no AI_PROVIDER: the backend each talks to is
# set by its own credentials. Execution mode is AGENTIC_ENGINES (default claude-cli).

# Anthropic API backend (default for the CLI)
# ANTHROPIC_API_KEY=sk-ant-...

# OR an AWS Bedrock backend (required when inkwest-agent is enabled)
# AWS_REGION=us-east-1
# AWS_ACCESS_KEY_ID=...
# AWS_SECRET_ACCESS_KEY=...
# AWS_BEARER_TOKEN_BEDROCK=...
# AGENTIC_ENGINES=claude-cli,inkwest-agent

# Database
DATABASE_URL=postgresql+asyncpg://inkwest:inkwest@localhost:5432/inkwest

# Kubeconfig directory (mount your kubeconfigs here)
KUBECONFIG_DIR=/path/to/kubeconfigs

# Optional: OIDC Authentication
# OIDC_ENABLED=true
# OIDC_ISSUER_URL=https://accounts.google.com
# OIDC_CLIENT_ID=...
# OIDC_CLIENT_SECRET=...

# Optional: Budget Management
# BUDGET_ENABLED=true
# DEFAULT_DAILY_TOKEN_LIMIT=100000
```

## AI Providers

| Provider      | Cost        | Setup            | Best For                  |
| ------------- | ----------- | ---------------- | ------------------------- |
| Claude CLI    | Free*       | Pre-installed    | Development, local use    |
| Anthropic     | Pay-per-use | API key          | Production, high volume   |
| AWS Bedrock   | Pay-per-use | AWS credentials  | AWS environments          |

*Claude CLI uses your existing Claude subscription: single-user local use only, a shared
deployment must use an API key or Bedrock (see [Claude Code terms](#claude-code-terms))

### Claude CLI Features

When using Claude CLI as the AI provider:
- **Session resumption**: Conversations persist across messages using `--resume`
- **Built-in tool execution**: Uses Claude's native tool calling via MCP
- **No API key needed**: Works with your existing Claude subscription (single-user only)

## Project Structure

```
kubepanion/
├── backend/                 # Python FastAPI backend
│   ├── app/
│   │   ├── api/             # API routes (REST + WebSocket)
│   │   ├── core/            # Configuration, security, telemetry
│   │   ├── models/          # SQLAlchemy models + Pydantic schemas
│   │   ├── services/        # Business logic (AI, budgets, etc.)
│   │   └── tools/           # kubectl tool definitions
│   └── migrations/          # Database migrations (dbmate-style SQL)
├── frontend/                # React TypeScript frontend
│   └── src/
│       ├── components/      # React components
│       │   ├── chat/        # Chat interface
│       │   ├── session/     # Session management
│       │   ├── layout/      # App layout
│       │   └── ui/          # Reusable UI components
│       ├── pages/           # Page components (admin, etc.)
│       ├── store/           # Zustand state management
│       ├── services/        # API clients
│       └── types/           # TypeScript types
├── docs/                    # Documentation
├── Dockerfile               # Combined production image (backend + built SPA)
├── docker-compose.yml       # Local development setup (hot reload, 2 containers)
└── docker-compose.prod.yml  # Production-style setup (single combined image)
```

## Deployment

Production ships as a **single combined image** built from the root `Dockerfile`:

- Stage 1 builds the frontend (`npm run build`) with `VITE_API_URL=""` / `VITE_WS_URL=""` → the SPA uses same-origin URLs
- Stage 2 is the FastAPI backend, with the built SPA baked into `/app/static`
- FastAPI serves everything on port 8000: SPA (with `index.html` fallback for client-side routes), REST API, WebSockets — plus gzip and the browser security headers nginx used to add
- Unknown `/api/*`, `/assets/*`, `/ws/*` paths return real 404s (no SPA fallback)

CI (`.github/workflows/ci.yml`) runs pre-commit + backend tests, then builds two images
with buildx (provenance + SBOM attestations, cosign keyless signatures): the combined
`ghcr.io/inkwest/inkwest` (root `Dockerfile`) and the sandbox agent
`ghcr.io/inkwest/inkwest/agent` (`agent/Dockerfile`, context `backend/`). PRs build
only; semver tags `vX.Y.Z` push, expanded to `vX.Y.Z`, `X.Y`, `X` and `latest`. Test
locally with [`act`](https://github.com/nektos/act) or plain `docker build .`.

Database migrations run separately (init container / Job pattern):

```bash
docker run --rm -e DATABASE_URL=... --entrypoint /app/entrypoint-migrate.sh <image>
```

Note: migrations create tables in the `inkwest` PostgreSQL schema. The app pins its
search_path to that schema at connection time (`DB_SCHEMA`, default `inkwest`), so no
role setup is required. The `ALTER ROLE <user> SET search_path = inkwest, public` step
is only needed for clients that reach the DB outside the app (e.g. manual `psql`).

Dev is unchanged: `docker-compose.yml` runs vite (hot reload) + uvicorn `--reload`
as two containers from `frontend/Dockerfile` and `backend/Dockerfile`.

## Available Tools

The AI can use these kubectl operations (read-only):

| Tool               | Description                                                 |
| ------------------ | ----------------------------------------------------------- |
| `kubectl_get`      | List/retrieve resources (pods, services, deployments, etc.) |
| `kubectl_describe` | Detailed resource information with events                   |
| `kubectl_logs`     | Pod logs (current and previous containers)                  |
| `kubectl_events`   | Cluster events                                              |
| `kubectl_top`      | Resource usage (CPU/memory)                                 |
| `kubectl_explain`  | Resource schema documentation                               |

## Admin Features

Access the admin panel at `/admin` (requires admin role):

- **Dashboard**: Overview of system usage
- **Clusters**: Manage Kubernetes cluster configurations
- **Budgets**: Set token/cost limits per user or globally
- **Users**: Manage user accounts and roles
- **Role Mappings**: Map OIDC roles to cluster access
- **Usage Reports**: View usage statistics by user/model
- **Model**: Curate which AI models users may select per conversation, and set
  the default. The catalog is auto-discovered at runtime from the configured
  provider (Anthropic `/v1/models`, Bedrock inference profiles, or the
  sandbox's own backend via `sandbox.env`), with a pricing-table fallback;
  `CLAUDE_CLI_MODELS` pins it manually for undetectable CLI backends

## Security

- **Read-only operations only**: No create, update, delete, exec
- **Command sanitization**: All inputs validated before execution
- **Timeout enforcement**: Commands timeout after 30 seconds
- **Cluster isolation**: Sessions bound to specific kubeconfig
- **OIDC support**: Enterprise authentication integration
- **Role-based access**: Control who can access which clusters
- **Budget limits**: Prevent runaway AI costs

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - System architecture overview
- [AI Tool Loop](docs/AI_TOOL_LOOP.md) - How AI orchestration works
- [Authentication](docs/AUTHENTICATION.md) - OIDC auth setup
- [Database Schema](docs/DATABASE_SCHEMA.md) - Data models
- [Budget](docs/BUDGET.md) - Usage tracking (informational, non-enforced)
- [MCP Servers](docs/MCP.md) - Per-conversation MCP selection (catalog + .claude.json merge)
- [Kubernetes RBAC](docs/KUBERNETES_RBAC_SETUP.md) - K8s access setup
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) - Development phases
- [Roadmap](docs/ROADMAP.md) - Future development

## Roadmap

**Phase 1 (MVP)** - Complete:
- Basic chat UI with session management
- Core kubectl tools
- Multiple AI provider support
- Session rename/delete

**Phase 2 (Enterprise)** - Complete:
- Admin dashboard
- Cluster management UI
- OIDC authentication
- Role-based cluster access
- Budget management

**Phase 3 (Advanced)** - In Progress:
- Web search integration
- MCP server support
- Interactive confirmation flow
- Extended thinking support

**Phase 4 (Future)**:
- Source code analysis (git clone integration)
- Observability integration (Prometheus, logs)
- Kubernetes deployment with Helm

## Contributing

Contributions welcome! Please read the contributing guidelines first.

## Claude Code terms

The images built here (`Dockerfile`, `backend/Dockerfile`, `agent/Dockerfile`) ship the
Claude Code CLI, installed with Anthropic's official installer.
Claude Code is Anthropic's software, not covered by this repository's license.

Source: [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance),
sections "Can customers offer Claude Code in their products?" and "Authentication and
credential use", checked 2026-11-24 (the page shows no revision date). Preinstalling Claude
Code in a product or hosted sandbox is allowed under Anthropic's Commercial Terms of Service
when:

- the binary is unmodified, installed and run as published by Anthropic, with no built-in
  authentication method removed, disabled or restricted;
- Claude usage is not paid for, resold or intermediated on end users' behalf: each deployment
  authenticates with its own Anthropic API key or inference provider credentials (Amazon
  Bedrock here), billed to that key owner. The LLM auth broker only swaps the deployment's own
  Bedrock credential for its own authorized users: the "own API keys for your own authorized
  users" case of the Authentication section (the page does not define "intermediate");
- Claude subscription credentials (Free, Pro, Max, Ultra) are never used to serve other users: the
  subscription login is for a single person's local use;
- the Claude Code and Anthropic names and logos stay out of this project's name and logo
  (stating that it runs Claude Code is fine).

Changing how the image installs or wraps Claude Code, or how deployments authenticate to
Claude, needs a new check against that page.

## License

MIT
