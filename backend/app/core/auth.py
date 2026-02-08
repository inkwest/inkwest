"""
OIDC Authentication module.

Handles OAuth2/OIDC authentication flow and JWT session management.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# JWT settings
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24
WS_TICKET_EXPIRATION_SECONDS = 10


class AuthError(Exception):
    """Authentication error."""

    pass


class OIDCClient:
    """
    OIDC client for authentication.

    Handles discovery, token exchange, and ID token validation.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._discovery_doc: dict[str, Any] | None = None
        self._jwks_client: PyJWKClient | None = None

    @property
    def is_enabled(self) -> bool:
        """Check if OIDC is enabled and configured."""
        return (
            self.settings.oidc_enabled
            and self.settings.oidc_issuer_url is not None
            and self.settings.oidc_client_id is not None
        )

    async def get_discovery_document(self) -> dict[str, Any]:
        """Fetch OIDC discovery document."""
        if self._discovery_doc is not None:
            return self._discovery_doc

        if not self.settings.oidc_issuer_url:
            raise AuthError("OIDC issuer URL not configured")

        discovery_url = (
            f"{self.settings.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration"
        )

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(discovery_url, timeout=10.0)
                response.raise_for_status()
                self._discovery_doc = response.json()
                return self._discovery_doc
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch OIDC discovery document: {e}")
                raise AuthError(f"Failed to fetch OIDC configuration: {e}")

    async def get_authorization_url(self, state: str) -> str:
        """
        Get the authorization URL for OIDC login.

        Args:
            state: Random state for CSRF protection

        Returns:
            Authorization URL to redirect user to
        """
        discovery = await self.get_discovery_document()
        auth_endpoint = discovery.get("authorization_endpoint")

        if not auth_endpoint:
            raise AuthError("Authorization endpoint not found in discovery document")

        scopes = self.settings.oidc_scopes.split()
        redirect_uri = (
            self.settings.oidc_redirect_uri or f"{self.settings.frontend_url}/api/auth/callback"
        )

        params = {
            "client_id": self.settings.oidc_client_id,
            "response_type": "code",
            "scope": " ".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
        }

        return f"{auth_endpoint}?{httpx.QueryParams(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """
        Exchange authorization code for tokens.

        Args:
            code: Authorization code from callback

        Returns:
            Token response containing access_token, id_token, etc.
        """
        discovery = await self.get_discovery_document()
        token_endpoint = discovery.get("token_endpoint")

        if not token_endpoint:
            raise AuthError("Token endpoint not found in discovery document")

        redirect_uri = (
            self.settings.oidc_redirect_uri or f"{self.settings.frontend_url}/api/auth/callback"
        )

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.settings.oidc_client_id,
            "client_secret": self.settings.oidc_client_secret,
            "redirect_uri": redirect_uri,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    token_endpoint,
                    data=data,
                    timeout=10.0,
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPError as e:
                logger.error(f"Failed to exchange authorization code: {e}")
                raise AuthError(f"Failed to exchange code: {e}")

    async def validate_id_token(self, id_token: str) -> dict[str, Any]:
        """
        Validate and decode an ID token.

        Args:
            id_token: The ID token from token exchange

        Returns:
            Decoded token claims
        """
        discovery = await self.get_discovery_document()
        jwks_uri = discovery.get("jwks_uri")

        if not jwks_uri:
            raise AuthError("JWKS URI not found in discovery document")

        # Get JWKS client
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(jwks_uri)

        try:
            # Get signing key
            signing_key = self._jwks_client.get_signing_key_from_jwt(id_token)

            # Decode and validate token
            claims: dict[str, Any] = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.oidc_client_id,
                issuer=self.settings.oidc_issuer_url,
            )

            return claims

        except jwt.ExpiredSignatureError:
            raise AuthError("ID token has expired")
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid ID token: {e}")
            raise AuthError(f"Invalid ID token: {e}")

    def extract_roles(self, claims: dict[str, Any]) -> list[str]:
        """
        Extract roles from token claims.

        Args:
            claims: Decoded token claims

        Returns:
            List of role names
        """
        # Navigate the claim path (e.g., "realm_access.roles" for Keycloak)
        claim_path = self.settings.oidc_roles_claim.split(".")
        value = claims

        for key in claim_path:
            if isinstance(value, dict):
                value = value.get(key, [])
            else:
                return []

        if isinstance(value, list):
            return [str(r) for r in value]
        elif isinstance(value, str):
            return [value]

        return []

    def extract_user_info(self, claims: dict[str, Any]) -> dict[str, Any]:
        """
        Extract user information from token claims.

        Args:
            claims: Decoded token claims

        Returns:
            Dict with user info
        """
        return {
            "oidc_subject": claims.get("sub"),
            "email": claims.get("email", claims.get("preferred_username", "")),
            "display_name": claims.get("name", claims.get("email", "Unknown")),
            "roles": self.extract_roles(claims),
        }


def create_session_token(user_id: UUID, roles: list[str]) -> str:
    """
    Create a JWT session token for the user.

    Args:
        user_id: User's UUID
        roles: User's roles

    Returns:
        JWT token string
    """
    settings = get_settings()
    now = datetime.now(UTC)

    payload = {
        "sub": str(user_id),
        "roles": roles,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRATION_HOURS),
        "type": "session",
    }

    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def verify_session_token(token: str) -> dict[str, Any] | None:
    """
    Verify and decode a session token.

    Args:
        token: JWT token string

    Returns:
        Decoded payload or None if invalid
    """
    settings = get_settings()

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
        )

        if payload.get("type") != "session":
            return None

        return payload

    except jwt.ExpiredSignatureError:
        logger.debug("Session token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Invalid session token: {e}")
        return None


def extract_ws_ticket(protocol_header: str) -> str | None:
    """Extract ticket from a Sec-WebSocket-Protocol header value (ticket|<token> format)."""
    for proto in (p.strip() for p in protocol_header.split(",")):
        if proto.startswith("ticket|"):
            return proto[7:]
    return None


def create_ws_ticket(user_id: UUID) -> str:
    """Create a short-lived JWT ticket for WebSocket authentication."""
    settings = get_settings()
    now = datetime.now(UTC)

    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=WS_TICKET_EXPIRATION_SECONDS),
        "type": "ws",
    }

    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def verify_ws_ticket(token: str) -> dict[str, Any] | None:
    """Verify and decode a WebSocket ticket. Returns payload or None if invalid."""
    settings = get_settings()

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
        )

        if payload.get("type") != "ws":
            return None

        return payload

    except jwt.ExpiredSignatureError:
        logger.debug("WebSocket ticket expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Invalid WebSocket ticket: {e}")
        return None


def get_user_from_token(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Extract user info from token payload.

    Args:
        payload: Decoded JWT payload

    Returns:
        User info dict
    """
    return {
        "user_id": UUID(payload["sub"]),
        "roles": payload.get("roles", []),
    }


# Singleton OIDC client
_oidc_client: OIDCClient | None = None


def get_oidc_client() -> OIDCClient:
    """Get the OIDC client singleton."""
    global _oidc_client
    if _oidc_client is None:
        _oidc_client = OIDCClient()
    return _oidc_client
