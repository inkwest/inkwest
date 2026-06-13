# MCP Servers (per-conversation)

> users enable a per-conversation set of MCP servers from a
> server-side **catalog**. The selection is merged into the conversation's
> `.claude.json` so the `claude-cli` agent picks the servers up. Coralogix is the
> first integration onboarded — one entry per environment (`Coralogix-<env>`:
> `coralogix-dev` / `coralogix-staging` / `coralogix-prod`).

## TL;DR

- A **catalog** of available MCP servers is defined by one env var, `MCP_CATALOG`
  (server-side). Only the **keys** ever reach the browser — never the definitions
  or any secret they embed.
- Each conversation stores its own list of enabled keys
  (`sessions.enabled_mcp_servers`).
- On every turn (and on every change), the enabled servers are merged into the
  conversation's `.claude.json` under `mcpServers`. Claude Code reads MCP config
  **at process start**, so a change takes effect on the **next message**.
- UI: an **MCP** button on the connector bar (green `N/total` when ≥1 enabled)
  opens a modal with a checkbox per server.
- Requires **sandbox mode** (`SANDBOX_ENABLED=true`) — that's what gives each
  conversation its own `.claude.json`. In non-sandbox mode the config is shared,
  so the merge is a no-op.

## The catalog (`MCP_CATALOG`)

A JSON object mapping a stable key to a **base64-encoded** JSON definition — the
value that goes under Claude's `mcpServers[<key>]` (command/args/env/url…):

```
MCP_CATALOG={"coralogix-prod": "<base64(server-def-json)>", "fetch": "<base64(...)>"}
```

Encode it locally (so secrets never leave your machine). Put your real
definitions in a local, gitignored file and run:

```bash
python3 -c '
import json, base64
servers = json.load(open("mcp_servers.json"))  # {key: {command, args, env, ...}}
print("MCP_CATALOG=" + json.dumps(
    {k: base64.b64encode(json.dumps(v).encode()).decode() for k, v in servers.items()}))
'
```

Paste the printed `MCP_CATALOG=…` into `backend/.env` and restart the backend.
Parsing is tolerant: a malformed catalog or a single bad entry is logged and
skipped — it never crashes startup, it just yields fewer selectable servers
(`app/services/mcp_catalog.py`).

## How a selection is applied

1. The user toggles servers in the modal → `PUT /api/sessions/{id}/mcp`.
2. Keys are validated against the catalog (no arbitrary server injection) and
   persisted to `sessions.enabled_mcp_servers`.
3. `app/services/mcp_config.py::apply_session_mcp_selection` reconciles the
   conversation's `.claude.json` `mcpServers`:
   - **adds/updates** the enabled catalog entries,
   - **prunes** catalog-managed entries that were deselected,
   - **preserves** any `mcpServers` entry not owned by the catalog.
4. The same reconcile runs each turn after `sandbox.ensure`, so a sandbox rebuild
   (which recreates an empty claude dir) re-applies the selection.

`.claude.json` lives at `<session_dir>/claude/.claude.json` on the host
(bind-mounted to `CLAUDE_CONFIG_DIR` in the toolbox).

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/mcp/catalog` | `{keys, servers}` — `keys` is the static-only list (pre-OAuth contract); `servers` is the OAuth-aware superset `[{key, kind, auth_status}]` |
| GET | `/api/sessions/{id}/mcp` | Keys enabled for the conversation |
| PUT | `/api/sessions/{id}/mcp` | Set enabled keys (validated vs catalog) |
| POST | `/api/mcp/{key}/auth/start` | Begin an OAuth2 connect — returns `{authorize_url}` for a popup |
| GET | `/api/mcp/oauth/callback` | OAuth redirect target (**unauthenticated**; bound to the user by the single-use pending state). Closes the popup |
| DELETE | `/api/mcp/{key}/auth` | Revoke the user's grant (best-effort upstream RFC 7009); returns the refreshed catalog |
| GET/POST/DELETE | `/api/mcp-proxy/{key}/{path}` | Reverse proxy the sandboxed CLI calls. Auth is the internal proxy JWT (no `get_current_user`); injects the real upstream token per request |

`auth_status` (per oauth key, per user): `static` (non-oauth, always usable) ·
`unavailable` (oauth but sandbox disabled) · `not_connected` (no grant → "Connect") ·
`connected` (active grant) · `needs_auth` (expired/revoked → "Reconnect").

## OAuth2 MCP servers (`MCP_CATALOG_OAUTH2`)

A **separate** catalog (same `{key: base64(def)}` outer shape as `MCP_CATALOG`) for remote
servers that authenticate via the MCP Authorization spec (OAuth 2.1). The static
`MCP_CATALOG` is never touched by the OAuth broker. Each decoded definition is the oauth
kind:

```json
{"type": "http", "url": "https://mcp.example.com/mcp", "scopes": ["mcp:read"],
 "client_id": "optional — omit for Dynamic Client Registration"}
