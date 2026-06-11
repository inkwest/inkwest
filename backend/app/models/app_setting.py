"""
App setting model: runtime-tunable key/value settings managed by admins.
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    """A runtime-tunable application setting.

    Plain key/value store for the few settings admins may change without a
    redeploy (e.g. the Anthropic model). Values override the corresponding
    environment-variable defaults from ``app.core.config.Settings``.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
