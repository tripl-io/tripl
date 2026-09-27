"""add project-level app-version retention

Revision ID: 0f3a4b5c6d7e
Revises: f2a3b4c5d6e7
Create Date: 2026-07-13 20:45:00.000000

App-version retention is one project policy shared by event monitoring and
catalog metrics. Existing projects inherit the strictest configured scan value
(``MIN``), preserving acme-ios's current value of 3. The legacy scan column is
kept temporarily and mirrored for rolling-deploy compatibility.

Downgrade preserves the current project policy in every versioned scan; it
cannot reconstruct intentionally collapsed, formerly divergent scan values.

SQLite tests build the schema from ``Base.metadata.create_all``; this migration
only runs on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0f3a4b5c6d7e"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "projects",
        sa.Column(
            "app_version_keep_releases",
            sa.Integer(),
            sa.CheckConstraint(
                "app_version_keep_releases BETWEEN 1 AND 100",
                name="ck_projects_app_version_keep_releases_range",
            ),
            nullable=False,
            server_default="5",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE projects AS project
            SET app_version_keep_releases = scan_policy.keep_releases
            FROM (
                SELECT
                    project_id,
                    LEAST(100, GREATEST(1, MIN(app_version_keep_releases))) AS keep_releases
                FROM scan_configs
                WHERE app_version_keep_releases IS NOT NULL
                  AND app_version_column IS NOT NULL
                GROUP BY project_id
            ) AS scan_policy
            WHERE project.id = scan_policy.project_id
            """
        )
    )
    # Existing mixed scan values must collapse immediately too: an old API
    # container in a rolling deploy still reads this compatibility column.
    op.execute(
        sa.text(
            """
            UPDATE scan_configs AS scan
            SET app_version_keep_releases = project.app_version_keep_releases
            FROM projects AS project
            WHERE scan.project_id = project.id
              AND scan.app_version_column IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            UPDATE scan_configs AS scan
            SET app_version_keep_releases = project.app_version_keep_releases
            FROM projects AS project
            WHERE scan.project_id = project.id
              AND scan.app_version_column IS NOT NULL
            """
        )
    )
    op.drop_column("projects", "app_version_keep_releases")
