"""The catalog-metric participation predicate has exactly ONE meaning.

``tripl.metric_monitoring`` publishes the rule in two encodings — SQL criteria
for the four sites that build a statement, and a Python test for the two that
hold loaded ORM rows. Two encodings of one rule is exactly the shape that drifted
in the first place (detection required ``status`` AND the flag; all six consumers
required only the flag), so both are pinned here against the same
population, across the whole status x flag matrix.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.metric_monitoring import is_metric_monitored, monitored_metric_criteria
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricAggregation, MetricKind, MetricStatus
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project

# Every combination a metric can actually be in. ``draft`` and ``archived`` are
# BOTH "not collected" (``schedule.check_metric_definitions_due`` dispatches only
# ``active``), so they must answer the same way.
_MATRIX = [
    (status, flag)
    for status in MetricStatus
    for flag in (True, False)  # 3 x 2
]


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metric_monitoring.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_matrix(session: Session) -> tuple[uuid.UUID, dict[tuple[MetricStatus, bool], uuid.UUID]]:
    """One metric per (status, flag) pair; returns the project id and the index."""
    project = Project(
        id=uuid.uuid4(),
        name="Monitoring Predicate",
        slug=f"monitoring-predicate-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])

    by_case: dict[tuple[MetricStatus, bool], uuid.UUID] = {}
    for status, flag in _MATRIX:
        metric_id = uuid.uuid4()
        by_case[(status, flag)] = metric_id
        session.add(
            MetricDefinition(
                id=metric_id,
                project_id=project.id,
                name=f"{status.value}_{'on' if flag else 'off'}",
                display_name=f"{status.value} / {flag}",
                kind=MetricKind.fact.value,
                aggregation=MetricAggregation.count.value,
                config={},
                data_source_id=data_source.id,
                interval="1h",
                status=status.value,
                anomaly_detection_enabled=flag,
            )
        )
    session.commit()
    return project.id, by_case


def test_the_predicate_is_active_and_enabled(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The rule itself: BOTH halves, and ``draft`` is as off as ``archived``.

    The conjunction is authoritative because detection cannot be widened —
    a metric that has stopped collecting is zero-filled when it is count-shaped,
    so scoring one would manufacture a false outage anomaly on every metric
    parked in ``draft``.
    """
    with sync_session_factory() as session:
        project_id, by_case = _seed_matrix(session)

        selected = set(
            session.execute(
                select(MetricDefinition.id).where(
                    MetricDefinition.project_id == project_id,
                    *monitored_metric_criteria(),
                )
            ).scalars()
        )

    assert selected == {by_case[(MetricStatus.active, True)]}


def test_both_encodings_of_the_predicate_select_the_same_metrics(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """SQL criteria and the Python test must never disagree on any case.

    Four sites push ``monitored_metric_criteria()`` into a statement and two
    apply ``is_metric_monitored`` to rows they already hold. If those two ever
    part company, the catalog list and the drilldown start contradicting the
    Anomalies page and the badge again — which is the class of bug this module
    was created to end.
    """
    with sync_session_factory() as session:
        project_id, _ = _seed_matrix(session)

        by_sql = set(
            session.execute(
                select(MetricDefinition.id).where(
                    MetricDefinition.project_id == project_id,
                    *monitored_metric_criteria(),
                )
            ).scalars()
        )
        rows = list(
            session.execute(
                select(MetricDefinition).where(MetricDefinition.project_id == project_id)
            ).scalars()
        )
        by_python = {metric.id for metric in rows if is_metric_monitored(metric)}

    assert len(rows) == len(_MATRIX)  # the matrix really was exercised
    assert by_python == by_sql


def test_is_metric_monitored_reads_a_status_held_as_a_plain_string(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``status`` reaches the Python half as either the member or its value.

    ``MetricDefinition.status`` is annotated ``Mapped[str]`` over a ``StrEnum``
    column, and callers assign it both ways (``metric_definition_service`` even
    normalises defensively). A predicate that compared with ``is`` or against
    ``MetricStatus`` identity would silently report an active metric unmonitored
    on whichever path hands it the raw string.
    """
    with sync_session_factory() as session:
        _, by_case = _seed_matrix(session)
        metric = session.get(MetricDefinition, by_case[(MetricStatus.active, True)])
        assert metric is not None

        metric.status = MetricStatus.active.value  # plain str
        assert is_metric_monitored(metric) is True

        metric.status = MetricStatus.active  # enum member
        assert is_metric_monitored(metric) is True

        metric.status = MetricStatus.archived.value
        assert is_metric_monitored(metric) is False
