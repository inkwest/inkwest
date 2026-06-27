"""Tests for the provider-aware model catalog."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_catalog
from app.services.model_catalog import (
    available_models,
    catalog_status,
    is_anthropic_model,
    refresh_catalog,
    static_catalog,
)


@pytest.fixture(autouse=True)
def reset_catalog_state():
    """Isolate the module-level cache and settings overrides between tests."""
    yield
    model_catalog._live_models = None
    model_catalog._fetched_at = 0.0
    model_catalog._last_fetch_failed = False
    model_catalog._last_fetch_error = None
    model_catalog._modalities = {}
    model_catalog._profile_arns = {}


def _settings(**overrides):
    base = {
        "ai_provider": "anthropic",
        "anthropic_api_key": "sk-test",  # pragma: allowlist secret
        "aws_region": "eu-west-1",
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "aws_bearer_token_bedrock": None,
        "aws_cli_path": "aws",
        "claude_cli_models": "",
        "sandbox_enabled": False,
        "sandbox_env_file": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_static_catalog_anthropic_excludes_bedrock_ids():
    with patch.object(model_catalog, "get_settings", return_value=_settings()):
        models = static_catalog()
    assert "claude-opus-4-8" in models
    assert not any(m.startswith("anthropic.") for m in models)


def test_static_catalog_bedrock_only_bedrock_ids():
    with patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")):
        models = static_catalog()
    assert all(m.startswith("anthropic.") for m in models)
    assert "anthropic.claude-3-5-sonnet-20241022-v2:0" in models


def test_available_models_prefers_live_cache():
    model_catalog._live_models = ["claude-live-1", "claude-live-2"]
    assert available_models() == ["claude-live-1", "claude-live-2"]


@pytest.mark.asyncio
async def test_refresh_catalog_anthropic_success():
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(
            model_catalog,
            "_fetch_anthropic_models",
            AsyncMock(return_value=["claude-fable-5", "claude-opus-4-8"]),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models == ["claude-fable-5", "claude-opus-4-8"]


@pytest.mark.asyncio
async def test_refresh_catalog_failure_keeps_fallback():
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(
            model_catalog,
            "_fetch_anthropic_models",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models is None  # static fallback applies
    with patch.object(model_catalog, "get_settings", return_value=_settings()):
        assert available_models() == static_catalog()


@pytest.mark.asyncio
async def test_refresh_catalog_claude_cli_declared_list_skips_fetch():
    """An operator-declared CLAUDE_CLI_MODELS wins over auto-discovery."""
    fetch = AsyncMock()
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(ai_provider="claude-cli", claude_cli_models="claude-x"),
        ),
        patch.object(model_catalog, "_fetch_claude_cli_models", fetch),
    ):
        await refresh_catalog(force=True)
    fetch.assert_not_awaited()
    assert model_catalog._live_models is None  # static_catalog serves the declared list


@pytest.mark.asyncio
async def test_refresh_catalog_claude_cli_auto_discovers():
    with (
        patch.object(
            model_catalog, "get_settings", return_value=_settings(ai_provider="claude-cli")
        ),
        patch.object(
            model_catalog,
            "_fetch_claude_cli_models",
            AsyncMock(return_value=["eu.anthropic.claude-fable-5"]),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models == ["eu.anthropic.claude-fable-5"]


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_bedrock_from_backend_settings():
    """A backend Bedrock key (NOT sandbox.env) drives a Bedrock catalog fetch."""
    bedrock_fetch = AsyncMock(return_value=["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"])
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(
                ai_provider="claude-cli",
                aws_bearer_token_bedrock="secret-token",  # pragma: allowlist secret
                aws_region="eu-west-1",
                # sandbox.env is irrelevant now — set it to prove it is NOT read.
                sandbox_enabled=True,
                sandbox_env_file="/does/not/exist.env",
            ),
        ),
        patch.object(model_catalog, "_fetch_bedrock_models", bedrock_fetch),
    ):
        models = await model_catalog._fetch_claude_cli_models()

    assert models == ["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    bedrock_fetch.assert_awaited_once_with(
        region="eu-west-1",
        extra_env={
            "AWS_BEARER_TOKEN_BEDROCK": "secret-token",  # pragma: allowlist secret
            "AWS_REGION": "eu-west-1",
        },
    )


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_bedrock_sigv4_from_backend_settings():
    """SigV4 access keys (no bearer) also drive Bedrock discovery from backend settings."""
    bedrock_fetch = AsyncMock(return_value=["eu.anthropic.claude-opus-4-7"])
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(
                ai_provider="claude-cli",
                aws_bearer_token_bedrock=None,
                aws_access_key_id="AKIA",  # pragma: allowlist secret
                aws_secret_access_key="shhh",  # pragma: allowlist secret
                aws_region="eu-west-1",
            ),
        ),
        patch.object(model_catalog, "_fetch_bedrock_models", bedrock_fetch),
    ):
        models = await model_catalog._fetch_claude_cli_models()
    assert models == ["eu.anthropic.claude-opus-4-7"]
    bedrock_fetch.assert_awaited_once_with(
        region="eu-west-1",
        extra_env={
            "AWS_ACCESS_KEY_ID": "AKIA",  # pragma: allowlist secret
            "AWS_SECRET_ACCESS_KEY": "shhh",  # pragma: allowlist secret
            "AWS_REGION": "eu-west-1",
        },
    )


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_anthropic_key_from_backend_settings():
    anthropic_fetch = AsyncMock(return_value=["claude-fable-5"])
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(
                ai_provider="claude-cli",
                aws_bearer_token_bedrock=None,
                anthropic_api_key="sk-backend",  # pragma: allowlist secret
            ),
        ),
        patch.object(model_catalog, "_fetch_anthropic_models", anthropic_fetch),
    ):
        models = await model_catalog._fetch_claude_cli_models()

    assert models == ["claude-fable-5"]
    anthropic_fetch.assert_awaited_once_with(
        api_key="sk-backend",  # pragma: allowlist secret
        base_url=None,
    )


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_undetectable_backend():
    """No Bedrock key/creds and no Anthropic key -> nothing to query -> static fallback."""
    with patch.object(
        model_catalog,
        "get_settings",
        return_value=_settings(
            ai_provider="claude-cli",
            aws_bearer_token_bedrock=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            anthropic_api_key=None,
        ),
    ):
        assert await model_catalog._fetch_claude_cli_models() is None


@pytest.mark.asyncio
async def test_refresh_catalog_ttl_noop():
    fetch = AsyncMock(return_value=["claude-x"])
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(model_catalog, "_fetch_anthropic_models", fetch),
    ):
        await refresh_catalog(force=True)
        await refresh_catalog()  # fresh -> no second fetch
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_fetch_bedrock_models_parses_profiles_and_on_demand():
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.anthropic.claude-opus-4-8"},
                {"inferenceProfileId": "eu.meta.llama-x"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
                {
                    "modelId": "anthropic.claude-profile-only",
                    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        # Claude-only path (include_open False): the meta profile is vendor-gated out,
        # the INFERENCE_PROFILE-only foundation row isn't on-demand → only these surface.
        models = await model_catalog._fetch_bedrock_models(include_open=False)

    assert models == [
        "eu.anthropic.claude-opus-4-8",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
    ]


@pytest.mark.asyncio
async def test_fetch_bedrock_models_captures_modalities():
    """P1.8: per-model input/output modalities are captured from list-foundation-models,
    keyed by the model id AND its route-stripped base; unknown ids resolve to empties."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "us.anthropic.claude-opus-4-v1:0"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-opus-4-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "inputModalities": ["TEXT", "IMAGE"],
                    "outputModalities": ["TEXT"],
                },
                {
                    "modelId": "amazon.titan-embed-text-v2:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["EMBEDDING"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        await model_catalog._fetch_bedrock_models(include_open=True)

    # Exact id lookup.
    assert model_catalog.model_modalities("anthropic.claude-opus-4-v1:0") == (
        ["TEXT", "IMAGE"],
        ["TEXT"],
    )
    # Embedding model captured too (the map covers non-chat models for the admin badge).
    assert model_catalog.model_modalities("amazon.titan-embed-text-v2:0") == (
        ["TEXT"],
        ["EMBEDDING"],
    )
    # Route-stripped base resolves: a regional inference-profile id carries no modality
    # data of its own, so it falls back to the underlying foundation model's.
    assert model_catalog.model_modalities("us.anthropic.claude-opus-4-v1:0") == (
        ["TEXT", "IMAGE"],
        ["TEXT"],
    )
    # Unknown id → empty (back-compat: caller treats as text-only).
    assert model_catalog.model_modalities("meta.unknown") == ([], [])


def test_static_catalog_claude_cli_declared_models():
    """CLAUDE_CLI_MODELS declares what the CLI's backend serves."""
    with patch.object(
        model_catalog,
        "get_settings",
        return_value=_settings(
            ai_provider="claude-cli",
            claude_cli_models="eu.anthropic.claude-fable-5, eu.anthropic.claude-opus-4-8",
        ),
    ):
        assert static_catalog() == [
            "eu.anthropic.claude-fable-5",
            "eu.anthropic.claude-opus-4-8",
        ]


@pytest.mark.asyncio
async def test_fetch_bedrock_models_dedupes_and_sorts():
    """Regional > global > on-demand for the same model; newest first."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0"},
                {"inferenceProfileId": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"},
                {"inferenceProfileId": "global.anthropic.claude-fable-5-20260301-v1:0"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
                {
                    "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        # Claude-only path keeps the dedupe assertion independent of the modality filter.
        models = await model_catalog._fetch_bedrock_models(include_open=False)

    assert models == [
        # Newest first; the eu. profile wins over global./on-demand duplicates
        "global.anthropic.claude-fable-5-20260301-v1:0",
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ]


@pytest.mark.asyncio
async def test_fetch_anthropic_models_sorts_newest_first():
    """The API's ordering is not contractual — default_model() needs newest-first."""

    class FakeModel:
        def __init__(self, model_id):
            self.id = model_id

    class FakePage:
        def __aiter__(self):
            async def gen():
                # Deliberately oldest-first
                for m in ["claude-haiku-4-5", "claude-opus-4-8", "claude-fable-5"]:
                    yield FakeModel(m)

            return gen()

    fake_client = MagicMock()
    fake_client.models.list = MagicMock(return_value=FakePage())
    fake_client.close = AsyncMock()

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(model_catalog, "AsyncAnthropic", return_value=fake_client),
    ):
        models = await model_catalog._fetch_anthropic_models()

    assert models == ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-opus-4-8", True),
        ("eu.anthropic.claude-opus-4-8", True),
        ("anthropic.claude-3-opus-20240229-v1:0", True),
        ("meta.llama3-1-70b-instruct-v1:0", False),
        ("mistral.mistral-large-2407-v1:0", False),
        ("amazon.nova-pro-v1:0", False),
    ],
)
def test_is_anthropic_model(model_id, expected):
    assert is_anthropic_model(model_id) is expected


