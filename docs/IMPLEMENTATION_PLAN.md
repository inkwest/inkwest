# Inkwest Implementation Plan

## Project Summary

**Inkwest** is an AI-powered Kubernetes debugging portal that allows users to chat with an AI assistant capable of executing kubectl commands to diagnose cluster issues.

### Core Value Proposition
- Natural language interface for Kubernetes debugging
- AI autonomously gathers information via kubectl
- Session persistence for resuming investigations
- Multi-cluster support with kubeconfig selection

### Technology Stack
| Component | Technology |
|-----------|------------|
| Frontend | React 18 + TypeScript + Vite |
| UI Styling | Tailwind CSS + shadcn/ui |
| Backend | Python 3.11 + FastAPI |
| Database | PostgreSQL 16 + SQLAlchemy 2.0 (async) |
| AI | Claude API (Anthropic) / AWS Bedrock |
| K8s Client | kubectl subprocess / kubernetes Python lib |
| Real-time | WebSocket (FastAPI native) |
| Deployment | Docker Compose (dev) / Kubernetes (prod) |

---

## Phase 1: MVP

Goal: Working chat interface that can debug a single Kubernetes cluster.

### Milestone 1.1: Backend Foundation

**Objective**: Running FastAPI server with database connectivity.

#### Tasks:
1. **Backend project structure**
   - Set up proper Python package structure (`app/`)
   - Configure pyproject.toml with all dependencies
   - Set up Alembic for migrations
   - Create Dockerfile for backend

2. **Configuration management**
   - Implement settings with pydantic-settings
   - Support environment variables and .env files
   - Configure logging

3. **Database setup**
   - Implement SQLAlchemy models (Cluster, Session, Message)
   - Create initial Alembic migration
   - Implement async database session management
   - Add database health check

4. **Basic API endpoints**
   - `GET /health` - Health check
   - `GET /api/clusters` - List clusters
   - `POST /api/clusters` - Add cluster
   - `GET /api/sessions` - List sessions
   - `POST /api/sessions` - Create session
   - `GET /api/sessions/{id}` - Get session with messages

**Deliverable**: Backend starts, connects to DB, CRUD operations work.

---

### Milestone 1.2: Tool Execution Engine

**Objective**: Safe, validated kubectl command execution.

#### Tasks:
1. **Tool definitions**
   - Define tool schemas (JSON Schema format for Claude)
   - Implement: kubectl_get, kubectl_describe, kubectl_logs, kubectl_events
   - Document each tool's purpose and parameters

2. **Command builder**
   - Build kubectl commands as argument lists (no shell)
   - Map tool inputs to kubectl flags
   - Handle all parameter combinations

3. **Input validation**
   - Validate resource types against allowlist
   - Validate namespace/name formats (Kubernetes naming rules)
   - Validate label selectors
   - Reject dangerous patterns

4. **Execution engine**
   - Execute kubectl via subprocess (async)
   - Implement timeout handling (30s default)
   - Capture stdout/stderr
   - Handle execution errors gracefully

5. **Output processing**
   - Truncate large outputs intelligently
   - Format errors for AI consumption
   - Add context to truncated outputs

**Deliverable**: Can execute kubectl commands safely, returns results.

---

### Milestone 1.3: AI Orchestration

**Objective**: Working tool-use loop with Claude API.

#### Tasks:
1. **Claude client setup**
   - Configure Anthropic client
   - (Optional) Add Bedrock client as alternative
   - Handle API key configuration

2. **System prompt**
   - Design effective system prompt for K8s debugging
   - Include cluster context (name, namespaces)
   - Define AI behavior and constraints

3. **Tool use loop implementation**
   - Implement the core orchestration loop
   - Handle stop_reason: "tool_use" vs "end_turn"
   - Execute tools and format results
   - Manage message history correctly

4. **Parallel tool execution**
   - Detect multiple tool calls in single response
   - Execute tools concurrently with asyncio.gather
   - Collect and format all results

5. **Error handling**
   - Handle API errors (rate limits, connection issues)
   - Handle tool execution errors
   - Return errors to Claude for interpretation

6. **Iteration limits**
   - Implement max iteration safeguard
   - Handle graceful termination

**Deliverable**: AI can answer questions using kubectl tools.

---

### Milestone 1.4: WebSocket Chat

