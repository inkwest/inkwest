# Authentication

## Modes

Inkwest ships with **two intentional auth modes**, selected by the single
`OIDC_ENABLED` flag. The default (SSO off) is a self-contained single-user mode;
flip the flag to switch to multi-user authenticated access.

| | `OIDC_ENABLED=false` (default) | `OIDC_ENABLED=true` |
|---|---|---|
| Who can access | Anyone reaching the app — no login | Only users authenticated via the IdP |
| Identity | Auto-created `dev@localhost`, `admin` role | Real user from the OIDC ID token |
| Roles / admin | Always admin | Driven by `OIDC_ROLES_CLAIM` / `OIDC_ADMIN_ROLE` |
| Intended for | Local dev, single-operator / mono-user deploys | Shared / multi-user / production deploys |

### Single-User (default)

`OIDC_ENABLED=false` -- auto-creates a `dev@localhost` user with `admin` role. All routes are accessible without login.

```python
# app/api/dependencies.py
async def get_or_create_dev_user(db) -> User:
    # Creates User(email="dev@localhost", roles=["admin"], oidc_subject="dev-local")
```

**This automatic admin is by design, not a misconfiguration.** With SSO off there
is no identity provider to authenticate against, so the app runs as a single
trusted operator with full rights — the expected model for local development and
single-user deployments. The same default applies across HTTP and WebSocket: all
skip token validation — `get_current_user` and the chat WS
(`validate_websocket_auth`) return the dev user, while the sync WS
(`_authenticate_sync_ws`) simply authorizes the connection.

> **Security:** single-user mode performs **no authentication**. Only run it on a
> trusted host / network (localhost, a private deployment you alone reach). To
> expose Inkwest to multiple users or the public, enable SSO (below).

### OIDC (enabling SSO)

`OIDC_ENABLED=true` -- standard OAuth2/OIDC authorization code flow. This is the
switch from single-user to multi-user authenticated access.

```
OIDC_ENABLED=true                                              # required: turns SSO on
OIDC_ISSUER_URL=https://accounts.google.com                    # required
OIDC_CLIENT_ID=...                                             # required
OIDC_CLIENT_SECRET=...                                         # required
OIDC_REDIRECT_URI=http://localhost:8000/api/auth/callback      # required: must match IdP config
OIDC_SCOPES=openid profile email                               # optional (default shown)
OIDC_ROLES_CLAIM=roles        # optional: claim path in ID token (default: roles)
OIDC_ADMIN_ROLE=admin          # optional: role name that grants admin (default: admin)
```

Register the `OIDC_REDIRECT_URI` as an allowed callback in your IdP. Once these
are set and the app is restarted, the single-user dev admin is no longer issued —
every request must carry a valid session JWT obtained through the flow below.

> **Set all four `required` vars together.** `get_current_user` switches to
> token-required mode on the `OIDC_ENABLED` flag **alone**, but `/api/auth/login`
> and `/api/auth/callback` only work once `OIDC_ISSUER_URL` and `OIDC_CLIENT_ID`
> are also present (`OIDCClient.is_enabled`). A partial config (flag on, issuer or
> client id missing) **locks everyone out**: every request 401s while `/login`
> returns `400 OIDC authentication is not enabled` — there is no startup
> validation to catch this.

**Flow:** `/api/auth/login` -> OIDC provider -> `/api/auth/callback` -> sets the
session JWT as an HttpOnly cookie and redirects to the SPA root. The token is
never placed in the URL fragment or in `localStorage`.

**Session token:** HS256 JWT signed with `SECRET_KEY`, 24h expiry, payload
`{sub: user_id, roles: [...]}`. Delivered as a cookie named `session_token`:
`HttpOnly` (unreadable by JS — an XSS can't exfiltrate it), `SameSite=Lax`,
`Path=/`, and `Secure` in production (`APP_ENV=production`). The browser attaches
it automatically; the SPA sends `credentials: 'include'` on every fetch.

> **Same-site requirement.** `SameSite=Lax` means the browser only sends the
> cookie when the SPA is *same-site* as the API. The combined prod image serves
> the SPA same-origin, and dev (`:3000` -> `:8000`) is same-site `localhost`, so
> both work. A **cross-site** SPA host (e.g. `app.example.com` calling
> `api.example.org`) will silently withhold the cookie and every request 401s —
> the startup log warns when `CORS_ORIGINS` is overridden for this reason.
> Cross-site bearer tokens (the previous scheme) are no longer accepted.

## Route Protection Matrix

| Route Pattern | Auth Level | Dependency |
|---|---|---|
| `/health`, `/health/ready` | Public | None |
| `/api/sessions/**` | Authenticated | `CurrentUser` |
| `/api/clusters` (GET) | Authenticated | `CurrentUser` |
| `/api/clusters` (POST/PATCH/DELETE) | Admin | `AdminUser` |
| `/api/admin/**` | Admin | `AdminUser` |
| `/ws/chat/{session_id}` | Authenticated | WebSocket auth |
| `/api/auth/**` | Public | None (except `/me`, `/ws-ticket`, `/logout` → `CurrentUser`) |

## Dependencies

```python
from app.api.dependencies import CurrentUser, AdminUser

# CurrentUser = Annotated[User, Depends(get_current_user)]
# AdminUser = Annotated[User, Depends(require_admin)]

@router.get("/something")
async def endpoint(user: CurrentUser): ...

@router.post("/admin-thing")
async def admin_endpoint(admin: AdminUser): ...
```

- `get_current_user`: returns dev user (single-user mode) or validates the JWT from the `session_token` cookie (OIDC mode)
- `require_admin`: chains `get_current_user` + checks `"admin" in user.roles`

## WebSocket Auth

WebSocket connections use a short-lived ticket (10s TTL). The long-lived session
token never touches the WebSocket connection — only the cookie-authed ticket POST.

```
POST /api/auth/ws-ticket  (auth via the session_token cookie)
  → { "ticket": "<jwt: sub + type:ws + exp:10s>" }

new WebSocket(url, ['inkwest', `ticket|<mini-jwt>`])
// Header: Sec-WebSocket-Protocol: inkwest, ticket|<ws-ticket>
```

The server extracts the ticket from the `ticket|` prefixed subprotocol, verifies
signature + `type=="ws"` + expiry, looks up the user, and accepts with `subprotocol="inkwest"`.

Single-user mode skips ticket validation and returns the dev user.

## Role-Based Cluster Access

Users see only clusters allowed by their roles via `RoleKubeconfigMapping`:

```python
# Returns None (unrestricted) for admins or when no mappings configured
# Returns set of cluster UUIDs for non-admin users
allowed_ids = await get_user_allowed_cluster_ids(user, db)
```

Admins always have unrestricted access. Non-admin users without matching role mappings get an empty set (no access) when any mappings exist in the system.

## Auth Routes

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/status` | GET | Check if OIDC enabled + current auth state |
| `/api/auth/login` | GET | Redirect to OIDC provider |
| `/api/auth/callback` | GET | Exchange code, create/update user, set session cookie, redirect to SPA |
| `/api/auth/logout` | POST | Clear the HttpOnly session cookie (requires the session cookie — anti logout-CSRF) |
| `/api/auth/ws-ticket` | POST | Issue short-lived WS ticket (requires the session cookie) |
| `/api/auth/me` | GET | Get current user info (requires the session cookie) |
