"""Model-level tests for MetricValue / MetricValueBreakdown storage."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics.metric_rows import (
    _delete_metric_value_breakdowns_window,
    _delete_metric_values_window,
    _upsert_metric_value_breakdown_rows,
    _upsert_metric_values_rows,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metric_value.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@dataclass(frozen=True)
class _Fixture:
    metric_id: uuid.UUID
    scan_config_id: uuid.UUID


def _seed(session: Session) -> _Fixture:
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
    session.add_all([project, data_source])
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Structured Events",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="revenue",
        display_name="Revenue",
        kind=MetricKind.sql,
    )
    session.add_all([scan_config, metric])
    session.flush()
    return _Fixture(metric_id=metric.id, scan_config_id=scan_config.id)


def test_metric_value_roundtrips(session_factory: sessionmaker[Session]) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        # One row bound to a scan grid, one unscoped (sql/fact).
        session.add_all(
            [
                MetricValue(
                    metric_definition_id=ctx.metric_id,
                    scan_config_id=ctx.scan_config_id,
                    bucket=bucket,
                    value=12.5,
                ),
                MetricValue(
                    metric_definition_id=ctx.metric_id,
                    scan_config_id=None,
                    bucket=bucket,
                    value=7.0,
                ),
            ]
        )
        session.commit()

    with session_factory() as session:
        rows = session.execute(select(MetricValue).order_by(MetricValue.value)).scalars().all()
        assert [row.value for row in rows] == [7.0, 12.5]
        scoped = next(row for row in rows if row.scan_config_id is not None)
        assert scoped.value == 12.5
        assert scoped.created_at is not None
        unscoped = next(row for row in rows if row.scan_config_id is None)
        assert unscoped.scan_config_id is None


def test_metric_value_breakdown_roundtrips(session_factory: sessionmaker[Session]) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.add(
            MetricValueBreakdown(
                metric_definition_id=ctx.metric_id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="US",
                value=3.5,
            )
        )
        session.commit()
        metric_id = ctx.metric_id

    with session_factory() as session:
        loaded = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == metric_id
                )
            )
            .scalars()
            .one()
        )
        assert loaded.breakdown_column == "country"
        assert loaded.breakdown_value == "US"
        assert loaded.value == 3.5
        # Default applied.
        assert loaded.is_other is False
        assert loaded.created_at is not None


def test_metric_value_unique_constraint(session_factory: sessionmaker[Session]) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.add(
            MetricValue(
                metric_definition_id=ctx.metric_id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                value=1.0,
            )
        )
        session.commit()
        session.add(
            MetricValue(
                metric_definition_id=ctx.metric_id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                value=2.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_metric_value_breakdown_unique_constraint(
    session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.add(
            MetricValueBreakdown(
                metric_definition_id=ctx.metric_id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="US",
                value=1.0,
            )
        )
        session.commit()
        session.add(
            MetricValueBreakdown(
                metric_definition_id=ctx.metric_id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="US",
                value=2.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_upsert_metric_values_is_idempotent(session_factory: sessionmaker[Session]) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.commit()

        _upsert_metric_values_rows(
            session,
            rows=[
                {
                    "id": uuid.uuid4(),
                    "metric_definition_id": ctx.metric_id,
                    "scan_config_id": ctx.scan_config_id,
                    "bucket": bucket,
                    "value": 1.5,
                }
            ],
        )
        session.commit()

        # Same key, new id and value -> UPDATE in place, not a second row.
        _upsert_metric_values_rows(
            session,
            rows=[
                {
                    "id": uuid.uuid4(),
                    "metric_definition_id": ctx.metric_id,
                    "scan_config_id": ctx.scan_config_id,
                    "bucket": bucket,
                    "value": 9.25,
                }
            ],
        )
        session.commit()

        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == ctx.metric_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].value == 9.25


def test_upsert_unscoped_metric_values_uses_partial_unique_index(
    session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.commit()
        base_row = {
            "metric_definition_id": ctx.metric_id,
            "scan_config_id": None,
            "bucket": bucket,
        }

        _upsert_metric_values_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 1.5}],
        )
        _upsert_metric_values_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 9.25}],
        )
        session.commit()

        rows = session.scalars(
            select(MetricValue).where(MetricValue.metric_definition_id == ctx.metric_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].value == 9.25


def test_upsert_metric_value_breakdown_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.commit()

        base_row = {
            "metric_definition_id": ctx.metric_id,
            "scan_config_id": ctx.scan_config_id,
            "bucket": bucket,
            "breakdown_column": "country",
            "breakdown_value": "US",
            "is_other": False,
        }
        _upsert_metric_value_breakdown_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 2.0}],
        )
        session.commit()

        _upsert_metric_value_breakdown_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 5.0}],
        )
        session.commit()

        rows = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == ctx.metric_id
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].value == 5.0


def test_upsert_unscoped_breakdown_uses_partial_unique_index(
    session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.commit()
        base_row = {
            "metric_definition_id": ctx.metric_id,
            "scan_config_id": None,
            "bucket": bucket,
            "breakdown_column": "country",
            "breakdown_value": "US",
            "is_other": False,
        }

        _upsert_metric_value_breakdown_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 2.0}],
        )
        _upsert_metric_value_breakdown_rows(
            session,
            rows=[{"id": uuid.uuid4(), **base_row, "value": 5.0}],
        )
        session.commit()

        rows = session.scalars(
            select(MetricValueBreakdown).where(
                MetricValueBreakdown.metric_definition_id == ctx.metric_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].value == 5.0


def test_unscoped_breakdown_preserves_literal_and_folded_other_rows(
    session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with session_factory() as session:
        ctx = _seed(session)
        session.commit()
        common = {
            "metric_definition_id": ctx.metric_id,
            "scan_config_id": None,
            "bucket": bucket,
            "breakdown_column": "country",
            "breakdown_value": "Other",
        }

        _upsert_metric_value_breakdown_rows(
            session,
            rows=[
                {"id": uuid.uuid4(), **common, "is_other": False, "value": 2.0},
                {"id": uuid.uuid4(), **common, "is_other": True, "value": 5.0},
            ],
        )
        session.commit()

        rows = session.scalars(
            select(MetricValueBreakdown).where(
                MetricValueBreakdown.metric_definition_id == ctx.metric_id
            )
        ).all()
        assert {(row.is_other, row.value) for row in rows} == {(False, 2.0), (True, 5.0)}


def test_delete_metric_values_window(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        ctx = _seed(session)
        for hour in range(4):
            session.add(
                MetricValue(
                    metric_definition_id=ctx.metric_id,
                    scan_config_id=ctx.scan_config_id,
                    bucket=datetime(2026, 1, 1, hour, tzinfo=UTC),
                    value=float(hour),
                )
            )
        session.commit()

        deleted = _delete_metric_values_window(
            session,
            metric_definition_id=ctx.metric_id,
            time_from=datetime(2026, 1, 1, 1, tzinfo=UTC),
            time_to=datetime(2026, 1, 1, 3, tzinfo=UTC),
            scan_config_id=ctx.scan_config_id,
        )
        session.commit()
        assert deleted == 2

        remaining = (
            session.execute(
                select(MetricValue.bucket).where(MetricValue.metric_definition_id == ctx.metric_id)
            )
            .scalars()
            .all()
        )
        assert {row.hour for row in remaining} == {0, 3}


def test_delete_metric_value_breakdowns_window(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        ctx = _seed(session)
        for hour in range(3):
            session.add(
                MetricValueBreakdown(
                    metric_definition_id=ctx.metric_id,
                    scan_config_id=ctx.scan_config_id,
                    bucket=datetime(2026, 1, 1, hour, tzinfo=UTC),
                    breakdown_column="country",
                    breakdown_value="US",
                    value=float(hour),
                )
            )
        session.commit()

        deleted = _delete_metric_value_breakdowns_window(
            session,
            metric_definition_id=ctx.metric_id,
            time_from=datetime(2026, 1, 1, 0, tzinfo=UTC),
            time_to=datetime(2026, 1, 1, 2, tzinfo=UTC),
        )
        session.commit()
        assert deleted == 2

        remaining = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == ctx.metric_id
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].bucket.hour == 2
