"""per-organization uniqueness, project/org composite keys, no default organization (F20 PR5)

1. :func:`align_project_bound_rows` — a data source or API key bound to a
   project takes that project's organization. PR1 backfilled the keys this way
   and every write since has used the project's organization, so on a healthy
   database this changes nothing; it runs so the composite foreign keys below
   can never refuse an old row.
2. ``projects.slug`` is unique per organization (``uq_projects_organization_slug``)
   instead of instance-wide (``ix_projects_slug``), and ``data_sources.name`` per
   organization (``uq_data_sources_organization_name``) instead of
   ``uq_data_source_name``. Two organizations may both have a project ``web``.
3. ``UNIQUE projects(id, organization_id)`` plus composite foreign keys
   ``(project_id, organization_id) -> projects(id, organization_id)`` on
   ``data_sources`` and ``api_keys``: a project-bound row cannot name another
   organization than its project's. ``MATCH SIMPLE`` (the default) skips the
   check when ``project_id`` is NULL, so workspace-wide sources and unbound keys
   are unaffected.
4. The server default on ``organization_id`` (the default organization) is
   dropped from ``projects``, ``data_sources``, ``api_keys`` and
   ``invitations``: an INSERT that forgets the organization now fails on NOT
   NULL instead of silently landing in the default organization.

Data work is dialect-neutral Core on ``sa.table()``; no bind parameter is
repeated in raw SQL.

Downgrade restores the instance-wide constraints and the server defaults. It
refuses while two organizations share a project slug or a data source name,
since the global constraints could not be rebuilt.

Revision ID: d4a6c8e0f2b4
Revises: c9e1a3b5d7f9
Create Date: 2026-09-28 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "d4a6c8e0f2b4"
down_revision: str | None = "c9e1a3b5d7f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy, not an import: this revision must keep meaning what it meant.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-00000000d0f1"
_OWNED_TABLES = ("projects", "data_sources", "api_keys", "invitations")
_PROJECT_BOUND_TABLES = ("data_sources", "api_keys")

_projects = sa.table(
    "projects",
    sa.column("id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
    sa.column("slug", sa.String()),
)
_data_sources = sa.table(
    "data_sources",
    sa.column("project_id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
    sa.column("name", sa.String()),
)
_api_keys = sa.table(
    "api_keys",
    sa.column("project_id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
)


def align_project_bound_rows(connection: Connection) -> dict[str, int]:
    """Give every project-bound data source and API key its project's organization."""
    counts: dict[str, int] = {}
    for name, table in (("data_sources", _data_sources), ("api_keys", _api_keys)):
        project_org = (
            sa.select(_projects.c.organization_id)
            .where(_projects.c.id == table.c.project_id)
            .scalar_subquery()
        )
        result = connection.execute(
            sa.update(table)
            .where(table.c.project_id.is_not(None), table.c.organization_id != project_org)
            .values(organization_id=project_org)
        )
        counts[name] = int(result.rowcount or 0)
    return counts


def cross_org_duplicates(connection: Connection) -> dict[str, list[str]]:
    """Project slugs and data source names held by more than one organization."""
    duplicates: dict[str, list[str]] = {}
    for label, column in (
        ("projects.slug", _projects.c.slug),
        ("data_sources.name", _data_sources.c.name),
    ):
        rows = connection.execute(
            sa.select(column).group_by(column).having(sa.func.count() > 1).order_by(column)
        ).scalars()
        values = [str(value) for value in rows]
        if values:
            duplicates[label] = values
    return duplicates


def _default_org_literal() -> sa.TextClause:
    return sa.text(f"'{DEFAULT_ORG_ID}'::uuid")


def upgrade() -> None:
    align_project_bound_rows(op.get_bind())

    for table in _OWNED_TABLES:
        op.alter_column(table, "organization_id", server_default=None)

    op.drop_index("ix_projects_slug", table_name="projects")
    op.create_unique_constraint(
        "uq_projects_organization_slug", "projects", ["organization_id", "slug"]
    )
    op.create_unique_constraint(
        "uq_projects_id_organization", "projects", ["id", "organization_id"]
    )

    op.drop_constraint("uq_data_source_name", "data_sources", type_="unique")
    op.create_unique_constraint(
        "uq_data_sources_organization_name", "data_sources", ["organization_id", "name"]
    )

    for table in _PROJECT_BOUND_TABLES:
        op.create_foreign_key(
            f"fk_{table}_project_organization",
            table,
            "projects",
            ["project_id", "organization_id"],
            ["id", "organization_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    duplicates = cross_org_duplicates(op.get_bind())
    if duplicates:
        raise RuntimeError(
            "Cannot downgrade past d4a6c8e0f2b4: these values are used by more than one "
            f"organization and the instance-wide unique constraints cannot be restored: "
            f"{duplicates}. Rename them first."
        )

    for table in _PROJECT_BOUND_TABLES:
        op.drop_constraint(f"fk_{table}_project_organization", table, type_="foreignkey")

    op.drop_constraint("uq_data_sources_organization_name", "data_sources", type_="unique")
    op.create_unique_constraint("uq_data_source_name", "data_sources", ["name"])

    op.drop_constraint("uq_projects_id_organization", "projects", type_="unique")
    op.drop_constraint("uq_projects_organization_slug", "projects", type_="unique")
    op.create_index("ix_projects_slug", "projects", ["slug"], unique=True)

    for table in _OWNED_TABLES:
        op.alter_column(table, "organization_id", server_default=_default_org_literal())
