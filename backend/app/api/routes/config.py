"""
App config API route.

Read-only, non-sensitive runtime configuration for any authenticated user —
currently the AI provider and active model, shown in the chat console.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentUser
from app.core.config import get_settings
from app.services.engines import (
    configured_default_engine,
    enabled_engines,
    engine_for_turn,
    runnable_engines,
)
from app.services.model_catalog import refresh_catalog
from app.services.model_registry import default_model, merged_entries, selectable_models

router = APIRouter(prefix="/config", tags=["config"])


class ModelInfo(BaseModel):
    """A selectable model: internal ID, human-friendly display name, lifecycle flag."""

    id: str
    label: str
    legacy: bool
    # Enabled engines that can run this model (Claude → both; others → inkwest-agent
    # only). The picker pairs Engine·Model from these when more than one engine is on.
    engines: list[str]
    # Input modalities from the live catalog (P1.8): the chat picker shows an IMAGE icon
    # and enables uploads when "IMAGE" is present. Empty = text-only/unknown (no uploads).
    input_modalities: list[str]


class AppConfigResponse(BaseModel):
    """Schema for app config response."""

    provider: str
    # Default model (ID) applied when a session has no explicit choice.
    model: str
    # The engine the default model runs on (admin choice, else resolved heuristically) —
    # so the picker's "Default" entry names the exact Engine·Model pairing it runs.
    default_engine: str
    # Engines enabled in this deployment (AGENTIC_ENGINES). >1 ⇒ the picker shows
    # Engine·Model pairs; otherwise plain model names (no engine choice to make).
    engines: list[str]
    # Admin-enabled models users may pick per session.
    available_models: list[ModelInfo]
    # Whether file attachments are usable: requires the per-conversation sandbox
    # (claude-cli), the only orchestrator with a /workdir + @-file syntax. The UI
    # hides the attach affordances when false.
    attachments_enabled: bool


@router.get("", response_model=AppConfigResponse)
async def get_app_config(user: CurrentUser) -> AppConfigResponse:
    """Get the AI provider, default model, and per-session selectable models."""
    await refresh_catalog()  # TTL-aware; no-op while fresh
    settings = get_settings()
    selectable = set(selectable_models())
    default_id = default_model()
    return AppConfigResponse(
        provider=settings.ai_provider,
        model=default_id,
        default_engine=engine_for_turn(default_id, configured_default_engine()),
        engines=enabled_engines(),
        available_models=[
            ModelInfo(
                id=e.model_id,
                label=e.label,
                legacy=e.legacy,
                engines=runnable_engines(e.model_id),
                input_modalities=e.input_modalities,
            )
            for e in merged_entries()
            if e.model_id in selectable
        ],
        attachments_enabled=settings.sandbox_enabled and settings.ai_provider == "claude-cli",
    )
