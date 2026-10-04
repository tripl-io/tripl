"""Model-level tests for the MetricDefinition catalog entity."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metric_def.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _project(session: Session) -> Project:
    project = Project(
        id=uuid.uuid4(),
        name="Metrics Project",
        slug=f"metrics-{uuid.uuid4().hex[:8]}",
        description="",
    )
    session.add(project)
    session.flush()
    return project


def test_fact_metric_roundtrips(session_factory: sessionmaker[Session]) -> None:
    fact_table_id = uuid.uuid4()
    with session_factory() as session:
        project = _project(session)
        metric = MetricDefinition(
            project_id=project.id,
            name="signups_per_day",
            display_name="Signups / day",
            kind=MetricKind.fact,
            composition=MetricComposition.single,
            aggregation=MetricAggregation.count_distinct,
            fact_table_id=fact_table_id,
            unit="users",
            data_source_id=None,
            interval=ScanInterval.d1,
            breakdown_columns=["country", "plan"],
            breakdown_values_limit=10,
            app_version_column="app_version",
            platform_column="platform",
            config={
                "measure_column": None,
                "distinct_column": "user_id",
                "row_filter": "exclude_test",
            },
        )
        session.add(metric)
        session.commit()
        metric_id = metric.id

    with session_factory() as session:
        loaded = session.get(MetricDefinition, metric_id)
        assert loaded is not None
        # Defaults applied.
        assert loaded.status == MetricStatus.draft
        assert loaded.reviewed is False
        assert loaded.anomaly_detection_enabled is True
        assert loaded.color == "#6366f1"
        assert loaded.order == 0
        # Kind-specific fields.
        assert loaded.kind == MetricKind.fact
        assert loaded.composition == MetricComposition.single
        assert loaded.aggregation == MetricAggregation.count_distinct
        assert loaded.fact_table_id == fact_table_id
        assert loaded.breakdown_columns == ["country", "plan"]
        assert loaded.config["distinct_column"] == "user_id"
        assert loaded.config["row_filter"] == "exclude_test"


def test_sql_metric_roundtrips(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project = _project(session)
        metric = MetricDefinition(
            project_id=project.id,
            name="revenue",
            display_name="Revenue",
            kind=MetricKind.sql,
            unit="$",
            interval=ScanInterval.h1,
            config={
                "metric_sql": "SELECT sum(amount) AS value FROM orders",
                "time_column": "created_at",
            },
        )
        session.add(metric)
        session.commit()
        metric_id = metric.id

    with session_factory() as session:
        loaded = session.get(MetricDefinition, metric_id)
        assert loaded is not None
        assert loaded.kind == MetricKind.sql
        assert loaded.aggregation is None
        assert loaded.config["metric_sql"].startswith("SELECT sum(amount)")


def test_event_composition_metric_roundtrips(session_factory: sessionmaker[Session]) -> None:
    num_event = uuid.uuid4()
    den_event = uuid.uuid4()
    with session_factory() as session:
        project = _project(session)
        metric = MetricDefinition(
            project_id=project.id,
            name="purchase_per_view",
            display_name="Purchase / view",
            kind=MetricKind.event_composition,
            composition=MetricComposition.ratio,
            numerator_event_id=num_event,
            denominator_event_id=den_event,
        )
        session.add(metric)
        session.commit()
        metric_id = metric.id

    with session_factory() as session:
        loaded = session.get(MetricDefinition, metric_id)
        assert loaded is not None
        assert loaded.kind == MetricKind.event_composition
        assert loaded.composition == MetricComposition.ratio
        assert loaded.numerator_event_id == num_event
        assert loaded.denominator_event_id == den_event
        assert loaded.data_source_id is None
        # Empty JSON config default.
        assert loaded.config == {}
        assert loaded.breakdown_columns == []


def test_name_unique_per_project(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project = _project(session)
        session.add(
            MetricDefinition(
                project_id=project.id,
                name="dup",
                display_name="Dup",
                kind=MetricKind.sql,
            )
        )
        session.commit()
        session.add(
            MetricDefinition(
                project_id=project.id,
                name="dup",
                display_name="Dup 2",
                kind=MetricKind.sql,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_same_name_allowed_across_projects(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project_a = _project(session)
        project_b = _project(session)
        session.add_all(
            [
                MetricDefinition(
                    project_id=project_a.id,
                    name="shared",
                    display_name="Shared A",
                    kind=MetricKind.sql,
                ),
                MetricDefinition(
                    project_id=project_b.id,
                    name="shared",
                    display_name="Shared B",
                    kind=MetricKind.sql,
                ),
            ]
        )
        session.commit()
        rows = (
            session.execute(select(MetricDefinition).where(MetricDefinition.name == "shared"))
            .scalars()
            .all()
        )
        assert len(rows) == 2
