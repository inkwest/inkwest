"""
App config API route.

Read-only, non-sensitive runtime configuration for any authenticated user —
currently the AI provider and active model, shown in the chat console.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentUser
from app.core.config import get_settings
from app.services.model_catalog import default_model, refresh_catalog, selectable_models
from app.services.pricing import is_legacy_model, model_label

router = APIRouter(prefix="/config", tags=["config"])


class ModelInfo(BaseModel):
    """A selectable model: internal ID, human-friendly display name, lifecycle flag."""

    id: str
    label: str
    legacy: bool


class AppConfigResponse(BaseModel):
    """Schema for app config response."""

    provider: str
    # Default model (ID) applied when a session has no explicit choice.
    model: str
    # Admin-enabled models users may pick per session.
    available_models: list[ModelInfo]


@router.get("", response_model=AppConfigResponse)
async def get_app_config(user: CurrentUser) -> AppConfigResponse:
    """Get the AI provider, default model, and per-session selectable models."""
    await refresh_catalog()  # TTL-aware; no-op while fresh
    return AppConfigResponse(
        provider=get_settings().ai_provider,
        model=default_model(),
        available_models=[
            ModelInfo(id=m, label=model_label(m), legacy=is_legacy_model(m))
            for m in selectable_models()
        ],
    )
