"""Tests for the versioned SecretBox at-rest field encryption (app.core.secretbox)."""

import base64

import pytest

from app.core import secretbox

KEY_V1 = b"\x01" * secretbox.KEY_SIZE
KEY_V2 = b"\x02" * secretbox.KEY_SIZE


def _keyring(**versions: bytes) -> dict[int, bytes]:
    return {int(v): k for v, k in versions.items()}


# --- parse_keyring ---------------------------------------------------------------


def test_parse_keyring_valid() -> None:
    raw = f'{{"1": "{base64.b64encode(KEY_V1).decode()}"}}'
    assert secretbox.parse_keyring(raw) == {1: KEY_V1}


def test_parse_keyring_empty_is_disabled() -> None:
    assert secretbox.parse_keyring("") == {}
    assert secretbox.parse_keyring("   ") == {}


def test_parse_keyring_bad_json_raises() -> None:
    with pytest.raises(secretbox.SecretBoxError, match="valid JSON"):
        secretbox.parse_keyring("{not json")


def test_parse_keyring_wrong_key_length_raises() -> None:
    raw = f'{{"1": "{base64.b64encode(b"short").decode()}"}}'
    with pytest.raises(secretbox.SecretBoxError, match="32 bytes"):
        secretbox.parse_keyring(raw)


def test_parse_keyring_non_integer_version_raises() -> None:
    raw = f'{{"v1": "{base64.b64encode(KEY_V1).decode()}"}}'
    with pytest.raises(secretbox.SecretBoxError, match="integer"):
        secretbox.parse_keyring(raw)


# --- seal / unseal ---------------------------------------------------------------


def test_roundtrip() -> None:
    kr = _keyring(**{"1": KEY_V1})
    token = secretbox.seal("hunter2", kr)
    assert secretbox.unseal(token, kr) == "hunter2"


def test_sealed_token_is_recognizable_and_opaque() -> None:
    token = secretbox.seal("hunter2", _keyring(**{"1": KEY_V1}))
    assert secretbox.is_sealed(token)
    assert token.startswith("AAA")  # base64 of the 00 00 xx version prefix
    assert "hunter2" not in token


def test_is_sealed_on_plaintext_and_none() -> None:
    assert secretbox.is_sealed("plain-secret") is False
    assert secretbox.is_sealed(None) is False


def test_active_version_is_highest() -> None:
    kr = _keyring(**{"1": KEY_V1, "2": KEY_V2})
    assert secretbox.active_version(kr) == 2
    token = secretbox.seal("s", kr)
    # Sealed under v2 → second base64 char block differs from a v1 token ("AAAC" vs "AAAB").
    assert token.startswith("AAAC")


def test_unseal_dispatches_on_version() -> None:
    # Sealed under v1; a keyring that still has v1 (plus a newer v2) decrypts it.
    token = secretbox.seal("s", _keyring(**{"1": KEY_V1}))
    assert secretbox.unseal(token, _keyring(**{"1": KEY_V1, "2": KEY_V2})) == "s"


def test_unseal_missing_version_fails_closed() -> None:
    token = secretbox.seal("s", _keyring(**{"1": KEY_V1}))
    with pytest.raises(secretbox.SecretBoxError, match="no key for version 1"):
        secretbox.unseal(token, _keyring(**{"2": KEY_V2}))


def test_unseal_wrong_key_fails_authentication() -> None:
    token = secretbox.seal("s", _keyring(**{"1": KEY_V1}))
    # Same version number, different key bytes → MAC check fails.
    with pytest.raises(secretbox.SecretBoxError, match="authentication failed"):
        secretbox.unseal(token, {1: KEY_V2})


def test_unseal_rejects_bad_base64_and_short() -> None:
    kr = _keyring(**{"1": KEY_V1})
    with pytest.raises(secretbox.SecretBoxError, match="base64"):
        secretbox.unseal("@@@not-base64@@@", kr)
    with pytest.raises(secretbox.SecretBoxError, match="too short"):
        secretbox.unseal(base64.b64encode(b"\x00\x00\x01tiny").decode(), kr)


def test_seal_without_keyring_raises() -> None:
    with pytest.raises(secretbox.SecretBoxError, match="no keyring"):
        secretbox.seal("s", {})


def test_sealed_version_reads_version_without_decrypting() -> None:
    kr = _keyring(**{"1": KEY_V1, "2": KEY_V2})
    assert secretbox.sealed_version(secretbox.seal("s", _keyring(**{"1": KEY_V1}))) == 1
    assert secretbox.sealed_version(secretbox.seal("s", kr)) == 2  # active = highest


def test_sealed_version_none_on_bad_token() -> None:
    assert secretbox.sealed_version("not-base64!!") is None
    assert secretbox.sealed_version("AAABtiny") is None  # decodes but too short