@pytest.mark.asyncio
async def test_fetch_bedrock_models_include_open_surfaces_non_anthropic():
    """With include_open, open-weight (TEXT) models join the catalog; the
    foundation list is fetched without the --by-provider anthropic narrowing."""
    captured_args = []

    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.anthropic.claude-opus-4-8"},
                {"inferenceProfileId": "us.meta.llama3-1-70b-instruct-v1:0"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "mistral.mistral-large-2407-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "outputModalities": ["TEXT"],
                },
                {
                    "modelId": "amazon.titan-embed-text-v2:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "outputModalities": ["EMBEDDING"],  # filtered out — not chat
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        captured_args.append(args)
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models(include_open=True)

    # Open inference profile + open TEXT foundation model surface; embedding does not.
    assert "us.meta.llama3-1-70b-instruct-v1:0" in models
    assert "mistral.mistral-large-2407-v1:0" in models
    assert "eu.anthropic.claude-opus-4-8" in models
    assert not any("titan-embed" in m for m in models)
    # No --by-provider narrowing when open models are requested.
    foundation_call = next(a for a in captured_args if a[1] == "list-foundation-models")
    assert "--by-provider" not in foundation_call


@pytest.mark.asyncio
async def test_fetch_bedrock_models_default_excludes_open():
    """Without the flag, only Anthropic models are requested (--by-provider)."""
    captured_args = []

    async def fake_aws_json(args, extra_env=None):
        captured_args.append(args)
        return {"inferenceProfileSummaries": [], "modelSummaries": []}

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
        patch.object(model_catalog, "get_bool_setting", return_value=False),
    ):
        await model_catalog._fetch_bedrock_models()

    foundation_call = next(a for a in captured_args if a[1] == "list-foundation-models")
    assert "--by-provider" in foundation_call and "anthropic" in foundation_call


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("anthropic.claude-3-5-sonnet-20241022-v2:0", "anthropic"),
        ("eu.anthropic.claude-opus-4-8", "anthropic"),
        ("meta.llama3-1-70b-instruct-v1:0", "meta"),
        ("us.meta.llama3-1-70b-instruct-v1:0", "meta"),
        ("mistral.mistral-large-2407-v1:0", "mistral"),
        ("amazon.nova-pro-v1:0", "amazon"),
        ("cohere.command-r-v1:0", "cohere"),
        ("ai21.jamba-1-5-large-v1:0", "ai21"),
        ("deepseek.r1-v1:0", "deepseek"),
        ("stability.stable-image-core-v1:0", "stability"),
        # Vendor derived verbatim from the namespace (no allowlist) — vendors Bedrock
        # keeps adding get their own tag instead of all collapsing to "bedrock".
        ("google.gemma-3-27b-it", "google"),
        ("qwen.qwen3-coder-30b-a3b-v1:0", "qwen"),
        ("openai.gpt-oss-120b:0", "openai"),
        ("us.minimax.minimax-m2-5", "minimax"),
        ("newvendor.some-model-v1:0", "newvendor"),  # any namespace → its own vendor tag
        ("no-dot-id", "bedrock"),  # no namespace at all → generic fallback
    ],
)
def test_bedrock_vendor(model_id, expected):
    assert model_catalog.bedrock_vendor(model_id) == expected


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", True),
        ("bedrock", True),  # legacy Claude-on-Bedrock tag
        ("meta", False),
        ("mistral", False),
        ("amazon", False),
        ("bedrock-open", False),  # legacy open tag
        ("claude-cli", False),
    ],
)
def test_provider_speaks_messages(provider, expected):
    assert model_catalog.provider_speaks_messages(provider) is expected


