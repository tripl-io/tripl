"""Regression tests for the WORKER half of the metric fixes, and for its own repairs.

That change fixed four things on both sides of the request/worker seam but only
tested the request side, so reverting the worker half was invisible
(``_reject_foreign_data_source``, the ``validate_select_sql`` re-raise,
``_metric_breakdown_columns``' self-dedupe and ``_drop_non_finite_values`` were
all unreferenced anywhere under ``tests/``). The review of that batch then found
the scope guard wired into the ``sql`` collector only, a value count taken
before the non-finite filter, and an unscoped ``EventType`` name lookup; those
repairs are pinned here too. Each test names the revert that reddens it, because
a test that passes either way certifies the defect instead of catching it.

Sync-sqlite fixture style follows ``test_metric_collection.py``: a file-backed
engine from ``Base.metadata.create_all`` with the task module's ``_build_adapter``
/ ``_resolve_value_window`` globals monkey-patched per test. The two async tests
at the bottom use the suite's shared in-memory engine, because the helper they
cover lives on the request path.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.intervals import get_interval
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event_type import EventType
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import metrics_service
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks._errors import ScanError, user_facing_error
from tripl.worker.tasks.metrics import metric_collect, metric_rows

_HOUR = get_interval("1h")
_BUCKET = datetime(2026, 1, 1, 10, tzinfo=UTC)
_NEXT_BUCKET = datetime(2026, 1, 1, 11, tzinfo=UTC)
_WINDOW_END = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch6_repairs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


# ── seeding ──────────────────────────────────────────────────────────────────


def _seed_project(session: Session, label: str) -> Project:
    project = Project(
        id=uuid.uuid4(),
        name=f"{label} Project",
        slug=f"{label}-{uuid.uuid4().hex[:8]}",
        description="",
    )
    session.add(project)
    session.commit()
    return project


def _seed_data_source(session: Session, *, owner: Project | None = None) -> DataSource:
    """A warehouse credential, optionally OWNED by one project.

    ``project_id`` NULL is the workspace-global source; set, it is the demo /
    per-project source that ``data_source_out_of_project_scope`` refuses from
    anywhere else.
    """
    data_source = DataSource(
        id=uuid.uuid4(),
        project_id=None if owner is None else owner.id,
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add(data_source)
    session.commit()
    return data_source


def _seed_scan_config(session: Session, project: Project, data_source: DataSource) -> ScanConfig:
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Source Scan",
        base_query="SELECT ts, user_id FROM events",
        time_column="ts",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(config)
    session.commit()
    return config


def _seed_fact_table(session: Session, project: Project, data_source: DataSource) -> FactTable:
    fact_table = FactTable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"ft-{uuid.uuid4().hex[:6]}",
        display_name="Revenue Facts",
        data_source_id=data_source.id,
        sql="SELECT ts, amount, user_id FROM revenue",
        timestamp_column="ts",
        columns=[
            {"name": "ts", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ],
        identifier_columns=["user_id"],
        row_filters=[],
    )
    session.add(fact_table)
    session.commit()
    return fact_table


def _seed_sql_metric(
    session: Session,
    project: Project,
    data_source: DataSource,
    *,
    metric_sql: str = "SELECT ts AS bucket_ts, count() AS value FROM t GROUP BY bucket_ts",
) -> MetricDefinition:
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"sql-{uuid.uuid4().hex[:6]}",
        display_name="Active sessions",
        kind=MetricKind.sql,
        config={"metric_sql": metric_sql, "time_column": "bucket_ts"},
        data_source_id=data_source.id,
        interval=ScanInterval.h1,
        status=MetricStatus.active,
    )
    session.add(definition)
    session.commit()
    return definition


def _seed_fact_metric(
    session: Session,
    project: Project,
    fact_table: FactTable,
) -> MetricDefinition:
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"fact-{uuid.uuid4().hex[:6]}",
        display_name="Revenue",
        kind=MetricKind.fact,
        composition=MetricComposition.single,
        aggregation=MetricAggregation.count,
        fact_table_id=fact_table.id,
        config={},
        interval=ScanInterval.h1,
        status=MetricStatus.active,
    )
    session.add(definition)
    session.commit()
    return definition


class _CredentialOpened(RuntimeError):
    """Raised by the stand-in ``_build_adapter`` when a credential is opened.

    Deliberately NOT a ``ScanError``: every guard below is supposed to refuse
    before the warehouse connection is built, so a test that expects a
    ``ScanError`` fails loudly — rather than passing on the wrong exception — if
    the guard is removed and the collector walks on to ``_build_adapter``.
    """


class _StubAdapter:
    """Enough adapter surface for a resolve that is ALLOWED to reach one."""

    def test_connection(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _SqlAdapter(_StubAdapter):
    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self._column_names = column_names
        self._rows = rows

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return self._column_names, self._rows


def _forbid_adapters(monkeypatch: MonkeyPatch) -> None:
    def _build(ds: DataSource) -> object:
        raise _CredentialOpened(f"opened data source {ds.id}")

    monkeypatch.setattr(metric_collect, "_build_adapter", _build)


# ── the collector applies the save door's scope rule ──────────


class TestCollectionRefusesAForeignWarehouse:
    """The request-path predicate is unit-tested; these pin the CALLERS.

    ``data_source_out_of_project_scope`` being correct proves nothing about the
    collector, which is the door that was open: the beat dispatches every active
    metric with no scoping join and the credential is resolved by primary key.
    """

    def test_a_sql_metric_stored_against_another_projects_source_fails(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """Reverting: delete the ``_reject_foreign_data_source`` call in
        ``_collect_sql`` and this raises ``_CredentialOpened`` instead of
        ``ScanError`` — i.e. the collection reached the foreign credential."""
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            other = _seed_project(session, "intruder")
            foreign_ds = _seed_data_source(session, owner=other)
            definition = _seed_sql_metric(session, project, foreign_ds)

            _forbid_adapters(monkeypatch)

            with pytest.raises(ScanError, match="belongs to another project"):
                metric_collect._collect_sql(session, definition=definition)

    def test_a_single_operand_fact_metric_is_held_to_the_same_rule(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """The fact half reaches its warehouse through ``fact_tables``.

        ``_load_fact_table`` scopes the fact TABLE to the metric's project, which
        this seeding satisfies — the fact table IS the project's. What is foreign
        is the credential behind it, which nothing on this path checked.

        Reverting: delete the ``_reject_foreign_data_source`` call in
        ``_collect_fact_single`` and this raises ``_CredentialOpened``.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            other = _seed_project(session, "intruder")
            foreign_ds = _seed_data_source(session, owner=other)
            fact_table = _seed_fact_table(session, project, foreign_ds)
            definition = _seed_fact_metric(session, project, fact_table)

            _forbid_adapters(monkeypatch)

            with pytest.raises(ScanError, match="belongs to another project"):
                metric_collect._collect_fact_single(
                    session,
                    definition=definition,
                    interval_spec=_HOUR,
                    delta=_HOUR.delta,
                    time_from=_BUCKET,
                    time_to=_NEXT_BUCKET,
                )

    def test_a_ratio_is_refused_on_its_denominator_operand_alone(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """Each operand may sit on its own warehouse, so each is its own door.

        The numerator here is legitimate; only the denominator's fact table
        points at another project's source. Reverting: drop the denominator's
        ``_reject_foreign_data_source`` call (keeping the numerator's) and this
        raises ``_CredentialOpened``.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            other = _seed_project(session, "intruder")
            local_ds = _seed_data_source(session)
            foreign_ds = _seed_data_source(session, owner=other)
            numerator_ft = _seed_fact_table(session, project, local_ds)
            denominator_ft = _seed_fact_table(session, project, foreign_ds)
            definition = _seed_fact_metric(session, project, numerator_ft)
            definition.composition = MetricComposition.ratio
            definition.config = {
                "numerator": {"fact_table_id": str(numerator_ft.id), "aggregation": "count"},
                "denominator": {"fact_table_id": str(denominator_ft.id), "aggregation": "count"},
            }
            session.commit()

            _forbid_adapters(monkeypatch)

            with pytest.raises(ScanError, match="belongs to another project"):
                metric_collect._collect_fact_ratio(
                    session,
                    definition=definition,
                    interval_spec=_HOUR,
                    delta=_HOUR.delta,
                    time_from=_BUCKET,
                    time_to=_NEXT_BUCKET,
                )

    def test_the_batch_adapter_cache_never_lends_one_project_anothers_verdict(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """One fact batch spans every project due at an interval.

        ``check_metric_definitions_due`` groups the fact metrics it dispatches by
        interval alone, so ``_FactBatchContext`` legitimately caches an adapter
        for project A and is then asked for the same source by project B. A owns
        the claim here (it is the only project with a ``ScanConfig`` on a
        workspace-global source); B must still be refused.

        Reverting: change the resolve guard back to ``if adapter is None:`` and
        B's call returns A's cached adapter with no ``ScanError`` at all, so the
        ``pytest.raises`` below fails.
        """
        with sync_session_factory() as session:
            scanning = _seed_project(session, "scanning")
            borrower = _seed_project(session, "borrower")
            shared_ds = _seed_data_source(session)
            _seed_scan_config(session, scanning, shared_ds)
            scanning_ft = _seed_fact_table(session, scanning, shared_ds)
            borrower_ft = _seed_fact_table(session, borrower, shared_ds)

            builds: list[uuid.UUID] = []

            def _build(ds: DataSource) -> object:
                builds.append(ds.id)
                return _StubAdapter()

            monkeypatch.setattr(metric_collect, "_build_adapter", _build)

            context = metric_collect._FactBatchContext(session=session)
            fact_table, _adapter = context.resolve(scanning_ft.id, project_id=scanning.id)

            assert fact_table.id == scanning_ft.id
            assert builds == [shared_ds.id]

            with pytest.raises(ScanError, match="belongs to another project"):
                context.resolve(borrower_ft.id, project_id=borrower.id)

            # The refusal cost no second connection either.
            assert builds == [shared_ds.id]

    def test_a_source_this_project_scans_is_still_collected(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """The positive control: the guard must not simply refuse everything.

        Same workspace-global source, but resolved BY the project that scans it.
        Without this a "fix" that raises unconditionally would look correct.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            shared_ds = _seed_data_source(session)
            _seed_scan_config(session, project, shared_ds)
            fact_table = _seed_fact_table(session, project, shared_ds)

            monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: _StubAdapter())

            context = metric_collect._FactBatchContext(session=session)
            resolved, adapter = context.resolve(fact_table.id, project_id=project.id)

            assert resolved.id == fact_table.id
            assert isinstance(adapter, _StubAdapter)


