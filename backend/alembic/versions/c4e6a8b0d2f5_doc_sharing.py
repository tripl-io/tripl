"""docs catalog note visibility and sharing (F24, #308)

* ``doc_files`` gains ``visibility`` (``private`` | ``restricted`` | ``level``;
  every existing note is ``level``, today's behaviour) and
  ``visibility_inherited`` (true: take the nearest folder's setting).
* ``doc_shares``: the users and organization groups a ``restricted`` note is
  shared with, at ``view`` or ``edit``. One principal per row, each through a
  real foreign key, so deleting a user or a group deletes its shares.
* ``doc_folder_settings`` and ``doc_folder_shares``: a visibility set on a folder
  (a path prefix, per scope like ``doc_files``) and its shares.

Downgrade drops the three tables and the two columns: every note is visible at
its level again, which is what the previous release enforces anyway.

Revision ID: c4e6a8b0d2f5
Revises: b3d5f7a9c1e4
Create Date: 2026-10-09 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e6a8b0d2f5"
down_revision: str | None = "b3d5f7a9c1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _share_columns(owner: str, owner_table: str, prefix: str) -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(owner, sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("permission", sa.String(length=8), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (group_id IS NULL)", name=f"ck_{prefix}_one_principal"
        ),
        sa.CheckConstraint("permission IN ('view', 'edit')", name=f"ck_{prefix}_permission"),
        sa.ForeignKeyConstraint([owner], [f"{owner_table}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["organization_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(owner, "user_id", name=f"uq_{prefix}_user"),
        sa.UniqueConstraint(owner, "group_id", name=f"uq_{prefix}_group"),
    ]


def upgrade() -> None:
    with op.batch_alter_table("doc_files") as batch:
        batch.add_column(
            sa.Column("visibility", sa.String(length=16), server_default="level", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "visibility_inherited", sa.Boolean(), server_default=sa.true(), nullable=False
            )
        )
        batch.create_check_constraint(
            "ck_doc_files_visibility", "visibility IN ('private', 'restricted', 'level')"
        )

    op.create_table("doc_shares", *_share_columns("doc_file_id", "doc_files", "doc_shares"))
    op.create_index("ix_doc_shares_user_id", "doc_shares", ["user_id"], unique=False)
    op.create_index("ix_doc_shares_group_id", "doc_shares", ["group_id"], unique=False)

    op.create_table(
        "doc_folder_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("path_key", sa.String(length=512), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "(project_id IS NULL) <> (organization_id IS NULL)",
            name="ck_doc_folder_settings_one_scope",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'restricted', 'level')",
            name="ck_doc_folder_settings_visibility",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_doc_folder_settings_project_path",
        "doc_folder_settings",
        ["project_id", "path_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_doc_folder_settings_org_path",
        "doc_folder_settings",
        ["organization_id", "path_key"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
        sqlite_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "ix_doc_folder_settings_org", "doc_folder_settings", ["organization_id"], unique=False
    )

    op.create_table(
        "doc_folder_shares",
        *_share_columns("folder_id", "doc_folder_settings", "doc_folder_shares"),
    )
    op.create_index("ix_doc_folder_shares_user_id", "doc_folder_shares", ["user_id"], unique=False)
    op.create_index(
        "ix_doc_folder_shares_group_id", "doc_folder_shares", ["group_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_doc_folder_shares_group_id", table_name="doc_folder_shares")
    op.drop_index("ix_doc_folder_shares_user_id", table_name="doc_folder_shares")
    op.drop_table("doc_folder_shares")
    op.drop_index("ix_doc_folder_settings_org", table_name="doc_folder_settings")
    op.drop_index("uq_doc_folder_settings_org_path", table_name="doc_folder_settings")
    op.drop_index("uq_doc_folder_settings_project_path", table_name="doc_folder_settings")
    op.drop_table("doc_folder_settings")
    op.drop_index("ix_doc_shares_group_id", table_name="doc_shares")
    op.drop_index("ix_doc_shares_user_id", table_name="doc_shares")
    op.drop_table("doc_shares")
    with op.batch_alter_table("doc_files") as batch:
        # Dropping the column drops its CHECK with it on PostgreSQL; the batch
        # rebuild on SQLite needs it named.
        batch.drop_constraint("ck_doc_files_visibility", type_="check")
        batch.drop_column("visibility_inherited")
        batch.drop_column("visibility")