@pytest.mark.asyncio
async def test_fetch_bedrock_models_engine_gate_forces_claude_only():
    """Even with the admin flag set, no engine that can run open models → Claude-only.

    The engine gate (inkwest-agent ∉ AGENTIC_ENGINES) wins over the flag, so the
    foundation list is still narrowed to anthropic."""
    captured_args = []

    async def fake_aws_json(args, extra_env=None):
        captured_args.append(args)
        return {"inferenceProfileSummaries": [], "modelSummaries": []}

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
        patch.object(model_catalog, "get_bool_setting", return_value=True),
        patch.object(model_catalog, "inkwest_agent_enabled", return_value=False),
    ):
        await model_catalog._fetch_bedrock_models()

    foundation_call = next(a for a in captured_args if a[1] == "list-foundation-models")
    assert "--by-provider" in foundation_call and "anthropic" in foundation_call


@pytest.mark.asyncio
async def test_fetch_bedrock_models_engine_enabled_and_flag_surfaces_open():
    """inkwest-agent enabled AND the admin flag set → open models are fetched."""
    captured_args = []

    async def fake_aws_json(args, extra_env=None):
        captured_args.append(args)
        return {"inferenceProfileSummaries": [], "modelSummaries": []}

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
        patch.object(model_catalog, "get_bool_setting", return_value=True),
        patch.object(model_catalog, "inkwest_agent_enabled", return_value=True),
    ):
        await model_catalog._fetch_bedrock_models()

    foundation_call = next(a for a in captured_args if a[1] == "list-foundation-models")
    assert "--by-provider" not in foundation_call


