"""At-rest field encryption for operator secrets (libsodium SecretBox, XSalsa20-Poly1305).

Seals sensitive configuration values (e.g. an OAuth client secret) before they touch the
database, so a DB-only leak (backup, replica, ``pg_dump`` in a log) yields ciphertext, not
the secret. It does **not** defend a full host compromise (key + ciphertext together) — the
key is loaded from the environment; that is an accepted, documented tier for operator
secrets, distinct from the stronger per-user token custody elsewhere.

Wire format (interoperable, self-describing)::

    base64( [version: 3 bytes big-endian] + [nonce: 24 bytes] + [ciphertext + 16-byte MAC] )

The **version selects the key**: each key generation gets its own version number, so
rotation is "add a new key under a new version, encrypt new writes with it" while old
ciphertexts keep decrypting under their original key until re-sealed. All sealed values are
valid base64 starting with ``AAA`` (the encoding of the ``00 00 xx`` version prefix), which
``is_sealed`` uses to tell a sealed value from a legacy plaintext one during migration.

Keys come from one env var holding a JSON map ``{"<version>": "<base64 32-byte key>"}``; the
active version (used to encrypt) is the highest version present. With no keyring configured,
sealing raises (a write that must seal fails loudly) while plaintext reads pass through — so
a value can migrate plaintext -> sealed on its next write without a big-bang re-encryption.
"""

from __future__ import annotations

import base64
import json

import nacl.exceptions
import nacl.secret
import nacl.utils

NONCE_SIZE = 24
KEY_SIZE = 32
MAC_SIZE = 16
VERSION_SIZE = 3
# base64 of the 3-byte version prefix (00 00 xx) — every sealed value starts with this.
SEALED_PREFIX = "AAA"


class SecretBoxError(Exception):
    """Sealing/unsealing failure (bad key, unknown version, auth failure, bad format)."""


def _version_bytes(version: int) -> bytes:
    if not 1 <= version < (1 << (8 * VERSION_SIZE)):
        raise SecretBoxError(f"version out of range: {version}")
    return version.to_bytes(VERSION_SIZE, "big")


def parse_keyring(raw: str) -> dict[int, bytes]:
    """Parse the env value ``{"1": "<b64 32B>"}`` into ``{version: key_bytes}``.

    Empty/blank yields an empty keyring (sealing disabled, plaintext reads still work).
    Raises on malformed JSON or a key that isn't exactly 32 bytes — a misconfigured key
    must fail loudly, not silently disable encryption.
    """
    if not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SecretBoxError(f"keyring is not valid JSON: {e}") from e
    if not isinstance(entries, dict) or not entries:
        raise SecretBoxError("keyring must be a non-empty JSON object {version: base64key}")

    keyring: dict[int, bytes] = {}
    for k, v in entries.items():
        try:
            version = int(k)
        except (TypeError, ValueError) as e:
            raise SecretBoxError(f"keyring version must be an integer, got {k!r}") from e
        if not isinstance(v, str):
            raise SecretBoxError(f"keyring entry {k!r} must be a base64 string")
        try:
            key = base64.b64decode(v, validate=True)
        except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
            raise SecretBoxError(f"keyring entry {k!r} is not valid base64: {e}") from e
        if len(key) != KEY_SIZE:
            raise SecretBoxError(f"keyring entry {k!r} must be {KEY_SIZE} bytes, got {len(key)}")
        keyring[version] = key
    return keyring


def active_version(keyring: dict[int, bytes]) -> int:
    """Highest version in the keyring — the one new writes are sealed with."""
    if not keyring:
        raise SecretBoxError("no keyring configured — cannot seal")
    return max(keyring)


def is_sealed(value: str | None) -> bool:
    """True if ``value`` looks like a sealed token (vs a legacy plaintext secret)."""
    return value is not None and value.startswith(SEALED_PREFIX)


def sealed_version(token: str) -> int | None:
    """Return the key version a sealed token was made with, without decrypting it.

    ``None`` if the token isn't a well-formed sealed value. Lets callers cheaply check
    whether the key needed to read a secret is present (key-version membership) instead of
    attempting a full decrypt.
    """
    try:
        payload = base64.b64decode(token, validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    if len(payload) < VERSION_SIZE + NONCE_SIZE + MAC_SIZE:
        return None
    return int.from_bytes(payload[:VERSION_SIZE], "big")


def seal(plaintext: str, keyring: dict[int, bytes]) -> str:
    """Encrypt ``plaintext`` under the keyring's active version; return the base64 token."""
    version = active_version(keyring)
    box = nacl.secret.SecretBox(keyring[version])
    nonce = nacl.utils.random(NONCE_SIZE)
    ciphertext = box.encrypt(plaintext.encode(), nonce).ciphertext
    return base64.b64encode(_version_bytes(version) + nonce + ciphertext).decode("ascii")


def unseal(token: str, keyring: dict[int, bytes]) -> str:
    """Decrypt a sealed token, dispatching to the key named by its version prefix."""
    try:
        payload = base64.b64decode(token, validate=True)
    except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
        raise SecretBoxError(f"invalid base64: {e}") from e
    if len(payload) < VERSION_SIZE + NONCE_SIZE + MAC_SIZE:
        raise SecretBoxError("sealed value too short")
    version = int.from_bytes(payload[:VERSION_SIZE], "big")
    key = keyring.get(version)
    if key is None:
        # Fail closed: the key for this version isn't loaded (retired, or not configured).
        raise SecretBoxError(f"no key for version {version}")
    nonce = payload[VERSION_SIZE : VERSION_SIZE + NONCE_SIZE]
    ciphertext = payload[VERSION_SIZE + NONCE_SIZE :]
    try:
        return nacl.secret.SecretBox(key).decrypt(ciphertext, nonce).decode()
    except nacl.exceptions.CryptoError as e:
        raise SecretBoxError(f"authentication failed: {e}") from e
