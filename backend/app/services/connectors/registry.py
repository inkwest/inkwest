"""
Connector registry.

Maps a connector name to its singleton instance. Adding a future connector (e.g.
Sentry) is a one-line addition here plus the connector class and its config block.
``get_connector`` only returns connectors that are enabled in config, so disabled
ones are invisible to the API (404).
"""

from app.services.connectors.aws import AwsConnector
from app.services.connectors.base import Connector
from app.services.connectors.gitlab import GitLabConnector

# Keyed by plain str so an arbitrary path param can be looked up safely.
_REGISTRY: dict[str, Connector] = {
    "aws": AwsConnector(),
    "gitlab": GitLabConnector(),
}


def get_connector(name: str) -> Connector | None:
    """Return the enabled connector for ``name``, or None if unknown/disabled."""
    connector = _REGISTRY.get(name)
    if connector is None or not connector.enabled:
        return None
    return connector


def all_connectors() -> list[Connector]:
    """Return every enabled connector."""
    return [c for c in _REGISTRY.values() if c.enabled]
