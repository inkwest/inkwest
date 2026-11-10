# Inkwest Quick Start

Get Inkwest running locally in under 5 minutes.

## Prerequisites

- Docker and Docker Compose
- Kubeconfig file(s) for clusters you want to debug
- (Optional) API key for a cloud AI provider — Claude CLI works by default with no key

## 1. Clone and Configure

```bash
git clone https://github.com/kubepanion/kubepanion.git
cd kubepanion

# Copy example env and configure your AI provider
cp .env.example .env
# Backend-only knobs. The app turns the per-conversation sandbox on by default; this
# example keeps it off until you have a toolbox image (see backend/.env.example).
cp backend/.env.example backend/.env
```

Edit `.env`. There is no AI_PROVIDER: inference runs through the Claude Code CLI
(default) and, when enabled, the sandboxed inkwest-agent. The backend each talks
to is set by its own credentials; execution mode is `AGENTIC_ENGINES`.

```bash
# Default: claude-cli engine, no API key needed
# AGENTIC_ENGINES=claude-cli

# Anthropic API backend for the CLI
ANTHROPIC_API_KEY=sk-ant-...

# OR an AWS Bedrock backend (required when inkwest-agent is enabled)
# AWS_REGION=us-east-1
# AWS_ACCESS_KEY_ID=AKIA...
# AWS_SECRET_ACCESS_KEY=...
# AWS_BEARER_TOKEN_BEDROCK=...
# AGENTIC_ENGINES=claude-cli,inkwest-agent
```

## 2. Add Kubeconfigs

```bash
# Create kubeconfigs directory
mkdir -p kubeconfigs

# Copy your kubeconfig(s)
cp ~/.kube/config kubeconfigs/my-cluster.yaml

# Or for multiple clusters
cp ~/.kube/prod-config kubeconfigs/prod.yaml
cp ~/.kube/staging-config kubeconfigs/staging.yaml
```

## 3. Start Services

```bash
docker compose up -d
```

Wait for services to be healthy:
```bash
docker compose ps
```

## 4. Access the UI

Open http://localhost:3000 in your browser.

1. Click "Add Cluster"
2. Enter cluster details and kubeconfig path (e.g., `/etc/inkwest/kubeconfigs/my-cluster.yaml`)
3. Start a new debug session
4. Ask questions like:
   - "Why are my pods crashing?"
   - "Show me recent error events"
   - "What's consuming the most memory?"

## Useful Commands

```bash
# View logs
docker compose logs -f backend

# Restart services
docker compose restart

# Stop everything
docker compose down

# Reset database
docker compose down -v
docker compose up -d
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENTIC_ENGINES` | Enabled engines (CSV): `claude-cli`, `inkwest-agent` | `claude-cli` |
| `ANTHROPIC_API_KEY` | Anthropic API key | - |
| `AWS_REGION` | AWS region for Bedrock | - |
| `KUBECONFIG_DIR` | Host path to kubeconfigs | `./kubeconfigs` |

## Troubleshooting

**Backend won't start:**
```bash
# Check logs
docker compose logs backend

# Verify API key is set
docker compose exec backend env | grep API
```

**Can't connect to cluster:**
```bash
# Verify kubeconfig is mounted
docker compose exec backend ls -la /etc/inkwest/kubeconfigs/

# Test kubectl manually
docker compose exec backend kubectl --kubeconfig=/etc/inkwest/kubeconfigs/my-cluster.yaml get nodes
```

**Database issues:**
```bash
# Reset database
docker compose down -v
docker compose up -d db
docker compose up -d
```

## Development Setup

For local development without Docker:

```bash
# Backend
cd backend
uv venv && uv pip install -e ".[dev]"
python db_migrate.py
uvicorn app.main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

## Production

Production uses a **single combined image** (root `Dockerfile`): FastAPI serves the
built SPA, the API, and WebSockets on one port. Built by CI; locally:

```bash
docker compose -f docker-compose.prod.yml up -d   # app on http://localhost:80
```

Run migrations first (separate entrypoint, init-container style):

```bash
docker compose -f docker-compose.prod.yml run --rm --entrypoint /app/entrypoint-migrate.sh backend
```

The dev URLs differ: dev = vite on :3000 + API on :8000; prod = everything on :80.

## Next Steps

- Read the full [architecture overview](./docs/ARCHITECTURE.md)
- Configure [authentication](./docs/AUTHENTICATION.md) for production
- Set up [Kubernetes RBAC](./docs/KUBERNETES_RBAC_SETUP.md) for cluster access