@pytest.mark.asyncio
async def test_fetch_bedrock_profile_only_model_surfaces_with_arn():
    """a model invocable only via an inference profile (omitted by
    list-foundation-models) is surfaced, keying vendor/pricing on the route-stripped
    underlying id, with the profile ARN carried as the invoke target."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "eu.zai.glm5",
                    "inferenceProfileArn": (
                        "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.zai.glm5"
                    ),
                    "type": "SYSTEM_DEFINED",
                },
            ]
        },
        "list-foundation-models": {
            # GLM5 is NOT in the foundation list (profile-only); only the flash variant is.
            "modelSummaries": [
                {
                    "modelId": "zai.glm-4.7-flash",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "outputModalities": ["TEXT"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models(include_open=True)

    assert "eu.zai.glm5" in models
    assert "zai.glm-4.7-flash" in models
    # Vendor + pricing key on the route-stripped underlying id.
    assert model_catalog.bedrock_vendor("eu.zai.glm5") == "zai"
    # ARN resolves by the surfaced (route-prefixed) id AND the underlying namespaced id.
    arn = "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.zai.glm5"
    assert model_catalog.profile_arn("eu.zai.glm5") == arn
    assert model_catalog.profile_arn("zai.glm5") == arn
    # An on-demand model needs no profile → no spurious ARN.
    assert model_catalog.profile_arn("zai.glm-4.7-flash") is None


@pytest.mark.asyncio
async def test_fetch_bedrock_profile_deduped_against_foundation_model():
    """An overlapping base+profile model yields one entry (no dupe); the profile id is the
    recommended invoke path so it wins, and its ARN is keyed by the underlying base id too."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "eu.anthropic.claude-opus-4-8",
                    "inferenceProfileArn": "arn:aws:bedrock:eu-west-1:123:inference-profile/opus",
                    "type": "SYSTEM_DEFINED",
                },
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-opus-4-8",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "outputModalities": ["TEXT"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    # Single entry for the underlying model (dedupe), the regional profile preferred.
    assert models == ["eu.anthropic.claude-opus-4-8"]
    # ARN resolves whether keyed by the surfaced id or the route-stripped base.
    arn = "arn:aws:bedrock:eu-west-1:123:inference-profile/opus"
    assert model_catalog.profile_arn("eu.anthropic.claude-opus-4-8") == arn
    assert model_catalog.profile_arn("anthropic.claude-opus-4-8") == arn


