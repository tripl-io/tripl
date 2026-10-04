"""Replay deletes: scope, double counting and the bind-parameter cap.

Three defects in the replay-only per-key deletes of the metrics worker:

* the scan-wide distribution drift row (``event_type_id IS NULL``) was folded
  into a row-value ``IN``, which never matches a NULL component, so every
  replay of an already-collected window appended another duplicate;
* the breakdown delete keyed on ``breakdown_value``/``is_other``, so a value
  that crossed the top-N / "Other" boundary between two runs survived under its
  old label next to the new one and was counted twice at read time;
* all four per-key deletes emitted one statement with one bind parameter per
  key element, which crosses PostgreSQL's 65535-parameter protocol ceiling on a
  large replay chunk.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics import tasks as metrics
from tripl.worker.tasks.metrics.metric_rows import (
    _MAX_BIND_PARAMS,
    _chunk_keys,
    _delete_distribution_drifts_rows,
    _delete_event_metric_breakdown_rows,
    _delete_event_metrics_rows,
    _delete_event_type_metrics_rows,
)

_BUCKET = datetime(2026, 1, 1, 10)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_b2.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _create_scan_config(session: Session) -> ScanConfig:
    """Minimal project/source/event-type/config quartet for the worker path."""
    project = Project(
        id=uuid.uuid4(),
        name="Metrics Project",
        slug=f"metrics-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Metrics DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="structured",
        display_name="Structured",
        description="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        event_type_id=event_type.id,
        name="Structured Events",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    # A replay rebuilds its column metadata from the catalog rather than from a
    # fresh cardinality scan, so the event name can only be derived from a
    # column that has a FieldDefinition. With no stored field values the column
    # reads as low-cardinality, which is what makes the name "event_name=Login".
    event_name_field = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=event_type.id,
        name="event_name",
        display_name="Event name",
        field_type="string",
        is_required=False,
        description="",
    )
    session.add_all([project, data_source, event_type, config, event_name_field])
    session.commit()
    return config


# --------------------------------------------------------------------------
# bind-parameter cap on the per-key deletes
# --------------------------------------------------------------------------


class _Result:
    rowcount = 7


class _RecordingSession:
    """Stands in for the worker Session; the deletes only ever call execute()."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, stmt: object) -> _Result:
        self.statements.append(stmt)
        return _Result()


def _param_count(stmt: object) -> int:
    compiled = stmt.compile(  # type: ignore[attr-defined]
        dialect=postgresql.dialect(paramstyle="pyformat"),
        compile_kwargs={"render_postcompile": True},
    )
    return len(compiled.params)


def _assert_under_ceiling(session: _RecordingSession) -> None:
    for stmt in session.statements:
        assert _param_count(stmt) <= _MAX_BIND_PARAMS


class TestChunkKeys:
    def test_batch_size_follows_the_key_width(self) -> None:
        keys = [(uuid.uuid4(), _BUCKET, "country") for _ in range(40_000)]

        batches = list(_chunk_keys(keys))

        assert len(batches) > 1
        assert len(batches[0]) == (_MAX_BIND_PARAMS - 1) // 3
        assert [key for batch in batches for key in batch] == keys

    def test_empty_input_yields_nothing(self) -> None:
        assert list(_chunk_keys([])) == []

    def test_small_key_set_is_a_single_batch(self) -> None:
        keys = [(uuid.uuid4(), _BUCKET) for _ in range(5)]

        assert list(_chunk_keys(keys)) == [keys]


def test_event_metric_deletes_stay_under_the_bind_ceiling() -> None:
    scan_config_id = uuid.uuid4()
    keys = [(uuid.uuid4(), _BUCKET) for _ in range(40_000)]

    for helper in (_delete_event_metrics_rows, _delete_event_type_metrics_rows):
        session = _RecordingSession()

        deleted = helper(session, scan_config_id=scan_config_id, keys=keys)  # type: ignore[arg-type]

        assert len(session.statements) > 1
        _assert_under_ceiling(session)
        # One bind per key element plus the scan_config_id bind per statement:
        # nothing dropped and nothing sent twice.
        assert sum(_param_count(stmt) for stmt in session.statements) == (
            2 * len(keys) + len(session.statements)
        )
        assert deleted == _Result.rowcount * len(session.statements)


@pytest.mark.parametrize("constraint", ["event", "type"])
def test_breakdown_delete_stays_under_the_bind_ceiling(constraint: str) -> None:
    scan_config_id = uuid.uuid4()
    keys = [(uuid.uuid4(), _BUCKET, "country") for _ in range(40_000)]
    session = _RecordingSession()

    deleted = _delete_event_metric_breakdown_rows(
        session,  # type: ignore[arg-type]
        scan_config_id=scan_config_id,
        keys=keys,
        constraint=constraint,
    )

    assert len(session.statements) > 1
    _assert_under_ceiling(session)
    assert sum(_param_count(stmt) for stmt in session.statements) == (
        3 * len(keys) + len(session.statements)
    )
    assert deleted == _Result.rowcount * len(session.statements)


