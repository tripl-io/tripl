"""docs catalog: Markdown notes per project and per organization (F22, #299)

Three tables:

* ``doc_files`` — one note, owned by EITHER a project OR an organization
  (``ck_doc_files_one_scope``). The raw content, frontmatter included, is stored
  verbatim; the frontmatter is also parsed into columns. Paths are unique per
  scope, case-insensitively, through ``path_key`` (``lower(path)``) and one
  partial unique index per scope.
* ``doc_revisions`` — the full content at every saved state, CASCADE with its
  file (a delete is hard; the audit row keeps the last hash).
* ``doc_links`` — ``[[event:...]]`` style references by name, rebuilt on every
  write. Resolution is computed live, so nothing here is ever reindexed.

``search_documents`` needs no change: ``entity_type`` is a String(32) and the
new ``doc`` kind fits.

Revision ID: c3f5a7b9d1e2
Revises: b8d0f2a4c6e8
Create Date: 2026-09-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a7b9d1e2"
down_revision: str | None = "b8d0f2a4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "doc_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("path_key", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("audience", sa.String(length=8), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "(project_id IS NULL) <> (organization_id IS NULL)",
            name="ck_doc_files_one_scope",
        ),
        sa.CheckConstraint(
            "audience IN ('human', 'agent', 'both')",
            name="ck_doc_files_audience",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_doc_files_project_path",
        "doc_files",
        ["project_id", "path_key"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
        sqlite_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_doc_files_org_path",
        "doc_files",
        ["organization_id", "path_key"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
        sqlite_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index("ix_doc_files_org", "doc_files", ["organization_id"], unique=False)

    op.create_table(
        "doc_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("doc_file_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        sa.Column("restored_from_number", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "action IN ('create', 'update', 'move', 'restore', 'import')",
            name="ck_doc_revisions_action",
        ),
        sa.ForeignKeyConstraint(["doc_file_id"], ["doc_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_file_id", "number", name="uq_doc_revisions_file_number"),
    )
    op.create_index("ix_doc_revisions_doc_file_id", "doc_revisions", ["doc_file_id"], unique=False)

    op.create_table(
        "doc_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("doc_file_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("qualifier", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "kind IN ('event', 'event_type', 'field')",
            name="ck_doc_links_kind",
        ),
        sa.ForeignKeyConstraint(["doc_file_id"], ["doc_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_doc_links_doc_file_id", "doc_links", ["doc_file_id"], unique=False)
    op.create_index("ix_doc_links_target", "doc_links", ["kind", "target"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_doc_links_target", table_name="doc_links")
    op.drop_index("ix_doc_links_doc_file_id", table_name="doc_links")
    op.drop_table("doc_links")
    op.drop_index("ix_doc_revisions_doc_file_id", table_name="doc_revisions")
    op.drop_table("doc_revisions")
    op.drop_index("ix_doc_files_org", table_name="doc_files")
    op.drop_index("uq_doc_files_org_path", table_name="doc_files")
    op.drop_index("uq_doc_files_project_path", table_name="doc_files")
    op.drop_table("doc_files")