@pytest.mark.asyncio
async def test_fetch_bedrock_application_profile_without_arn_falls_back_to_id():
    """An application profile missing inferenceProfileArn still surfaces, invoked by id."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.anthropic.claude-fable-5", "type": "APPLICATION"},
            ]
        },
        "list-foundation-models": {"modelSummaries": []},
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    assert models == ["eu.anthropic.claude-fable-5"]
    # Missing ARN ⇒ the id itself is the invoke target.
    assert model_catalog.profile_arn("eu.anthropic.claude-fable-5") == "eu.anthropic.claude-fable-5"


@pytest.mark.asyncio
async def test_fetch_bedrock_profiles_best_effort_on_failure():
    """A failing/throttled list-inference-profiles must not break discovery — the
    foundation-model catalog still yields, with no profile ARNs."""

    async def fake_aws_json(args, extra_env=None):
        if args[1] == "list-inference-profiles":
            raise RuntimeError("AccessDeniedException: bedrock:ListInferenceProfiles")
        return {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "outputModalities": ["TEXT"],
                },
            ]
        }

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    assert models == ["anthropic.claude-3-haiku-20240307-v1:0"]
    assert model_catalog._profile_arns == {}


@pytest.mark.asyncio
async def test_fetch_bedrock_profiles_junk_payload_does_not_crash():
    """Malformed profile entries (non-dict element / missing id) are skipped, not fatal —
    one bad entry must not downgrade the whole live catalog to the static fallback."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                "not-a-dict",  # non-dict element — skipped, must not raise
                {"type": "SYSTEM_DEFINED"},  # no inferenceProfileId — skipped
                {"inferenceProfileId": "eu.anthropic.claude-opus-4-8"},  # valid
            ]
        },
        "list-foundation-models": {"modelSummaries": []},
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    assert models == ["eu.anthropic.claude-opus-4-8"]