**Objective**: Real-time streaming chat over WebSocket.

#### Tasks:
1. **WebSocket endpoint**
   - Implement `/api/chat/{session_id}` WebSocket route
   - Handle connection lifecycle
   - Authenticate session (basic validation)

2. **Event streaming**
   - Define event types (text_delta, tool_start, tool_result, done, error)
   - Stream text as it arrives from Claude
   - Stream tool execution events

3. **Message persistence**
   - Save user messages to database
   - Save assistant messages after completion
   - Handle tool calls and results storage

4. **Connection handling**
   - Handle disconnection gracefully
   - Implement ping/pong for keepalive
   - Clean up resources on disconnect

5. **Error streaming**
   - Stream errors to client
   - Allow recovery from errors

**Deliverable**: Real-time chat working via WebSocket.

---

### Milestone 1.5: Frontend Chat UI

**Objective**: Functional React chat interface.

#### Tasks:
1. **Project setup**
   - Initialize Vite + React + TypeScript
   - Configure Tailwind CSS
   - Set up path aliases (@/)
   - Configure ESLint

2. **Core components**
   - `ChatView` - Main chat container
   - `MessageList` - Scrollable message area
   - `MessageBubble` - User/assistant message display
   - `ChatInput` - Message input with send button
   - `ToolCallCard` - Collapsible kubectl result display

3. **Session management**
   - `SessionList` - Sidebar with session history
   - `NewSessionDialog` - Cluster selection + create
   - `ClusterSelector` - Dropdown for cluster selection

4. **WebSocket client**
   - Implement WebSocket connection manager
   - Handle reconnection logic
   - Parse and dispatch events

5. **State management**
   - Set up Zustand store
   - Manage sessions, messages, connection state
   - Handle optimistic updates

6. **Styling and UX**
   - Implement responsive layout
   - Add loading states
   - Add error states
   - Syntax highlighting for kubectl output

**Deliverable**: Fully functional chat UI.

---

### Milestone 1.6: Integration and Polish

**Objective**: End-to-end working system.

#### Tasks:
1. **Docker Compose setup**
   - Finalize docker-compose.yml
   - Add volume mounts for development
   - Configure networking between services

2. **Cluster configuration**
   - Create seed script for adding clusters
   - Document kubeconfig mounting
   - Test with real cluster

3. **Session title generation**
   - Auto-generate titles from first message
   - (Optional) AI-generated summaries

4. **Error handling polish**
   - Consistent error display
   - Helpful error messages
   - Recovery suggestions

5. **Documentation**
   - Update README with setup instructions
   - Add development guide
   - Document API endpoints

6. **Testing**
   - Basic integration tests
   - Test tool execution
   - Test WebSocket flow

**Deliverable**: MVP ready for demo/use.

---

## Phase 2: Enterprise Features (Complete)

### Milestone 2.1: Multiple AI Providers
- [x] Claude CLI provider (default, no API key needed)
- [x] Anthropic API provider
- [x] AWS Bedrock provider
- [x] Azure OpenAI provider
- [x] OpenRouter provider (multi-model support)

### Milestone 2.2: Authentication & Authorization
- [x] OIDC integration (Google, Okta, etc.)
- [x] User model with roles
- [x] JWT-based sessions
- [x] Admin role support

### Milestone 2.3: Admin Dashboard
- [x] Admin layout and navigation
- [x] Dashboard with usage overview
- [x] Cluster management (CRUD)
- [x] User management
- [x] Role mappings (OIDC role -> cluster access)
- [x] Usage reports by user/model

### Milestone 2.4: Budget Management
- [x] Budget model (daily/monthly/total limits)
- [x] Usage tracking per user/session
- [x] Budget enforcement before API calls
- [x] Cost calculations by model

### Milestone 2.5: Session Management Enhancements
- [x] Session rename functionality
- [x] Session delete with confirmation
- [x] Inline editing UI
- [x] Keyboard shortcuts (Enter, Escape)

---

## Phase 3: Advanced Features (In Progress)

### Milestone 3.1: Web Search Integration
- [x] Brave Search API integration
- [x] SerpAPI integration
- [x] Google Search integration
- [x] Trusted/blocked domain filtering

### Milestone 3.2: MCP Server Support
- [x] MCP client implementation
- [x] Tool discovery from MCP servers
- [x] Dynamic tool registration

