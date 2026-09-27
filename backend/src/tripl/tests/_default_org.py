"""The default organization, present in every test database (F20 PR1).

Not a test module. ``projects``, ``data_sources``, ``api_keys`` and
``invitations`` carry a NOT NULL ``organization_id`` whose ORM default is
``DEFAULT_ORG_ID``, and the suite runs SQLite with foreign keys enforced
(``_sqlite``), so a database without that row refuses the first project any test
builds. Production gets the row from the migration; the suite builds its schema
from ``Base.metadata.create_all`` instead, so it is seeded here.

Seeding hangs off the ``organizations`` table's ``after_create`` event rather
than one fixture: roughly forty modules build their own engines with
``create_all``, and every one of them — the PostgreSQL gates included — gets the
row the moment the table exists, without knowing it is there.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, event

from tripl.models.organization import DEFAULT_ORG_ID, Organization, default_organization_values

_organizations = Organization.__table__


def seed_default_organization(connection: Connection) -> None:
    """Insert the default organization unless it is already there."""
    present = connection.execute(
        sa.select(_organizations.c.id).where(_organizations.c.id == DEFAULT_ORG_ID)
    ).first()
    if present is None:
        connection.execute(sa.insert(_organizations).values(**default_organization_values()))


def _seed_after_create(_table: sa.Table, connection: Connection, **_kw: Any) -> None:
    seed_default_organization(connection)


def install_default_organization_seeding() -> None:
    """Seed the row whenever ``organizations`` is created. Safe to call twice."""
    if not event.contains(_organizations, "after_create", _seed_after_create):
        event.listen(_organizations, "after_create", _seed_after_create)
