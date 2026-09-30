from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import ProjectMemberRole
from tripl.models.enum_types import db_enum


class ProjectMember(UUIDMixin, TimestampMixin, Base):
    """A user's membership of one project.

    Non-members do not see a project at all: every ``/projects/{slug}/...``
    route answers 404 for them and it is absent from every list and feed. An
    owner or admin of the project's organization needs no row — they see and
    manage every project of that organization. The project's creator is added
    as an ``editor`` member when the project is created.

    ``added_by_user_id`` is provenance only (SET NULL when that user is deleted).
    """

    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        db_enum(ProjectMemberRole, "project_member_role"),
        default=ProjectMemberRole.viewer.value,
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
