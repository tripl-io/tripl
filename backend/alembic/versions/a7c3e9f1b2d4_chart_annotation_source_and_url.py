"""chart annotation source and url (automatic release and deploy markers)

Charts only carried hand-made annotations. Two new writers now add them: the
metrics worker marks the bucket an app version activated in (``release``), and
CI/CLI clients post deploy markers through the API with a write-scoped key
(``api``). ``source`` records which one drew a marker so the UI can mute and hide
the automatic ones; ``url`` is where a marker links out to (release notes, PR,
deploy run).

Every existing row was drawn by a person, so the NOT NULL column backfills to
``manual`` through its server default.

A release marker is unique per (project, label) for good — two scans of one
project, or one scan re-run, see the same activation — so a partial unique index
over ``source = 'release'`` holds that line under concurrent scans, and the
worker inserts with ``ON CONFLICT DO NOTHING`` against it. API markers are only
de-duplicated inside a 24h window by the service (manual ones never are); the
plain (project, source, label) index serves that lookup.

Revision ID: a7c3e9f1b2d4
Revises: f4b8d2e6a913
Create Date: 2026-09-26 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9f1b2d4"
down_revision: str | None = "f4b8d2e6a913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_NAME = "chart_annotation_source"
_VALUES = ("manual", "release", "api")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_VALUES, name=_ENUM_NAME).create(bind, checkfirst=True)
    source_enum = postgresql.ENUM(*_VALUES, name=_ENUM_NAME, create_type=False)
    op.add_column(
        "chart_annotations",
        sa.Column(
            "source",
            source_enum,
            server_default=sa.text("'manual'"),
            nullable=False,
        ),
    )
    op.add_column("chart_annotations", sa.Column("url", sa.String(length=500), nullable=True))
    op.create_index(
        "ix_chart_annotation_project_source_label",
        "chart_annotations",
        ["project_id", "source", "label"],
    )
    op.create_index(
        "uq_chart_annotation_release_label",
        "chart_annotations",
        ["project_id", "label"],
        unique=True,
        postgresql_where=sa.text("source = 'release'"),
    )


def downgrade() -> None:
    op.drop_index("uq_chart_annotation_release_label", table_name="chart_annotations")
    op.drop_index("ix_chart_annotation_project_source_label", table_name="chart_annotations")
    op.drop_column("chart_annotations", "url")
    op.drop_column("chart_annotations", "source")
    postgresql.ENUM(name=_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