@pytest.mark.asyncio
async def test_fetch_bedrock_profiles_open_vendor_gated_off_under_claude_only():
    """Engine gate: with include_open False, open-vendor profiles stay out of the catalog
    (only Anthropic profiles surface) — profiles are not added under a Claude-only catalog."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.anthropic.claude-opus-4-8"},
                {"inferenceProfileId": "eu.zai.glm5", "inferenceProfileArn": "arn:zai"},
            ]
        },
        "list-foundation-models": {"modelSummaries": []},
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models(include_open=False)

    assert models == ["eu.anthropic.claude-opus-4-8"]
    assert model_catalog.profile_arn("eu.zai.glm5") is None  # open vendor not captured


def test_bedrock_include_open_defaults_on_with_agent_engine():
    """The open-vendor catalog defaults ON when inkwest-agent is enabled (surfacing the
    whole-Bedrock set is the point), an explicit admin toggle overriding; OFF without it."""
    with patch.object(model_catalog, "inkwest_agent_enabled", return_value=False):
        assert model_catalog.bedrock_include_open() is False  # no engine → never
    with patch.object(model_catalog, "inkwest_agent_enabled", return_value=True):
        # Unset setting → the default passed by the caller (True) is honored.
        with patch.object(model_catalog, "get_bool_setting", lambda k, default=False: default):
            assert model_catalog.bedrock_include_open() is True
        # Explicit admin off overrides the default.
        with patch.object(model_catalog, "get_bool_setting", lambda k, default=False: False):
            assert model_catalog.bedrock_include_open() is False


@pytest.mark.asyncio
async def test_fetch_bedrock_profiles_drops_non_chat_models():
    """Profile-backed embedding/non-TEXT models are dropped (Converse is text-only); a
    text profile and a true profile-only model with no modality data are kept."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.cohere.embed-v4:0", "inferenceProfileArn": "arn:emb"},
                {"inferenceProfileId": "eu.meta.llama-x", "inferenceProfileArn": "arn:llama"},
                {"inferenceProfileId": "eu.zai.glm5", "inferenceProfileArn": "arn:glm"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "cohere.embed-v4:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["EMBEDDING"],  # non-chat → filtered both paths
                },
                {
                    "modelId": "meta.llama-x",
                    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models(include_open=True)

    assert "eu.cohere.embed-v4:0" not in models  # embedding profile dropped
    assert "eu.meta.llama-x" in models  # text profile kept
    assert "eu.zai.glm5" in models  # profile-only, unknown modality → kept
    assert model_catalog.profile_arn("eu.zai.glm5") == "arn:glm"


def test_catalog_status_reports_static_when_no_live_models():
    model_catalog._live_models = None
    with patch.object(model_catalog, "get_settings", return_value=_settings()):
        status = catalog_status()
    assert status["source"] == "static"
    assert status["last_refreshed_at"] is None
    assert status["error"] is None
    assert status["count"] > 0


@pytest.mark.asyncio
async def test_refresh_captures_then_clears_fetch_error():
    """A failed discovery surfaces its error (not a silent static fallback); a later good
    fetch clears it."""
    boom = AsyncMock(side_effect=RuntimeError("aws auth boom"))
    with (
        patch.object(
            model_catalog, "get_settings", return_value=_settings(ai_provider="anthropic")
        ),
        patch.object(model_catalog, "_fetch_anthropic_models", boom),
        patch.object(model_catalog, "_refresh_live_pricing", AsyncMock()),
    ):
        await refresh_catalog(force=True)
        status = catalog_status()
        assert status["last_fetch_failed"] is True
        assert "aws auth boom" in (status["error"] or "")

    ok = AsyncMock(return_value=["claude-fable-5"])
    with (
        patch.object(
            model_catalog, "get_settings", return_value=_settings(ai_provider="anthropic")
        ),
        patch.object(model_catalog, "_fetch_anthropic_models", ok),
        patch.object(model_catalog, "_refresh_live_pricing", AsyncMock()),
    ):
        await refresh_catalog(force=True)
        status = catalog_status()
        assert status["last_fetch_failed"] is False
        assert status["error"] is None
