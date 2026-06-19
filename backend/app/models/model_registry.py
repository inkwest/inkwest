"""
Model registry: admin-managed catalog of selectable models.

Source of truth for which models users may pick, the default, per-model
metadata (endpoint/arn/options), and pricing overrides. Discovered provider
models are upserted as ``source='discovered'`` rows; operators add
``source='custom'`` rows for models the discovery filter does not surface
(e.g. open-weight Bedrock models). See app.services.model_registry.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ModelRegistry(Base, TimestampMixin):
    """A registered model and its admin-configured metadata."""

    __tablename__ = "model_registry"

    model_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    # anthropic | bedrock | bedrock-open | claude-cli
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # NULL = derive the label from the ID (pricing.model_label).
    display_name: Mapped[str | None] = mapped_column(String(255))
    endpoint: Mapped[str | None] = mapped_column(String(1024))
    arn: Mapped[str | None] = mapped_column(String(1024))
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Per-million-token price overrides; NULL = fall back to the static table.
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # discovered | custom
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="discovered")
    legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # created_at / updated_at come from TimestampMixin.
