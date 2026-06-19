"""
Provider-aware model discovery.

Resolves the list of models available on the configured AI provider:
- anthropic: live `GET /v1/models` via the SDK
- bedrock: live `aws bedrock list-inference-profiles` + `list-foundation-models`
  (the backend already shells out to the aws CLI for connectors); when the
  ``bedrock_include_open`` admin flag is set, non-Anthropic (open-weight) models
  are surfaced too
- claude-cli: discovered from the backend the CLI itself talks to (its env —
  sandbox_env_file or the process env — configures Bedrock or the Anthropic
  API); CLAUDE_CLI_MODELS overrides, undetectable backends fall back to static

The live list is cached in memory with a TTL and refreshed lazily by
``refresh_catalog()``; any fetch failure falls back to the static catalog derived
from the pricing table, so the app never depends on provider availability.

This module is pure discovery — which model IDs exist on the provider. The
admin-facing selection (enabled subset, default, per-model metadata, pricing
overrides) lives in app.services.model_registry, which consumes this catalog.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime

from anthropic import AsyncAnthropic

from app.core.config import get_settings
from app.services.app_settings import BEDROCK_INCLUDE_OPEN_KEY, get_bool_setting
from app.services.pricing import MODEL_PRICING, model_label

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 3600
# After a fetch *failure*, retry sooner so a boot-time network blip doesn't pin
# the static fallback for a full hour.
CATALOG_RETRY_SECONDS = 120
_AWS_CLI_TIMEOUT = 15

_live_models: list[str] | None = None
_fetched_at: float = 0.0
_fetched_wall: datetime | None = None
_last_fetch_failed: bool = False
_refresh_lock = asyncio.Lock()


def static_catalog() -> list[str]:
    """Provider-appropriate model list when no live catalog is available.

    claude-cli has no catalog API; the operator may declare what the CLI's
    backend serves via CLAUDE_CLI_MODELS. Otherwise the pricing table is the
    fallback. Both are non-empty by construction, so the catalog never is.
    """
    settings = get_settings()
    if settings.ai_provider == "claude-cli":
        declared = [m.strip() for m in settings.claude_cli_models.split(",") if m.strip()]
        if declared:
            return declared
    if settings.ai_provider == "bedrock":
        return [m for m in MODEL_PRICING if m.startswith("anthropic.")]
    return [m for m in MODEL_PRICING if not m.startswith("anthropic.")]


async def _fetch_anthropic_models(
    api_key: str | None = None, base_url: str | None = None
) -> list[str] | None:
    settings = get_settings()
    api_key = api_key or settings.anthropic_api_key
    if not api_key:
        return None
    # Tight timeout: this runs inline in startup and TTL-expired requests —
    # the SDK's 600s default would let a stalled provider hang them.
    client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=15.0, max_retries=1)
    try:
        models = [model.id async for model in client.models.list()]
    finally:
        await client.close()
    # The API's ordering is not contractual; default_model() relies on
    # newest-first, so enforce it like the other catalog sources.
    return sorted(models, key=_model_sort_key) or None


async def _aws_json(args: list[str], extra_env: dict[str, str] | None = None) -> dict:
    """Run an aws CLI command and parse its JSON output."""
    settings = get_settings()
    env = os.environ.copy()
    # Explicit credentials when configured; otherwise the default chain applies.
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        env["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
        env["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        settings.aws_cli_path,
        *args,
        "--output",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_AWS_CLI_TIMEOUT)
    except TimeoutError:
        # wait_for only cancels the wait — reap the process or it leaks.
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"aws {args[0]} timed out after {_AWS_CLI_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"aws {args[0]} failed: {stderr.decode(errors='replace').strip()}")
    return json.loads(stdout)


_BEDROCK_ROUTE_PREFIX = re.compile(r"^(us|eu|apac|us-gov|global)\.")
# Routing preference for the same underlying model: regional cross-region
# profile (recommended invocation) > global profile > bare on-demand ID.
_BEDROCK_ROUTE_RANK = {"us": 0, "eu": 0, "apac": 0, "us-gov": 0, "global": 1, None: 2}
# Newer/larger families first at equal version.
_FAMILY_RANK = {"fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}


def is_anthropic_model(model_id: str) -> bool:
    """Whether a model speaks the Anthropic Messages protocol (a Claude model).

    Non-Anthropic Bedrock models (Llama, Mistral, Nova, DeepSeek...) are reached
    through the Bedrock Converse API instead — see orchestrator_converse.
    """
    lowered = model_id.lower()
    return "anthropic" in lowered or "claude" in lowered


def _model_sort_key(model_id: str) -> tuple:
    """Newest version first, then family tier, then label for stability."""
    label = model_label(model_id)
    match = re.search(r"(\d+)\.(\d+)$", label)
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    family = label.split()[0].lower() if label else ""
    return (-version[0], -version[1], _FAMILY_RANK.get(family, 9), label)


def _dedupe_bedrock_models(models: list[str]) -> list[str]:
    """One entry per underlying model, keeping the preferred routing variant."""
    best: dict[str, str] = {}
    for model_id in models:
        prefix_match = _BEDROCK_ROUTE_PREFIX.match(model_id)
        base = _BEDROCK_ROUTE_PREFIX.sub("", model_id)
        rank = _BEDROCK_ROUTE_RANK[prefix_match.group(1) if prefix_match else None]
        current = best.get(base)
        if (
            current is None
            or rank
            < _BEDROCK_ROUTE_RANK[
                (m.group(1) if (m := _BEDROCK_ROUTE_PREFIX.match(current)) else None)
            ]
        ):
            best[base] = model_id
    return list(best.values())


async def _fetch_bedrock_models(
    region: str | None = None,
    extra_env: dict[str, str] | None = None,
    include_open: bool | None = None,
) -> list[str] | None:
    region = region or get_settings().aws_region
    if not region:
        return None
    if include_open is None:
        include_open = get_bool_setting(BEDROCK_INCLUDE_OPEN_KEY)
    region_args = ["--region", region]
    # Cross-region inference profiles (eu.anthropic..., us.anthropic...) are the
    # recommended way to invoke recent models; on-demand foundation models cover
    # the rest. Both lists are needed for a complete picture.
    profiles = await _aws_json(["bedrock", "list-inference-profiles", *region_args], extra_env)
    # --by-provider narrows the foundation list to Anthropic unless the admin
    # opted into open-weight models, in which case every provider is listed.
    foundation_args = ["bedrock", "list-foundation-models", *region_args]
    if not include_open:
        foundation_args += ["--by-provider", "anthropic"]
    foundation = await _aws_json(foundation_args, extra_env)
    models = [
        p["inferenceProfileId"]
        for p in profiles.get("inferenceProfileSummaries", [])
        if include_open or ".anthropic." in p.get("inferenceProfileId", "")
    ]
    models += [
        m["modelId"]
        for m in foundation.get("modelSummaries", [])
        if "ON_DEMAND" in (m.get("inferenceTypesSupported") or [])
        # Open-weight providers ship embedding/image models too; keep only
        # text-generation ones (Converse is a text API). Anthropic-only listings
        # are all chat models, so the modality filter is skipped there.
        and (not include_open or "TEXT" in (m.get("outputModalities") or []))
    ]
    return sorted(_dedupe_bedrock_models(models), key=_model_sort_key) or None


def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal dotenv parser for the sandbox env file.

    Supports the `docker run --env-file` subset only: plain KEY=VALUE lines and
    `#` comments. No `export` keyword, no multi-line values; surrounding quotes
    are stripped (mixed quoting is not).
    """
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _claude_cli_backend_env() -> dict[str, str]:
    """The environment the in-toolbox/local `claude` actually runs with.

    Sandbox mode passes sandbox_env_file via `docker run --env-file`; the local
    CLI inherits the backend's own environment.
    """
    settings = get_settings()
    if settings.sandbox_enabled and settings.sandbox_env_file:
        try:
            return _parse_env_file(settings.sandbox_env_file)
        except OSError as e:
            logger.warning(f"Cannot read sandbox env file {settings.sandbox_env_file}: {e}")
            return {}
    return dict(os.environ)


