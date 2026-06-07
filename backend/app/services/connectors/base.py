"""
Connector abstraction.

Defines the common interface every external-access connector implements, plus the
data types that flow through the broker. The single most important rule encoded
here: a ``MintedCredential.secret`` must never be logged, serialized to the client,
or sent to the model. The dataclass is built so the secret is structurally absent
from ``repr``/``str`` and from every public projection (``ConnectorStatus``).
"""

import base64
import hashlib
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

ConnectorType = Literal["aws", "gitlab", "coralogix"]

# Lifecycle state surfaced to the UI. Never carries a secret.
ConnState = Literal[
    "not_connected",  # no credential held
    "pending_auth",  # OAuth flow started, awaiting callback
    "needs_selection",  # AWS: authenticated, awaiting account/role choice
    "connected",  # usable credential held
    "expired",  # credential present but past expiry
    "error",
]


class ConnectorError(Exception):
    """Raised when a connector flow fails (auth exchange, minting, revoke)."""


@dataclass
class MintedCredential:
    """
    A temporary scoped credential held in RAM for one investigation session.

    ``secret`` is excluded from ``repr`` (``field(repr=False)``) and there is no
    code path that serializes it to the client or the model. ``selection`` and the
    other fields are safe metadata.
    """

    connector: ConnectorType
    scope: str
    expires_at: datetime | None  # None for fixed keys (Coralogix)
    secret: dict[str, Any] = field(repr=False, default_factory=dict)
    selection: dict[str, Any] | None = None
    # AWS only: populated between auth and account/role selection.
    available_accounts: list[dict[str, Any]] | None = None
    available_roles: list[dict[str, Any]] | None = None

    def __repr__(self) -> str:
        # Defensive: never let the secret leak via repr/log interpolation.
        return f"MintedCredential(connector={self.connector!r}, expires_at={self.expires_at!r})"

    def is_expired(self, *, skew_seconds: int = 0) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at


@dataclass
class PendingAuth:
    """
    In-flight OAuth handshake material, consumed once on callback.

    Short-lived (TTL ~10 min) and single-use. ``code_verifier`` is the PKCE secret;
    it is useless without the matching authorization code that arrives separately.
    """

    connector: ConnectorType
    user_id: UUID
    session_id: UUID
    state: str
    code_verifier: str = field(repr=False)
    created_at: datetime


@dataclass
class ConnectorStatus:
    """Safe, secret-free projection returned by the REST API and shown in the UI."""

    connector: ConnectorType
    state: ConnState
    expires_at: datetime | None = None
    scope: str | None = None
    selection: dict[str, Any] | None = None
    available_accounts: list[dict[str, Any]] | None = None
    available_roles: list[dict[str, Any]] | None = None
    error: str | None = None


def generate_pkce() -> tuple[str, str]:
    """
    Generate a PKCE ``(code_verifier, code_challenge)`` pair using S256.

    The verifier is a high-entropy URL-safe string; the challenge is the base64url
    (unpadded) SHA-256 of the verifier, per RFC 7636.
    """
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


class Connector(ABC):
    """
    Common interface for external-access connectors.

    Subclasses are registered in :mod:`app.services.connectors.registry` based on
    config. Adding a new connector (e.g. Sentry) means implementing this interface
    and registering it — no changes elsewhere in the broker.
    """

    type: ClassVar[ConnectorType]
    # OAuth connectors redirect the browser to an IdP; key-based ones (Coralogix)
    # are activated directly without a redirect.
    requires_oauth: ClassVar[bool] = True

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this connector is configured and turned on."""

    def build_authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        """Build the IdP authorization URL (PKCE S256). OAuth connectors override this.

        State and PKCE generation/persistence are owned by the REST route, mirroring
        the existing OIDC login flow, so connectors stay stateless.
        """
        raise NotImplementedError

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> MintedCredential:
        """Exchange an authorization code for a (possibly provisional) credential."""
        raise NotImplementedError

    async def activate(self, user_id: UUID, session_id: UUID) -> MintedCredential:
        """Activate a non-OAuth (fixed-key) connector for this session."""
        raise NotImplementedError

    async def select_account_role(
        self,
        provisional: MintedCredential,
        *,
        account_id: str,
        role_name: str,
    ) -> MintedCredential:
        """AWS-only: mint scoped STS credentials for the chosen account/role."""
        raise NotImplementedError

    async def revoke(self, cred: MintedCredential) -> None:
        """Best-effort provider-side revocation. Must be idempotent."""
        return None

    @abstractmethod
    def public_status(self, cred: MintedCredential | None) -> ConnectorStatus:
        """Project a held credential (or its absence) to a secret-free status."""