# ── a stored SELECT reports the validator's English ───────────


class TestStoredSqlReportsTheValidatorsMessage:
    def test_a_missing_time_column_is_named_instead_of_summarised(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """The save-side 422 was already pinned; this is the STORED-row path.

        The point of the finding was what an operator reads on a metric that was
        saved before the check existed: a bare ``ValueError`` is not curated by
        ``user_facing_error``, so it was persisted as "Scan failed due to an
        internal error." on a mistake the user can fix in one edit.

        Reverting: remove the ``except ValueError: raise ScanError(str(exc))``
        around ``validate_select_sql`` and the ``ValueError`` escapes, so
        ``pytest.raises(ScanError)`` fails. Downgrading the message to a generic
        ScanError fails the ``user_facing_error`` assertion instead.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            data_source = _seed_data_source(session)
            definition = _seed_sql_metric(
                session,
                project,
                data_source,
                metric_sql="SELECT count() AS value FROM t",
            )

            _forbid_adapters(monkeypatch)

            with pytest.raises(ScanError) as caught:
                metric_collect._collect_sql(session, definition=definition)

        surfaced = user_facing_error(caught.value)
        assert "must project the time column" in surfaced
        assert "bucket_ts" in surfaced
        assert "internal error" not in surfaced


# ── a stored duplicate breakdown column ──────────────────────


class TestStoredBreakdownColumnsAreDeduplicated:
    def test_a_legacy_row_listing_one_column_twice_collects_it_once(self) -> None:
        """The schema refuses to SAVE a repeat; the stored rows still carry one.

        A column listed twice makes assembly emit each ``(bucket, value)`` row
        twice inside one ``INSERT ... ON CONFLICT DO UPDATE``, which Postgres
        refuses with "command cannot affect row a second time" — the metric then
        errors on every tick. ``TestBreakdownColumnsAreDeduplicated`` covers the
        schema; nothing covered the collector reading the stored list.

        Reverting: drop the ``seen`` set from ``_metric_breakdown_columns`` and
        the result is ``["user_id", "user_id", "country", "app_version"]``.
        """
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="legacy",
            display_name="Legacy",
            kind=MetricKind.fact,
            breakdown_columns=["user_id", "user_id", "country"],
            app_version_column="app_version",
            platform_column="country",
        )

        columns = metric_collect._metric_breakdown_columns(definition)

        assert columns == ["user_id", "country", "app_version"]


# ── non-finite values never reach the table ──────────────────


class TestNonFiniteValuesAreDroppedBeforeTheUpsert:
    """The batch tested the READ side (``_densify_value_rows``) only.

    That test proves a series survives a value already stored. These pin the
    WRITE side, which is supposed to stop the row being stored at all.
    """

    def test_an_infinite_metric_value_is_not_stored(
        self, sync_session_factory: sessionmaker[Session]
    ) -> None:
        """Reverting: delete the ``_drop_non_finite_values`` call at the top of
        ``_upsert_metric_values_rows`` and all three rows are written, so the
        count below is 3 and the value set carries ``inf``.

        ``inf`` rather than NaN on purpose: SQLite keeps ``inf`` verbatim, so the
        revert stores a row instead of tripping the NOT NULL constraint — the
        assertion fails on the behaviour, not on an incidental error.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            data_source = _seed_data_source(session)
            definition = _seed_sql_metric(session, project, data_source)

            written = metric_rows._upsert_metric_values_rows(
                session,
                rows=[
                    _value_row(definition.id, _BUCKET, 1.5),
                    _value_row(definition.id, _NEXT_BUCKET, float("inf")),
                    _value_row(definition.id, _WINDOW_END, float("-inf")),
                ],
            )
            session.commit()

            stored = (
                session.execute(
                    select(MetricValue).where(MetricValue.metric_definition_id == definition.id)
                )
                .scalars()
                .all()
            )

            assert written == 1
            assert [row.value for row in stored] == [1.5]

    def test_an_infinite_breakdown_value_is_not_stored(
        self, sync_session_factory: sessionmaker[Session]
    ) -> None:
        """Same guard on the breakdown upsert. Reverting: delete the
        ``_drop_non_finite_values`` call in
        ``_upsert_metric_value_breakdown_rows`` and both rows are written."""
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            data_source = _seed_data_source(session)
            definition = _seed_sql_metric(session, project, data_source)

            written = metric_rows._upsert_metric_value_breakdown_rows(
                session,
                rows=[
                    _breakdown_row(definition.id, _BUCKET, "US", 4.0),
                    _breakdown_row(definition.id, _BUCKET, "DE", float("inf")),
                ],
            )
            session.commit()

            stored = (
                session.execute(
                    select(MetricValueBreakdown).where(
                        MetricValueBreakdown.metric_definition_id == definition.id
                    )
                )
                .scalars()
                .all()
            )

            assert written == 1
            assert [(row.breakdown_value, row.value) for row in stored] == [("US", 4.0)]

    def test_nan_is_dropped_and_a_non_number_is_left_to_the_db(self) -> None:
        """NaN covered directly, off the DB, because SQLite rewrites it to NULL.

        The second half is the ``_is_finite_value`` contract its own docstring
        now states: a ``str``/``None``/``bool`` passes THIS filter, so the column
        rejects it with a type error naming the row instead of the filter
        silently swallowing it. Reverting ``_is_finite_value`` to reject
        non-numbers drops the ``"nope"`` row and fails the second assertion.
        """
        rows: list[dict[str, object]] = [
            {"value": 2.0},
            {"value": math.nan},
            {"value": "nope"},
            {"value": None},
        ]

        kept = metric_rows._drop_non_finite_values(rows, kind="metric value")

        assert kept == [{"value": 2.0}, {"value": "nope"}, {"value": None}]


def _value_row(definition_id: uuid.UUID, bucket: datetime, value: float) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "metric_definition_id": definition_id,
        "scan_config_id": None,
        "bucket": bucket,
        "value": value,
    }


