"""Tests for the token-vault crypto codec (app.core.crypto)."""

import pytest

from app.core.crypto import (
    DEK_BYTES,
    VaultDecryptError,
    generate_dek,
    seal,
    unseal,
    vault_ref,
)


def test_generate_dek_length() -> None:
    assert len(generate_dek()) == DEK_BYTES


def test_round_trip() -> None:
    dek = generate_dek()
    plaintext = b'{"access_token":"abc","refresh_token":"def"}'
    ciphertext, nonce = seal(dek, plaintext)
    assert unseal(dek, ciphertext, nonce) == plaintext


@pytest.mark.parametrize("plaintext", [b"", b"x", b"A" * 8192])
def test_round_trip_sizes(plaintext: bytes) -> None:
    """Empty, single-byte, and multi-KB (realistic long refresh-token bundle) all round-trip."""
    dek = generate_dek()
    ciphertext, nonce = seal(dek, plaintext)
    assert unseal(dek, ciphertext, nonce) == plaintext


def test_ciphertext_is_opaque() -> None:
    """The ciphertext must not leak the plaintext (no substring of the token survives)."""
    dek = generate_dek()
    plaintext = b"super-secret-access-token"
    ciphertext, _ = seal(dek, plaintext)
    assert plaintext not in ciphertext
    assert b"secret" not in ciphertext


def test_unique_nonce_per_call() -> None:
    """A fresh nonce per call → identical plaintext yields different ciphertext (no GCM reuse)."""
    dek = generate_dek()
    c1, n1 = seal(dek, b"same")
    c2, n2 = seal(dek, b"same")
    assert n1 != n2
    assert c1 != c2


def test_wrong_dek_fails() -> None:
    dek = generate_dek()
    ciphertext, nonce = seal(dek, b"payload")
    with pytest.raises(VaultDecryptError):
        unseal(generate_dek(), ciphertext, nonce)


def test_tampered_ciphertext_fails() -> None:
    dek = generate_dek()
    ciphertext, nonce = seal(dek, b"payload")
    tampered = bytes([ciphertext[0] ^ 0x01]) + ciphertext[1:]
    with pytest.raises(VaultDecryptError):
        unseal(dek, tampered, nonce)


def test_wrong_length_dek_rejected() -> None:
    with pytest.raises(ValueError):
        seal(b"short", b"x")
    with pytest.raises(VaultDecryptError):
        unseal(b"short", b"x", b"y")


def test_vault_ref_is_stable_and_secret_free() -> None:
    import uuid

    uid = uuid.UUID("00000000-0000-4000-8000-000000000001")
    ref = vault_ref(uid, "notion")
    assert ref == f"mcp:{uid}:notion"
    assert ref == vault_ref(uid, "notion")  # deterministic
