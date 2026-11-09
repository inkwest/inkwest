# Credentials at rest: audit and status

**Goal**: a dump of DB + disk + env vars, taken while no user is connected, must not yield
usable credentials.

**TL;DR**

- Connector credentials (AWS STS, GitLab OAuth) meet the goal in the backend: RAM only,
  expiring. Their sandbox projection on tmpfs outlives the sandbox until revoke or session
  delete.
- Per-user MCP OAuth tokens meet the goal. They are sealed under a per-user DEK that exists
  in server RAM only while the user's browser is connected (Phase C, done).
- HTTP/SSE MCP catalog servers keep their real credentials out of the sandbox. The MCP proxy
  injects them server-side; the sandbox holds an internal JWT valid up to 6 h, which stays in
  the durable `.claude.json`.
- Org-level secrets stay in env vars. A full env dump yields them. That is the accepted tier
  for operator secrets; KMS or a sealed-at-startup key is the upgrade path (Phase B, not
  built).
- Open items: OAuth client secrets in plaintext in `mcp_oauth_clients`, no tmpfs guard on
  `sandbox_creds_root`, GitLab token can land in a durable `.git/config`, stdio MCP
  definitions injected verbatim, static kubeconfigs on plain disk.

## Inventory

### Connector credentials (AWS / GitLab)

| Item | Where | Status |
|---|---|---|
| AWS SSO token, STS creds, GitLab access/refresh token | `CredentialStore`, backend RAM only (`app/services/connectors/store.py`) | OK. Never on disk or DB, never in argv (`docker exec -e KEY` passes the name only). Cost: single replica, re-auth after restart |
| AWS `~/.aws` projection | `sandbox_creds_root/<sid>/.aws`, else `/dev/shm/inkwest/<sid>/.aws` (`connector_exec.write_aws_profiles`) | Files are 0644 when the volume is shared, 0600 otherwise. Kept until revoke or session delete, not reaped with the sandbox; it survives a backend restart until the STS creds expire. **Nothing checks that `sandbox_creds_root` is tmpfs**, and without `/dev/shm` the projection falls back to `$TMPDIR/inkwest-aws` on disk |
| EKS kubeconfigs | `<sid>/kube/` (`eks_refresh.py`) | OK. No embedded secret, `aws eks get-token` exec plugin |
| PKCE `code_verifier` | Postgres `connector_pending_states`, `mcp_oauth_pending_states` | Acceptable. Short-lived, useless without the auth code |
| `GITLAB_TOKEN` in the sandbox env | `tools/connector_exec.py` | In-use only, but an agent can run `git clone https://oauth2:$GITLAB_TOKEN@…` and persist the token in `workdir/**/.git/config` on durable disk. No credential helper prevents it |

### MCP servers

| Item | Where | Status |
|---|---|---|
| Per-user OAuth tokens | Postgres `mcp_user_tokens`, AES-256-GCM under a per-user DEK (`app/core/crypto.py`) | OK. The DEK is wrapped by a non-extractable browser key. The server holds the raw DEK in RAM while the user has a chat or sync socket open and evicts it when the last one closes, no grace period (`mcp_dek_handshake.py`, `mcp_dek_store.py`) |
| OAuth catalog client secrets | Postgres `mcp_oauth_catalog`, sealed with `SECRETBOX_KEYS` (`app/core/secretbox.py`) | Sealed in the catalog, but copied in **plaintext** to `mcp_oauth_clients.client_secret_ref` at first connect (`mcp_oauth._register_and_persist_client`). DCR-issued secrets land there in plaintext too. A DB-only dump yields them |
| `MCP_CATALOG`, `MCP_CATALOG_OAUTH2` | env vars | Plaintext-equivalent in env (operator tier). `MCP_CATALOG_OAUTH2` keeps client secrets after they are seeded into the table |
| HTTP/SSE static servers in the sandbox | `.claude.json`, `opencode.json`, agent MCP file | Rewritten to `/api/mcp-proxy/{key}/` + internal JWT (`mcp_config.build_session_static_servers`). With `mcp_proxy_base_url` unset, or no `user_id` to bind a token to, every entry falls back to verbatim, so credentials end up in the sandbox |
| stdio static servers | same files | Injected verbatim, including any `env` secret. Accepted by the catalog parser and run by both engines; nothing rejects a secret-bearing `env`. No stdio server is used today |
| `.claude.json` | `<sid>/claude/.claude.json`, durable, mode 0666 | Holds proxy JWTs (6 h TTL, reused across turns) and stdio defs, plus verbatim HTTP defs when the proxy is unwired. A disk dump yields JWTs that can call static HTTP MCP servers through the proxy for up to 6 h without the user connected, unless the tool policy is `ask` or `deny`. 0666 is accepted: one conversation per container, the toolbox uid must rewrite it |
| `opencode.json` | `<sid>/workdir/`, durable, umask mode | Same content as `.claude.json`. Pruned at turn start, removed at turn end (`orchestrator_opencode.py`). A crash leaves it until the next turn |
| inkwest-agent MCP file | `<sid>/claude/`, 0644 | Re-minted each turn, removed at turn end (`agent_mcp_config.remove_agent_mcp_config`). A crash leaves it until the next turn |

