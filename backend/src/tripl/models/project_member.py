from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, UniqueConstraint, delete, event, select
from sqlalchemy.orm import Mapped, Session, mapped_column

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


@event.listens_for(Session, "before_flush")
def _drop_rows_left_from_an_earlier_membership(
    session: Session, _flush_context: Any, _instances: Any
) -> None:
    """A user joining an organization starts on its default, not on old rows.

    Access counts a ``project_members`` row only while its holder is a member
    of the project's organization (``project_access``). A row its holder kept
    after leaving — left by a removal before ``org_service.remove_member``
    deleted them, or written by a path racing that removal — would take effect
    again on rejoining and override the organization default without anyone
    choosing it. So every new organization membership, whichever path writes
    it (invitation, an owner's add, SSO, SCIM), deletes its holder's rows in
    that organization's projects first.

    Before the flush, so only rows already in the database go: a row staged
    beside the membership (an acceptance granting a project) is not yet there.
    """
    from tripl.models.organization import OrganizationMember
    from tripl.models.project import Project

    for member in session.new:
        if not isinstance(member, OrganizationMember):
            continue
        # A user or organization created in this same flush has no rows yet.
        if member.user_id is None or member.organization_id is None:
            continue
        session.execute(
            delete(ProjectMember)
            .where(
                ProjectMember.user_id == member.user_id,
                ProjectMember.project_id.in_(
                    select(Project.id).where(Project.organization_id == member.organization_id)
                ),
            )
            .execution_options(synchronize_session="fetch")
        )
