"""The scan-identity repair (``f3a9b7c15d2e``) on PostgreSQL, through asyncpg.

The SQLite unit test proves the repair's logic; it cannot prove the SQL. SQLite
binds parameters client-side, PostgreSQL through asyncpg prepares the statement
and asks the server to deduce one type per parameter — and the stamping UPDATE
used its identity parameter three times, in positions the server read as
``character varying`` and as ``text``. The deploy died on "inconsistent types
deduced for parameter $1" before touching a row, with the whole upgrade rolled
back and the API containers never started. This test runs the real repair
against a real PostgreSQL over the driver production uses, on the rows the
migration exists for.

Gated like the digest concurrency test: skipped without ``TRIPL_TEST_PG_URL``,
a failure when ``TRIPL_TEST_PG_REQUIRED=1`` says CI must not skip it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.tests.test_alembic_revisions import _load_migration
from tripl.tests.test_alert_digest_concurrency_pg import _PG_URL, _engine_or_skip

pytestmark = pytest.mark.postgres

REPAIR_MIGRATION = "f3a9b7c15d2e_repair_branch_scan_identities.py"
_PSYCOPG_PREFIX = "postgresql+psycopg://"


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


def _asyncpg_url() -> str:
    assert _PG_URL is not None
    if not _PG_URL.startswith(_PSYCOPG_PREFIX):
        pytest.skip(
            "TRIPL_TEST_PG_URL is not a postgresql+psycopg URL; cannot derive the asyncpg one"
        )
    return "postgresql+asyncpg://" + _PG_URL.removeprefix(_PSYCOPG_PREFIX)


def _seed(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """A branch event authored without an identity; returns (event id, twin id)."""
    project = Project(id=uuid.uuid4(), name="Repair PG", slug=f"repair-pg-{uuid.uuid4().hex[:8]}")
    main = PlanBranch(id=uuid.uuid4(), project_id=project.id, name="main", kind="main")
    working = PlanBranch(
        id=uuid.uuid4(), project_id=project.id, name="PROJ-1", kind="working", status="draft"
    )
    main_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=main.id,
        name="track",
        display_name="Track",
    )
    branch_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=working.id,
        name="track",
        display_name="Track",
    )
    field = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=branch_type.id,
        name="name",
        display_name="Name",
        field_type="string",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name="DS",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
        event_type_id=main_type.id,
        event_name_format="track:{name}",
    )
    labelled = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=branch_type.id,
        branch_id=working.id,
        name="Tap on a model card",
        source_name=None,
    )
    twin = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=branch_type.id,
        branch_id=working.id,
        name="Tap on a model card (again)",
        source_name=None,
    )
    labelled_id, twin_id = labelled.id, twin.id
    # Flushed in dependency order by hand: nothing maps ``branch_id`` as a
    # relationship, so the unit of work cannot order these rows itself.
    with Session(engine) as session, session.begin():
        session.add(project)
        session.flush()
        session.add_all([main, working, data_source])
        session.flush()
        session.add_all([main_type, branch_type])
        session.flush()
        session.add_all([field, config])
        session.flush()
        session.add_all([labelled, twin])
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(), event_id=event.id, field_definition_id=field.id, value="tap"
                )
                for event in (labelled, twin)
            ]
        )
    return labelled_id, twin_id


@pytest.mark.asyncio
async def test_repair_branch_scan_identities_stamps_on_postgres(pg_engine: Engine) -> None:
    labelled_id, twin_id = _seed(pg_engine)
    migration = _load_migration("repair_branch_scan_identities_pg", REPAIR_MIGRATION)

    async_engine = create_async_engine(_asyncpg_url())
    try:
        async with async_engine.begin() as conn:
            outcome = await conn.run_sync(migration.repair_branch_scan_identities)
    finally:
        await async_engine.dispose()

    # One of the two rows deriving ``track:tap`` is stamped, the other reported
    # taken — the same split the SQLite test pins; the point here is that the
    # UPDATE ran at all on this driver.
    assert sorted(outcome["stamped"] + outcome["taken"]) == sorted([str(labelled_id), str(twin_id)])
    assert len(outcome["stamped"]) == 1 and len(outcome["taken"]) == 1
    assert outcome["unfilled"] == []

    with Session(pg_engine) as session:
        stamped = session.get(Event, uuid.UUID(outcome["stamped"][0]))
        assert stamped is not None
        assert stamped.source_name == "track:tap"
        assert stamped.name == "track:tap"
        assert stamped.title in {"Tap on a model card", "Tap on a model card (again)"}
        skipped = session.get(Event, uuid.UUID(outcome["taken"][0]))
        assert skipped is not None and skipped.source_name is None