### Org-level secrets (env)

| Setting (`config.py`) | Blast radius if env dumps |
|---|---|
| `secret_key` | forge any user/admin session and the internal MCP proxy JWTs. Empty or default value refused when `APP_ENV=production` |
| `oidc_client_secret` | impersonate the app at the IdP |
| `aws_sso_oidc_client_secret` | run device grants as the app (user consent still needed) |
| `aws_bearer_token_bedrock`, `anthropic_api_key`, `web_search_api_key` | spend, model access |
| `aws_access_key_id`, `aws_secret_access_key` | Bedrock and catalog discovery as the app's AWS identity |
| `secretbox_keys` | unseal the OAuth catalog client secrets |
| `vapid_private_key` | send Web Push notifications to every subscribed browser |
| `sandbox_env_file` | Plaintext file on host disk, passed with `--env-file`, so its values are also copied into each conversation container's config (dind daemon disk, `docker inspect`). With `llm_broker_enabled` the sandbox gets a per-turn grant instead of the Bedrock bearer (`docs/llm-auth-broker.md`), but the code only overrides it per turn: the operator must remove the bearer from the file |

### Database and static files

| Item | Status |
|---|---|
| Static kubeconfigs (`/etc/inkwest/kubeconfigs`) | Plaintext on disk, may embed long-lived tokens. Prefer exec-plugin kubeconfigs for new clusters |
| `session_share_links.token`, `push_subscriptions` keys | Plaintext capabilities: a DB dump yields read access to shared transcripts and push delivery. Not upstream credentials |
| `connector_audit_log`, `role_kubeconfig_mappings` | Metadata and paths only |
| `messages` | Not credentials, but tool outputs are sensitive data at rest. Out of scope |

## Design choices

**Browser-held keys only for per-user tokens.** A browser key needs a user who owns the
secret. Org-level secrets have none, so they get server-side custody. Per-user OAuth tokens
fit: the user owns them, and a lost key costs a reconnect, not data, since every token can be
obtained again.

**Non-extractable wrapping key.** The browser never sends its key. It unwraps the DEK locally
and sends the raw DEK over the TLS WebSocket. The server cannot verify the DEK, so it keys it
by the authenticated `user_id`: a bad DEK only breaks that user's own tokens.

**Server-side custody options**, for the org tier:

| Scheme | DB + disk + env dump yields secrets? | Ops cost |
|---|---|---|
| Key in env (current, `secretbox_keys`) | yes | none |
| Sealed key at startup, RAM only | no | manual unseal on each restart |
| KMS envelope, instance/IRSA role, no static AWS creds | no, decryption needs code running in the role | low, ties the deploy to AWS |

**Scope of the claim.** While a turn runs, the sandbox sees plaintext (env vars, tmpfs files).
That is in-use exposure. The claim is: no long-lived secret survives on durable media after
the turn. The exception is the MCP proxy JWTs in `.claude.json`, valid up to 6 h.

## Plan

### Phase A: disk and DB leaks, no crypto

1. ~~Secrets out of `.claude.json`~~ done for HTTP/SSE servers (MCP proxy).
2. ~~`.claude.json` mode~~ accepted as 0666, see above.
3. Seal `mcp_oauth_clients.client_secret_ref` with secretbox, like the catalog column.
4. Workdir token leak: `GIT_TERMINAL_PROMPT=0` + a `git credential` helper reading
   `$GITLAB_TOKEN`, so the agent never needs to inline it in a URL. Optionally scrub
   `workdir/**/.git/config` at teardown.
5. Startup guard: refuse a `sandbox_creds_root` that is not on tmpfs (`/proc/mounts` or
   `statvfs`).
6. Static kubeconfigs: document as legacy, rotate or retire embedded tokens.
7. stdio MCP definitions: reject secrets in their `env`, or reject stdio outright.

### Phase B: org-level key custody (not built)

8. KMS envelope (instance/IRSA role) or a sealed-at-startup key for `secretbox_keys`, behind
   the keyring in `app/core/secretbox.py`.
9. Move `MCP_CATALOG` secret values into the sealed store, so the env var stops holding
   credentials.

### Phase C: browser-wrapped per-user tokens (done)

10. ~~Per-user DEK, non-extractable browser wrapping key, RAM-only on the server~~ done.
11. Key loss = "reconnect your MCPs". No recovery code.

## Not protected

- Code execution on the live backend: it reads RAM keys and DEKs, or calls KMS with the role.
- Malicious server code: the browser hands the DEK to the server by design.
- XSS in the SPA: it can drive authenticated sessions. CSP hygiene is a prerequisite.
- In-use exposure in the sandbox while a turn runs.
- Secrets the agent prints during a turn persist in durable transcripts (`<sid>/claude/`,
  `<sid>/opencode/`, `messages`).
