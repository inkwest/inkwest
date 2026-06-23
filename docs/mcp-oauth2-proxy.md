# MCP OAuth2 Proxy — Implementation Plan

> **STATUS — Increments 1 (connect flow) + 2 (proxy) shipped; this plan diverged on the
> points below.** Read this box before the rest of the document, which still describes the
> original design.
> - **Separate env var.** OAuth entries live in a new `MCP_CATALOG_OAUTH2`
>   (`app/services/mcp_oauth_catalog.py`), **not** mixed into `MCP_CATALOG` / `parse_catalog`.
>   The static catalog is untouched.
> - **Browser-fractioned key custody (audit-doc Phase C), not KMS/sealed.** Per-user tokens
>   are AES-256-GCM under a per-user DEK whose wrapped form lives in a new `mcp_user_keys`
>   table; the DEK is supplied by the browser over the chat WS and held in RAM only while
>   connected (`app/core/crypto.py`, `app/services/mcp_dek_store.py`, `mcp_dek_handshake.py`).
>   There are **no** `token_vault_*` / KMS settings. Phase B (KMS/sealed) is not used here.
> - **`oauth_flow.py` was NOT extracted** from the GitLab connector (only `generate_pkce` is
>   reused). The OAuth client is self-contained in `app/services/mcp_oauth.py`.
> - **Increment 2 (shipped):** the reverse **proxy** (`app/api/routes/mcp_proxy.py`), the
>   internal **JWT** (`app/services/mcp_proxy_token.py`), `.claude.json` **injection**
>   (`mcp_config._oauth_proxy_servers`), the refresh hot path, the `mcp_auth_required` event +
>   reconnect banner, and squid host-gateway reachability (`MCP_PROXY_BASE_URL`). Upstream
>   fetches are IP-pinned; requires single-worker / session affinity. **Still pending: rollout
>   step 0** — the live end-to-end check that the headless CLI honours `HTTP_PROXY` through
>   squid (needs the real toolbox/squid; manual acceptance gate, not unit-testable).
> - The body sections below predate both increments; treat the four bullets above as the
>   source of truth where they conflict.

**TL;DR**: Extend the static MCP catalog with `auth: oauth2` entries. Backend brokers the
OAuth 2.1 flow (browser redirect through the frontend), stores per-user tokens as encrypted
blobs behind the shared `TokenVault` interface (envelope encryption — see
`docs/credentials-at-rest-audit.md`), and exposes a single path-based reverse proxy
(`/api/mcp-proxy/{key}`) that injects a fresh upstream access token per request. The
sandbox `.claude.json` only ever contains the proxy URL plus a short-lived internal JWT —
real tokens never touch sandbox disk. The CLI reaches the proxy through the
per-conversation squid egress proxy via an allowlisted backend hostname routed over the
Docker host-gateway (see work item 5).

Sibling designs — read together:
- `docs/connector-hot-auth.md` — defines the `TokenVault` interface and `platform_grants`;
  this plan consumes the same vault and the same refresh-rotation rules (§ Storage below).
- `docs/credentials-at-rest-audit.md` — defines key custody for the vault (KMS
  instance-role envelope or sealed-at-startup KEK; browser-wrapped per-user DEK as the
  optional Phase C upgrade, for which these per-user OAuth tokens are the primary fit).

## Prerequisites

- **Sandbox mode is required.** Per-conversation `.claude.json` only exists with
  `sandbox_enabled=true` — without it `claude_json_path()` returns `None` and
  `apply_session_mcp_selection()` is a no-op (`app/services/mcp_config.py:74,121`), so
  oauth entries could never reach the CLI. When sandbox is disabled, the catalog endpoint
  reports oauth entries as `auth_status: "unavailable"` so the panel never shows a
  "Connect" button that leads nowhere.
- **Rollout step 0 — validate the transport assumption.** Nothing today proves the
  headless CLI's streamable-HTTP MCP client honors `HTTP_PROXY` (the toolbox sits on an
  `--internal` network; squid is the *only* route out). Before any OAuth work, add a plain
  static-header `type: http` catalog entry and exercise it end-to-end through the sandbox.
  If the CLI's MCP transport ignores proxy env vars, this entire design is moot and we need
  a different reachability mechanism first.

## Why backend-brokered

The MCP Authorization spec is OAuth 2.1: discovery via `401` + `WWW-Authenticate` →
protected-resource metadata (RFC 9728) → AS metadata (RFC 8414), optional dynamic client
registration (RFC 7591), authorization-code + PKCE, `resource` parameter (RFC 8707).
The flow requires a user browser. The Claude CLI can authenticate interactively (`/mcp`),
but our CLI runs headless in an ephemeral per-conversation sandbox with no keychain —
CLI-side auth is a dead end.

