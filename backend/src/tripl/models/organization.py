"""Organizations: the tenant boundary above projects (F20, GH #273).

PR1 adds the schema and nothing else. Every project, data source, API key and
invitation belongs to exactly one organization, and today that is always the
default one below: the migration puts every existing row and every existing user
there, and the ORM default puts every new row there too. Nothing reads these
tables for a permission decision yet — ``users.role`` is still the source of
truth until the gates switch over in a later PR.
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
from tripl.models.domain_enums import OrganizationRole, ProjectMemberRole
from tripl.models.enum_types import db_enum

#: The organization every pre-organization row was migrated into. A fixed,
#: well-known id rather than a generated one, so the migration, the ORM default
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
    """The DDL default for an ``organization_id`` column during the transition.

    Kept alongside the ORM default so a container still running the previous
    release during a deploy — which knows nothing about organizations — can keep
    inserting rows after the migration has made the column NOT NULL.
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
