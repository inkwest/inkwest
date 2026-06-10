# Inkwest

AI-powered Kubernetes debugging assistant. Chat with an AI that can execute kubectl commands to help diagnose cluster issues, explain configurations, and debug applications.

## Why Inkwest?

Most developers don't debug Kubernetes every day. When they need to, the overhead is real: setting up VPN access, configuring kubectl, getting the right kubeconfig, authenticating — all before even starting to investigate. Tools like MCP servers and CLI plugins still require local setup per developer.

Inkwest removes that friction. A shared web interface that's always connected, already authenticated, and scoped to the right clusters. Developers ask questions in plain language; the AI runs kubectl and explains what's going on.

It also solves the permissions problem. Instead of giving developers broad cluster access "just in case", Inkwest acts as a controlled read-only proxy. The AI only runs approved read operations, and access is managed through OIDC and role mappings — not by handing out kubeconfigs.

## Features

- **Conversational debugging**: Describe your problem in natural language
- **Intelligent tool use**: AI automatically runs relevant kubectl commands
- **Session persistence**: Resume previous debugging sessions with rename/delete support
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

# Copy environment template
cp .env.example .env
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
# AI Provider (default: claude-cli, no API key needed!)
AI_PROVIDER=claude-cli

# OR for Anthropic API
# AI_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-...

# OR for AWS Bedrock
# AI_PROVIDER=bedrock
# AWS_REGION=us-east-1
# AWS_ACCESS_KEY_ID=...
# AWS_SECRET_ACCESS_KEY=...

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

*Claude CLI uses your existing Claude subscription

### Claude CLI Features

When using Claude CLI as the AI provider:
- **Session resumption**: Conversations persist across messages using `--resume`
- **Built-in tool execution**: Uses Claude's native tool calling via MCP
- **No API key needed**: Works with your existing Claude subscription

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
├── deploy/                  # Kubernetes manifests
└── docker-compose.yml       # Local development setup
```

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

## License

MIT