async def _fetch_claude_cli_models() -> list[str] | None:
    """Discover the catalog of the backend the claude CLI talks to.

    The CLI's backend is whatever its environment configures (same detection
    the CLI itself performs): Bedrock via CLAUDE_CODE_USE_BEDROCK + AWS_* (the
    aws CLI natively honors AWS_BEARER_TOKEN_BEDROCK), or the Anthropic API /
    a gateway via ANTHROPIC_API_KEY (+ ANTHROPIC_BASE_URL). Vertex and keyless
    gateways are not detectable — static fallback applies.
    """
    env = _claude_cli_backend_env()
    if env.get("CLAUDE_CODE_USE_BEDROCK", "").lower() in ("1", "true", "yes"):
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        if not region:
            return None
        aws_env = {k: v for k, v in env.items() if k.startswith("AWS_")}
        return await _fetch_bedrock_models(region=region, extra_env=aws_env)
    if env.get("ANTHROPIC_API_KEY"):
        return await _fetch_anthropic_models(
            api_key=env["ANTHROPIC_API_KEY"], base_url=env.get("ANTHROPIC_BASE_URL") or None
        )
    return None


def _cache_is_fresh() -> bool:
    if not _fetched_at:
        return False
    ttl = CATALOG_RETRY_SECONDS if _last_fetch_failed else CATALOG_TTL_SECONDS
    return (time.monotonic() - _fetched_at) < ttl


