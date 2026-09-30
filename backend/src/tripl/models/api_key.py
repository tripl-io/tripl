from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import ApiKeyScope
from tripl.models.enum_types import db_enum


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
    __table_args__ = (
        # A project-bound key lives in its project's organization (F20 PR5).
        # MATCH SIMPLE: an unbound key (NULL project_id) is not checked.
        # Two FKs now point at ``projects`` (this and ``project_id``'s own), so
        # ``join(Project)`` without an ON clause raises AmbiguousForeignKeysError:
        # join with ``ApiKey.project_id == Project.id``, and give any future
        # ``relationship()`` to Project ``foreign_keys=[project_id]``.
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            name="fk_api_keys_project_organization",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # The owning organization; a project-bound key's is its project's (enforced
    # by fk_api_keys_project_organization). No ORM or server default (F20 PR5).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
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
    # The organization whose SSO session minted the key (F20), else NULL. In an
    # organization that requires SSO only such a key (or an owner's) is accepted.
    created_with_sso_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