def test_distribution_drift_delete_stays_under_the_bind_ceiling() -> None:
    scan_config_id = uuid.uuid4()
    # Every fifth key is the scan-wide (NULL event_type_id) row the producer
    # emits for each (field, bucket); it is deleted by a two-element key.
    keys: list[tuple[uuid.UUID | None, datetime, str]] = [
        (None if index % 5 == 0 else uuid.uuid4(), _BUCKET, "platform") for index in range(25_000)
    ]
    scan_wide = sum(1 for key in keys if key[0] is None)
    session = _RecordingSession()

    deleted = _delete_distribution_drifts_rows(
        session,  # type: ignore[arg-type]
        scan_config_id=scan_config_id,
        keys=keys,
    )

    assert len(session.statements) > 1
    _assert_under_ceiling(session)
    assert sum(_param_count(stmt) for stmt in session.statements) == (
        3 * (len(keys) - scan_wide) + 2 * scan_wide + len(session.statements)
    )
    assert deleted == _Result.rowcount * len(session.statements)


def test_small_key_sets_stay_a_single_statement() -> None:
    scan_config_id = uuid.uuid4()
    session = _RecordingSession()

    deleted = _delete_event_metric_breakdown_rows(
        session,  # type: ignore[arg-type]
        scan_config_id=scan_config_id,
        keys=[(uuid.uuid4(), _BUCKET, "country") for _ in range(5)],
        constraint="event",
    )

    assert len(session.statements) == 1
    assert deleted == _Result.rowcount

    empty = _RecordingSession()
    assert (
        _delete_event_metric_breakdown_rows(
            empty,  # type: ignore[arg-type]
            scan_config_id=scan_config_id,
            keys=[],
            constraint="event",
        )
        == 0
    )
    assert empty.statements == []


# --------------------------------------------------------------------------
# the scan-wide drift row is NULL-scoped
# --------------------------------------------------------------------------


