"""Organizations: the tenant boundary above projects (F20, GH #273).

Every project, data source, API key and invitation belongs to exactly one
organization. The PR1 migration put every existing row and every existing user
in the default one below. Since PR5 there is no ORM or server default on those
``organization_id`` columns: every write names its organization (the bound one,
``middleware.org_context``) and a write that forgets fails on NOT NULL instead
of landing in the default organization. Since PR4 ``organization_members.role``
is the source of truth for every organization-level permission
(``services.project_access``, ``api.deps``); ``users.role`` is no longer read.

"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, true
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus, ProjectMemberRole
from tripl.models.enum_types import db_enum

#: The organization every pre-organization row was migrated into. A fixed,
#: well-known id rather than a generated one, so the migration, the audit-log default
#: and the test fixtures all name the same row without looking it up.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0f1")
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default organization"


class _DefaultOrgIdLiteral(ColumnElement[uuid.UUID]):
    """``DEFAULT_ORG_ID`` as a column DDL default, spelled per dialect.

    PostgreSQL stores a native ``uuid``; SQLite stores ``sa.Uuid`` as 32 hex
    characters without dashes. One literal cannot be right for both, and a
    server default written in the wrong spelling would reference no organization
    at all on the test database.
    """

    inherit_cache = True
    type = sa.Uuid()


@compiles(_DefaultOrgIdLiteral)
def _compile_default_org_id(element: _DefaultOrgIdLiteral, compiler: SQLCompiler, **kw: Any) -> str:
    return f"'{DEFAULT_ORG_ID.hex}'"


@compiles(_DefaultOrgIdLiteral, "postgresql")
def _compile_default_org_id_pg(
    element: _DefaultOrgIdLiteral, compiler: SQLCompiler, **kw: Any
) -> str:
    return f"'{DEFAULT_ORG_ID}'::uuid"


def default_org_server_default() -> _DefaultOrgIdLiteral:
    """The DDL default for ``audit_log.organization_id``.

    Only the audit log keeps it (its column is nullable anyway: platform-level
    actions have no organization). Projects, data sources, API keys and
    invitations lost theirs in F20 PR5, so a write that forgets its organization
    fails instead of landing in the default one.
    """
    return _DefaultOrgIdLiteral()


def default_organization_values() -> dict[str, Any]:
    """The default organization's row, for the fixtures that seed it."""
    return {
        "id": DEFAULT_ORG_ID,
        "slug": DEFAULT_ORG_SLUG,
        "name": DEFAULT_ORG_NAME,
        "members_can_create_projects": True,
    }


class Organization(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    # Reserved for a later PR: the project role an organization member gets on a
    # project they hold no membership row for. NULL means none (404, as today).
    default_project_role: Mapped[str | None] = mapped_column(
        db_enum(ProjectMemberRole, "project_member_role"), nullable=True, default=None
    )
    members_can_create_projects: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # ``deleting`` from the moment an owner asks to delete the organization
    # until the purge job removes the row (F20 PR6, critique #26). Every read
    # resolves ``active`` organizations only.
    status: Mapped[str] = mapped_column(
        db_enum(OrganizationStatus, "organization_status"),
        default=OrganizationStatus.active.value,
        server_default=OrganizationStatus.active.value,
        nullable=False,
    )


class OrganizationMember(UUIDMixin, TimestampMixin, Base):
    """A user's membership of one organization, with their organization role."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_member"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        db_enum(OrganizationRole, "organization_member_role"),
        default=OrganizationRole.member.value,
    )
