from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

if TYPE_CHECKING:
    from tripl.models.user_session import UserSession


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    # Operator of the whole instance, independent of any organization role (F20).
    # Grants the operator settings (``api.deps.require_platform_admin``) and no
    # organization or project access at all.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # When the account proved it owns ``email`` (F20 hosted sign-up): a
    # verification link, an invitation redeemed into a new account, or a
    # password reset confirmed. Stored in both deployment modes, ENFORCED only
    # when ``DEPLOYMENT_MODE=hosted`` (``api.deps.get_current_user``).
    email_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
