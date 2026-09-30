"""The schema as it stood before the legacy instance role was dropped.

Not a test module: a helper for the tests of data migrations that ran while
``users.role`` and ``invitations.role`` still existed (``b8d0f2a4c6e8``,
``c9e1a3b5d7f9``, the project-members backfill). Revision ``a2c4e6f8b0d3``
drops both columns and the ``user_role`` enum, and makes
``invitations.org_role`` NOT NULL; those older revisions still read and write
them, so their tests build this shape instead of the models' own.

The models no longer map ``role`` on ``User`` or ``Invitation``: seed the rows
through the ORM, then set the legacy role with :func:`set_user_role` /
:func:`set_invitation_roles` (which also writes a NULL ``org_role``, a value the
ORM default would otherwise replace).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session

import tripl.models  # noqa: F401  (populates Base.metadata with every table)
from tripl.models.base import Base
from tripl.tests._default_org import seed_default_organization

LEGACY_ROLE_ENUM = "user_role"
LEGACY_ROLE_VALUES = ("owner", "editor", "viewer")

# Typed for the data statements (no DDL): on PostgreSQL the binds are cast to
# the native enum; on SQLite they are plain strings.
_legacy_role = sa.Enum(*LEGACY_ROLE_VALUES, name=LEGACY_ROLE_ENUM, create_constraint=False)
_users = sa.table("users", sa.column("id", sa.Uuid()), sa.column("role", _legacy_role))
_org_role = sa.Enum(
    "owner", "admin", "member", name="organization_member_role", create_constraint=False
)
_invitations = sa.table(
    "invitations",
    sa.column("id", sa.Uuid()),
    sa.column("role", _legacy_role),
    sa.column("org_role", _org_role),
)


def legacy_role_metadata() -> sa.MetaData:
    """A copy of the model metadata with the legacy role columns put back."""
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    role_type = sa.Enum(*LEGACY_ROLE_VALUES, name=LEGACY_ROLE_ENUM, metadata=metadata)
    for table_name in ("users", "invitations"):
        metadata.tables[table_name].append_column(
            sa.Column("role", role_type, nullable=False, server_default="editor")
        )
    metadata.tables["invitations"].c.org_role.nullable = True
    return metadata


def create_legacy_role_schema(engine: Engine) -> sa.MetaData:
    """Create the pre-drop schema on ``engine`` and return its metadata.

    The copied ``organizations`` table does not carry the model table's
    ``after_create`` seeding (``_default_org``), so the default organization is
    seeded here.
    """
    metadata = legacy_role_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        seed_default_organization(connection)
    return metadata


def drop_legacy_role_schema(engine: Engine) -> None:
    """Drop every table and enum of both shapes (PostgreSQL leaves no ``user_role``)."""
    Base.metadata.drop_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP TYPE IF EXISTS {LEGACY_ROLE_ENUM}"))


def set_user_role(session: Session, user_id: uuid.UUID, role: str) -> None:
    session.execute(sa.update(_users).where(_users.c.id == user_id).values(role=role))


def set_invitation_roles(
    session: Session, invitation_id: uuid.UUID, role: str, *, org_role: str | None
) -> None:
    session.execute(
        sa.update(_invitations)
        .where(_invitations.c.id == invitation_id)
        .values(role=role, org_role=org_role)
    )
