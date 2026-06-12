"""
In-RAM per-user DEK store (browser-fractioned key custody, audit-doc Phase C).

The data-encryption key that seals a user's MCP token blobs is reconstructed in server
RAM only while that user's browser is connected: on WS connect the browser unwraps the
DEK with its non-extractable IndexedDB key and supplies it over the (TLS) socket, and on
disconnect the server evicts it. While no user is connected the DEK exists nowhere on the
server, so a DB+disk+env dump cannot decrypt any token blob.

Mirrors the design of :class:`app.services.connectors.warm.WarmGrantStore` and inherits
the same operational constraint as the credential store
(:mod:`app.services.connectors.store`): because it is process-local, the WS, the OAuth
callback, and any token use for a given user must be handled by the same process — deploy
single-replica or with session affinity. The interface is narrow so a future swap to an
encrypted shared cache is a one-class change.
"""

import asyncio
import logging
from uuid import UUID

logger = logging.getLogger(__name__)


class DekStore:
    """Async-safe in-memory map of ``user_id -> raw DEK`` (32 bytes)."""

    def __init__(self) -> None:
        self._deks: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put(self, user_id: UUID, dek: bytes) -> None:
        async with self._lock:
            self._deks[str(user_id)] = dek

    async def get(self, user_id: UUID) -> bytes | None:
        async with self._lock:
            return self._deks.get(str(user_id))

    async def delete(self, user_id: UUID) -> None:
        """Evict the user's DEK (e.g. on WS disconnect)."""
        async with self._lock:
            self._deks.pop(str(user_id), None)

    async def has(self, user_id: UUID) -> bool:
        async with self._lock:
            return str(user_id) in self._deks


_store: DekStore | None = None


def get_dek_store() -> DekStore:
    """Return the process-wide DEK store singleton."""
    global _store
    if _store is None:
        _store = DekStore()
    return _store
