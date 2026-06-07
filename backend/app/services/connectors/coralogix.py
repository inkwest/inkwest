"""
Coralogix connector.

Coralogix has no SSO-derived ephemeral credential, so Inkwest brokers a fixed
read-only API key held in AWS Secrets Manager. The "Give access" button activates
the key for the current investigation session; the key itself never leaves the
backend (not to the browser, logs, or model prompt).
"""

import asyncio
import json
import logging
from typing import Any, ClassVar
from uuid import UUID

from app.core.config import get_settings
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorStatus,
    ConnectorType,
    MintedCredential,
)

logger = logging.getLogger(__name__)


class CoralogixConnector(Connector):
    type: ClassVar[ConnectorType] = "coralogix"
    requires_oauth: ClassVar[bool] = False

    @property
    def enabled(self) -> bool:
        s = get_settings()
        return bool(
            s.connectors_enabled
            and s.coralogix_enabled
            and s.coralogix_secret_id
            and s.coralogix_api_endpoint
        )

    async def activate(self, user_id: UUID, session_id: UUID) -> MintedCredential:
        s = get_settings()
        if not self.enabled:
            raise ConnectorError("Coralogix connector is not configured")

        api_key = await asyncio.to_thread(
            _fetch_secret, s.coralogix_secret_id, s.coralogix_secret_region or s.aws_region
        )
        # The key must cover the three Coralogix pillars; scope label documents intent.
        return MintedCredential(
            connector="coralogix",
            scope="logs metrics traces (read-only)",
            expires_at=None,  # fixed key, no expiry
            secret={"api_key": api_key, "endpoint": s.coralogix_api_endpoint},
        )

    def public_status(self, cred: MintedCredential | None) -> ConnectorStatus:
        if cred is None:
            return ConnectorStatus(connector="coralogix", state="not_connected")
        return ConnectorStatus(
            connector="coralogix",
            state="connected",
            expires_at=None,
            scope=cred.scope,
        )


def _fetch_secret(secret_id: str | None, region: str | None) -> str:
    """Fetch the Coralogix API key from AWS Secrets Manager (sync; run in a thread)."""
    import boto3  # imported lazily so the dep is only needed when connectors are used

    client: Any = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString")
    if not raw:
        raise ConnectorError("Coralogix secret has no SecretString")
    # Support both a raw key and a JSON blob {"api_key": "..."}.
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if isinstance(parsed, dict):
        key = parsed.get("api_key") or parsed.get("apiKey")
        if not key:
            raise ConnectorError("Coralogix secret JSON missing 'api_key'")
        return str(key)
    return str(parsed)
