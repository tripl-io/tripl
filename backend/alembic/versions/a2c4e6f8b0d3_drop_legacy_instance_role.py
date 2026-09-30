"""drop the legacy instance role: users.role, invitations.role, user_role (F20, GH #273)

Organization roles (``organization_members``) and project roles replaced the
instance role in F20 PR4; nothing has read ``users.role`` or
``invitations.role`` since. This revision removes them:

* ``invitations.org_role``: every row still NULL gets ``member`` (the role
  redemption already applied to a NULL), then the column becomes NOT NULL;
* ``invitations.role`` and ``users.role`` are dropped;
* the PostgreSQL enum type ``user_role`` is dropped once no column uses it.

Downgrade restores both columns as the previous release declared them
(``user_role`` NOT NULL, server default ``editor``): every user is ``editor``
except an ``owner`` of the default organization, who is ``owner`` again; an
invitation is ``owner`` where its ``org_role`` is ``owner`` and ``editor``
otherwise (``admin`` and ``member`` both land on ``editor``, the instance
vocabulary having no admin). ``invitations.org_role`` becomes nullable again.

The data statements are Core on ``sa.table()`` with typed enum columns, so
asyncpg casts the binds to the native types and no bind parameter repeats.

Revision ID: a2c4e6f8b0d3
Revises: f8a0c2e4b6d9
Create Date: 2026-10-06 10:00:00.000000

"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2c4e6f8b0d3"
down_revision: str | None = "f8a0c2e4b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies, not imports: this revision must keep meaning what it meant when
# it ran, whatever the application's constants become.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0f1")
_USER_ROLE_ENUM = "user_role"
_USER_ROLE_VALUES = ("owner", "editor", "viewer")
_ORG_ROLE_ENUM = "organization_member_role"
_ORG_ROLE_VALUES = ("owner", "admin", "member")

# Typed enums for the data statements (``create_constraint=False``: no DDL).
_user_role = sa.Enum(*_USER_ROLE_VALUES, name=_USER_ROLE_ENUM, create_constraint=False)
_org_role = sa.Enum(*_ORG_ROLE_VALUES, name=_ORG_ROLE_ENUM, create_constraint=False)

_users = sa.table("users", sa.column("id", sa.Uuid()), sa.column("role", _user_role))
_invitations = sa.table(
    "invitations", sa.column("role", _user_role), sa.column("org_role", _org_role)
)
_organization_members = sa.table(
    "organization_members",
    sa.column("organization_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("role", _org_role),
)


def _org_role_ddl_type() -> postgresql.ENUM:
    return postgresql.ENUM(*_ORG_ROLE_VALUES, name=_ORG_ROLE_ENUM, create_type=False)


def _user_role_ddl_type() -> postgresql.ENUM:
    return postgresql.ENUM(*_USER_ROLE_VALUES, name=_USER_ROLE_ENUM, create_type=False)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.update(_invitations).where(_invitations.c.org_role.is_(None)).values(org_role="member")
    )
    op.alter_column("invitations", "org_role", existing_type=_org_role_ddl_type(), nullable=False)

    # The columns first: a type still in use cannot be dropped.
    op.drop_column("invitations", "role")
    op.drop_column("users", "role")
    if connection.dialect.name == "postgresql":
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_USER_ROLE_ENUM}"))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        postgresql.ENUM(*_USER_ROLE_VALUES, name=_USER_ROLE_ENUM).create(
            connection, checkfirst=True
        )

    op.add_column(
        "users",
        sa.Column("role", _user_role_ddl_type(), nullable=False, server_default="editor"),
    )
    default_org_owners = sa.select(_organization_members.c.user_id).where(
        _organization_members.c.organization_id == DEFAULT_ORG_ID,
        _organization_members.c.role == "owner",
    )
    connection.execute(
        sa.update(_users).where(_users.c.id.in_(default_org_owners)).values(role="owner")
    )

    op.add_column(
        "invitations",
        sa.Column("role", _user_role_ddl_type(), nullable=False, server_default="editor"),
    )
    connection.execute(
        sa.update(_invitations).where(_invitations.c.org_role == "owner").values(role="owner")
    )
    op.alter_column("invitations", "org_role", existing_type=_org_role_ddl_type(), nullable=True)