def test_delete_distribution_drift_rows_clears_the_scan_wide_scope(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        event_type_id = config.event_type_id
        assert event_type_id is not None

        def _drift(scope: uuid.UUID | None, field_name: str) -> DistributionDrift:
            return DistributionDrift(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_type_id=scope,
                field_name=field_name,
                bucket=_BUCKET,
                psi=0.1,
                band="stable",
                baseline_total=1,
                current_total=1,
                top_movers=[],
            )

        session.add_all(
            [
                _drift(None, "platform"),
                _drift(event_type_id, "platform"),
                _drift(None, "country"),
            ]
        )
        session.commit()

        deleted = _delete_distribution_drifts_rows(
            session,
            scan_config_id=config.id,
            keys=[(None, _BUCKET, "platform"), (event_type_id, _BUCKET, "platform")],
        )
        session.commit()

        assert deleted == 2
        remaining = session.execute(select(DistributionDrift)).scalars().all()
        assert [(row.event_type_id, row.field_name) for row in remaining] == [(None, "country")]


def test_replay_does_not_duplicate_scan_wide_distribution_drifts(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        assert config.event_type_id is not None
        config.distribution_drift_fields = ["platform"]
        config.baseline_window_buckets = 2
        config.min_history_buckets = 2
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        session.add(login_event)
        session.commit()
        config_id = str(config.id)
        event_type_id = config.event_type_id

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="platform", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name", "platform"],
                [],
                [
                    (_BUCKET, "Login", "ios", 90),
                    (_BUCKET, "Login", "android", 10),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name", "platform"],
                [],
                [
                    (datetime(2026, 1, 1, 8), "platform", "ios", False, "Login", "ios", 50),
                    (datetime(2026, 1, 1, 8), "platform", "android", False, "Login", "android", 50),
                    (datetime(2026, 1, 1, 9), "platform", "ios", False, "Login", "ios", 50),
                    (datetime(2026, 1, 1, 9), "platform", "android", False, "Login", "android", 50),
                    (_BUCKET, "platform", "ios", False, "Login", "ios", 90),
                    (_BUCKET, "platform", "android", False, "Login", "android", 10),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (_BUCKET, datetime(2026, 1, 1, 11), True),
    )
    first = metrics.collect_metrics.run(config_id)
    assert first["mode"] == "metrics_replay"
    assert first["distribution_drifts"] == 2

    second = metrics.collect_metrics.run(config_id)

    # The scan-wide row produced by the first replay must be deleted by the
    # second one, not left behind for the Distribution tab to sum twice.
    assert second["distribution_drifts_deleted"] == 2
    with sync_session_factory() as session:
        drifts = session.execute(select(DistributionDrift)).scalars().all()
        assert len(drifts) == 2
        assert {drift.event_type_id for drift in drifts} == {None, event_type_id}


# --------------------------------------------------------------------------
# a breakdown value that crossed the "Other" boundary
# --------------------------------------------------------------------------


def test_delete_event_metric_breakdown_rows_clears_every_value_of_the_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        event_id = uuid.uuid4()
        other_event_id = uuid.uuid4()

        def _row(
            *,
            scope_event_id: uuid.UUID | None,
            scope_event_type_id: uuid.UUID | None,
            column: str,
            value: str,
            is_other: bool,
        ) -> EventMetricBreakdown:
            return EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=scope_event_id,
                event_type_id=scope_event_type_id,
                bucket=_BUCKET,
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                count=1,
            )

        session.add_all(
            [
                _row(
                    scope_event_id=event_id,
                    scope_event_type_id=None,
                    column="country",
                    value="NL",
                    is_other=False,
                ),
                _row(
                    scope_event_id=event_id,
                    scope_event_type_id=None,
                    column="country",
                    value="Other",
                    is_other=True,
                ),
                _row(
                    scope_event_id=event_id,
                    scope_event_type_id=None,
                    column="device",
                    value="ios",
                    is_other=False,
                ),
                _row(
                    scope_event_id=other_event_id,
                    scope_event_type_id=None,
                    column="country",
                    value="NL",
                    is_other=False,
                ),
                _row(
                    scope_event_id=None,
                    scope_event_type_id=config.event_type_id,
                    column="country",
                    value="NL",
                    is_other=False,
                ),
            ]
        )
        session.commit()

        deleted = _delete_event_metric_breakdown_rows(
            session,
            scan_config_id=config.id,
            keys=[(event_id, _BUCKET, "country")],
            constraint="event",
        )
        session.commit()

        # Both labels of the re-derived (event, bucket, column) go, including
        # the "Other" bucket the new run may no longer produce.
        assert deleted == 2
        remaining = session.execute(select(EventMetricBreakdown)).scalars().all()
        assert {(row.event_id, row.event_type_id, row.breakdown_column) for row in remaining} == {
            (event_id, None, "device"),
            (other_event_id, None, "country"),
            (None, config.event_type_id, "country"),
        }


def test_replay_rewrites_a_breakdown_value_that_crossed_the_other_boundary(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        assert config.event_type_id is not None
        config.metric_breakdown_columns = ["country"]
        config.metric_breakdown_values_limit = 2
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        stale_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Legacy",
            description="",
            status="implemented",
        )
        session.add_all([login_event, stale_event])
        session.flush()
        # An earlier, narrower run where "NL" was still its own top-N series.
        session.add_all(
            [
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=login_event.id,
                    event_type_id=None,
                    bucket=_BUCKET,
                    breakdown_column="country",
                    breakdown_value="NL",
                    is_other=False,
                    count=40,
                ),
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=config.event_type_id,
                    bucket=_BUCKET,
                    breakdown_column="country",
                    breakdown_value="NL",
                    is_other=False,
                    count=40,
                ),
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=stale_event.id,
                    event_type_id=None,
                    bucket=_BUCKET,
                    breakdown_column="country",
                    breakdown_value="NL",
                    is_other=False,
                    count=99,
                ),
            ]
        )
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        stale_event_id = stale_event.id
        event_type_id = config.event_type_id

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="country", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name", "country"],
                [],
                [(_BUCKET, "Login", "US", 100)],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            # The wider replay chunk ranks "NL" out of the top-N, so it now
            # arrives folded into "Other" instead of under its own label.
            return (
                ["event_name", "country"],
                [],
                [
                    (_BUCKET, "country", "US", False, "Login", "US", 60),
                    (_BUCKET, "country", "Other", True, "Login", "FR", 40),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (_BUCKET, datetime(2026, 1, 1, 11), True),
    )
    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_replay"
    # Both stale "NL" rows of the re-collected scopes are cleared; the event the
    # chunk derived nothing for keeps its row.
    assert result["breakdown_metrics_deleted"] == 2

    with sync_session_factory() as session:
        event_rows = (
            session.execute(
                select(EventMetricBreakdown).where(EventMetricBreakdown.event_id == login_event_id)
            )
            .scalars()
            .all()
        )
        assert {(row.breakdown_value, row.is_other, row.count) for row in event_rows} == {
            ("US", False, 60),
            ("Other", True, 40),
        }
        event_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == login_event_id)
        ).scalar_one()
        assert sum(row.count for row in event_rows) == event_metric.count

        type_rows = (
            session.execute(
                select(EventMetricBreakdown).where(
                    EventMetricBreakdown.event_type_id == event_type_id
                )
            )
            .scalars()
            .all()
        )
        assert {(row.breakdown_value, row.is_other, row.count) for row in type_rows} == {
            ("US", False, 60),
            ("Other", True, 40),
        }

        stale_row = session.execute(
            select(EventMetricBreakdown).where(EventMetricBreakdown.event_id == stale_event_id)
        ).scalar_one()
        assert stale_row.count == 99
