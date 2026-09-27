"""A user's notification delivery preferences (#259). No row means the defaults.

Email is the only external channel in v1. ``email_mode`` decides how the
non-mention notifications reach the inbox: ``off``, ``instant`` (one email per
notification), or a ``daily`` / ``weekly`` digest of what is still unread.
``mentions_email`` sends every @mention on its own, whatever ``email_mode`` is.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin


class EmailMode(enum.StrEnum):
    off = "off"
    instant = "instant"
    daily = "daily"
    weekly = "weekly"


DEFAULT_EMAIL_MODE = EmailMode.daily.value
DEFAULT_MENTIONS_EMAIL = True


class UserNotificationPrefs(TimestampMixin, Base):
    __tablename__ = "user_notification_prefs"
    __table_args__ = (
        CheckConstraint(
            "email_mode IN ('off', 'instant', 'daily', 'weekly')",
            name="ck_user_notification_prefs_email_mode",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    email_mode: Mapped[str] = mapped_column(
        String(16), default=DEFAULT_EMAIL_MODE, server_default=DEFAULT_EMAIL_MODE
    )
    mentions_email: Mapped[bool] = mapped_column(
        Boolean, default=DEFAULT_MENTIONS_EMAIL, server_default="true"
    )
