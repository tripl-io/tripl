"""stored translations of docs catalog notes

* ``doc_translations``: one language of one note, made once by the AI model and
  editable after; ``source_revision`` says which revision of the original it
  matches.
* ``doc_translation_revisions``: every saved state of a translation.
* ``projects.docs_agent_lang`` / ``docs_human_lang``: the languages agents and
  people get by default (NULL: the original).

Downgrade drops the tables and the columns.

Revision ID: c2e4a6b8d0f1
Revises: a1c3e5f7b9d2
Create Date: 2026-10-01 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("docs_agent_lang", sa.String(35), nullable=True))
    op.add_column("projects", sa.Column("docs_human_lang", sa.String(35), nullable=True))
    op.create_table(
        "doc_translations",
        sa.Column("doc_file_id", sa.Uuid(), nullable=False),
        sa.Column("lang", sa.String(35), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("machine", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_doc_translations_status"
        ),
        sa.ForeignKeyConstraint(["doc_file_id"], ["doc_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_file_id", "lang", name="uq_doc_translations_doc_lang"),
    )
    op.create_index("ix_doc_translations_doc_file_id", "doc_translations", ["doc_file_id"])
    op.create_table(
        "doc_translation_revisions",
        sa.Column("translation_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "action IN ('translate', 'edit', 'restore')",
            name="ck_doc_translation_revisions_action",
        ),
        sa.ForeignKeyConstraint(["translation_id"], ["doc_translations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("translation_id", "number", name="uq_doc_translation_revisions_number"),
    )
    op.create_index(
        "ix_doc_translation_revisions_translation_id",
        "doc_translation_revisions",
        ["translation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_doc_translation_revisions_translation_id", table_name="doc_translation_revisions"
    )
    op.drop_table("doc_translation_revisions")
    op.drop_index("ix_doc_translations_doc_file_id", table_name="doc_translations")
    op.drop_table("doc_translations")
    op.drop_column("projects", "docs_human_lang")
    op.drop_column("projects", "docs_agent_lang")
