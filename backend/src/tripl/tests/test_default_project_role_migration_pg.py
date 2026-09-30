"""The default-project-role revision (``d2f4a6c8e0b1``) on PostgreSQL, both ways.

Runs the revision's real ``downgrade`` and ``upgrade`` over asyncpg, the driver
production migrates with, inside a migration-context transaction the way
``alembic/env.py`` opens one, so the ``ALTER TYPE ... ADD VALUE`` autocommit
block runs as it does in production. Covers the ``none`` row cleanup and the
enum rebuild on the way down, the NULL -> ``none`` normalisation, NOT NULL,
server default and CHECK on the way up.

Gated like the other ``*_pg`` tests: skipped without ``TRIPL_TEST_PG_URL``, a
failure when ``TRIPL_TEST_PG_REQUIRED=1`` says CI must not skip it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from tripl.models import Base
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.tests.test_alembic_revisions import _load_migration
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.tests.test_organizations_migration_pg import _asyncpg_url

pytestmark = pytest.mark.postgres

MIGRATION = "d2f4a6c8e0b1_default_project_role.py"
_CHECK = "ck_organizations_default_project_role"


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    engine = _engine_or_skip()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


async def _run(step: Callable[[], None]) -> None:
    """One direction of the revision as ``alembic/env.py`` runs it.

    The context owns the transaction (``begin_transaction``), which is what lets
    ``autocommit_block`` commit it and step outside for ``ADD VALUE``.
    """

    def _in_context(connection: Connection) -> None:
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            step()

    engine = create_async_engine(_asyncpg_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_in_context)
    finally:
        await engine.dispose()


def _seed(engine: Engine) -> dict[str, uuid.UUID]:
    """At the head schema: an ``editor``-default org and a ``none``-default one."""
    ids = {name: uuid.uuid4() for name in ("open", "closed", "shop", "ada", "bob")}
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Organization(
                    id=ids["open"], slug="open-org", name="Open", default_project_role="editor"
                ),
                Organization(
                    id=ids["closed"], slug="closed-org", name="Closed", default_project_role="none"
                ),
            ]
        )
        session.flush()
        session.add(Project(id=ids["shop"], name="Shop", slug="shop", organization_id=ids["open"]))
        for name in ("ada", "bob"):
            session.add(
                User(id=ids[name], email=f"{name}@example.com", name=name, password_hash="x")
            )
        session.flush()
        session.add_all(
            [
                ProjectMember(project_id=ids["shop"], user_id=ids["ada"], role="editor"),
                ProjectMember(project_id=ids["shop"], user_id=ids["bob"], role="none"),
            ]
        )
    return ids


def _labels(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'project_member_role' ORDER BY e.enumsortorder"
                )
            ).scalars()
        )


def _defaults(engine: Engine) -> dict[uuid.UUID, str | None]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                sa.text("SELECT id, default_project_role::text FROM organizations")
            ).all()
        )


def _member_roles(engine: Engine, project_id: uuid.UUID) -> dict[uuid.UUID, str]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                sa.text("SELECT user_id, role::text FROM project_members WHERE project_id = :p"),
                {"p": project_id},
            ).all()
        )


def _column(engine: Engine) -> tuple[str, str | None]:
    """``(is_nullable, column_default)`` of ``organizations.default_project_role``."""
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'organizations' "
                "AND column_name = 'default_project_role'"
            )
        ).one()
    return row[0], row[1]


def _has_check(engine: Engine) -> bool:
    with engine.connect() as connection:
        return (
            connection.execute(
                sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": _CHECK}
            ).first()
            is not None
        )


def _assert_downgraded(engine: Engine, ids: dict[str, uuid.UUID]) -> None:
    assert _labels(engine) == ["editor", "viewer"]
    # The release before reads a ``none`` row as a grant, so it is deleted.
    assert _member_roles(engine, ids["shop"]) == {ids["ada"]: "editor"}
    defaults = _defaults(engine)
    assert defaults[ids["open"]] == "editor"
    assert defaults[ids["closed"]] is None
    assert _column(engine) == ("YES", None)
    assert not _has_check(engine)


@pytest.mark.asyncio
async def test_default_project_role_revision_round_trips_on_postgres(pg_engine: Engine) -> None:
    migration: ModuleType = _load_migration("default_project_role_migration_pg", MIGRATION)
    ids = _seed(pg_engine)
    assert _labels(pg_engine) == ["none", "editor", "viewer"]

    await _run(migration.downgrade)
    _assert_downgraded(pg_engine, ids)

    await _run(migration.upgrade)

    # ``none`` sits before ``editor``, the order the model declares.
    assert _labels(pg_engine) == ["none", "editor", "viewer"]
    defaults = _defaults(pg_engine)
    assert defaults[ids["open"]] == "editor"
    assert defaults[ids["closed"]] == "none"
    nullable, server_default = _column(pg_engine)
    assert nullable == "NO"
    assert server_default is not None and "'none'" in server_default
    assert _has_check(pg_engine)
    with pg_engine.begin() as connection:
        # The server default applies to a new organization.
        fresh = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO organizations (id, slug, name, created_at, updated_at) "
                "VALUES (:id, 'fresh-org', 'Fresh', now(), now())"
            ),
            {"id": fresh},
        )
        # The new label is usable by a row once the upgrade has committed.
        connection.execute(
            sa.text(
                "INSERT INTO project_members "
                "(id, project_id, user_id, role, created_at, updated_at) "
                "VALUES (:id, :p, :u, CAST('none' AS project_member_role), now(), now())"
            ),
            {"id": uuid.uuid4(), "p": ids["shop"], "u": ids["bob"]},
        )
    assert _defaults(pg_engine)[fresh] == "none"
    with pytest.raises(sa.exc.DBAPIError), pg_engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE organizations SET default_project_role = NULL WHERE id = :id"),
            {"id": ids["open"]},
        )

    await _run(migration.downgrade)
    _assert_downgraded(pg_engine, ids)
    assert _defaults(pg_engine)[fresh] is None

    # Back to head so the fixture's drop_all sees the model schema.
    await _run(migration.upgrade)
    assert _labels(pg_engine) == ["none", "editor", "viewer"]
