# Connector "hot auth" — connect once per day, grant per conversation

> **TL;DR** — Split the current single per-conversation login into two layers:
> a durable **per-user platform grant** (refresh-capable warm session, stored as an
> opaque encrypted blob in a token vault), and short-lived **per-conversation derived
> credentials** (today's RAM `MintedCredential`, unchanged downstream). Connecting to
> AWS/GitLab happens roughly once per day; granting a conversation access becomes
> *enumerate → pick → mint* — a popup, never a login replay. The platform login flows
> we already have (device grant, OAuth+PKCE) stay as-is; they just feed the warm
> layer instead of being consumed per conversation.

Status: **design only** — no code changes. Durable secret storage is studied in
`docs/credentials-at-rest-audit.md` (key custody: KMS instance-role envelope or
sealed-at-startup KEK; optional browser-wrapped per-user DEK); this doc treats it as a
`TokenVault` interface (§5). The MCP OAuth2 proxy design
(`docs/mcp-oauth2-proxy.md`) stores its per-user tokens through the **same vault
interface and settings** — one storage mechanism, two consumers.

---

## 1. Current state — why users re-login so often

Credentials are keyed `(user_id, session_id, connector)` in a process-RAM store
(`backend/app/services/connectors/store.py`). Consequences:

| Re-login trigger | Cause |
|---|---|
| **Every new conversation** | Store key includes `session_id`; nothing is shared across conversations even for the same user, same day. |
| **AWS after ~1h** | `select_account_roles()` (aws.py) builds the final cred with **only** the STS profiles and **discards the SSO access token** — yet that token is good for ~8h and could re-mint STS silently. |
| **GitLab after ~2h** | `CredentialStore.get()` **evicts expired creds**, destroying the refresh token exactly when `gitlab_renew` would need it → 409 → full OAuth replay. The refresh path only works *before* expiry. |
| **Backend restart / redeploy** | RAM-only store; everything is lost. (`connector_pending_states` is durable, but it only covers the 10-min OAuth handshake.) |
| **Multi-replica** | Store is process-local; without session affinity a cred minted on one pod is invisible on another. |

What *is* already durable: `connector_audit_log` (who granted what, AWS selection
JSON) — `grants.py` reduces it to "renewable" connectors so the **selection** can be
replayed, but the **auth** still must be redone in full.

Additional findings (blockers for hot auth, fix regardless):

- `_create_token_device()` (aws.py) drops the `refreshToken` field of the
  `CreateToken` response on the floor — only `accessToken`/`expiresIn` are kept.
- The device client registration runbook (`CONNECTOR_AWS_SETUP.md` §1) registers
  **without** `--grant-types ... refresh_token`. Whether IdC then refuses to issue a
  refresh token is **to be verified empirically** — the runbook itself claims the device
  grant takes no explicit grant-types, and legacy `aws sso login` caches do contain
  `refreshToken` from clients registered that way. Either way the runbook and this doc
  currently contradict each other; migration step 3 includes the verification and the
  runbook update.
- `_aws_device_pending` (routes/connectors.py) is a RAM dict — same single-replica
  constraint as the store.
- GitLab scope is fixed at the OAuth app (`read_api read_repository`, user-wide):
  there is **no token-level way to narrow to one project**. Narrowing must be
  enforced broker-side (§3.3).

## 2. Platform "warm session" primitives

### GitLab (OAuth 2.0, public PKCE client)
- Every token response carries a **refresh token**; access tokens live ~2h
  (gitlab.com enforced).
- `grant_type=refresh_token` silently mints a new access token **and rotates the
  refresh token** (old one becomes invalid, single-use). No user interaction.
- Refresh tokens have no hard provider-side expiry → warm session is effectively
  unbounded until revoked. We bound it **by policy** (§3.4).
- Enumeration with any valid access token: `GET /api/v4/projects?membership=true`,
  `GET /api/v4/groups` — the project/group picker needs nothing extra.
- Revocation: `POST /oauth/revoke` (works for access and refresh tokens).

### AWS IAM Identity Center (sso-oidc device grant)
- `CreateToken` (device grant) returns an SSO **access token** (~8h `expiresIn`)
  and, *iff the OIDC client was registered with the `refresh_token` grant type*, a
  **refresh token**.
- `CreateToken` with `grant_type=refresh_token` silently renews the SSO access
  token for the duration of the **IdC session** (admin-configurable, 8h up to
  90 days). This is exactly what `aws sso login`'s cache does.
  **Prerequisite**: at the 8h default, the refresh token expires with the session and
  buys nothing over the ~8h access token — verify/extend the IdC session duration to
  ≥ the policy cap (§3.4) *before* investing in the refresh-token re-registration
  (migration step 3), or the whole step is dead weight.
- With a live SSO access token: `ListAccounts` + `ListAccountRoles` = enumeration
  (already implemented, incl. `AWS_SSO_ROLE_FILTER`), `GetRoleCredentials` = mint
  ~1h STS per account/role pair. **No user interaction for any of these.**
- Revocation: `sso-oidc Logout` invalidates the SSO session token; STS creds cannot
  be revoked early (drop + expire, as today).
- Operational: the registered client (`clientId`/`clientSecret`) itself expires
  ~90 days — unchanged from today.

### Generic (future connectors, e.g. Sentry)
Any OAuth provider with `offline_access`/refresh-token support fits the same shape:
durable refresh token = warm session; access token minted at conversation-grant
time; enumeration via the provider's "list my resources" API.

## 3. Proposed architecture

```
                    once per day                        per conversation
User ──(device/OAuth flow)──► PlatformGrant ──(enumerate+pick+mint)──► MintedCredential
                              per (user, platform)                     per (user, session, connector)
                              vault blob + DB metadata                 RAM store (unchanged)
                              refresh-capable                          STS profiles / access token
                                                                       → tmpfs ~/.aws, kubeconfigs, headers
```

### 3.1 Durable layer: `PlatformGrant`

New DB table `inkwest.platform_grants` (metadata only, **no secrets**; `inkwest`
schema like every other table):

| column | notes |
|---|---|
| `id`, `user_id` | one row per (user, platform); unique constraint |
| `platform` | reuse the existing `ConnectorType` literal (`connectors/base.py`) — do not mint a parallel vocabulary |
| `status` | `active` \| `expired` \| `revoked` |
| `vault_ref` | opaque handle into the token vault (§5) |
| `scope` | label, e.g. `sso:account:access`, `read_api read_repository` |
| `identity_hint` | display only: SSO start URL user, GitLab username |
| `created_at`, `last_refreshed_at`, `not_after` | `not_after` = policy cap (§3.4) |

Vault blob contents (opaque, encrypted, schema per platform):
- **AWS**: `{sso_access_token, sso_token_expires_at, sso_refresh_token?, region}`
- **GitLab**: `{refresh_token}` (access tokens are *not* persisted — re-minted on
  demand; keeps the blob single-purpose)

**Refresh-token rotation safety (GitLab AND AWS)**: rotation makes concurrent
refreshes self-destructive (two conversations refreshing at once → second one uses a
dead token → warm session bricked). GitLab rotates on every refresh; AWS IdC
`CreateToken(grant_type=refresh_token)` may also return a new `refreshToken` — apply
the same rules to both blobs:
- serialize per `(user_id, platform)` with an asyncio lock; after acquiring the lock,
  re-check — if a competitor refreshed within the last ~30 s, reuse its result instead
  of rotating again;
- persist the rotated token to the vault **before** using the new access token;
- if the vault write fails *after* the provider already rotated, mark the grant
  `expired` (best-effort provider revoke) — the old token is dead and the new one is
  RAM-only; leaving the grant `active` bricks it silently at process exit;
- on refresh failure (`invalid_grant`), mark the grant `expired` → user reconnects
  (once).

The `status` vocabulary (`active | expired | revoked`) and these rotation rules are
shared with `mcp_user_tokens` in `docs/mcp-oauth2-proxy.md` — one convention, one
shared keyed-lock utility, two consumers.

### 3.2 Derived layer: per-conversation credentials (mostly unchanged)

The existing RAM `CredentialStore`, `MintedCredential`, tmpfs `~/.aws` projection,
EKS kubeconfig refresh, and tool executors stay exactly as they are. What changes
is **where the input comes from**: instead of a fresh interactive login per
conversation, derivation reads the warm `PlatformGrant`:

- **AWS grant to conversation**: load blob → (refresh SSO token if `expires_at`
  near) → `ListAccounts`/`ListAccountRoles` (+ role filter) → selection popup →
  `GetRoleCredentials` × N → `MintedCredential` with STS profiles → store/put,
  `write_aws_profiles`, `schedule_session_eks_refresh` (existing tail,
  `_apply_aws_selection`).
- **GitLab grant to conversation**: load blob → refresh-token grant → new ~2h
  access token → `MintedCredential` (the *rotated refresh token goes back to the
  vault*, **not** into the per-session cred) → project picker (§3.3).

### 3.3 Scope narrowing at derivation

- **AWS**: native — the narrowing *is* the account/role pick; STS creds carry only
  the chosen permission sets. Unchanged.
- **GitLab**: the access token is user-wide; narrowing is **broker-enforced**, with
  two hard caveats:
  1. **It only holds on the `gitlab_api` executor path.** In CLI-sandbox mode the raw
     token is injected into the toolbox as `GITLAB_TOKEN` and the agent `curl`s GitLab
     directly (`backend/app/tools/connector_exec.py:240,264-268`) —
     `validate_gitlab_api` never sees those requests, so the agent reaches every
     endpoint the user can. Hot auth makes that token derive from a durable 24h grant,
     i.e. strictly worse. Target state: **stop injecting the raw token in sandbox
     mode** and route GitLab calls through a backend broker endpoint (same
     token-injecting-proxy pattern as `docs/mcp-oauth2-proxy.md`). Until that lands,
     the scoping claim applies to API-executor mode only — say so in the UI/docs, do
     not advertise per-project scoping for sandbox conversations.
  2. **Prefix matching must canonicalize.** GitLab accepts URL-encoded namespace paths
     (`/projects/group%2Fproj`) and nested-group routes; naive id-or-path prefix
     checks leak. Resolve the user's picks to **numeric project/group IDs at grant
     time**, and have the validator URL-decode, resolve path-form references to IDs,
     and compare IDs only. Global endpoints are deny-by-default with a small read-only
     allowlist (`/user`, `/version`).
  The per-conversation popup lists projects/groups (`membership=true`); the selection
  is stored in `MintedCredential.selection` (e.g. `{"projects": [{id, path}, ...]}`).
  Defense in depth on top of the already-read-only scopes — honest framing: it bounds
  the *agent on the validated path*, not a stolen token.

### 3.4 Expiry & renewal strategy

| layer | lifetime | renewal |
|---|---|---|
| Platform grant (warm) | `not_after` = min(provider session, **policy cap**, setting `connector_grant_max_age: int = 86400` — seconds, like every duration in `config.py`) | interactive reconnect (the once-per-day login) |
| AWS SSO access token | ~8h | silent, refresh token, at derivation time and lazily mid-conversation |
| AWS STS profiles | ~1h | **silent re-mint**: when a tool execution (or a pre-flight check with ~5 min skew) finds the cred expired, re-derive from the warm grant with the *saved selection* — no popup, then rewrite tmpfs `~/.aws` (kubeconfigs reference profiles by name → no kubeconfig change needed) |
| GitLab access token | ~2h | silent refresh-token grant at derivation and mid-conversation (same lazy trigger), selection preserved |

Mid-conversation flow: tool executor detects expired/near-expiry cred → calls a
`rederive(user, session, connector)` service (proposed home:
`app/services/connectors/rederive.py`; platform endpoints in a new
`app/api/routes/platforms.py`, vault in `app/services/token_vault.py`) → success:
transparent; failure (warm grant dead): emit the existing `connector_required` event →
UI shows "Reconnect AWS" (the daily login), then auto-re-applies the saved selection
(today's `renew-select` logic, kept).

Re-derivation discipline (a burst of concurrent tool executions all finding the cred
expired must not fan out):
- single-flight per `(user, session, connector)` — same keyed-lock utility as §3.1;
- tmpfs `~/.aws` rewrite is **atomic** (write temp file + `os.replace`) — in-flight
  `aws` subprocesses must never read a partially written profile file;
- `schedule_session_eks_refresh` is keyed to the old credential's expiry — re-mint
  must **reschedule** it against the new expiry or kubeconfig refresh dies with the
  old timer.

This also fixes restart amnesia: after a backend restart, the RAM store is empty
but `platform_grants` + audit-log selections survive → first tool call (or panel
load) silently re-derives. The single-replica constraint then only covers a
seconds-long window instead of credential lifetimes.

## 4. UX flow

**Connect (once per day, user-level — not tied to a conversation):**
1. New "Platforms" block (settings page and/or first-use prompt): `Connect AWS`,
   `Connect GitLab` with status badge `connected · until 18:42` / `not connected`.
2. Clicking runs today's exact flows (AWS device code popup, GitLab OAuth
   redirect) — but the result lands in the vault as a `PlatformGrant`.

**Grant a conversation (the popup, no login):**
1. In the conversation, `Give access` on AWS/GitLab.
2. Warm grant exists → backend enumerates immediately (accounts/roles or
   projects/groups) → selection modal (reuse `AwsAccountRoleModal`; new analogous
   GitLab project picker — unlike the bounded AWS list, `membership=true` can return
   hundreds of paginated projects, so the picker needs search + pagination) →
   `Grant` → connected. Total: one popup, two clicks.
3. No warm grant (first time today / expired / revoked) → inline fallback to the
   connect flow, then continue at step 2 automatically.

**Renew**: the `Renew access` button becomes silent re-derivation (no device code
modal) whenever the warm grant is live; it only surfaces the login when the daily
grant itself has expired.

**New/changed endpoints (sketch):**
```
GET    /api/platforms                          # per-user grant statuses (secret-free)
POST   /api/platforms/{platform}/connect       # start device/OAuth flow (user-level)
POST   /api/platforms/{platform}/disconnect    # revoke warm grant (vault delete + provider revoke)
GET    /api/connectors/{sid}/{conn}/options    # enumerate from warm grant (AWS exists; add GitLab)
POST   /api/connectors/{sid}/{conn}/grant      # pick → derive → MintedCredential
POST   /api/connectors/{sid}/{conn}/revoke     # unchanged (conversation-level)
```

Note: the existing route family is connector-hardcoded (`/{sid}/aws/options`,
`/aws/select`, `/gitlab/renew` — `routes/connectors.py`); the generic
`/{sid}/{conn}/...` shape above implies a small route refactor — keep the old paths as
aliases during migration.

**Relationship to the MCP Connect surface**: `docs/mcp-oauth2-proxy.md` adds a per-key
"Connect" button in `McpPanel` backed by `mcp_user_tokens`. If a catalog MCP server
uses the same provider as a platform (e.g. a GitLab MCP), users would see two Connect
surfaces for one provider. The Platforms block (here) owns user-level identity; a
later unification can back matching MCP entries onto the same `platform_grants` row.
Until then both docs cross-reference this as a known, deliberate duplication.

## 5. Security

**Token vault interface** (storage hardening studied separately — this is the
contract the connector layer codes against):

```python
class TokenVault(Protocol):
    async def put(self, *, user_id: UUID, platform: str, blob: bytes) -> str: ...  # → vault_ref
    async def get(self, vault_ref: str) -> bytes | None: ...
    async def delete(self, vault_ref: str) -> None: ...
```

Blobs are opaque and encrypted at rest by the vault implementation — envelope
encryption with key custody per `docs/credentials-at-rest-audit.md` Phase B (KMS
instance-role, or sealed-at-startup; **not** a bare key in env), implemented once in
`app/core/crypto.py` and shared with the MCP OAuth tokens
(`docs/mcp-oauth2-proxy.md`). The connector layer never logs blob contents and never
returns them via REST (the `ConnectorStatus`/`PlatformGrant` projections stay
secret-free by construction, same invariant as `MintedCredential.secret` today).

**Threat delta vs. today** (be explicit): a durable refresh token outlives any RAM
credential and survives restarts — strictly more valuable to an attacker with DB or
vault access. Mitigations: read-only scopes only (unchanged), policy cap
`not_after` (default 24h — the "per day" in hot auth is a *security control*, not
just UX), per-user isolation, vault encryption, and first-class revocation:

- **Conversation revoke**: unchanged (drop derived cred + tmpfs cleanup).
- **Platform disconnect**: delete vault blob + provider-side revoke
  (GitLab `/oauth/revoke` on the refresh token; AWS `sso-oidc Logout`); cascades a
  drop of every derived cred for that user.
- **Admin kill switch**: mark all grants `revoked` (incident response).

**Audit** (`connector_audit_log`, extended `action` values): `platform_connect`,
`platform_refresh` (silent renewals are still consent-relevant events),
`platform_disconnect`, plus existing `grant`/`select`/`revoke` for the conversation
layer. **One migration needed**: `session_id` is `NOT NULL` today
(`migrations/20260531000000_connectors.sql`) and platform events are user-level —
`ALTER ... ALTER COLUMN session_id DROP NOT NULL` (shared with
`docs/mcp-oauth2-proxy.md` work item 1; whichever lands first carries it). New rows
always carry `user_id`. GitLab project selections land in `selection` JSONB like AWS
profiles do, so "which projects did conversation X touch" is answerable.

## 6. Migration path from the RAM store

Incremental, each step shippable, feature-flagged `CONNECTOR_HOT_AUTH_ENABLED`:

1. **Bug fixes (independent of hot auth)**: keep evicted GitLab refresh tokens
   usable, and keep the AWS SSO access token alongside STS profiles in the final
   `MintedCredential`, re-minting STS from it silently while it lives (~8h instead of
   ~1h between logins, still RAM-only). The eviction fix is a **contract change** to
   `CredentialStore.get()` (`store.py:44-53` currently deletes expired creds and
   returns `None`; callers rely on that): add `get(include_expired=True)` (or a
   `peek()`) used only by the renew path, keep the default behavior for every other
   call site. Note: `tests/test_connectors.py:50` *asserts the buggy eviction
   behavior* — rewrite it alongside a new regression test "expired cred keeps its
   refresh token usable for renew".
2. **Vault + table**: add `TokenVault` (envelope encryption per
   `docs/credentials-at-rest-audit.md` Phase B, implemented in `app/core/crypto.py`,
   shared with the MCP OAuth design) + `inkwest.platform_grants` migration.
3. **Dual-write**: successful device/OAuth flows also persist warm material to the
   vault keyed per *user*. Existing per-session flows untouched — pure addition.
   Verify empirically whether IdC issues `refreshToken` to the current client; if
   not, re-register the AWS OIDC client with the `refresh_token` grant type, capture
   `refreshToken` from `CreateToken`, and **update `CONNECTOR_AWS_SETUP.md` §1** (it
   currently contradicts this doc on grant-types).
4. **Derivation path**: new platform endpoints + `grant-from-platform` derivation;
   frontend popup-only grant when a warm grant is live, fallback otherwise. RAM
   store remains the only home of derived creds (single-replica constraint shrinks
   to the derivation round-trip; full multi-replica needs the already-anticipated
   shared-store swap in `store.py`, unchanged scope).
5. **Cutover**: per-session login flows demoted to the fallback inside the connect
   path; `renewable`/`renew-select` logic folds into silent re-derivation. Remove
   `_aws_device_pending` RAM dict (device handshakes move next to
   `connector_pending_states` in the DB).
6. **Cleanup**: drop the flag once stable.

No data migration needed: the RAM store has nothing durable to migrate; audit-log
selections already provide the replayable per-conversation picks.

Docs to update in the same MRs as the code that invalidates them:
`docs/CONNECTORS.md` ("never a long-term secret holder … live in process RAM" — no
longer true once the vault lands), `docs/DATABASE_SCHEMA.md` (`platform_grants` +
audit-log ALTER), `docs/CONNECTOR_AWS_SETUP.md` §1 (grant-types, see step 3).

## 7. Test plan

Conventions: `httpx.MockTransport` for provider HTTP (no new dep), injected
clock/constructed timestamps for every expiry threshold (the existing
`datetime.now(UTC) ± timedelta` style), `asyncio_mode = "auto"` for concurrency tests.
Every step ships flag-on **and** flag-off coverage while `CONNECTOR_HOT_AUTH_ENABLED`
exists.

- **Step 1 regression tests (before the fix lands)**: expired GitLab cred retains a
  usable refresh token through `renew` (rewrites `test_connectors.py:50`, which
  currently asserts the eviction); STS re-mint from a retained SSO token; existing
  callers of `CredentialStore.get()` still see `None` for expired creds.
- **Vault contract test** (shared with the MCP design): put → get round-trip,
  delete → get `None`, blob opacity — written against the `TokenVault` protocol so
  every backend (envelope-KMS, sealed, future browser-DEK) runs the same suite.
- **Rotation safety**: concurrent refreshes serialize (single-flight + post-lock
  recheck → exactly one provider call); rotated token persisted before use;
  vault-write failure → grant `expired` + best-effort revoke; `invalid_grant` →
  `expired`.
- **Re-derivation**: lazy re-mint single-flight per (user, session, connector);
  atomic `~/.aws` rewrite (no partial reads); EKS refresh rescheduled to the new
  expiry; dead warm grant → `connector_required` event, saved selection replayed
  after reconnect.
- **GitLab narrowing**: validator resolves URL-encoded/path-form references to
  numeric IDs; out-of-selection project rejected; global endpoints deny-by-default
  with the read-only allowlist.
- **Routes**: `/api/platforms` status projection is secret-free; connect/disconnect
  audit rows written (user-level, `session_id NULL`).
- Optional `@pytest.mark.e2e` against real gitlab.com / IdC sandboxes (skip when
  infra unavailable) — mocked rotation semantics must be validated against the real
  providers at least once.
