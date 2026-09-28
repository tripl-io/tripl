"""Organization default project role and per-project opt-out (F20)

* ``project_member_role`` gains ``none``. As a ``project_members`` row it opts
  one organization member out of one project: the project is a 404 for them,
  whatever the organization's default.
* ``organizations.default_project_role`` — reserved since the organizations
  revision, nullable and unread — becomes the project role an organization
  member gets on a project they hold no row in: ``none`` (the release before
  this one: only rows grant access), ``viewer`` or ``editor``; never ``owner``
  (the type has no such label, and the CHECK states the rule). NULL is
  normalised to ``none``; the column becomes NOT NULL with server default
  ``none``, so every existing organization keeps today's behaviour.

Downgrade deletes every ``none`` row (the release before this one would read
it as ``editor``), puts the column back to nullable with no default and NULL
for ``none``, then rebuilds the enum type without the label: PostgreSQL cannot
drop an enum label in place. ``viewer``/``editor`` defaults are kept; the
previous release does not read them.

Revision ID: d2f4a6c8e0b1
Revises: c1e3a5b7d9f2
Create Date: 2026-09-30 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f4a6c8e0b1"
down_revision: str | None = "c1e3a5b7d9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_ENUM = "project_member_role"
_CHECK = "ck_organizations_default_project_role"
_CHECK_SQL = "default_project_role IN ('none', 'viewer', 'editor')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # every supported PostgreSQL version. The block commits it, so the
        # statements below (a new transaction) may use the label.
        with op.get_context().autocommit_block():
            op.execute(f"ALTER TYPE {_ROLE_ENUM} ADD VALUE IF NOT EXISTS 'none' BEFORE 'editor'")

    op.execute(
        "UPDATE organizations SET default_project_role = 'none' WHERE default_project_role IS NULL"
    )
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "default_project_role",
            existing_type=sa.Enum("none", "editor", "viewer", name=_ROLE_ENUM),
            nullable=False,
            server_default="none",
        )
        batch.create_check_constraint(_CHECK, _CHECK_SQL)


def downgrade() -> None:
    op.execute("DELETE FROM project_members WHERE role = 'none'")
    with op.batch_alter_table("organizations") as batch:
        batch.drop_constraint(_CHECK, type_="check")
        batch.alter_column(
            "default_project_role",
            existing_type=sa.Enum("none", "editor", "viewer", name=_ROLE_ENUM),
            nullable=True,
            server_default=None,
        )
    op.execute(
        "UPDATE organizations SET default_project_role = NULL WHERE default_project_role = 'none'"
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TYPE {_ROLE_ENUM} RENAME TO {_ROLE_ENUM}_old")
        op.execute(f"CREATE TYPE {_ROLE_ENUM} AS ENUM ('editor', 'viewer')")
        for table, column in (
            ("project_members", "role"),
            ("organizations", "default_project_role"),
        ):
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {_ROLE_ENUM} "
                f"USING {column}::text::{_ROLE_ENUM}"
            )
        op.execute(f"DROP TYPE {_ROLE_ENUM}_old")
