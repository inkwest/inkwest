"""
GitLab connector.

OAuth 2.0 Authorization Code + PKCE against gitlab.com as a public client (no
client secret). Read-only scopes only (``read_api read_repository``); a write or
full ``api`` scope is never requested. Mints a short-lived access token (~2h) plus
a refresh token, both held in RAM.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

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

logger = logging.getLogger(__name__)


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

    async def refresh(self, cred: MintedCredential) -> MintedCredential:
        """Mint a fresh access token from the held refresh token."""
        s = get_settings()
        refresh_token = cred.secret.get("refresh_token")
        if not refresh_token:
            raise ConnectorError("No refresh token available")
        data = {
            "client_id": s.gitlab_oauth_client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        token = await _post_token(s.gitlab_base_url, data)
        return _credential_from_token(token, s.gitlab_oauth_scopes)

    async def revoke(self, cred: MintedCredential) -> None:
        s = get_settings()
        access_token = cred.secret.get("access_token")
        if not access_token:
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{s.gitlab_base_url.rstrip('/')}/oauth/revoke",
                    data={"client_id": s.gitlab_oauth_client_id, "token": access_token},
                    timeout=10.0,
                )
        except httpx.HTTPError:
            # Best-effort: dropping the in-RAM credential is what actually matters.
            logger.debug("gitlab token revoke failed (best-effort)")

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
        except httpx.HTTPError as e:
            # Do not log the response body — it may carry tokens.
            raise ConnectorError(f"GitLab token exchange failed: {type(e).__name__}") from e


def _credential_from_token(token: dict[str, Any], scope: str) -> MintedCredential:
    access_token = token.get("access_token")
    if not access_token:
        raise ConnectorError("GitLab token response missing access_token")
    expires_in = int(token.get("expires_in", 7200))
    return MintedCredential(
        connector="gitlab",
        scope=scope,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        secret={
            "access_token": access_token,
            "refresh_token": token.get("refresh_token"),
            "token_type": token.get("token_type", "Bearer"),
        },
    )