async def refresh_catalog(force: bool = False) -> None:
    """Fetch the provider's live model list into the cache (best-effort).

    No-op while the cache is fresh unless ``force``. Failures keep the previous
    cache (or the static fallback) and never raise. Refreshes are serialized:
    when one is already running, other callers return immediately and serve the
    current (possibly stale) data instead of stampeding the provider.

    Accepted trade-off: the single request that triggers an expired-TTL refresh
    pays the fetch latency inline (worst case ~30s — two aws CLI calls at the
    15s cap). Fine at hourly cadence; move to a background task if it bites.
    """
    global _live_models, _fetched_at, _fetched_wall, _last_fetch_failed
    if not force and _cache_is_fresh():
        return
    if _refresh_lock.locked() and not force:
        return  # another request is refreshing; serve current data meanwhile
    async with _refresh_lock:
        if not force and _cache_is_fresh():
            return  # refreshed while we waited for the lock
        settings = get_settings()
        failed = False
        try:
            if settings.ai_provider == "anthropic":
                fetched = await _fetch_anthropic_models()
            elif settings.ai_provider == "bedrock":
                fetched = await _fetch_bedrock_models()
            elif settings.claude_cli_models.strip():
                fetched = None  # claude-cli with an operator-declared list — it wins
            else:
                # claude-cli: discover from the backend the CLI itself talks to
                fetched = await _fetch_claude_cli_models()
        except Exception as e:
            logger.warning(f"Model catalog fetch failed ({settings.ai_provider}): {e}")
            fetched = None
            failed = True

        _fetched_at = time.monotonic()
        _last_fetch_failed = failed
        if fetched:
            _live_models = fetched
            _fetched_wall = datetime.now(UTC)
            logger.info(f"Model catalog refreshed: {len(fetched)} models ({settings.ai_provider})")


def available_models() -> list[str]:
    """Models available on the current provider: live list, or static fallback."""
    return list(_live_models) if _live_models else static_catalog()


def catalog_status() -> dict:
    """Discovery status for the admin UI: freshness, source, last error state."""
    return {
        "last_refreshed_at": _fetched_wall.isoformat() if _fetched_wall else None,
        "source": "live" if _live_models else "static",
        "last_fetch_failed": _last_fetch_failed,
        "count": len(available_models()),
    }