The backend already runs two OAuth flows: OIDC login (`app/core/auth.py`) and the GitLab
connector's authorization-code + PKCE + refresh + revoke
(`app/services/connectors/gitlab.py`). The MCP OAuth client reuses their building blocks
(see work item 3) rather than adding a third independent stack.

## Why one proxy endpoint, not one port per MCP

- No port allocation/lifecycle/firewall surface — one route on the existing FastAPI app.
- Streamable HTTP MCP is a single endpoint (POST JSON-RPC, optional GET SSE stream), so
  proxying is one catch-all route.
- Mid-session token expiry is solved for free: the proxy refreshes per request, so a long
  conversation never sees a stale token.

## Storage: one vault, not a parallel mechanism

Per-user MCP tokens are stored through the **same `TokenVault` interface** that
`docs/connector-hot-auth.md` §5 defines for platform grants — opaque encrypted blobs,
`put/get/delete`. Do **not** build a second encryption path:

- `app/core/crypto.py` implements the vault's envelope encryption: per-blob data key,
  wrapped by a KEK whose custody follows `docs/credentials-at-rest-audit.md` Phase B —
  AWS KMS via instance/IRSA role (no static KMS creds in env) where available,
  sealed-at-startup key otherwise. **Not** a raw Fernet key in an env var: the audit doc
  rejects that as "DB dump + env dump = plaintext" (its §1.6), and this plan follows it.
- Settings are vault-scoped and generic, not MCP-branded: `token_vault_backend`
  (`kms | sealed`), `token_vault_kms_key_id`, … — the connector hot-auth work consumes the
  same settings.
- Phase C of the audit doc (browser-wrapped per-user DEK) upgrades key custody behind the
  same interface with no schema change here.
- Blob content for an MCP grant: `{access_token, refresh_token?, expires_at?, scopes}` —
  one JSON blob per (user, key), referenced by `vault_ref` (below).

## Architecture

```
McpPanel "Connect" ──► POST /api/mcp/{key}/auth/start ──► authorize URL (popup)
                                                              │ user consents at AS
GET /api/mcp/oauth/callback ◄─────────────────────────────────┘
  └─ code exchange (PKCE) ─► vault blob + mcp_user_tokens row ─► popup closes, panel refetches

session start: apply_session_mcp_selection()
  └─ oauth entry ─► .claude.json: {"type":"http",
                                   "url":"http://host-gateway-alias:8000/api/mcp-proxy/{key}/",
                                   "headers":{"Authorization":"Bearer <internal JWT>"}}

CLI tool call ──► squid (allowlisted backend host, host-gateway) ──► /api/mcp-proxy/{key}
  └─ verify internal JWT (enforced aud + type; identifies user_id, session_id, mcp_key)
  └─ load + refresh-if-expired upstream token (vault)
  └─ swap Authorization header, stream passthrough
  └─ upstream 401 → force refresh, retry once → still 401 → grant expired, return 401
```

## Catalog entry shape (new kind)

Existing entries (stdio command / static-header HTTP) are untouched. New kind recognized by
`parse_catalog()` (`app/services/mcp_catalog.py`):

```json
{
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "auth": "oauth2",
  "scopes": ["mcp:read"],
  "client_id": "optional — omit to use Dynamic Client Registration",
  "client_secret": "optional"  // pragma: allowlist secret
}
```

## Work items

### 1. DB — migration `backend/migrations/<ts>_mcp_oauth.sql`

Applied by the existing `db_migrate.py` (raw SQL, no alembic). All app tables live in the
`inkwest` schema; follow the existing migrations' `-- migrate:up` / `-- migrate:down`
markers and sized `VARCHAR` conventions.

```sql
-- DCR results, one row per catalog key that registered dynamically
CREATE TABLE inkwest.mcp_oauth_clients (
    mcp_key            VARCHAR(255) PRIMARY KEY,
    issuer             TEXT NOT NULL,
    token_endpoint     TEXT NOT NULL,   -- persisted at connect: refresh must not re-discover
    revocation_endpoint TEXT,
    client_id          TEXT NOT NULL,
    client_secret_ref  TEXT,            -- vault_ref, nullable (public client)
    client_expires_at  TIMESTAMPTZ,     -- RFC 7591 client_secret_expires_at; re-register past it
    registered_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- per-user grants (metadata only — token material lives in the vault)
CREATE TABLE inkwest.mcp_user_tokens (
    user_id        UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    mcp_key        VARCHAR(255) NOT NULL,
    vault_ref      TEXT NOT NULL,       -- opaque handle into the shared TokenVault
    expires_at     TIMESTAMPTZ,         -- nullable: some AS issue non-expiring tokens
    scopes         TEXT,
    status         VARCHAR(20) NOT NULL DEFAULT 'active',  -- active | expired | revoked
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, mcp_key)
);

-- pending authorization states; mirrors inkwest.connector_pending_states column-for-column
CREATE TABLE inkwest.mcp_oauth_pending_states (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    state         VARCHAR(255) NOT NULL UNIQUE,
    code_verifier VARCHAR(128) NOT NULL,
    mcp_key       VARCHAR(255) NOT NULL,
    user_id       UUID NOT NULL REFERENCES inkwest.users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- audit events for MCP grants are user-level (no conversation); relax the constraint
ALTER TABLE inkwest.connector_audit_log ALTER COLUMN session_id DROP NOT NULL;
```

