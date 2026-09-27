"""add search_documents.builder_version

Stamps each search document with the generation of the document builders that
produced it, so a builder change can be reconciled on branches nothing else
rebuilds (tripl-uji9).

Existing rows deliberately get 0 rather than the current version. They were
written by an older generation and the whole point is that the sweep should see
them: on the deployed database this is what makes the eight acme-ios working
branches — 7117 documents still built by the pre-keywords-fix code — visible for
repair. Backfilling them to the current version would declare the problem solved
without touching a single document.

The rebuild those branches then get is the ordinary incremental one, so a
document whose text is unchanged keeps its vector and its embedding and only has
its stamp corrected; nothing is re-embedded except what genuinely changed.

Revision ID: c1d2e3f4a5b6
Revises: b6d1f0a3c7e2
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "b6d1f0a3c7e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "search_documents",
        sa.Column("builder_version", sa.Integer(), nullable=False, server_default="0"),
    )
    # The sweep's only query is "which (project, branch) pairs hold a row below
    # the current version". It runs every 10 minutes forever and matches nothing
    # between builder bumps, so index it to keep the quiet case cheap.
    op.create_index(
        "ix_search_documents_builder_version",
        "search_documents",
        ["builder_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_search_documents_builder_version", table_name="search_documents")
    op.drop_column("search_documents", "builder_version")
