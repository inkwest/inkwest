# Authentication

## Modes

### Single-User (default)

`OIDC_ENABLED=false` -- auto-creates a `dev@localhost` user with `admin` role. All routes are accessible without login.

```python
# app/api/dependencies.py
async def get_or_create_dev_user(db) -> User:
    # Creates User(email="dev@localhost", roles=["admin"], oidc_subject="dev-local")
```

### OIDC

`OIDC_ENABLED=true` -- standard OAuth2/OIDC authorization code flow.

```
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://accounts.google.com
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URI=http://localhost:8000/api/auth/callback
OIDC_SCOPES=openid profile email
OIDC_ROLES_CLAIM=roles        # claim path in ID token
OIDC_ADMIN_ROLE=admin          # role name that grants admin
```

**Flow:** `/api/auth/login` -> OIDC provider -> `/api/auth/callback` -> returns JWT session token.

**Session token:** HS256 JWT signed with `SECRET_KEY`, 24h expiry, payload `{sub: user_id, roles: [...]}`.

## Route Protection Matrix

| Route Pattern | Auth Level | Dependency |
|---|---|---|
| `/health`, `/health/ready` | Public | None |
| `/api/sessions/**` | Authenticated | `CurrentUser` |
| `/api/clusters` (GET) | Authenticated | `CurrentUser` |
| `/api/clusters` (POST/PATCH/DELETE) | Admin | `AdminUser` |
| `/api/admin/**` | Admin | `AdminUser` |
| `/ws/chat/{session_id}` | Authenticated | WebSocket auth |
| `/api/auth/**` | Public | None |

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

- `get_current_user`: returns dev user (single-user mode) or validates JWT (OIDC mode)
- `require_admin`: chains `get_current_user` + checks `"admin" in user.roles`

## WebSocket Auth

WebSocket connections use a short-lived ticket (10s TTL) obtained via the session JWT.
The long-lived session token never touches the WebSocket connection.

```
POST /api/auth/ws-ticket  (Authorization: Bearer <session-jwt>)
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
| `/api/auth/callback` | GET | Exchange code for token, create/update user |
| `/api/auth/logout` | POST | Client-side token discard |
| `/api/auth/ws-ticket` | POST | Issue short-lived WS ticket (requires Bearer token) |
| `/api/auth/me` | GET | Get current user info (requires Bearer token) |