Notes:
- The pending-state table mirrors `connector_pending_states` (which has `code_verifier` +
  `user_id`), **not** `oidc_pending_states` (which has neither). Reusing
  `connector_pending_states` directly was considered and rejected: its FK/cleanup lifecycle
  is owned by the connector subsystem and `mcp_key` doesn't fit its `connector` column
  domain. Expiry follows the repo convention (`created_at` + TTL constant).
- Sweeping: opportunistic delete on every `auth/start` **plus** a background sweep — the
  repo pattern is both (`auth_cleanup.py:22-39` runs a 60 s loop for OIDC states,
  `connector_cleanup.py` for connector states); register the MCP sweep alongside them.
- `status` uses the same vocabulary as `platform_grants`
  (`docs/connector-hot-auth.md` §3.1): `active | expired | revoked`. `expired` = grant
  needs a reconnect (what the API surfaces as `needs_auth`).
- The `connector_audit_log` ALTER is shared with the hot-auth design (its §5 audit
  extension needs the same relaxation) — whichever lands first carries it.
- Lifecycle gap to handle in code: removing a key from `MCP_CATALOG` orphans
  `mcp_user_tokens` rows and their vault blobs. Proxy fails closed (key unknown), but add
  an admin/startup sweep that revokes + deletes rows for keys no longer in the catalog.

SQLAlchemy models: one file per concept, matching the repo convention —
`app/models/mcp_oauth_client.py`, `app/models/mcp_user_token.py`,
`app/models/mcp_oauth_pending_state.py`.

### 2. Vault-backed crypto — `app/core/crypto.py`

- New dependency: `cryptography`. Add to `pyproject.toml [project.dependencies]`,
  `uv lock`.
- Implements the envelope scheme behind the `TokenVault` interface (see Storage section):
  data key per blob, KEK from KMS (instance-role) or sealed-at-startup. Settings:
  `token_vault_backend`, `token_vault_kms_key_id`, … — generic names, shared with the
  connector hot-auth work.
- Startup fails fast if oauth entries exist in the catalog but the vault is unconfigured.
- Audit trail: MCP grant lifecycle events are written to `connector_audit_log` with
  actions `mcp_connect`, `mcp_refresh`, `mcp_disconnect` (session_id NULL — user-level
  events), mirroring the `platform_*` actions in `docs/connector-hot-auth.md` §5. The
  squid egress log covers tool-call traffic; consent events belong in the audit table.

### 3. OAuth2 client — `app/services/mcp_oauth.py` + shared `app/services/oauth_flow.py`

Reuse, don't re-extract:
- `generate_pkce()` **already exists** as a shared helper in
  `app/services/connectors/base.py:97-107` — import it; do not duplicate it.
- Factor the endpoint-agnostic pieces out of the GitLab connector into
  `app/services/oauth_flow.py` (committed name): token-endpoint POST, refresh grant,
  best-effort RFC 7009 revoke (`connectors/gitlab.py:54-95`), state generation
  (currently inline in `app/api/routes/connectors.py:246`). The GitLab connector then
  consumes `oauth_flow.py` too. **This refactor is NOT currently covered by tests** — no
  test exercises `build_authorize_url`/`exchange_code`/`refresh`/`revoke`
  (`tests/test_connectors.py` covers only `generate_pkce`; API tests mock the connector
  object). Write characterization tests for the GitLab flow *before* moving code (see work
  item 10).

What stays MCP-specific — and why this is not simply a new connector kind: connectors
target one hard-coded platform with config-pinned endpoints; MCP oauth targets arbitrary
authorization servers (discovered at runtime), needs DCR, and persists per-user grants.

- `discover(server_url)` — unauthenticated request → `401` `WWW-Authenticate:
  resource_metadata=...` → fetch RFC 9728 doc → fetch AS metadata (RFC 8414 /
  `.well-known/oauth-authorization-server`). Fallback: probe well-known paths directly.
  **SSRF guards** (the backend has unrestricted egress, unlike the sandbox): the
  resource-metadata and issuer URLs must be `https` and share the catalog entry's origin
  (RFC 9728 binding); resolve and reject RFC 1918 / link-local / loopback targets before
  fetching. Discovery runs once per connect, and its results (`token_endpoint`,
  `revocation_endpoint`) are **persisted in `mcp_oauth_clients`** — the refresh hot path
  must never depend on a live metadata fetch (latency + an AS-metadata outage would break
  refresh while the refresh token is perfectly valid).
