"""Unit tests for the fact-table column introspection service.

No live warehouse: the adapter is faked and ``build_adapter`` is monkeypatched.
Scope checks use an in-memory SQLite ``AsyncSession`` with Project / DataSource /
ScanConfig fixtures (a data source "belongs to" a project via a ScanConfig).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.fact_table_introspection_service import (
    FactTableColumn,
    FactTableIntrospectionError,
    bucket_warehouse_type,
    introspect_fact_table,
    is_identifier_column,
)

# --------------------------------------------------------------------------- #
# bucket_warehouse_type — pure helper, no DB / adapter                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        # ClickHouse
        ("Int64", "number"),
        ("UInt8", "number"),
        ("Float64", "number"),
        ("Decimal(18, 2)", "number"),
        ("String", "string"),
        ("FixedString(8)", "string"),
        ("UUID", "string"),
        ("Bool", "bool"),
        ("DateTime", "timestamp"),
        ("DateTime64(3)", "timestamp"),
        ("Date", "timestamp"),
        ("Nullable(Int64)", "number"),
        ("LowCardinality(String)", "string"),
        ("Nullable(DateTime64(3))", "timestamp"),
        ("Array(String)", "string"),
        # Postgres
        ("integer", "number"),
        ("bigint", "number"),
        ("double precision", "number"),
        ("numeric(10,2)", "number"),
        ("boolean", "bool"),
        ("character varying", "string"),
        ("text", "string"),
        ("jsonb", "string"),
        ("timestamp with time zone", "timestamp"),
        ("timestamp without time zone", "timestamp"),
        ("date", "timestamp"),
        # ``interval`` shares the ``int`` prefix but is a time-span, not a number.
        ("interval", "string"),
        ("INTERVAL", "string"),
        # BigQuery
        ("INT64", "number"),
        ("FLOAT64", "number"),
        ("NUMERIC", "number"),
        ("BIGNUMERIC", "number"),
        ("STRING", "string"),
        ("BYTES", "string"),
        ("BOOL", "bool"),
        ("TIMESTAMP", "timestamp"),
        ("DATETIME", "timestamp"),
        # Unknown / empty -> string
        ("", "string"),
        ("SomeFutureType", "string"),
    ],
)
def test_bucket_warehouse_type(type_name: str, expected: str) -> None:
    assert bucket_warehouse_type(type_name) == expected


# --------------------------------------------------------------------------- #
# is_identifier_column — pure helper, no DB / adapter                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "type_name", "expected"),
    [
        # Bare identifier names (any case).
        ("id", "String", True),
        ("ID", "String", True),
        ("Id", "String", True),
        ("uuid", "String", True),
        ("guid", "String", True),
        # snake_case suffixes (any case).
        ("user_id", "String", True),
        ("USER_ID", "String", True),
        ("device_uuid", "LowCardinality(String)", True),
        ("session_guid", "text", True),
        # camelCase suffixes (lower/digit boundary before the suffix).
        ("deviceId", "String", True),
        ("deviceID", "String", True),
        ("sessionUuid", "String", True),
        # Declared-type signal: UUID-typed columns are identifiers by construction.
        ("trace", "UUID", True),
        ("trace", "Nullable(UUID)", True),
        # Plain words that merely end in "id" must NOT match.
        ("paid", "String", False),
        ("valid", "String", False),
        ("android", "String", False),
        ("PAID", "String", False),
        # Ordinary dimensions / measures.
        ("country", "String", False),
        ("session_start", "String", False),
        ("platform", "LowCardinality(String)", False),
        ("idea", "String", False),
        ("identity_theft_reports", "String", False),
        ("", "String", False),
    ],
)
def test_is_identifier_column(name: str, type_name: str, expected: bool) -> None:
    assert is_identifier_column(name, type_name) is expected


# --------------------------------------------------------------------------- #
# Fakes + fixtures                                                             #
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    """Minimal adapter stand-in returning canned columns and preview rows."""

    def __init__(
        self,
        columns: list[ColumnInfo],
        column_names: list[str],
        rows: list[tuple[object, ...]],
    ) -> None:
        self._columns = columns
        self._column_names = column_names
        self._rows = rows
        self.closed = False
        # The preview must read the column SHAPE and never the rows, so the
        # fake counts the calls rather than merely answering them.
        self.preview_calls = 0

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return self._columns

    def get_preview_rows(
        self, base_query: str, limit: int = 10, **kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.preview_calls += 1
        return self._column_names, self._rows[:limit]

    def close(self) -> None:
        self.closed = True


def _install_fake_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _FakeAdapter) -> None:
    from tripl.core.adapters import registry as registry_module

    monkeypatch.setattr(registry_module, "build_adapter", lambda ds: adapter)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _make_project(session: AsyncSession) -> Project:
    project = Project(
        id=uuid.uuid4(),
        name="Fact Project",
        slug=f"facts-{uuid.uuid4().hex[:8]}",
        description="",
    )
    session.add(project)
    await session.flush()
    return project


async def _make_data_source(session: AsyncSession) -> DataSource:
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"warehouse-{uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="analytics",
    )
    session.add(data_source)
    await session.flush()
    return data_source


async def _bind_scan_config(
    session: AsyncSession, project: Project, data_source: DataSource
) -> None:
    """Bind a data source to a project (the project's "membership" link)."""
    session.add(
        ScanConfig(
            project_id=project.id,
            data_source_id=data_source.id,
            name=f"scan-{uuid.uuid4().hex[:8]}",
            base_query="SELECT 1",
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# introspect_fact_table — happy path                                           #
# --------------------------------------------------------------------------- #


async def test_introspect_buckets_columns_and_identifiers(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    columns = [
        ColumnInfo(name="amount", type_name="Float64"),
        ColumnInfo(name="user_id", type_name="String"),
        ColumnInfo(name="is_active", type_name="Bool"),
        ColumnInfo(name="created_at", type_name="DateTime"),
        ColumnInfo(name="event_id", type_name="UUID"),
    ]
    rows: list[tuple[object, ...]] = [(12.5, "u1", True, datetime(2026, 1, 1, 12, 0, 0), "e1")]
    adapter = _FakeAdapter(columns, [c.name for c in columns], rows)
    _install_fake_adapter(monkeypatch, adapter)

    result = await introspect_fact_table(
        session,
        project_id=project.id,
        data_source_id=data_source.id,
        sql="SELECT amount, user_id, is_active, created_at, event_id FROM orders",
        timestamp_column="created_at",
    )

    assert result.columns == [
        FactTableColumn(name="amount", type="number", native_type="Float64"),
        FactTableColumn(name="user_id", type="string", native_type="String"),
        FactTableColumn(name="is_active", type="bool", native_type="Bool"),
        FactTableColumn(name="created_at", type="timestamp", native_type="DateTime"),
        FactTableColumn(name="event_id", type="string", native_type="UUID"),
    ]
    # Id-like string columns, in projection order, excluding the timestamp
    # column: ``user_id`` by name, ``event_id`` by name and UUID type.
    assert result.identifier_candidates == ["user_id", "event_id"]
    assert adapter.closed is True


async def test_introspect_does_not_suggest_ordinary_string_columns(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for tripl-8pc0: string-typed is not enough to be an identifier.

    Under the old rule every non-timestamp string column was a candidate, so
    ordinary dimensions (``country``) and words merely ending in "id"
    (``paid``/``valid``/``android``) were suggested — and then persisted by the
    edit UI's pre-selection.
    """
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    columns = [
        ColumnInfo(name="id", type_name="String"),
        ColumnInfo(name="paid", type_name="String"),
        ColumnInfo(name="valid", type_name="String"),
        ColumnInfo(name="android", type_name="String"),
        ColumnInfo(name="country", type_name="LowCardinality(String)"),
        ColumnInfo(name="session_start", type_name="String"),
        ColumnInfo(name="user_id", type_name="String"),
        ColumnInfo(name="deviceId", type_name="String"),
        ColumnInfo(name="created_at", type_name="DateTime"),
    ]
    adapter = _FakeAdapter(columns, [c.name for c in columns], [])
    _install_fake_adapter(monkeypatch, adapter)

    result = await introspect_fact_table(
        session,
        project_id=project.id,
        data_source_id=data_source.id,
        sql=(
            "SELECT id, paid, valid, android, country, session_start,"
            " user_id, deviceId, created_at FROM events"
        ),
        timestamp_column="created_at",
    )

    assert result.identifier_candidates == ["id", "user_id", "deviceId"]


async def test_introspect_wraps_adapter_failure(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    class _BoomAdapter(_FakeAdapter):
        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            raise RuntimeError("connection refused to secret-host:9000 user=admin")

    adapter = _BoomAdapter([], [], [])
    _install_fake_adapter(monkeypatch, adapter)

    with pytest.raises(FactTableIntrospectionError) as exc_info:
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=data_source.id,
            sql="SELECT bad FROM nope",
        )

    # The user-safe message must not leak the underlying host/credential text.
    assert "secret-host" not in str(exc_info.value)
    assert "admin" not in str(exc_info.value)
    # The adapter is still closed after a query failure.
    assert adapter.closed is True


# --------------------------------------------------------------------------- #
# introspect_fact_table — scope / validation errors                            #
# --------------------------------------------------------------------------- #


async def test_introspect_rejects_missing_data_source_id(
    session: AsyncSession,
) -> None:
    project = await _make_project(session)

    with pytest.raises(FactTableIntrospectionError):
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=None,
            sql="SELECT 1",
        )


async def test_introspect_rejects_unknown_data_source(
    session: AsyncSession,
) -> None:
    project = await _make_project(session)

    with pytest.raises(FactTableIntrospectionError):
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=uuid.uuid4(),
            sql="SELECT 1",
        )


async def test_introspect_rejects_data_source_from_other_project(
    session: AsyncSession,
) -> None:
    project = await _make_project(session)
    other_project = await _make_project(session)
    data_source = await _make_data_source(session)
    # Bound only to the other project — not in scope for ``project``.
    await _bind_scan_config(session, other_project, data_source)

    with pytest.raises(FactTableIntrospectionError):
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=data_source.id,
            sql="SELECT 1",
        )


async def test_introspect_rejects_unsafe_sql_before_touching_adapter(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-SELECT / UNION SQL is rejected before any warehouse call."""
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    columns = [ColumnInfo(name="id", type_name="Int64")]
    adapter = _FakeAdapter(columns, ["id"], [(1,)])
    _install_fake_adapter(monkeypatch, adapter)

    with pytest.raises(FactTableIntrospectionError) as exc_info:
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=data_source.id,
            sql="SELECT id FROM orders UNION SELECT secret FROM users",
        )

    # The SELECT-safety check fires first: the message is the validator's, not the
    # generic adapter-failure text, and the adapter is never opened/closed.
    assert "Could not read columns" not in str(exc_info.value)
    assert adapter.closed is False


async def test_introspect_wraps_adapter_build_failure(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside ``build_adapter`` is wrapped without leaking credentials."""
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    from tripl.core.adapters import registry as registry_module

    def _boom(ds: DataSource) -> object:
        raise RuntimeError("cannot connect to secret-host:5432 password=hunter2")

    monkeypatch.setattr(registry_module, "build_adapter", _boom)

    with pytest.raises(FactTableIntrospectionError) as exc_info:
        await introspect_fact_table(
            session,
            project_id=project.id,
            data_source_id=data_source.id,
            sql="SELECT 1 FROM t",
        )

    message = str(exc_info.value)
    assert "secret-host" not in message
    assert "hunter2" not in message


async def test_introspect_reads_the_shape_and_never_the_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview must not fetch rows at all.

    This replaces three tests that pinned how sample rows were coerced — JSON
    safety, the row cap, non-finite floats. Their subject is gone rather than
    relaxed: nothing in the product ever displayed those rows (not the
    fact-table form, not the CLI, not the MCP server), so the route ran a second
    warehouse query and handed back up to twenty raw rows to nobody.

    Asserted as "get_preview_rows was never called" rather than "sample_rows is
    absent from the response", because the field could be dropped while the
    query still ran — removing the disclosure and keeping the cost. Column
    discovery goes through get_columns and is unaffected, which the identifier
    assertion keeps honest.
    """
    project = await _make_project(session)
    data_source = await _make_data_source(session)
    await _bind_scan_config(session, project, data_source)

    columns = [
        ColumnInfo(name="user_id", type_name="String"),
        ColumnInfo(name="amount", type_name="Float64"),
    ]
    adapter = _FakeAdapter(columns, [c.name for c in columns], [("u1", 1.5)])
    _install_fake_adapter(monkeypatch, adapter)

    result = await introspect_fact_table(
        session,
        project_id=project.id,
        data_source_id=data_source.id,
        sql="SELECT user_id, amount FROM orders",
    )

    assert [c.name for c in result.columns] == ["user_id", "amount"]
    assert result.identifier_candidates == ["user_id"]
    assert adapter.preview_calls == 0, (
        "the fact-table preview fetched warehouse rows; it must read the column shape only"
    )
