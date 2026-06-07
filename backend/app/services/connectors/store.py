"""
In-RAM credential store.

Minted credentials live only in process memory, keyed by
``(user_id, session_id, connector)``. This keeps secrets out of the database and
logs, and isolates them per investigation session.

Operational constraint: because the store is process-local, the OAuth callback,
the REST routes, and the chat WebSocket for a given session must all be handled by
the same process. Deploy single-replica or with session affinity. The interface
below is deliberately narrow so a future swap to an encrypted shared cache (Redis)
is a one-class change.
"""

import asyncio
import logging
from uuid import UUID

from app.services.connectors.base import ConnectorType, MintedCredential

logger = logging.getLogger(__name__)

_Key = tuple[str, str, str]


def _key(user_id: UUID, session_id: UUID, connector: ConnectorType) -> _Key:
    return (str(user_id), str(session_id), connector)


class CredentialStore:
    """Async-safe in-memory store of minted credentials with lazy expiry."""

    def __init__(self) -> None:
        self._creds: dict[_Key, MintedCredential] = {}
        self._lock = asyncio.Lock()

    async def put(self, user_id: UUID, session_id: UUID, cred: MintedCredential) -> None:
        async with self._lock:
            self._creds[_key(user_id, session_id, cred.connector)] = cred

    async def get(
        self, user_id: UUID, session_id: UUID, connector: ConnectorType
    ) -> MintedCredential | None:
        """Return the held credential, evicting and returning None if expired."""
        async with self._lock:
            k = _key(user_id, session_id, connector)
            cred = self._creds.get(k)
            if cred is None:
                return None
            if cred.is_expired():
                del self._creds[k]
                return None
            return cred

    async def revoke(
        self, user_id: UUID, session_id: UUID, connector: ConnectorType
    ) -> MintedCredential | None:
        """Drop a credential from the store and return it (for provider-side revoke)."""
        async with self._lock:
            return self._creds.pop(_key(user_id, session_id, connector), None)

    async def revoke_session(self, user_id: UUID, session_id: UUID) -> None:
        """Drop every credential held for a session (e.g. on session deletion)."""
        prefix = (str(user_id), str(session_id))
        async with self._lock:
            for k in [k for k in self._creds if k[:2] == prefix]:
                del self._creds[k]

    async def sweep(self) -> int:
        """Evict all expired credentials. Returns the count removed."""
        async with self._lock:
            expired = [k for k, c in self._creds.items() if c.is_expired()]
            for k in expired:
                del self._creds[k]
            return len(expired)


_store: CredentialStore | None = None


def get_credential_store() -> CredentialStore:
    """Return the process-wide credential store singleton."""
    global _store
    if _store is None:
        _store = CredentialStore()
    return _store
