from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import ApiKeyScope
from tripl.models.enum_types import db_enum
from tripl.models.organization import DEFAULT_ORG_ID, default_org_server_default


class ApiKey(UUIDMixin, TimestampMixin, Base):
    """Long-lived bearer token a user can give to non-browser clients.

    Two scopes are supported in v1:
      - ``read``  — GET endpoints only; write surfaces return 403.
      - ``write`` — full editor-level access (subject to the user's role).

    Orthogonal to scope, a key may be bound to a single project via
    ``project_id``. A bound key only authenticates ``/projects/{slug}/...``
    routes for that project; any other project (or a non-project route) is
    rejected with 403. ``project_id`` is ``NULL`` for unscoped keys, which
    keep full cross-project access — that's the legacy default.

    The raw secret is shown to the operator exactly once at creation. Only
    ``key_hash`` (sha-256 of the raw string) is stored; the displayed
    ``key_prefix`` is a short non-secret slice the UI can show in a list so
    operators recognise the key without exposing it.
    """

    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # A project-bound key belongs to its project's organization.
    # F20 PR1: the owning organization. Always the default one for now — the
    # ORM default and the server default both name it (see models/organization).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        default=DEFAULT_ORG_ID,
        server_default=default_org_server_default(),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(db_enum(ApiKeyScope, "api_key_scope"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