def _breakdown_row(
    definition_id: uuid.UUID, bucket: datetime, breakdown_value: str, value: float
) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "metric_definition_id": definition_id,
        "scan_config_id": None,
        "bucket": bucket,
        "breakdown_column": "country",
        "breakdown_value": breakdown_value,
        "is_other": False,
        "value": value,
    }


# ── the collector reports what it stored ─────────────────────────────────────


class TestCollectionCountsWhatItStored:
    def test_the_sql_value_count_excludes_the_rows_the_filter_dropped(
        self, sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
    ) -> None:
        """A ClickHouse ``countIf(a)/countIf(b)`` over an empty denominator is
        exactly the case ``_drop_non_finite_values`` exists for, so the summary
        an operator reads must not claim the dropped bucket as a stored value.

        Reverting: put ``total_values += len(value_rows)`` back after the upsert
        in ``_collect_sql`` and the task reports 2 values with 1 row stored, so
        the first assertion fails.
        """
        with sync_session_factory() as session:
            project = _seed_project(session, "owner")
            data_source = _seed_data_source(session)
            definition = _seed_sql_metric(session, project, data_source)

            adapter = _SqlAdapter(
                ["bucket_ts", "value"],
                [(_BUCKET, 5), (_NEXT_BUCKET, float("inf"))],
            )
            monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
            monkeypatch.setattr(
                metric_collect,
                "_resolve_value_window",
                lambda *a, **k: (_BUCKET, _WINDOW_END),
            )

            result = metric_collect._collect_sql(session, definition=definition)

            stored = (
                session.execute(
                    select(MetricValue).where(MetricValue.metric_definition_id == definition.id)
                )
                .scalars()
                .all()
            )

            assert result["values"] == 1
            assert [row.value for row in stored] == [5.0]


