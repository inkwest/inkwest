"""
GitLab connector.

OAuth 2.0 Authorization Code + PKCE against gitlab.com as a public client (no
client secret). Read-only scopes only (``read_api read_repository``); a write or
full ``api`` scope is never requested. Mints a short-lived access token (~2h) plus
a refresh token, both held in RAM.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

import httpx

from app.core.config import get_settings
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorStatus,
    ConnectorType,
    ConnState,
    MintedCredential,
)

# Policy cap on how long a held refresh token may silently renew access tokens.
# GitLab refresh tokens never expire provider-side; without a cap a RAM credential
# would be renewable forever. 24h ≈ "re-consent once a day".
_RENEW_WINDOW: Final = timedelta(hours=24)


class GitLabConnector(Connector):
    type: ClassVar[ConnectorType] = "gitlab"
    requires_oauth: ClassVar[bool] = True

    @property
    def enabled(self) -> bool:
        s = get_settings()
        return bool(s.connectors_enabled and s.gitlab_enabled and s.gitlab_oauth_client_id)

    def build_authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        s = get_settings()
        if not self.enabled:
            raise ConnectorError("GitLab connector is not configured")
        params = {
            "client_id": s.gitlab_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": s.gitlab_oauth_scopes,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{s.gitlab_base_url.rstrip('/')}/oauth/authorize?{httpx.QueryParams(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> MintedCredential:
        s = get_settings()
        data = {
            "client_id": s.gitlab_oauth_client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        token = await _post_token(s.gitlab_base_url, data)
        return _credential_from_token(token, s.gitlab_oauth_scopes)

    async def refresh_from_token(self, refresh_token: str) -> MintedCredential:
        """Mint a fresh access token from a refresh token.

        The returned credential's secret includes the ROTATED refresh token (GitLab
        rotation is single-use) — the caller owns persisting it and stripping it
        from any session-scoped copy (see app.services.connectors.warm).
        """
        if not refresh_token:
            raise ConnectorError("No refresh token available")
        s = get_settings()
        data = {
            "client_id": s.gitlab_oauth_client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        token = await _post_token(s.gitlab_base_url, data)
        return _credential_from_token(token, s.gitlab_oauth_scopes)

    async def revoke(self, cred: MintedCredential) -> None:
        # Per-session revoke is a no-op provider-side, mirroring the AWS connector:
        # dropping the in-RAM session credential is the effective de-scoping. The
        # short-lived access token (~2h) expires on its own.
        #
        # Do NOT POST to /oauth/revoke here. In GitLab (Doorkeeper) the access token
        # and its refresh token share one grant row, so revoking the access token
        # also revokes the refresh token — which is exactly the user's WARM master
        # auth (see app.services.connectors.warm.store_gitlab_warm). That would brick
        # silent re-grant within the 24h warm window and force a full OAuth replay on
        # the very next grant. A future "disconnect platform" action is where the
        # provider-side refresh-token revoke + warm-grant clear belongs.
        return None

    def public_status(self, cred: MintedCredential | None) -> ConnectorStatus:
        if cred is None:
            return ConnectorStatus(connector="gitlab", state="not_connected")
        state: ConnState = "expired" if cred.is_expired() else "connected"
        return ConnectorStatus(
            connector="gitlab",
            state=state,
            expires_at=cred.expires_at,
            scope=cred.scope,
        )


async def _post_token(base_url: str, data: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{base_url.rstrip('/')}/oauth/token", data=data, timeout=10.0)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
        except httpx.HTTPStatusError as e:
            # 400/401 from the token endpoint = the grant itself is rejected
            # (invalid_grant, revoked client) — fatal. 5xx is a provider hiccup.
            fatal = e.response.status_code in (400, 401)
            # Do not log the response body — it may carry tokens.
            raise ConnectorError(
                f"GitLab token exchange failed: HTTP {e.response.status_code}", fatal=fatal
            ) from e
        except httpx.HTTPError as e:
            # Network-level failure (timeout, reset, DNS): the grant may be fine.
            raise ConnectorError(
                f"GitLab token exchange failed: {type(e).__name__}", fatal=False
            ) from e


def _credential_from_token(token: dict[str, Any], scope: str) -> MintedCredential:
    access_token = token.get("access_token")
    if not access_token:
        raise ConnectorError("GitLab token response missing access_token")
    expires_in = int(token.get("expires_in", 7200))
    refresh_token = token.get("refresh_token")
    now = datetime.now(UTC)
    return MintedCredential(
        connector="gitlab",
        scope=scope,
        expires_at=now + timedelta(seconds=expires_in),
        # With a refresh token held, the credential stays silently renewable past
        # access-token expiry — bounded by the policy window, not forever.
        renew_until=(now + _RENEW_WINDOW) if refresh_token else None,
        secret={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token.get("token_type", "Bearer"),
        },
    )
