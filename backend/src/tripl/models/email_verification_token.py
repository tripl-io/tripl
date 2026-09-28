from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


class EmailVerificationToken(UUIDMixin, TimestampMixin, Base):
    """Single-use, time-limited handle proving a user owns their email address (F20).

    Mirrors :class:`~tripl.models.password_reset_token.PasswordResetToken`: the
    raw token is **never** stored, only its keyed HMAC-SHA256 digest
    (``auth_utils.hash_session_token``), so a leaked column is useless without
    ``SECRET_KEY``. ``used_at`` enforces single use; a user's other tokens go
    with the ``ON DELETE CASCADE`` foreign key plus explicit deletes in
    ``email_verification_service``.
    """

    __tablename__ = "email_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
