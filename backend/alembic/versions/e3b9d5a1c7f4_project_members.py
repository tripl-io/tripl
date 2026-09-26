"""project membership: ``project_members`` and the grandfathering backfill

Projects stop being visible to every user on the instance. A non-member gets a
404 on every ``/projects/{slug}/...`` route and never sees the project in a list
or feed; the instance owner (``users.role = 'owner'``) keeps seeing everything
and needs no membership row.

* ``project_member_role`` — new native enum (``editor`` | ``viewer``).
* ``project_members`` — one row per (project, user), unique on the pair.
* Backfill, so nobody loses access on deploy:
  - every existing non-owner user becomes a member of every existing NON-demo
    project, with their instance role (editor -> editor, viewer -> viewer);
  - a demo project gets only its creator (as ``editor``, or ``viewer`` for a
    viewer creator). Owners are skipped on real projects (they see everything
    anyway) but a demo's owner-creator still gets a row, mirroring what the
    create path now writes.
  New users see no project until someone adds them.

Revision ID: e3b9d5a1c7f4
Revises: c4e8a2f6b1d3
Create Date: 2026-09-27 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3b9d5a1c7f4"
down_revision: str | None = "c4e8a2f6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_VALUES = ("editor", "viewer")
_ROLE_ENUM = "project_member_role"


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        postgresql.ENUM(*_ROLE_VALUES, name=_ROLE_ENUM).create(bind, checkfirst=True)
        role_type: sa.types.TypeEngine[str] = postgresql.ENUM(
            *_ROLE_VALUES, name=_ROLE_ENUM, create_type=False
        )
    else:
        role_type = sa.Enum(*_ROLE_VALUES, name=_ROLE_ENUM, native_enum=False)

    op.create_table(
        "project_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()") if is_pg else sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", role_type, nullable=False),
        sa.Column("added_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index(
        op.f("ix_project_members_user_id"), "project_members", ["user_id"], unique=False
    )

    if not is_pg:
        # The backfill below is Postgres SQL (gen_random_uuid, enum casts);
        # the SQLite test schema is built from the models and starts empty.
        return

    # Real projects: every non-owner user, with their instance role.
    op.execute(
        """
        INSERT INTO project_members (id, project_id, user_id, role, created_at, updated_at)
        SELECT gen_random_uuid(), p.id, u.id,
               (u.role::text)::project_member_role, now(), now()
        FROM projects p
        CROSS JOIN users u
        WHERE p.is_demo IS NOT TRUE
          AND u.role::text IN ('editor', 'viewer')
        ON CONFLICT ON CONSTRAINT uq_project_member DO NOTHING
        """
    )
    # Demo projects: only their creator.
    op.execute(
        """
        INSERT INTO project_members (id, project_id, user_id, role, created_at, updated_at)
        SELECT gen_random_uuid(), p.id, u.id,
               (CASE WHEN u.role::text = 'viewer' THEN 'viewer' ELSE 'editor' END)
                   ::project_member_role,
               now(), now()
        FROM projects p
        JOIN users u ON u.id = p.created_by_user_id
        WHERE p.is_demo IS TRUE
        ON CONFLICT ON CONSTRAINT uq_project_member DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_members_user_id"), table_name="project_members")
    op.drop_table("project_members")
    if op.get_bind().dialect.name == "postgresql":
        postgresql.ENUM(name=_ROLE_ENUM).drop(op.get_bind(), checkfirst=True)
