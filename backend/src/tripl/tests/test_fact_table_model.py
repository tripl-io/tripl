"""Model-level tests for the FactTable catalog entity."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.domain_enums import MetricKind
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'fact_table.db'}")
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
        name="Fact Project",
        slug=f"facts-{uuid.uuid4().hex[:8]}",
        description="",
    )
    session.add(project)
    session.flush()
    return project


def test_fact_table_roundtrips(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project = _project(session)
        fact_table = FactTable(
            project_id=project.id,
            name="orders",
            display_name="Orders",
            sql="SELECT id, user_id, amount, created_at FROM orders",
            timestamp_column="created_at",
            columns=[
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
            ],
            identifier_columns=["user_id"],
            row_filters=[{"name": "paid", "sql": "status = 'paid'"}],
        )
        session.add(fact_table)
        session.commit()
        fact_table_id = fact_table.id

    with session_factory() as session:
        loaded = session.get(FactTable, fact_table_id)
        assert loaded is not None
        assert loaded.sql.startswith("SELECT id, user_id")
        assert loaded.timestamp_column == "created_at"
        assert loaded.columns == [
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ]
        assert loaded.identifier_columns == ["user_id"]
        assert loaded.row_filters == [{"name": "paid", "sql": "status = 'paid'"}]
        # Defaults applied.
        assert loaded.color == "#6366f1"
        assert loaded.order == 0
        assert loaded.description == ""
        assert loaded.data_source_id is None


def test_fact_table_json_defaults_empty(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project = _project(session)
        fact_table = FactTable(
            project_id=project.id,
            name="sessions",
            display_name="Sessions",
            sql="SELECT id, ts FROM sessions",
            timestamp_column="ts",
        )
        session.add(fact_table)
        session.commit()
        fact_table_id = fact_table.id

    with session_factory() as session:
        loaded = session.get(FactTable, fact_table_id)
        assert loaded is not None
        assert loaded.columns == []
        assert loaded.identifier_columns == []
        assert loaded.row_filters == []


def test_name_unique_per_project(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        project = _project(session)
        session.add(
            FactTable(
                project_id=project.id,
                name="dup",
                display_name="Dup",
                sql="SELECT 1 AS one",
                timestamp_column="ts",
            )
        )
        session.commit()
        session.add(
            FactTable(
                project_id=project.id,
                name="dup",
                display_name="Dup 2",
                sql="SELECT 2 AS two",
                timestamp_column="ts",
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
                FactTable(
                    project_id=project_a.id,
                    name="shared",
                    display_name="Shared A",
                    sql="SELECT 1 AS one",
                    timestamp_column="ts",
                ),
                FactTable(
                    project_id=project_b.id,
                    name="shared",
                    display_name="Shared B",
                    sql="SELECT 1 AS one",
                    timestamp_column="ts",
                ),
            ]
        )
        session.commit()


def test_metric_definition_fact_table_fk_roundtrips(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        project = _project(session)
        fact_table = FactTable(
            project_id=project.id,
            name="orders",
            display_name="Orders",
            sql="SELECT id, ts FROM orders",
            timestamp_column="ts",
        )
        session.add(fact_table)
        session.flush()
        metric = MetricDefinition(
            project_id=project.id,
            name="orders_count",
            display_name="Orders count",
            kind=MetricKind.fact,
            fact_table_id=fact_table.id,
        )
        session.add(metric)
        session.commit()
        metric_id = metric.id
        fact_table_id = fact_table.id

    with session_factory() as session:
        loaded = session.get(MetricDefinition, metric_id)
        assert loaded is not None
        assert loaded.kind == MetricKind.fact
        assert loaded.fact_table_id == fact_table_id