- `ensure_client(key)` — return catalog-pinned `client_id` or perform RFC 7591 DCR once,
  persist in `mcp_oauth_clients` (secret via vault). Single-flight per key (same
  `asyncio.Lock` registry as token refresh) plus `INSERT ... ON CONFLICT (mcp_key) DO
  NOTHING` + re-read, so two users connecting the same server concurrently cannot
  double-register. Store `client_secret_expires_at`; on expiry or `invalid_client`,
  re-register once (single-flight) and update the row. Caveat: if DCR succeeds at the AS
  but our insert fails, the retry registers a second client — harmless to us, untidy
  AS-side; log it.
- `build_authorize_url(key, user)` — `code_challenge` (S256), `state`
  (`token_urlsafe(32)`), `resource` param, scopes from catalog; persist pending state row.
- `exchange_code(state, code)` — validate state (single-use: delete row in the same
  transaction), token endpoint exchange with `code_verifier`, write vault blob, upsert
  `mcp_user_tokens`. If the exchange succeeds but the DB write fails, do **not** retry the
  exchange — the auth code is spent; surface the error and let the user redo the popup.
- `get_fresh_token(user_id, key, *, force=False)` — load blob; refresh when `force`, or
  when `expires_at` is non-null and within 60 s (null `expires_at` = treat as valid, rely
  on the proxy's 401-retry path). Refresh discipline — same rules as
  `docs/connector-hot-auth.md` §3.1, because OAuth 2.1 ASes rotate refresh tokens on use:
  - single-flight per (user, key) via an `asyncio.Lock` registry;
  - **after acquiring the lock, re-check**: if the token was refreshed in the last 30 s
    (by a competitor on the same lock), use it and skip the refresh — N concurrent 401s
    must not produce N back-to-back rotations;
  - **persist the rotated blob to the vault before first use** of the new access token;
  - if the vault write fails after the AS already rotated, mark the grant `expired`
    (best-effort revoke upstream) — the old refresh token is dead and the new one is
    RAM-only; pretending the grant is alive bricks it silently on process exit;
  - on `invalid_grant`: mark `expired`, raise;
  - negative cache: if a refresh against the AS failed < 30 s ago, fail fast instead of
    queueing on the lock — an AS outage with expired tokens must not become a sequential
    retry storm amplified by CLI tool-call retries.
- All outbound calls (discovery, token endpoint, DCR, revoke) use explicit timeouts —
  inherit the connector convention (`timeout=10.0`, `connectors/gitlab.py:92,112`). A hung
  token endpoint while holding the single-flight lock would otherwise wedge every
  conversation of that user (DCR: every user of that key).
- Lock registries: bounded by users × catalog size; mirror the existing unbounded
  `_locks` precedent (`sandbox.py:71`) but note a periodic sweep / `WeakValueDictionary`
  as the cleanup path.

### 4. Internal JWT — `app/services/mcp_proxy_token.py`

- HS256 via existing PyJWT + existing `Settings.secret_key`.
- Claims: `{sub: user_id, sid: session_id, key: mcp_key, aud: "mcp-proxy",
  type: "mcp-proxy"}`, TTL **6 h** (identifies only; the real credential stays
  server-side and is revocable; the file holding it is durable 0666 in a 0777 dir that
  survives soft teardown — a 24 h bearer there would be disproportionate).
- The verifier **must enforce** `aud == "mcp-proxy"` and `type == "mcp-proxy"` (PyJWT
  `audience=` + explicit claim check) — the signing key is shared with user-session and WS
  tokens (`app/core/auth.py`); without the audience check, token classes are
  interchangeable.
- `verify_proxy_jwt(request, key) -> ProxyClaims` returns a small frozen dataclass
  `ProxyClaims(user_id: UUID, session_id: UUID, mcp_key: str)` — one shape, no ad-hoc
  attribute naming.
- Scoping honesty: the proxy looks tokens up by `(user_id, mcp_key)`; `sid` is carried
  for log/audit correlation only, it does not narrow access (the grant is per-user, all of
  that user's conversations share it).
- Mint-stable: before minting, decode the JWT already present in the current config entry;
  if valid for this (user, session, key) with > 1 h remaining, keep it unchanged. The CLI
  restarts per message and `apply_session_mcp_selection()` runs each time
  (`orchestrator_cli.py:195`) — without this, a fresh JWT every message would defeat the
  `merged == config` no-op check in `mcp_config.py:131` and rewrite the config on every
  turn.

### 5. Sandbox reachability — squid allowlist + host-gateway

The toolbox container sits on an `--internal` Docker network with **no direct egress**; its
only route out is the per-conversation squid proxy with a domain allowlist
(`app/services/sandbox.py:5-7`), and the container env sets
`HTTP_PROXY=http://<proxy>:3128` with `NO_PROXY=localhost,127.0.0.1`
(`sandbox.py:282-290`). Consequences:

- `127.0.0.1` from inside the toolbox is the toolbox itself — the backend is **not**
  there. The proxy URL must use a hostname routed through squid.
- New setting `Settings.mcp_proxy_base_url` (e.g.
  `http://host-gateway-alias:8000`) — the hostname the CLI uses. No localhost default;
  when unset, oauth MCP injection into `.claude.json` is **silently skipped** (logged at
  debug in `mcp_config.py:_oauth_proxy_servers`), not a startup error — the feature is simply
  not wired. Startup fails fast only on `WEB_CONCURRENCY > 1` (`main.py`).
- Reachability mechanism — **`MCP_PROXY_BASE_URL` + the squid allowlist are the contract;
  the resolution path is topology-specific** (`sandbox.py`, the `mcp_proxy_base_url` block):
  - **prod / kube:** set `MCP_PROXY_BASE_URL` to the backend's **Service DNS** (e.g.
    `http://inkwest-backend.<ns>.svc.cluster.local:8000`) or its ingress URL. The squid
    container resolves it with its own resolver and the allowlist gates it — **no `--add-host`
    is injected.** This is the native path; nothing host-gateway-shaped is involved.
  - **local dev (docker-in-docker):** Docker's `host-gateway` magic resolves, *inside dind*,
    to dind's inner bridge gateway — a dead end (nothing serves the backend there), and there
    is no Service DNS. So the **sentinel host `host.docker.internal`** is special-cased: the
    backend maps it (`--add-host host.docker.internal:<backend-own-container-IP>`, IP discovered
    at provisioning via `socket.gethostbyname(socket.gethostname())`) to its own IP, which the
    nested squid reaches through dind's NAT. Portable across developers — **no hardcoded compose
    subnet** (do NOT pin dind's `--host-gateway-ip`; the subnet differs per machine/project).
    Assumes the backend's primary NIC is on the dind-shared network (true in the compose stack).
  - Do **not** attach the backend container to the per-conversation external networks instead:
    `_remove_pair()` removes only the conv+proxy containers before `network rm`, so a
    still-attached backend makes every network removal fail silently (`check=False`) on idle
    reaps and startup sweeps — leaked networks accumulate until Docker's address pool is
    exhausted and **all** sandbox provisioning fails platform-wide.
- The alias must be added to the squid domain allowlist (`sandbox_squid_conf`), and the
  squid config must be checked for SSE-compatibility: default `read_timeout` (15 min idle)
  will sever quiet long-lived streams — raise it for the backend host entry, and verify
  no response buffering directive applies. Pin both requirements in the squid conf
  shipped for sandboxes.
- Plain HTTP through squid is fine (no CONNECT needed) — **but the proxy's port must be in
  squid's `Safe_ports`.** `squid.conf` ships `Safe_ports = {80, 443}` and `http_access deny
  !Safe_ports` runs *before* the domain allow, so a backend served on e.g. `:8000` is
  rejected (`TCP_DENIED/403`) despite an allowlisted host — the OAuth2 MCP server then
  silently fails to load and the agent reports "no such connector". Add `acl Safe_ports port
  <mcp-proxy-port>` alongside the host allow in the conf.d fragment (it accumulates). No-op
  when the proxy is served on 443.
- Side effect, by design: every MCP tool call shows up in the per-conversation squid
  access log and therefore in the egress UI — useful traffic audit (consent events go to
  `connector_audit_log`, work item 2). The log records URLs only (the JWT is in a header);
  keep the proxy path/query data-free.

### 6. Proxy route — `app/api/routes/mcp_proxy.py`

Included with the `/api` prefix like the other REST routers (`app/main.py:161-169`), so
the full path is `/api/mcp-proxy/{key}/{path}`. This also keeps the prod SPA fallback's
`api/` 404-guard correct with no changes.

```python
@router.api_route("/mcp-proxy/{key}/{path:path}", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(key: str, path: str, request: Request) -> StreamingResponse:
    claims = verify_proxy_jwt(request, key)      # 401: bad sig / aud / type / key mismatch
    body = await request.body()                  # buffer once: needed again on 401-retry
    try:
        token = await get_fresh_token(claims.user_id, key)
    except GrantDead:
        raise HTTPException(401, "MCP authorization expired — reconnect in the panel")
    except VaultUnavailable:                     # Postgres/KMS down ≠ grant dead
        raise HTTPException(503, "MCP broker temporarily unavailable")
    upstream = await forward(key, path, request.headers, body, token, stream=True)
    if upstream.status_code == 401:              # skew / early revocation upstream
        await upstream.aclose()
        token = await get_fresh_token(claims.user_id, key, force=True)  # raises GrantDead
        upstream = await forward(key, path, request.headers, body, token, stream=True)
        if upstream.status_code == 401:
            await upstream.aclose()
            await mark_expired(claims.user_id, key)
            raise HTTPException(401, "MCP authorization expired — reconnect in the panel")
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=passthrough_headers(upstream),
        background=BackgroundTask(upstream.aclose),  # close on client disconnect too
    )
```

- Catch-all `{path:path}` because servers differ on `/mcp` vs `/`; catalog stores the full
  base URL, the proxy appends the path **as an opaque path segment** — it must not be able
  to inject a scheme/host (join against the parsed base URL, never string-concatenate into
  the authority part). Starlette only matches the catch-all with the literal `/` after
  `{key}` present — the injected `.claude.json` URL therefore ends with a trailing slash
  (work item 8); keep the two in sync or add an empty-path route variant.
- Request bodies are JSON-RPC and small — buffering once is fine and is what makes the
  401-retry correct (a consumed `Request` stream cannot be re-sent; a "pure streaming
  optimization" here silently breaks the retry — there is a test for body-intact-on-retry
  in work item 10). Only responses stream.
- Upstream client: `httpx.AsyncClient` with an **injectable transport** (constructor
  argument, module-level default) — both for connection pooling and so tests can substitute
  `httpx.MockTransport` without monkeypatching module globals. Timeouts: connect/write
  bounded (5 s / 30 s), read unbounded only for the streaming response phase (SSE), bounded
  for the initial response headers.
- Forward `Accept`, `Content-Type`, `Mcp-Session-Id`, **`MCP-Protocol-Version`** (required
  on every post-initialize request since the 2025-06-18 spec — compliant servers may 400
  without it) and **`Last-Event-ID`** (SSE stream resumption); strip hop-by-hop headers;
  swap `Authorization` for the upstream bearer.
- `BackgroundTask(upstream.aclose)` is mandatory: long-lived SSE responses must release
  the pooled connection when the CLI disconnects mid-stream, or connections leak under
  load.
- Error semantics: grant problems → 401 ("reconnect" UX); broker degradation (DB/vault/KMS
  unavailable) → 503 — the CLI and panel must be able to tell "you need to reconnect" from
  "the broker is having a moment".
- No `get_current_user` dependency on this route — the internal JWT *is* the auth.

### 7. API routes — extend `app/api/routes/mcp.py`

| Route | Behavior |
|---|---|
| `GET /api/mcp/catalog` | per-key `auth_status` (see mapping below); joins `mcp_user_tokens` for the current user. **Breaking response-shape change**: `{keys: [...]}` becomes `{servers: [{key, auth_status}]}` — `frontend/src/services/api.ts:233` and `McpPanel` update in lockstep (work item 9) |
| `POST /api/mcp/{key}/auth/start` | create pending state (sweeping expired rows), return `{authorize_url}` |
| `GET /api/mcp/oauth/callback` | validate `state`, exchange code, persist tokens, return tiny HTML that `window.close()`s the popup |
| `DELETE /api/mcp/{key}/auth` | mark `revoked`, delete vault blob, best-effort RFC 7009 revocation upstream, audit `mcp_disconnect` |

`auth_status` mapping (API vocabulary, derived from `mcp_user_tokens.status`):

| API `auth_status` | Meaning | Source |
|---|---|---|
| `static` | non-oauth entry, always usable | catalog def |
| `unavailable` | oauth entry but sandbox disabled | settings |
| `not_connected` | oauth entry, no grant row | no row |
| `connected` | grant `active` | row status |
| `needs_auth` | grant `expired` or `revoked` | row status |

`not_connected` vs `needs_auth` is deliberate: the panel says "Connect" for the former,
"Reconnect" for the latter.

The callback is **deliberately unauthenticated**: it is reached by an AS redirect in the
popup, which carries no `Authorization` header (frontend auth is a Bearer token in JS, not
a cookie). The single-use, short-expiry pending-state row is the sole binding between the
callback and a user — which is exactly why `state` must be unguessable (`token_urlsafe(32)`)
and deleted on first use.

Callback redirect URI = `<backend base URL>/api/mcp/oauth/callback`, single fixed value
registered at the AS / sent in DCR.

Known UX dilution: two catalog entries behind the *same* AS still mean two Connect popups
and (with DCR) two registrations — tokens are keyed `(user_id, mcp_key)`. Acceptable (the
browser's AS session makes the second consent fast); a later unification with
`platform_grants` could share the grant where the provider matches — see
`docs/connector-hot-auth.md` §4, which owns the user-level "Platforms" connect surface.

### 8. Token injection — extend `apply_session_mcp_selection()` (`app/services/mcp_config.py`)

For selected oauth entries, emit into the sandbox `.claude.json`:

```json
{
  "type": "http",
  "url": "http://host-gateway-alias:8000/api/mcp-proxy/notion/",
  "headers": {"Authorization": "Bearer <internal-jwt>"}
}
```

Signature change this implies — today the function is sync and user-blind
(`apply_session_mcp_selection(session_id, enabled_keys)`, `mcp_config.py:114`); minting
the JWT needs `user_id` and skipping dead grants needs an async DB read:

```python
async def apply_session_mcp_selection(
    session_id: str, enabled_keys: list[str], user_id: UUID, db: AsyncSession
) -> None: ...
```

Both call sites change: `app/api/routes/sessions.py` (PUT `/{session_id}/mcp`, which has
user + db in scope) and `app/services/orchestrator_cli.py:195` (which loads the session —
its `user_id` — right before). Non-oauth entries keep working if `db` is unused for them.

If the user's grant is not `active`, skip the entry, log a warning, **and emit an
in-conversation `mcp_auth_required` ChatEvent** (mirroring the existing
`connector_required` event the connector layer uses for exactly this case) so the user
sees "Notion MCP needs reconnecting" in the conversation instead of tools silently
vanishing while the panel still shows the server selected.

Base URL from `Settings.mcp_proxy_base_url` (work item 5). JWT minting is mint-stable
(work item 4) so unchanged selections remain a no-op write.

### 9. Frontend — `frontend/src/components/connector/McpPanel.tsx`

- Catalog response now carries `auth_status` per key (shape change, see work item 7):
  badge for `connected` / `needs auth`; "Connect" vs "Reconnect" per the status mapping;
  hide or grey out `unavailable` entries.
- "Connect" button: open the popup **synchronously in the click handler**
  (`window.open('about:blank')`), then `POST .../auth/start` and navigate the popup to the
  returned URL — calling `window.open` after the await trips popup blockers.
- Poll catalog every 2 s while popup open, stop on close → status flips to `connected`.
- Checkbox disabled while `needs_auth`/`not_connected`.
- Render the `mcp_auth_required` chat event as the existing `connector_required` banner
  pattern.
- `frontend/src/services/api.ts`: `startMcpAuth(key)`, `disconnectMcpAuth(key)`, updated
  `getMcpCatalog()` return type.

### 10. Tests

Conventions: `httpx.MockTransport` for all fake AS/upstream HTTP (no new dep — `respx` is
**not** in `pyproject.toml`; don't add it), `httpx.ASGITransport` for route tests,
injectable transport on the proxy's upstream client (work item 6), injected
clock/constructed timestamps for every time-threshold behavior (60 s refresh skew, > 1 h
mint-stable window, JWT `exp`) — PyJWT validates `exp` against the wall clock, so pass
`leeway`/freeze time to keep boundary tests deterministic.

- **Characterization first** (rollout step 2 precondition): tests for the GitLab connector
  flow as it exists today — `build_authorize_url`, `exchange_code`, `refresh`, `revoke`,
  `_post_token` against a `MockTransport` — written *before* factoring `oauth_flow.py`
  out, then kept green across the move. (No such coverage exists today;
  `test_connectors.py` covers only `generate_pkce`.)
- `backend/tests/test_mcp_oauth.py` — discovery chain incl. the no-401 well-known
  fallback branch and the SSRF guards (cross-origin metadata URL rejected, RFC 1918
  target rejected); PKCE exchange; refresh single-flight + post-lock 30 s recheck (no
  double rotation); persist-before-use ordering; vault-write-failure → `expired`;
  negative cache fail-fast; DCR single-flight; `invalid_grant` → `expired`;
  null-`expires_at` treated as valid; client re-registration on `invalid_client`.
  ON CONFLICT honesty: the SQL-level `ON CONFLICT` semantics can't be exercised through
  `AsyncMock(spec=AsyncSession)` and aiosqlite doesn't speak the migration's Postgres
  dialect — service-level race logic is tested with a fake repo layer; the SQL clause
  itself is covered only by the optional Postgres-backed e2e tier below.
- `backend/tests/test_mcp_proxy.py` — JWT validation (bad sig, wrong `aud`, wrong `type`,
  key mismatch, expired); header swap incl. `MCP-Protocol-Version`/`Last-Event-ID`
  passthrough; streaming passthrough; **body re-sent intact on the 401-retry**;
  upstream-401 → refresh-retry-once → second 401 → `expired` + 401; vault-down → 503
  (not 401); upstream close on client disconnect.
- `backend/tests/test_mcp_config.py` — mint-stable JWT (re-apply with valid existing JWT
  = no-op write); dead-grant entry skipped + `mcp_auth_required` event emitted; sandbox
  disabled = no-op.
- `backend/tests/test_api_mcp.py` — extend the existing file: catalog `auth_status` for
  all five values incl. `unavailable` (sandbox off) and `not_connected` vs `needs_auth`;
  `auth/start` sweeps expired pending states; **callback state replay rejected**
  (single-use — the one regression that is a CSRF/account-linking hole), unknown and
  expired state rejected; disconnect revokes + audits.
- Fail-fast startup checks (vault unconfigured with oauth entries; `mcp_proxy_base_url`
  unset): tested — fail-fast code that is never exercised tends not to fire.
- Vault contract test: put → get round-trip, delete → get None, blob opacity — written
  against the `TokenVault` protocol so every future backend (KMS, sealed, browser-DEK)
  runs the same suite (shared with `docs/connector-hot-auth.md`).
- Optional `@pytest.mark.e2e` (repo convention: skip when infra unavailable): the OAuth
  service against a reference MCP server / real Postgres — without one tier of unmocked
  coverage, a refactor can keep every mock green while breaking real discovery/DCR.
- Squid reachability is **not unit-testable** — rollout step 5 below carries an explicit
  compose smoke check (curl the proxy URL from inside a toolbox container) as the
  acceptance gate.
- Frontend: extend `McpPanel.test.tsx` using its existing pattern — `vi.mock` of
  `@/services/api` (not raw `fetch`-mocking): badge per status, Connect-vs-Reconnect,
  connect-flow happy path, popup-blocker ordering (synchronous `window.open` before the
  await).
- Run `pre-commit run --all-files` (detect-secrets: test fixtures must use obviously fake
  tokens).

## Security checklist

- Upstream tokens encrypted at rest via the shared vault (envelope; key custody per
  `docs/credentials-at-rest-audit.md` — never a bare key in env), never serialized to the
  frontend or sandbox.
- Discovery SSRF guards: https-only, origin-bound metadata URLs, private-range denial
  (work item 3).
- `state` is single-use with short expiry (CSRF on callback); PKCE S256 mandatory; the
  callback endpoint is intentionally unauthenticated — the pending-state row is the user
  binding (work item 7).
- Internal JWT: enforced `aud`/`type` (no token-class confusion on the shared signing
  key), 6 h TTL, scoped to one `(user, key)` grant — `sid` is correlation metadata, not an
  access scope. A leaked sandbox file cannot reach other MCPs or other users' grants.
- Fixed allowlisted redirect URI; reject callback `state` not found or expired.
- `DELETE .../auth` + user deletion (FK `ON DELETE CASCADE`) revoke access immediately —
  proxy lookups fail closed; catalog-key removal sweep cleans orphaned grants.
- Consent/lifecycle audit in `connector_audit_log` (`mcp_connect`/`mcp_refresh`/
  `mcp_disconnect`); traffic audit for free in the per-conversation egress log.

## Rollout order

0. **Transport validation**: plain static-header `type: http` catalog entry exercised
   end-to-end through the sandbox (squid allowlist + host-gateway alias) — proves the CLI
   MCP client honors `HTTP_PROXY` before anything else is built.
1. Migration + models + vault/crypto helper (inert without catalog entries).
2. Characterization tests for the GitLab OAuth flow, then the behavior-neutral
   `oauth_flow.py` factoring kept green against them.
3. OAuth service + auth routes (testable standalone against a reference MCP server).
4. Proxy route + internal JWT + injection in `apply_session_mcp_selection()` (incl. the
   async/user-aware signature change at both call sites).
5. Sandbox reachability: squid allowlist + SSE timeout config + host-gateway alias +
   `mcp_proxy_base_url`; acceptance gate = curl smoke test from inside a toolbox.
6. Frontend panel changes.
7. Add first `auth: "oauth2"` entry to `MCP_CATALOG`, configure the vault settings.
8. Doc updates in the same MR as the code they invalidate: `docs/MCP.md` (keys-only API →
   `auth_status`), `docs/DATABASE_SCHEMA.md` (three new tables + audit-log ALTER).

Each step is independently revertable; tables are additive, existing catalog entries and
stdio MCPs are untouched throughout.

## Out of scope (deliberate)

- Multi-replica / multi-worker backend: refresh and DCR single-flight locks are
  per-process; acceptable now (worst case a redundant refresh or a lost DCR race caught by
  ON CONFLICT). Revisit with Postgres advisory locks if replicas land.
- Token issuance for stdio MCPs — only `type: http` entries can be oauth.
- Admin UI for the catalog — catalog stays env-var driven.
- Converging connector (AWS/GitLab) warm-session storage onto the same vault — that is
  `docs/connector-hot-auth.md`'s scope; this plan only shares the interface and settings
  so the convergence is a backend swap, not a redesign.
- Browser-wrapped per-user DEK key custody (audit doc Phase C) — interface-compatible
  upgrade, not part of this plan.
