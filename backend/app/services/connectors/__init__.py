"""
External access connectors.

Inkwest acts as a credential *broker*: it mints temporary, scoped credentials for
external services (AWS, GitLab, Coralogix) on explicit user consent, holds them in
RAM for the lifetime of an investigation session, and injects them into tool
execution. Secrets never reach the browser, the logs, or the model prompt.
"""

from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorStatus,
    ConnectorType,
    ConnState,
    MintedCredential,
    PendingAuth,
    generate_pkce,
)
from app.services.connectors.registry import all_connectors, get_connector
from app.services.connectors.store import CredentialStore, get_credential_store

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorStatus",
    "ConnectorType",
    "ConnState",
    "MintedCredential",
    "PendingAuth",
    "generate_pkce",
    "all_connectors",
    "get_connector",
    "CredentialStore",
    "get_credential_store",
]
