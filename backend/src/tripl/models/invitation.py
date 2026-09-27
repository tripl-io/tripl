from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import OrganizationRole, UserRole
from tripl.models.enum_types import db_enum
from tripl.models.organization import DEFAULT_ORG_ID, default_org_server_default


class Invitation(UUIDMixin, TimestampMixin, Base):
    """Single-use, time-limited handle letting an owner add one person by name.

    Deliberately modelled on ``PasswordResetToken`` rather than inventing a
    second token shape: the raw token is **never** stored, only its keyed
    HMAC-SHA256 digest (``auth_utils.hash_session_token``, 64-char hex), so a
    leaked ``invitations`` table is useless without ``SECRET_KEY``. ``used_at``
    enforces single use and doubles as an audit marker of when the invitee
    actually joined.

    Why this exists: registration used to be the only way in, which forced the
    instance-wide ``registration_mode`` to ``open`` on any deployment that still
    needed to onboard someone (tripl-jfm3.80). An invitation is the narrow
    alternative — it admits exactly one address, at a role the owner chose, for
    a bounded window — so the instance-wide door can stay shut.

    ``email`` is stored normalized (``auth_utils.normalize_email``) because it is
    compared against the address presented at redemption; an invite is bound to
    the person it was sent to, not merely to whoever holds the link.

    ``invited_by_user_id`` is nullable ON DELETE SET NULL rather than CASCADE: an
    owner leaving must not silently void invitations they had already sent, and
    the row is still worth keeping as a record of how an account came to exist.
    """

    __tablename__ = "invitations"

    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", create_type=False),
        default=UserRole.editor,
    )
    # Reserved for a later PR: the organization role the invitee will join with.
    # ``role`` above is still the one redemption applies.
    org_role: Mapped[str | None] = mapped_column(
        db_enum(OrganizationRole, "organization_member_role"), nullable=True, default=None
    )
    # F20 PR1: the owning organization. Always the default one for now — the
    # ORM default and the server default both name it (see models/organization).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        default=DEFAULT_ORG_ID,
        server_default=default_org_server_default(),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