```

Flow:

1. **Connect.** The backend brokers OAuth 2.1: discovery (RFC 9728 protected-resource metadata
   → RFC 8414 AS metadata, with SSRF guards + IP-pinning) → optional DCR (RFC 7591) →
   auth-code + PKCE (S256) + `resource` (RFC 8707). Discovered token/revocation endpoints are
   persisted so refresh never re-discovers.
2. **Token custody is browser-fractioned (no server-side key).** Each user's tokens are
   sealed (AES-256-GCM) under a per-user DEK; the DEK is wrapped by a non-extractable
   WebCrypto key in the browser's IndexedDB (shared across same-profile tabs). On WS connect
   the browser supplies the unwrapped DEK; the server holds it in RAM only while connected
   (refcount-evicted on the last disconnect). A DB+disk+env dump taken with nobody connected
   yields no plaintext. A different browser profile reconnects OAuth.
3. **Enable + inject.** A connected oauth server is enabled per-conversation (the panel
   checkbox → `enabled_mcp_servers`, like a static server). Each turn,
   `apply_session_mcp_selection` injects into `.claude.json`, for every enabled oauth grant
   that is `active`, a proxy entry `{type:http, url:<MCP_PROXY_BASE_URL>/api/mcp-proxy/<key>/,
   headers:{Authorization: Bearer <internal JWT>}}`. The **real upstream token never touches
   sandbox disk** — only the short-lived internal JWT does (mint-stable, so an unchanged
   selection is a no-op write).
4. **Proxy.** The CLI's MCP calls hit `/api/mcp-proxy/{key}/` through the per-conversation
   squid; the proxy verifies the internal JWT, refreshes the upstream token if needed
   (single-flight per user+key), swaps the `Authorization` header, and streams the response.
   Reachability: the CLI egresses only via squid, so `MCP_PROXY_BASE_URL`'s host must be
   squid-allowlisted (`sandbox/squid.conf`) and routed to the backend over the Docker
   host-gateway (added automatically as `--add-host` on the squid container).
5. **Reconnect.** An enabled oauth grant that is missing/expired/revoked is skipped at
   injection and emits an `mcp_auth_required` event, surfacing an in-conversation **Reconnect**
   banner (`McpInlineNotice`) instead of the tools silently vanishing.

Lifecycle events are audited in `connector_audit_log` (`connector="mcp"`,
`action=grant|revoke`, `session_id` NULL — user-level).

`MCP_OAUTH_REDIRECT_BASE` fixes the callback base (defaults to `FRONTEND_URL`); it must match
what is registered at the AS / sent in DCR. `MCP_PROXY_BASE_URL` is the host the sandboxed CLI
uses to reach the proxy — squid-allowlisted + host-gateway-routable, **not** `127.0.0.1`;
unset disables oauth injection.

> **Single-replica / session affinity required (operational).** The per-user DEK and the
> in-flight token live in the serving process's RAM (browser-fractioned custody), and the
> OAuth callback must reach the same process that holds the DEK. Run the backend
> **single-worker** (startup fails fast if `WEB_CONCURRENCY > 1` while `MCP_CATALOG_OAUTH2` is
> set) and, across pods, with **session affinity** — otherwise a callback can land on a
> replica without the DEK and the connect fails after the auth code is spent. Same constraint
> as the connector RAM stores.

## Secrets

The catalog value carries the **full** server definition, **including any secret**
in its `env` (decision: option A — written in clear into the conversation's
`.claude.json` on enable). This is acceptable because that file lives in the
**per-conversation isolated** sandbox dir and is never served to the browser nor
put in the prompt/logs. Mitigations: use read-only credentials, the secret is
removed from the file on deselection / session teardown, and `MCP_CATALOG` stays
server-side. If clear-text-at-rest is not acceptable for a given server, switch
that entry to an env reference (`${VAR}`) and inject the secret into the toolbox
container environment instead.

## Code map

| Concern | Location |
|---|---|
| Catalog parse/validate | `backend/app/services/mcp_catalog.py` |
| Merge into `.claude.json` | `backend/app/services/mcp_config.py` |
| Per-turn apply | `backend/app/services/orchestrator_cli.py` (after `sandbox.ensure`) |
| Selection storage | `sessions.enabled_mcp_servers` (JSONB) |
| API | `backend/app/api/routes/mcp.py`, `backend/app/api/routes/sessions.py` |
| Config | `MCP_CATALOG` in `backend/app/core/config.py` |
| UI | `frontend/src/components/connector/McpPanel.tsx` |