### Milestone 3.3: Interactive Confirmation Flow
- [x] Confirmation modes (none, tools, expensive, iterations, all)
- [x] Cost threshold confirmation
- [x] Iteration interval confirmation
- [x] Token tracking per session

### Milestone 3.4: Extended Thinking Support
- [x] Extended thinking content storage
- [x] Latency tracking
- [x] Input/output token tracking per message

### Milestone 3.5: Message Retry & Error Recovery
- [x] Message status tracking (pending, success, error)
- [x] Error message storage
- [x] Retry button UI for failed messages
- [x] Retry count tracking
- [x] API endpoints for retry and failed message listing

### Milestone 3.6: Claude CLI Session Resumption
- [x] CLI session ID capture from init event
- [x] Session ID storage in database
- [x] --resume flag for conversation continuity
- [x] Automatic session management across messages

### Milestone 3.7: Enhanced Chat UI
- [x] Inline streaming blocks (text and tools in order)
- [x] Smart tool cards with description in title
- [x] Auto-expand on tool start, auto-collapse on complete
- [x] Copy to clipboard for commands and code blocks
- [x] TodoWrite tool renders as visual checklist with progress counter
- [x] Smart auto-scroll (only when user is at bottom)
- [x] Syntax highlighting for code blocks with language detection
- [x] Improved markdown rendering with custom components

---

## Phase 4: Future Enhancements

### Milestone 4.1: Kubernetes Deployment
- [ ] Helm chart
- [ ] ConfigMaps and Secrets
- [ ] Health probes
- [ ] Resource limits

### Milestone 4.2: Source Analysis
- [ ] Git clone integration
- [ ] Dockerfile analysis
- [ ] Manifest analysis

### Milestone 4.3: Observability
- [ ] Prometheus integration
- [ ] Log aggregation queries
- [ ] Grafana dashboards

---

## Technical Decisions

### Why subprocess for kubectl?

Options considered:
1. **kubernetes Python client** - Full API access, but complex for simple queries
2. **subprocess kubectl** - Simple, familiar output format, easy to debug

Decision: Start with subprocess for MVP (simpler), can add Python client later for programmatic access.

### Why WebSocket over SSE?

Options considered:
1. **Server-Sent Events (SSE)** - Simpler, one-way
2. **WebSocket** - Bidirectional, more flexible

Decision: WebSocket because we need bidirectional (user sends messages, server streams responses).

### Why Zustand over Redux?

Options considered:
1. **Redux Toolkit** - Full-featured, more boilerplate
2. **Zustand** - Minimal, hooks-based, less boilerplate

Decision: Zustand for simplicity in MVP. Can migrate if needed.

### Why SQLAlchemy 2.0 async?

Required for non-blocking database access in async FastAPI handlers. Aligns with modern Python async patterns.

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Claude API rate limits | Implement retry with backoff, consider caching |
| Large kubectl outputs | Truncation with smart summarization |
| Long-running commands | Timeout enforcement, progress indicators |
| Injection attacks | Input validation, argument lists (no shell) |
| Session state corruption | Database transactions, recovery logic |
| WebSocket disconnects | Reconnection logic, message persistence |

---

## Success Criteria

### MVP (Complete)
- [x] Can start a new debugging session
- [x] Can select from available clusters
- [x] Can ask questions in natural language
- [x] AI executes relevant kubectl commands
- [x] See kubectl output in collapsible cards
- [x] Get helpful debugging answers
- [x] Resume previous sessions
- [x] Works with Docker Compose locally

### Enterprise (Complete)
- [x] Multiple AI providers (Claude CLI, Anthropic, Bedrock, Azure, OpenRouter)
- [x] Admin dashboard for cluster/user/budget management
- [x] OIDC authentication support
- [x] Role-based cluster access
- [x] Budget management and enforcement
- [x] Usage tracking and reporting
- [x] Session rename/delete functionality

### Advanced (In Progress)
- [x] Web search integration
- [x] MCP server support
- [x] Interactive confirmation flow
- [x] Message retry on error
- [x] Claude CLI session resumption
- [x] Enhanced chat UI with inline streaming
- [x] Smart tool cards with auto-collapse
- [x] Code blocks with copy to clipboard
- [ ] Full production Kubernetes deployment
