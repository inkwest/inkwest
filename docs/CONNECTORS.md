# External Access Connectors

Inkwest can broker **temporary, scoped credentials** to external services so the AI
can investigate beyond the cluster. Access is granted by explicit per-session
"Give access" buttons in the UI.

> **Principle:** Inkwest is a credential *broker*, never a long-term secret holder.
> No secret ever reaches the browser, the logs, or the model prompt. Credentials
> live in process RAM, keyed by `(user_id, session_id, connector)`, and expire.

| Connector | Credential | Lifetime | Scope |
|-----------|-----------|----------|-------|
| **AWS** | STS creds via IAM Identity Center SSO | ~1h | capped by the user's SSO permission sets, read-only verbs only |
| **GitLab** | OAuth access token (+ refresh) | ~2h | `read_api read_repository` only |

> **Coralogix** is no longer a connector. Observability (logs/metrics/traces) is
> provided per-conversation through the **MCP catalog** — see [MCP.md](MCP.md).

## Deployment constraint (read this first)

Credentials are held **in process RAM**. The OAuth callback, the REST calls, and
the chat WebSocket for a session must therefore hit the **same backend process**.
Deploy **single-replica** or with **session affinity** (route `/api/connectors/*`
and `/ws/chat/*` by session/user). Otherwise a credential minted on one pod is
invisible from another and the user just sees "not connected".

## Enabling

```bash
CONNECTORS_ENABLED=true
CONNECTOR_AUDIT_ENABLED=true          # default true; logs who granted what & when
# Callback URL the IdPs redirect to. Default: {FRONTEND_URL}/api/connectors/callback
CONNECTOR_REDIRECT_BASE=https://inkwest.example.com/api/connectors/callback
```

Run the migration (adds `connector_pending_states` + `connector_audit_log`):

```bash
cd backend && .venv/bin/python db_migrate.py
```

The backend image must contain the `aws` CLI (already added to `backend/Dockerfile`)
for the AWS `aws_call` tool.

---

## 1. AWS (IAM Identity Center)

### a. Prerequisites
- IAM Identity Center enabled, with **permission sets assigned** to your users.
  These cap what Inkwest can read — no escalation beyond the user's own SSO.
- Prefer **dedicated read-only permission sets** for Inkwest (defense in depth),
  even though the SSO already caps access.

### b. Register the OAuth application (one-time)

Authorization Code + PKCE needs an OIDC client registered with Inkwest's callback
URL. Register a **public** client (no secret) via `sso-oidc`:

```bash
aws sso-oidc register-client \
  --client-name "inkwest" \
  --client-type public \
  --grant-types authorization_code refresh_token \
  --scopes sso:account:access \
  --redirect-uris "https://inkwest.example.com/api/connectors/callback" \
  --region eu-west-1
```

> The `--redirect-uris` value must match `CONNECTOR_REDIRECT_BASE` **exactly**
> (scheme + host + path). In dev: `http://localhost:3000/api/connectors/callback`.

Keep the returned `clientId` (ignore `clientSecret` for a public client).

### c. Configure Inkwest

```bash
AWS_SSO_ENABLED=true
AWS_SSO_START_URL=https://my-org.awsapps.com/start
AWS_SSO_REGION=eu-west-1
AWS_SSO_OIDC_CLIENT_ID=<clientId from register-client>
# AWS_SSO_OIDC_CLIENT_SECRET=...   # only for a confidential client
# AWS_SSO_OIDC_ISSUER=...          # default: https://oidc.{AWS_SSO_REGION}.amazonaws.com
# AWS_CLI_PATH=aws                 # default
# AWS_SSO_ROLE_FILTER=read-?only$  # optional: case-insensitive regex matched on
#                                  # "<account_name>.<role_name>"; only matching roles
#                                  # are offered/mintable. Unset = all roles. Invalid
#                                  # regex = startup failure.
```

### d. Flow
1. User clicks **Give access to AWS** → redirect to Identity Center (PKCE).
2. Callback → Inkwest exchanges the code for an SSO token, then lists the
   accounts/roles the user's SSO allows (`ListAccounts` / `ListAccountRoles`).
3. User picks account + role → `GetRoleCredentials` mints ~1h STS creds (held in RAM).
4. Selecting roles also kicks off a per-session EKS kubeconfig refresh (clusters are
   discovered and one tmpfs kubeconfig is generated per cluster) — see
   [`EKS_REFRESH.md`](./EKS_REFRESH.md).
5. The AI can call `aws_call` — **read-only only** (`describe-*` / `get-*` / `list-*`;
   secret-returning ops and `secretsmanager`/`kms`/`sts` are denied).

---

## 2. GitLab (gitlab.com)

### a. Register an OAuth application
GitLab → **User/Group Settings → Applications**. Create an application:
- **Redirect URI:** `https://inkwest.example.com/api/connectors/callback`
- **Confidential:** No (public PKCE client)
- **Scopes:** `read_api`, `read_repository` **only** (never `api` or any write scope)

Copy the **Application ID**.

### b. Configure Inkwest

```bash
GITLAB_ENABLED=true
GITLAB_BASE_URL=https://gitlab.com
GITLAB_OAUTH_CLIENT_ID=<application id>
# GITLAB_OAUTH_SCOPES="read_api read_repository"   # default
```

### c. Flow
Click **Give access to GitLab** → OAuth (PKCE) → ~2h access token + refresh token in
RAM. The AI uses `gitlab_api` (GET-only) against `/api/v4/...`.

---

## Isolation & security

- **Per (user, session, connector)**: a credential minted in one session is never
  visible to another session or another user. With OIDC enabled, isolation is by
  user **and** session; in dev mode (`OIDC_ENABLED=false`) everyone shares
  `dev@localhost`, so isolation is by session only.
- **Never to the client/model/logs**: REST only returns a secret-free
  `ConnectorStatus`; AWS creds are injected into the subprocess **environment**
  (never argv); GitLab via an Authorization header at execution time. The
  model only names a tool + parameters.
- **Consent is bounded in time**: credentials expire (STS ~1h, GitLab ~2h). On
  expiry a tool returns a `connector_required` event and the UI prompts to re-grant.
- **Audit**: grants/selections/revocations are logged to `connector_audit_log`
  (no secrets) when `CONNECTOR_AUDIT_ENABLED=true`.
- **Revocation**: the **Revoke** button drops the credential immediately; deleting a
  session drops all of its credentials.

## Adding a connector later (e.g. Sentry)

1. Implement `Connector` in `backend/app/services/connectors/<name>.py`.
2. Register it in `backend/app/services/connectors/registry.py`.
3. Add its config block to `backend/app/core/config.py`.
4. Add a tool definition in `backend/app/tools/definitions.py` + an executor in
   `backend/app/tools/connector_exec.py` (dispatch in `backend/app/tools/dispatch.py`).

No other changes are needed — the broker, store, UI panel and audit are generic.
