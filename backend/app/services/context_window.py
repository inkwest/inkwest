"""
Per-model context-window sizes — the denominator of the context-pressure gauge.

**Every value here is sourced, none inferred.** A model we cannot source resolves to
``None``, which disables the gauge for it rather than showing a percentage of a guessed
capacity (spec 003: never act on a guessed window).

Sources:
- Anthropic (Claude): platform.claude.com/docs/en/about-claude/models/overview §Latest
  models comparison and §Legacy models — Fable 5, Mythos 5, Opus 5, Opus 4.8/4.7/4.6,
  Sonnet 5 and Sonnet 4.6 are 1M; Haiku 4.5, Sonnet 4.5, Opus 4.5 and Opus 4.1 are 200K.
  Claude on Bedrock carries the same windows (same models, ``anthropic.``-prefixed IDs).
- Amazon Nova: docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html §Model
  specifications (Premier 1M, Pro 300k, Lite 300k, Micro 128k).
- Other Bedrock vendors: the per-model Bedrock model card
  (``model-card-<vendor>-<model>.html``), "Context window" field.

Deliberately absent: Claude Opus 4 / Sonnet 4 (the vendor table lists no window for them)
and the long tail of the Bedrock catalog (Qwen, GLM, Kimi, Gemma, GPT-OSS, …). Both are
covered by the per-model registry override, and adding a row here needs a citation.

Values are the literal decimal count the vendor states ("128K" → 128_000), never a
power-of-two reading.
"""

from typing import Any

from app.services.pricing import price_match_keys, pricing_candidates

# Keyed by the vendor-agnostic normalized form (see ``price_match_keys``), so one entry
# matches the bare ID, the ``anthropic.``/``amazon.`` namespaced form, and any regional
# routing prefix or version suffix Bedrock adds.
CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claudefable5": 1_000_000,
    "claudemythos5": 1_000_000,
    "claudeopus5": 1_000_000,
    "claudeopus48": 1_000_000,
    "claudeopus47": 1_000_000,
    "claudeopus46": 1_000_000,
    "claudesonnet5": 1_000_000,
    "claudesonnet46": 1_000_000,
    "claudehaiku45": 200_000,
    "claudesonnet45": 200_000,
    "claudeopus45": 200_000,
    "claudeopus41": 200_000,
    # Amazon Nova
    "novapremier": 1_000_000,
    "novapro": 300_000,
    "novalite": 300_000,
    "novamicro": 128_000,
    # Meta
    "llama3370binstruct": 128_000,
    # Mistral
    "mistrallarge3675binstruct": 256_000,
    # DeepSeek
    "deepseekv32": 164_000,
}

# ponytail: static table, refreshed by hand from vendor docs. Anthropic's
# ``GET /v1/models`` now exposes ``max_input_tokens``, and each Bedrock model card states
# a "Context window" — wire either into the catalog refresh (like ``pricing_live``) if
# these rows start rotting or the covered-model list has to grow much further.


def resolve_context_window(model_id: str, options: dict[str, Any] | None = None) -> int | None:
    """Context window for a model, or ``None`` when unknown.

    An operator-set ``options["context_window"]`` on the registry row wins over the table:
    it is how a deployment covers a model we haven't sourced, or corrects one whose window
    changed. Non-positive or non-integer overrides are ignored rather than trusted.
    """
    if options:
        override = options.get("context_window")
        if isinstance(override, int) and not isinstance(override, bool) and override > 0:
            return override

    for candidate in pricing_candidates(model_id):
        for key in price_match_keys(candidate):
            hit = CONTEXT_WINDOWS.get(key)
            if hit is not None:
                return hit
    return None
