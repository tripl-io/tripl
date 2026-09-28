"""Organization groups: named sets of an organization's members (F20, GH #273).

A group belongs to exactly one organization and its members are users who are
members of that organization. The database cannot express "a member of the
group's organization" as a foreign key, so :mod:`tripl.services.org_group_service`
enforces it on every add, and ``org_service.remove_member`` drops the user's
group memberships with their organization membership.

Groups are managed by the organization's owners and admins. Note sharing (F24),
event-type ownership and alert routing will read them through
``org_group_service.group_member_ids``; SCIM group sync attaches in the SCIM PR.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin

GROUP_NAME_MAX_LENGTH = 255
GROUP_DESCRIPTION_MAX_LENGTH = 2000


class OrganizationGroup(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "organization_groups"
    # Names are unique per organization regardless of case ("Ops" and "ops"
    # collide): a functional unique index, so a concurrent create or rename that
    # slips past the service's pre-check still fails at the database.
    __table_args__ = (
        Index(
            "uq_organization_group_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(GROUP_NAME_MAX_LENGTH))
    description: Mapped[str] = mapped_column(
        String(GROUP_DESCRIPTION_MAX_LENGTH), default="", server_default=""
    )


class OrganizationGroupMember(UUIDMixin, Base):
    """One user in one group. The user must be a member of the group's organization."""

    __tablename__ = "organization_group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_organization_group_member"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization_groups.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
