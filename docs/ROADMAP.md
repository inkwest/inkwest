# Inkwest Roadmap

## Phase 1: MVP (Core Functionality)

### Milestone 1.1: Project Setup
- [x] Initialize backend Python project structure
- [x] Initialize frontend React/TypeScript project
- [x] Set up Docker Compose for local development (backend, frontend, postgres)
- [x] Configure linting, formatting, and pre-commit hooks
- [ ] Set up basic CI with GitHub Actions

**Deliverable:** Running dev environment with hot reload

### Milestone 1.2: Backend Foundation
- [x] Set up FastAPI application structure
- [x] Configure SQLAlchemy 2.0 with async support
- [x] Implement database models (Cluster, Session, Message)
- [x] Set up Alembic migrations
- [x] Create basic CRUD endpoints:
  - `GET/POST /api/clusters`
  - `GET/POST /api/sessions`
  - `GET /api/sessions/{id}/messages`
- [x] Add health check endpoint
- [x] Configure CORS for frontend

**Deliverable:** Working REST API with database persistence

### Milestone 1.3: AI Integration
- [x] Set up Claude API client (with Bedrock as alternative)
- [x] Define kubectl tool schemas
- [x] Implement tool executor service:
  - `kubectl_get`
  - `kubectl_describe`
  - `kubectl_logs`
  - `kubectl_events`
- [x] Build AI orchestrator with tool-use loop
- [x] Add command sanitization and timeout handling
- [x] Write tests for tool execution

**Deliverable:** AI can respond to queries and execute kubectl commands

### Milestone 1.4: WebSocket Chat
- [x] Implement WebSocket endpoint for chat
- [x] Add streaming response support
- [x] Handle connection lifecycle (connect, disconnect, reconnect)
- [x] Stream tool execution events to frontend
- [x] Save messages to database after completion

**Deliverable:** Real-time chat communication working

### Milestone 1.5: Frontend Chat UI
- [x] Set up React project with Vite
- [x] Configure Tailwind CSS
- [x] Build core components:
  - `ChatView` - main chat interface
  - `MessageBubble` - user/assistant messages
  - `ToolCallCard` - collapsible kubectl results
  - `SessionList` - sidebar with session history
  - `ClusterSelector` - cluster picker
- [x] Implement WebSocket client with reconnection
- [x] Add message input with send/loading states
- [x] Implement syntax highlighting for kubectl output
- [x] Add session creation and resumption flows

**Deliverable:** Fully functional chat UI

### Milestone 1.6: Polish & Local Deployment
- [x] Add loading states and error handling
- [x] Implement session title auto-generation
- [x] Add cluster configuration UI (or config file)
- [x] Write basic documentation (README, setup guide)
- [x] Create Docker Compose production config
- [x] Test end-to-end flow

**Deliverable:** MVP ready for local/self-hosted deployment

---

## Phase 2: Production Readiness

### Milestone 2.1: Kubernetes Deployment
- [ ] Create Helm chart or Kustomize manifests
- [ ] Configure Kubernetes deployment (Deployment, Service, Ingress)
- [ ] Set up secrets management (kubeconfigs, API keys)
- [ ] Configure persistent volume for kubeconfigs
- [ ] Add resource limits and health probes
- [ ] Test in-cluster kubectl execution

**Deliverable:** Deployable to any Kubernetes cluster

### Milestone 2.2: Authentication (OIDC)
- [x] Integrate OIDC authentication library
- [x] Add login/logout flow in frontend
- [x] Protect API endpoints with auth middleware
- [x] Implement user creation from OIDC claims
- [x] Add user_id to sessions

**Deliverable:** Multi-user support with SSO

### Milestone 2.3: Authorization & Multi-tenancy
- [x] Implement user-cluster access control
- [x] Add role-based permissions (viewer, debugger, admin)
- [x] Filter clusters by user permissions
- [x] Ensure session isolation between users
- [ ] Add audit logging for kubectl commands

**Deliverable:** Secure multi-tenant deployment

### Milestone 2.4: Enhanced Tools
- [x] Add more kubectl tools:
  - `kubectl_top` (resource usage)
  - `kubectl_explain`
  - `kubectl_api_resources`
  - `kubectl_get_all` (common resources overview)
- [ ] Add Helm integration (read-only):
  - `helm_list`
  - `helm_status`
  - `helm_values`
- [ ] Implement tool result caching

**Deliverable:** Richer debugging capabilities

---

## Phase 3: Advanced Features

### Milestone 3.1: Source Code Analysis
- [ ] Add git clone tool (to temp directory)
- [ ] Implement source file reading tool
- [ ] Add Dockerfile analysis
- [ ] Kubernetes manifest analysis
- [ ] Clean up cloned repos after session

**Deliverable:** AI can correlate cluster state with source code

### Milestone 3.2: Smart Features
- [x] Auto-summarize sessions
- [x] Stop/cancel AI processing mid-stream (stop button + cancel_event)
- [ ] Suggested follow-up questions
- [ ] Common issue detection patterns
- [ ] Cluster health dashboard
- [x] Session sharing (read-only links)

### Milestone 3.3: Observability Integration
- [ ] Prometheus metrics query tool
- [ ] Grafana dashboard links
- [ ] Log aggregation integration (Loki, ELK)
- [ ] Trace correlation (Jaeger, Zipkin)

---

## Technical Debt & Improvements (Ongoing)

- [x] Comprehensive test coverage (unit, integration, e2e)
- [ ] Performance optimization (message pagination, caching)
- [x] Rate limiting and quota management (budget system)
- [ ] Backup and restore for sessions
- [x] Mobile-responsive UI
- [x] Dark mode support
- [ ] Keyboard shortcuts
- [ ] Export session as markdown

---

## Non-Goals (Out of Scope)

- **Write operations**: No create, update, delete, exec, port-forward
- **Real-time cluster monitoring**: This is a debugging tool, not a dashboard
- **Multi-cloud management**: Focus on Kubernetes, not cloud providers
- **CI/CD integration**: Not a deployment tool

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Time to first answer | < 5 seconds |
| Tool execution latency | < 3 seconds per call |
| Session resumption | Instant (< 1 second) |
| Concurrent users | 50+ (Phase 2) |
| Uptime | 99.5% |
