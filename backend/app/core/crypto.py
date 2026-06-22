"""
Token vault crypto — AES-256-GCM codec keyed by a per-user DEK.

This is the security-critical core of the browser-fractioned key custody (audit-doc
Phase C): MCP OAuth token blobs are sealed under a per-user **data-encryption key (DEK)**
that the server holds in RAM only while the user's browser is connected (see
:mod:`app.services.mcp_dek_store`). The DEK itself is wrapped by a non-extractable
WebCrypto key that never leaves the browser, so the ciphertext + the wrapped DEK on disk
are useless without a connected user.

The codec is deliberately keyless-at-rest: callers pass the DEK explicitly. There is no
module-level key and no env-var key — a dump of DB + disk + env yields no plaintext.

``TokenVault`` is the forward-compatible interface from ``docs/connector-hot-auth.md`` §5;
``put``/``get``/``delete`` over an opaque ``vault_ref`` are realized at the repository
layer (``mcp_user_tokens`` rows) in :mod:`app.services.mcp_oauth`, which call the codec
below. A future shared/KMS vault is a backend swap behind the same codec, not a redesign.
"""

import os
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256-GCM: 32-byte key, 12-byte nonce (the GCM-recommended size).
DEK_BYTES = 32
_NONCE_BYTES = 12


class VaultDecryptError(Exception):
    """Raised when a blob cannot be decrypted (wrong/absent DEK or tampered ciphertext)."""


def generate_dek() -> bytes:
    """Return a fresh 256-bit data-encryption key."""
    return AESGCM.generate_key(bit_length=256)


def seal(dek: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt ``plaintext`` under ``dek`` (AES-256-GCM). Returns ``(ciphertext, nonce)``.

    A fresh random nonce is generated per call — never reuse a (key, nonce) pair with GCM.
    """
    if len(dek) != DEK_BYTES:
        raise ValueError(f"DEK must be {DEK_BYTES} bytes, got {len(dek)}")
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, None)
    return ciphertext, nonce


def unseal(dek: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    """Decrypt ``ciphertext`` under ``dek``. Raises :class:`VaultDecryptError` on any failure.

    A wrong DEK, a tampered ciphertext, or a wrong nonce all surface as the same error —
    GCM authentication failure is indistinguishable by design, and callers must not branch
    on the reason (it would leak an oracle).
    """
    if len(dek) != DEK_BYTES:
        raise VaultDecryptError("DEK has wrong length")
    if len(nonce) != _NONCE_BYTES:
        # Reject a corrupted/short nonce up front; AESGCM would raise ValueError (not
        # InvalidTag) on it, and the docstring's "any failure → VaultDecryptError" contract
        # must hold so callers can branch solely on VaultDecryptError.
        raise VaultDecryptError("nonce has wrong length")
    try:
        return AESGCM(dek).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError) as e:
        # InvalidTag = wrong DEK / tampered ciphertext; ValueError = any other malformed input
        # AESGCM rejects. Both normalize to the single opaque error (no decryption oracle).
        raise VaultDecryptError("authentication failed") from e


def vault_ref(user_id: UUID, mcp_key: str) -> str:
    """Opaque handle for a user's MCP token blob. Carries no secret material."""
    return f"mcp:{user_id}:{mcp_key}"


class TokenVault(Protocol):
    """Storage interface for encrypted token blobs (``docs/connector-hot-auth.md`` §5).

    Implementations encrypt with the per-user DEK before persisting and decrypt on read;
    ``vault_ref`` is an opaque handle. Realized by the ``mcp_user_tokens`` repository.
    """

    async def put(self, *, user_id: UUID, platform: str, blob: bytes) -> str: ...

    async def get(self, vault_ref: str) -> bytes | None: ...

    async def delete(self, vault_ref: str) -> None: ...
