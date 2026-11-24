"""Tests for per-model context-window resolution."""

import pytest

from app.services.context_window import CONTEXT_WINDOWS, resolve_context_window


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # Bare Anthropic IDs
        ("claude-opus-5", 1_000_000),
        ("claude-sonnet-5", 1_000_000),
        ("claude-haiku-4-5", 200_000),
        # Still-selectable older generations: dated IDs and their aliases both resolve.
        ("claude-sonnet-4-5", 200_000),
        ("claude-sonnet-4-5-20250929", 200_000),
        ("claude-opus-4-5-20251101", 200_000),
        # Bedrock forms of the same models: routing prefix, namespace, date snapshot and
        # version suffix must all normalize to the same table row.
        ("anthropic.claude-opus-5", 1_000_000),
        ("eu.anthropic.claude-opus-4-8", 1_000_000),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 200_000),
        # Non-Anthropic Bedrock vendors (the inkwest-agent / opencode engines)
        ("us.amazon.nova-pro-v1:0", 300_000),
        ("amazon.nova-micro-v1:0", 128_000),
        ("amazon.nova-premier-v1:0", 1_000_000),
        ("us.meta.llama3-3-70b-instruct-v1:0", 128_000),
        ("mistral.mistral-large-3-675b-instruct", 256_000),
        ("deepseek.v3.2", 164_000),
    ],
)
def test_known_models_resolve(model_id: str, expected: int) -> None:
    assert resolve_context_window(model_id) == expected


@pytest.mark.parametrize(
    "model_id",
    [
        # Retired (Opus 4.1, 2026-08-05) and the 3.x line: absent from the vendor tables the
        # module cites, so a row would be a number nobody can re-verify. Registry override.
        "anthropic.claude-opus-4-1-20250805-v1:0",
        "claude-3-5-sonnet-20241022",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        # Never published a window.
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
    ],
)
def test_unsourceable_models_have_no_window(model_id: str) -> None:
    assert resolve_context_window(model_id) is None


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-20250514",  # older generation: no published window
        "qwen.qwen3-32b-v1:0",  # long tail of the Bedrock catalog
        "some-operator-custom-model",
    ],
)
def test_unsourced_models_resolve_to_none(model_id: str) -> None:
    """Unknown ⇒ None, never a default: the UI must not gauge a guessed capacity."""
    assert resolve_context_window(model_id) is None


def test_registry_override_wins_over_table() -> None:
    assert resolve_context_window("claude-opus-5", {"context_window": 500_000}) == 500_000


def test_registry_override_covers_an_unsourced_model() -> None:
    assert resolve_context_window("qwen.qwen3-32b-v1:0", {"context_window": 262_144}) == 262_144


@pytest.mark.parametrize(
    "override",
    [0, -1, "200000", 1.5, None, True, {"nested": 1}],
)
def test_invalid_overrides_are_ignored(override: object) -> None:
    """A bad override falls through to the table (or None) instead of being trusted —
    ``True`` included, since ``isinstance(True, int)`` would otherwise yield a 1-token
    window and pin the gauge at 100%."""
    assert resolve_context_window("claude-opus-5", {"context_window": override}) == 1_000_000
    assert resolve_context_window("qwen.qwen3-32b-v1:0", {"context_window": override}) is None


def test_table_values_are_plausible() -> None:
    """Guards against a typo'd row (a byte count, or a K/M mix-up) reaching production."""
    assert all(50_000 <= window <= 2_000_000 for window in CONTEXT_WINDOWS.values())
