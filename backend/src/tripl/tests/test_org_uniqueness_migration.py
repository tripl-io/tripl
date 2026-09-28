"""F20 PR5's revision (``d4a6c8e0f2b4``): per-org uniqueness, no default org.

The data helpers run on a SQLite schema built from the models; the DDL round
trip (downgrade to the instance-wide constraints and the server defaults, the
refused downgrade, upgrade again) runs on PostgreSQL through asyncpg, gated like
the other ``*_pg`` tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import ModuleType

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, create_engine

from tripl.models import Base
from tripl.models.organization import DEFAULT_ORG_ID, Organization
from tripl.tests._default_org import seed_default_organization
from tripl.tests.test_alembic_revisions import _load_migration

MIGRATION = "d4a6c8e0f2b4_per_org_uniqueness_and_no_org_default.py"
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000d4a60")
_OWNED = ("projects", "data_sources", "api_keys", "invitations")


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_migration("org_uniqueness_migration", MIGRATION)


@pytest.fixture
def engine() -> Iterator[Engine]:
    # Foreign keys left OFF on purpose: the rows below are the misaligned ones
    # the revision exists to repair, which the model schema would refuse.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        seed_default_organization(connection)
        connection.execute(
            sa.insert(Organization.__table__).values(
                id=OTHER_ORG,
                slug="other",
                name="Other",
                members_can_create_projects=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _now() -> dict[str, datetime]:
    now = datetime.now(UTC)
    return {"created_at": now, "updated_at": now}


def _project(connection: sa.Connection, slug: str, org: uuid.UUID) -> uuid.UUID:
    project_id = uuid.uuid4()
    connection.execute(
        sa.insert(Base.metadata.tables["projects"]).values(
            id=project_id, name=slug, slug=slug, description="", organization_id=org, **_now()
        )
    )
    return project_id


def _source(
    connection: sa.Connection, name: str, org: uuid.UUID, project_id: uuid.UUID | None
) -> uuid.UUID:
    source_id = uuid.uuid4()
    connection.execute(
        sa.insert(Base.metadata.tables["data_sources"]).values(
            id=source_id,
            name=name,
            organization_id=org,
            project_id=project_id,
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="",
            password_encrypted="",
            **_now(),
        )
    )
    return source_id


def _api_key(connection: sa.Connection, org: uuid.UUID, project_id: uuid.UUID | None) -> uuid.UUID:
    user_id = uuid.uuid4()
    connection.execute(
        sa.insert(Base.metadata.tables["users"]).values(
            id=user_id, email=f"{user_id.hex}@example.com", password_hash="x", **_now()
        )
    )
    key_id = uuid.uuid4()
    connection.execute(
        sa.insert(Base.metadata.tables["api_keys"]).values(
            id=key_id,
            user_id=user_id,
            organization_id=org,
            project_id=project_id,
            name="key",
            key_prefix="tk_",
            key_hash=key_id.hex,
            scope="write",
            **_now(),
        )
    )
    return key_id


def test_revision_chain(migration: ModuleType) -> None:
    assert migration.revision == "d4a6c8e0f2b4"
    assert migration.down_revision == "c9e1a3b5d7f9"


def test_project_bound_rows_take_their_projects_organization(
    engine: Engine, migration: ModuleType
) -> None:
    with engine.begin() as connection:
        elsewhere = _project(connection, "elsewhere", OTHER_ORG)
        misaligned = _source(connection, "misaligned", DEFAULT_ORG_ID, elsewhere)
        aligned = _source(connection, "aligned", OTHER_ORG, elsewhere)
        free = _source(connection, "free", DEFAULT_ORG_ID, None)

    with engine.begin() as connection:
        counts = migration.align_project_bound_rows(connection)
    assert counts == {"data_sources": 1, "api_keys": 0}

    with engine.connect() as connection:
        table = Base.metadata.tables["data_sources"]
        orgs = dict(connection.execute(sa.select(table.c.id, table.c.organization_id)).all())
    assert orgs == {misaligned: OTHER_ORG, aligned: OTHER_ORG, free: DEFAULT_ORG_ID}


def test_cross_org_duplicates_name_what_blocks_the_downgrade(
    engine: Engine, migration: ModuleType
) -> None:
    with engine.begin() as connection:
        _project(connection, "web", DEFAULT_ORG_ID)
        _project(connection, "shop", DEFAULT_ORG_ID)
        _source(connection, "warehouse", DEFAULT_ORG_ID, None)
    with engine.connect() as connection:
        assert migration.cross_org_duplicates(connection) == {}

    with engine.begin() as connection:
        _project(connection, "web", OTHER_ORG)
        _source(connection, "warehouse", OTHER_ORG, None)
    with engine.connect() as connection:
        assert migration.cross_org_duplicates(connection) == {
            "projects.slug": ["web"],
            "data_sources.name": ["warehouse"],
        }


# --- PostgreSQL ---------------------------------------------------------------


def _column_defaults(engine: Engine) -> dict[str, str | None]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT table_name, column_default FROM information_schema.columns "
                "WHERE column_name = 'organization_id' AND table_name IN "
                "('projects', 'data_sources', 'api_keys', 'invitations')"
            )
        ).all()
    return {str(table): default for table, default in rows}


def _constraints(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        names = connection.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid IN "
                "('projects'::regclass, 'data_sources'::regclass, 'api_keys'::regclass)"
            )
        ).scalars()
        indexes = connection.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'projects'")
        ).scalars()
        return set(names) | set(indexes)


_PR5_CONSTRAINTS = {
    "uq_projects_organization_slug",
    "uq_projects_id_organization",
    "uq_data_sources_organization_name",
    "fk_data_sources_project_organization",
    "fk_api_keys_project_organization",
}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_revision_round_trips_on_postgres(migration: ModuleType) -> None:
    from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
    from tripl.tests.test_organizations_migration_pg import _run

    pg_engine = _engine_or_skip()
    Base.metadata.drop_all(pg_engine)
    Base.metadata.create_all(pg_engine)
    try:
        with pg_engine.begin() as connection:
            seed_default_organization(connection)
        assert _constraints(pg_engine) >= _PR5_CONSTRAINTS
        assert set(_column_defaults(pg_engine).values()) == {None}

        await _run(migration.downgrade)
        after_down = _constraints(pg_engine)
        assert not (_PR5_CONSTRAINTS & after_down)
        assert {"uq_data_source_name", "ix_projects_slug"} <= after_down
        defaults = _column_defaults(pg_engine)
        assert set(defaults) == set(_OWNED)
        assert all(default and str(DEFAULT_ORG_ID) in default for default in defaults.values())

        # The pre-PR5 shape the upgrade really meets: a project in another
        # organization whose bound source and key still carry the default one,
        # next to a workspace-wide source. The composite FKs are gone, so the
        # misaligned rows are accepted here, as they were before PR5.
        with pg_engine.begin() as connection:
            connection.execute(
                sa.insert(Organization.__table__).values(
                    id=OTHER_ORG, slug="other", name="Other", members_can_create_projects=True
                )
            )
            elsewhere = _project(connection, "elsewhere", OTHER_ORG)
            bound_source = _source(connection, "bound", DEFAULT_ORG_ID, elsewhere)
            free_source = _source(connection, "free", DEFAULT_ORG_ID, None)
            bound_key = _api_key(connection, DEFAULT_ORG_ID, elsewhere)

        await _run(migration.upgrade)
        assert _constraints(pg_engine) >= _PR5_CONSTRAINTS
        assert "ix_projects_slug" not in _constraints(pg_engine)
        assert set(_column_defaults(pg_engine).values()) == {None}
        with pg_engine.connect() as connection:
            sources = Base.metadata.tables["data_sources"]
            keys = Base.metadata.tables["api_keys"]
            source_orgs = dict(
                connection.execute(sa.select(sources.c.id, sources.c.organization_id)).all()
            )
            key_orgs = dict(connection.execute(sa.select(keys.c.id, keys.c.organization_id)).all())
        assert source_orgs == {bound_source: OTHER_ORG, free_source: DEFAULT_ORG_ID}
        assert key_orgs == {bound_key: OTHER_ORG}

        # Two organizations sharing a slug: the downgrade refuses, schema intact.
        with pg_engine.begin() as connection:
            for org in (DEFAULT_ORG_ID, OTHER_ORG):
                connection.execute(
                    sa.insert(Base.metadata.tables["projects"]).values(
                        id=uuid.uuid4(), name="web", slug="web", description="", organization_id=org
                    )
                )
        with pytest.raises(RuntimeError, match="more than one organization"):
            await _run(migration.downgrade)
        assert _constraints(pg_engine) >= _PR5_CONSTRAINTS
    finally:
        Base.metadata.drop_all(pg_engine)
        pg_engine.dispose()
