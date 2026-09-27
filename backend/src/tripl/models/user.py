from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import UserRole
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.user_session import UserSession


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    # Legacy instance role. Not read since F20 PR4 (organization roles replaced
    # it; the guard test ``test_user_role_readers_guard`` keeps it that way);
    # dropped by a later cleanup PR.
    role: Mapped[str] = mapped_column(
        db_enum(UserRole, "user_role"),
        default=UserRole.editor.value,
        server_default=UserRole.editor.value,
    )
    # Operator of the whole instance, independent of any organization role (F20).
    # Grants the operator settings (``api.deps.require_platform_admin``) and no
    # organization or project access at all.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
