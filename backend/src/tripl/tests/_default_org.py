"""The default organization, present in every test database (F20 PR1).

Not a test module. ``projects``, ``data_sources``, ``api_keys`` and
``invitations`` carry a NOT NULL ``organization_id``, and the suite runs SQLite
with foreign keys enforced (``_sqlite``), so a database without that row refuses
the first project any test builds. Production gets the row from the migration;
the suite builds its schema from ``Base.metadata.create_all`` instead, so it is
seeded here.

Seeding hangs off the ``organizations`` table's ``after_create`` event rather
than one fixture: roughly forty modules build their own engines with
``create_all``, and every one of them — the PostgreSQL gates included — gets the
row the moment the table exists, without knowing it is there.

Since F20 PR5 those columns have no ORM or server default: application code
must name the organization, and a write that forgets fails. Tests still build
hundreds of rows with ``Project(name=..., slug=...)``, so
:func:`install_test_row_default_org` gives the default organization to a row
CONSTRUCTED BY TEST CODE (the caller's file is under ``tripl/tests``) that names
none. A row built by application code — a service a test calls — gets nothing
from it, so the suite still catches a write path that forgets the organization.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, event

from tripl.models.api_key import ApiKey
from tripl.models.data_source import DataSource
from tripl.models.invitation import Invitation
from tripl.models.organization import DEFAULT_ORG_ID, Organization, default_organization_values
from tripl.models.project import Project

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


#: The models whose ``organization_id`` has no default since F20 PR5.
ORG_OWNED_MODELS: tuple[type, ...] = (Project, DataSource, ApiKey, Invitation)

_TESTS_DIR = str(Path(__file__).resolve().parent)
_THIS_FILE = str(Path(__file__).resolve())


def _constructed_by_test_code() -> bool:
    """Whether the nearest caller outside SQLAlchemy and this file is test code."""
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        module = frame.f_globals.get("__name__", "")
        # "<string>" frames are SQLAlchemy's generated instrumented __init__.
        generated = filename.startswith("<")
        if filename != _THIS_FILE and not generated and not module.startswith("sqlalchemy"):
            return str(Path(filename).resolve()).startswith(_TESTS_DIR)
        frame = frame.f_back  # type: ignore[assignment]
    return False


def _default_org_for_test_rows(
    _target: object, _args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    if kwargs.get("organization_id") is None and _constructed_by_test_code():
        kwargs["organization_id"] = DEFAULT_ORG_ID


def install_test_row_default_org() -> None:
    """Default test-built org-owned rows to the default organization. Safe to call twice."""
    for model in ORG_OWNED_MODELS:
        if not event.contains(model, "init", _default_org_for_test_rows):
            event.listen(model, "init", _default_org_for_test_rows)
