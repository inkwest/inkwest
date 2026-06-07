# Inkwest — Flux d'un message (utilisateur → Claude)

Diagrammes du parcours d'un message en mode **`claude-cli`** (config actuelle), plus
les variantes (providers API, sandbox). Le secret ne traverse jamais le frontend ni
le prompt : il est injecté dans l'environnement du process `claude` côté backend.

## 1. Vue composants (flowchart)

```mermaid
flowchart TD
    U["👤 Utilisateur (navigateur)"]

    subgraph FE["Frontend — React (port 3000)"]
      WSHOOK["useWebSocket.ts / websocket.ts"]
      PANEL["ConnectorPanel — boutons Donner accès"]
    end

    subgraph BE["Backend — conteneur inkwest-backend (port 8000)"]
      CHAT["api/routes/chat.py<br/>auth + validation session"]
      ORCH["services/orchestrator_cli.py<br/>orchestrate_chat_cli()"]
      ENVB["build_connector_env(uid, sid)"]
      CLI["ai_cli_client.execute_claude_cli()<br/>create_subprocess_exec"]
      PARSE["parse_cli_response()<br/>→ ChatEvent"]
      subgraph SUB["Sous-process claude — cwd /tmp/inkwest-sessions/&lt;sid&gt;"]
        CLAUDE["claude -p --output-format stream-json<br/>[--resume cli_session_id]"]
        SHELL["shell de l'agent : kubectl / aws / curl"]
      end
    end

    STORE[("CredentialStore RAM<br/>clé (user, session, connector)")]
    DB[("PostgreSQL<br/>messages, sessions, cli_session_id")]
    MODEL["☁️ API du modèle Claude"]
    K8S["Cluster Kubernetes"]
    EXT["AWS / GitLab / Coralogix"]

    U -->|"message (WebSocket)"| WSHOOK
    WSHOOK -->|"ws/chat/&lt;sid&gt; + ticket JWT"| CHAT
    CHAT --> ORCH
    ORCH <-->|"save / get cli_session_id"| DB
    ORCH --> ENVB
    ENVB -->|"lit les creds"| STORE
    ENVB -->|"AWS_* / GITLAB_TOKEN (env)"| CLI
    ORCH --> CLI
    CLI --> CLAUDE
    CLAUDE --> SHELL
    SHELL --> K8S
    SHELL --> EXT
    CLAUDE <-->|"raisonnement"| MODEL
    CLAUDE -->|"stdout stream-json"| PARSE
    PARSE -->|"ChatEvent (streaming)"| WSHOOK
    WSHOOK --> U

    PANEL -.->|"OAuth / activate / revoke"| CHAT
    CHAT -.->|"mint → put"| STORE
```

## 2. Cycle de vie d'un message (séquence)

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant FE as Frontend
    participant WSH as chat.py (WS)
    participant ORCH as orchestrator_cli
    participant STORE as CredentialStore (RAM)
    participant DB as PostgreSQL
    participant CLI as execute_claude_cli
    participant CLA as claude (subprocess)
    participant EXT as kubectl / AWS / GitLab

    U->>FE: tape un message
    FE->>WSH: WebSocket {type: message}
    WSH->>ORCH: orchestrate_chat_cli(...)
    ORCH->>DB: save_message(user) + get_cli_session_id
    ORCH->>STORE: build_connector_env(uid, sid)
    STORE-->>ORCH: env {AWS_*, GITLAB_TOKEN}
    ORCH->>CLI: execute_claude_cli(prompt, env, cwd, --resume)
    CLI->>CLA: spawn claude -p (env injecté, cwd isolé)
    CLA->>EXT: kubectl / aws / curl (creds via env)
    EXT-->>CLA: résultats
    CLA-->>CLI: stdout stream-json
    CLI-->>ORCH: ChatEvent (text / tool / done)
    ORCH-->>WSH: yield ChatEvent
    WSH-->>FE: WebSocket ChatEvent (streaming)
    FE-->>U: rendu progressif
    ORCH->>DB: save_message(assistant) + set_cli_session_id
```

## 3. Variante — providers API (Gemini / GPT / Anthropic…)

Pas de shell : le modèle demande un *tool*, le backend injecte le secret et exécute,
puis renvoie le résultat au modèle (boucle tool-use). Outils scopés, isolation native.

```mermaid
flowchart LR
    M["Modèle (API)"] -->|"appel tool<br/>aws_call / gitlab_api / coralogix_query"| D["execute_tool() (dispatch)"]
    D -->|"lit creds (user, session)"| S[("CredentialStore RAM")]
    D -->|"injecte secret + exécute"| X["AWS CLI / API GitLab / API Coralogix"]
    X -->|"résultat (sans secret)"| M
```

## 4. Variante — sandbox (proposée, non implémentée)

Un conteneur éphémère par message → isolation forte (l'agent ne voit ni `/app` ni
`~/.claude` du backend).

```mermaid
flowchart TD
    ORCH["orchestrator_cli"] -->|"docker run --rm"| RUN["Conteneur éphémère par message"]
    subgraph RUN_DETAIL["Conteneur jetable (sans socket Docker)"]
      C2["claude -p"]
      V1["-v inkwest_sess_&lt;sid&gt;:/home/app/.claude  (par session, pour --resume)"]
      E1["-e AWS_* / GITLAB_TOKEN  (creds de la session)"]
      KC["-v kubeconfigs:/etc/inkwest/kubeconfigs:ro"]
    end
    RUN --> C2
    C2 --> OUT["stdout stream-json → parse_cli_response"]
```

---

### Notes
- **Secret jamais exposé** : navigateur/prompt/logs ne voient jamais les creds ;
  injection dans l'`env` du process `claude` (ou en-tête HTTP côté tools API).
- **Historique** : en `claude-cli`, tenu par Claude Code via `--resume <cli_session_id>`
  (Inkwest mémorise l'ID en DB).
- **Isolation conversations** : cwd `/tmp/inkwest-sessions/<sid>` distinct par session.
- **Limite actuelle** : `claude` tourne dans le conteneur backend (accès filesystem) →
  voir la variante sandbox §4 pour l'isolation forte.
```