# ── the events-metrics type resolution stays inside the project ──────────────


async def _seed_branch_and_type(
    session: AsyncSession, project_id: uuid.UUID, *, kind: BranchKind, type_name: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Add a branch of ``kind`` plus one EventType on it. Caller commits.

    The branch is flushed before the type is added: ``event_types.branch_id`` is
    a foreign key, and one ``add_all`` leaves the insert order to the unit of
    work, which sorts by mapper and emitted the child first.
    """
    branch = PlanBranch(
        id=uuid.uuid4(),
        project_id=project_id,
        name=f"{kind.value}-{uuid.uuid4().hex[:6]}",
        kind=kind.value,
        status=(BranchStatus.merged if kind is BranchKind.main else BranchStatus.draft).value,
    )
    session.add(branch)
    await session.flush()
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project_id,
        branch_id=branch.id,
        name=type_name,
        display_name=type_name.title(),
    )
    session.add(event_type)
    await session.flush()
    return branch.id, event_type.id


class TestMainBranchEventTypeResolutionIsProjectScoped:
    async def test_another_projects_type_id_resolves_to_nothing_local(self) -> None:
        """``event_type_id`` is a raw query parameter on ``GET /events-metrics``
        and nothing on that route checks it belongs to the project in the path.

        Reverting: drop ``EventType.project_id == project_id`` from the NAME
        lookup and the foreign id resolves to its name, then to this project's
        same-named main type — so the assertion that the id comes back unchanged
        fails, and a caller gets a series for a type they never named.
        """
        async with TestSessionLocal() as session:
            here = Project(
                id=uuid.uuid4(),
                name="Here",
                slug=f"here-{uuid.uuid4().hex[:8]}",
                description="",
            )
            there = Project(
                id=uuid.uuid4(),
                name="There",
                slug=f"there-{uuid.uuid4().hex[:8]}",
                description="",
            )
            session.add_all([here, there])
            await session.flush()

            _, here_main_type = await _seed_branch_and_type(
                session, here.id, kind=BranchKind.main, type_name="signup"
            )
            _, there_main_type = await _seed_branch_and_type(
                session, there.id, kind=BranchKind.main, type_name="signup"
            )
            await session.commit()

            resolved = await metrics_service._main_branch_event_type_id(
                session, here.id, there_main_type
            )

            assert resolved == there_main_type
            assert resolved != here_main_type

    async def test_this_projects_branch_copy_still_resolves_to_its_main_twin(self) -> None:
        """The positive control for the predicate above: pairing by NAME inside
        the project is the whole feature, so a fix that simply
        stopped resolving would pass the test above and break the Dynamics card.
        """
        async with TestSessionLocal() as session:
            project = Project(
                id=uuid.uuid4(),
                name="Branchy",
                slug=f"branchy-{uuid.uuid4().hex[:8]}",
                description="",
            )
            session.add(project)
            await session.flush()

            _, main_type = await _seed_branch_and_type(
                session, project.id, kind=BranchKind.main, type_name="signup"
            )
            _, branch_copy_type = await _seed_branch_and_type(
                session, project.id, kind=BranchKind.working, type_name="signup"
            )
            await session.commit()

            resolved = await metrics_service._main_branch_event_type_id(
                session, project.id, branch_copy_type
            )

            assert resolved == main_type
            assert resolved != branch_copy_type
