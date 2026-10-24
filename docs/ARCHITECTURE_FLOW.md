# Inkwest — Message flow (user → Claude)

Diagrams of a message's path in **`claude-cli`** mode (current config), plus
the variants (API providers, sandbox). The secret never crosses the frontend or
the prompt: it is injected into the `claude` process environment on the backend side.

## 1. Component view (flowchart)

```mermaid
flowchart TD
    U["👤 User (browser)"]

    subgraph FE["Frontend — React (port 3000)"]
      WSHOOK["useWebSocket.ts / websocket.ts"]
      PANEL["ConnectorPanel — Give access buttons"]
    end

    subgraph BE["Backend — inkwest-backend container (port 8000)"]
      CHAT["api/routes/chat.py<br/>auth + session validation"]
      ORCH["services/orchestrator_cli.py<br/>orchestrate_chat_cli()"]
      ENVB["build_connector_env(uid, sid)"]
      CLI["ai_cli_client.execute_claude_cli()<br/>create_subprocess_exec"]
      PARSE["parse_cli_response()<br/>→ ChatEvent"]
      subgraph SUB["claude subprocess — cwd /tmp/inkwest-sessions/&lt;sid&gt;"]
        CLAUDE["claude -p --output-format stream-json<br/>[--resume cli_session_id]"]
        SHELL["agent shell: kubectl / aws / curl"]
      end
    end

    STORE[("CredentialStore RAM<br/>key (user, session, connector)")]
    DB[("PostgreSQL<br/>messages, sessions, cli_session_id")]
    MODEL["☁️ Claude model API"]
    K8S["Kubernetes cluster"]
    EXT["AWS / GitLab"]

    U -->|"message (WebSocket)"| WSHOOK
    WSHOOK -->|"ws/chat/&lt;sid&gt; + JWT ticket"| CHAT
    CHAT --> ORCH
    ORCH <-->|"save / get cli_session_id"| DB
    ORCH --> ENVB
    ENVB -->|"reads creds"| STORE
    ENVB -->|"AWS_* / GITLAB_TOKEN (env)"| CLI
    ORCH --> CLI
    CLI --> CLAUDE
    CLAUDE --> SHELL
    SHELL --> K8S
    SHELL --> EXT
    CLAUDE <-->|"reasoning"| MODEL
    CLAUDE -->|"stdout stream-json"| PARSE
    PARSE -->|"ChatEvent (streaming)"| WSHOOK
    WSHOOK --> U

    PANEL -.->|"OAuth / activate / revoke"| CHAT
    CHAT -.->|"mint → put"| STORE
```

## 2. Message lifecycle (sequence)

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant FE as Frontend
    participant WSH as chat.py (WS)
    participant ORCH as orchestrator_cli
    participant STORE as CredentialStore (RAM)
    participant DB as PostgreSQL
    participant CLI as execute_claude_cli
    participant CLA as claude (subprocess)
    participant EXT as kubectl / AWS / GitLab

    U->>FE: types a message
    FE->>WSH: WebSocket {type: message}
    WSH->>ORCH: orchestrate_chat_cli(...)
    ORCH->>DB: save_message(user) + get_cli_session_id
    ORCH->>STORE: build_connector_env(uid, sid)
    STORE-->>ORCH: env {AWS_*, GITLAB_TOKEN}
    ORCH->>CLI: execute_claude_cli(prompt, env, cwd, --resume)
    CLI->>CLA: spawn claude -p (env injected, isolated cwd)
    CLA->>EXT: kubectl / aws / curl (creds via env)
    EXT-->>CLA: results
    CLA-->>CLI: stdout stream-json
    CLI-->>ORCH: ChatEvent (text / tool / done)
    ORCH-->>WSH: yield ChatEvent
    WSH-->>FE: WebSocket ChatEvent (streaming)
    FE-->>U: progressive rendering
    ORCH->>DB: save_message(assistant) + set_cli_session_id
```

## 3. Variant — API providers (Gemini / GPT / Anthropic…)

No shell: the model requests a *tool*, the backend injects the secret and executes,
then returns the result to the model (tool-use loop). Scoped tools, native isolation.

```mermaid
flowchart LR
    M["Model (API)"] -->|"tool call<br/>aws_call / gitlab_api"| D["execute_tool() (dispatch)"]
    D -->|"reads creds (user, session)"| S[("CredentialStore RAM")]
    D -->|"injects secret + executes"| X["AWS CLI / GitLab API"]
    X -->|"result (no secret)"| M
```

## 4. Variant — sandbox (implemented, `SANDBOX_ENABLED=true`)

Strong isolation: **one container pair per conversation** (reused from one
message to the next, for `--resume`) instead of running `claude` in the backend
container. See `app/services/sandbox.py`.

- **toolbox** (`inkwest_conv_<sid>`) — image `SANDBOX_TOOLBOX_IMAGE` (`claude` +
  `kubectl` + `aws`). Placed on an `--internal` network: **no direct egress**. The
  backend runs `docker exec … claude -p` in it per message (stdout unchanged →
  `parse_cli_response`).
- **squid proxy** (`inkwest_proxy_<sid>`) — dual-network (internal + external). Applies
  a **static allowlist** (`sandbox/squid.conf`, mounted read-only) that
  already covers every connector + model backend; a grant does **not** open a
  domain (no reload). `access.log` is mounted on the backend side → durable egress audit
  + UI tab.
- The backend drives a dedicated **dind** daemon; lazy provisioning
  on the 1st message, idle reaper (TTL), orphan sweep at startup.

```mermaid
flowchart TD
    ORCH["orchestrator_cli<br/>sandbox.ensure(sid)"] -->|"docker exec (per message)"| CONV
    subgraph PAIR["Per-conversation pair — internal network (no direct route)"]
      CONV["toolbox inkwest_conv_&lt;sid&gt;<br/>claude -p · HTTP(S)_PROXY=proxy:3128<br/>-v .claude (--resume) · -v /workdir<br/>--tmpfs .aws/.kube (RAM creds)"]
      PROXY["proxy inkwest_proxy_&lt;sid&gt;<br/>squid.conf: static allowlist"]
    end
    EXT["Allowed egress: model API · .eks · *.eu-west-1.amazonaws.com<br/>gitlab.com · .coralogix.com · package registries"]
    CONV -->|"stdout stream-json → parse_cli_response"| ORCH
    CONV ==>|"all egress via the proxy"| PROXY
    PROXY --> EXT
    CONV -. "direct attempt → blocked (internal network)" .-> EXT
```

---

### Notes
- **Secret never exposed**: browser/prompt/logs never see the creds;
  injected into the `claude` process `env` (or HTTP header for API tools).
- **History**: in `claude-cli`, held by Claude Code via `--resume <cli_session_id>`
  (Inkwest stores the ID in DB).
- **Conversation isolation**: distinct cwd `/tmp/inkwest-sessions/<sid>` per session.
- **Filesystem/network isolation**: by default `claude` runs in the backend
  container (filesystem access, unrestricted egress); with `SANDBOX_ENABLED=true` each
  conversation is isolated in its toolbox+squid pair (§4).
