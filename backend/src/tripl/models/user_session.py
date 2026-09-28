from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

if TYPE_CHECKING:
    from tripl.models.user import User


#: How a browser session signed in (F20 SSO).
AUTH_METHOD_PASSWORD = "password"
AUTH_METHOD_SSO = "sso"


class UserSession(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        CheckConstraint("auth_method IN ('password', 'sso')", name="ck_user_sessions_auth_method"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    # ``sso`` when the session came out of an organization's OIDC sign-in, and
    # ``sso_organization_id`` names that organization: the session satisfies
    # that organization's "SSO required" and no other's (F20).
    auth_method: Mapped[str] = mapped_column(
        String(16), default=AUTH_METHOD_PASSWORD, server_default=AUTH_METHOD_PASSWORD
    )
    sso_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    user: Mapped[User] = relationship(back_populates="sessions", lazy="selectin")
