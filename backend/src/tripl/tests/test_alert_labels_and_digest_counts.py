"""Reader-facing labels and the weekly digest's persisted count lines."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import tripl.worker.celery_app  # noqa: F401
from tripl.alert_templates import alert_scope_label
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import MetricScopeType
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.worker.tasks.alerts_messages import _build_plan_digest_message

NOW = datetime(2026, 9, 14, 9, tzinfo=UTC)


@pytest.fixture
def digest_rows():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(id=uuid.uuid4(), name="Checkout", slug="checkout", description="")
        other = Project(id=uuid.uuid4(), name="Other", slug="other", description="")
        session.add_all([project, other])
        session.flush()
        main = PlanBranch(
            id=uuid.uuid4(),
            project_id=project.id,
            name="main",
            kind=BranchKind.main.value,
            status=BranchStatus.merged.value,
        )
        working = PlanBranch(
            id=uuid.uuid4(),
            project_id=project.id,
            name="draft",
            kind=BranchKind.working.value,
            status=BranchStatus.draft.value,
        )
        other_main = PlanBranch(
            id=uuid.uuid4(),
            project_id=other.id,
            name="main",
            kind=BranchKind.main.value,
            status=BranchStatus.merged.value,
        )
        session.add_all([main, working, other_main])
        session.flush()
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=main.id,
            name="checkout",
            display_name="Checkout",
            description="",
        )
        other_type = EventType(
            id=uuid.uuid4(),
            project_id=other.id,
            branch_id=other_main.id,
            name="other",
            display_name="Other",
            description="",
        )
        session.add_all([event_type, other_type])
        source = DataSource(
            id=uuid.uuid4(),
            name="warehouse",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(source)
        session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            project_id=project.id,
            data_source_id=source.id,
            name="scan",
            base_query="SELECT 1",
        )
        session.add(scan)
        session.flush()
        yield session, project, other_type, event_type, scan, main, working
    Base.metadata.drop_all(engine)
    engine.dispose()


def _message(session: Session, project: Project) -> str:
    return _build_plan_digest_message(session, project=project, now=NOW)


@pytest.mark.parametrize(
    ("scope", "label"),
    [
        (MetricScopeType.metric.value, "Metric"),
        (MetricScopeType.variable_value_drift.value, "Variable value drift"),
    ],
)
def test_reader_facing_scope_labels(scope: str, label: str) -> None:
    assert alert_scope_label(scope) == label


def test_weekly_schema_drift_count_honours_project_window_and_status(digest_rows) -> None:
    session, project, other_type, event_type, scan, *_ = digest_rows
    for event_type_id, age, status in (
        (event_type.id, 1, "open"),
        (event_type.id, 2, "snoozed"),
        (event_type.id, 8, "open"),
        (other_type.id, 1, "open"),
    ):
        session.add(
            SchemaDrift(
                id=uuid.uuid4(),
                event_type_id=event_type_id,
                scan_config_id=scan.id if event_type_id == event_type.id else None,
                field_name=uuid.uuid4().hex,
                drift_type="type_changed",
                status=status,
                detected_at=NOW - timedelta(days=age),
                snoozed_until=NOW + timedelta(days=1) if status == "snoozed" else None,
            )
        )
    session.flush()
    assert "- Active schema drifts: 1" in _message(session, project)


def test_weekly_distribution_count_requires_significant_band_and_window(digest_rows) -> None:
    session, project, _, _, scan, *_ = digest_rows
    for age, band in ((1, "significant"), (2, "minor"), (8, "significant")):
        session.add(
            DistributionDrift(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                field_name=uuid.uuid4().hex,
                bucket=NOW - timedelta(days=age),
                psi=0.4,
                band=band,
                baseline_total=100,
                current_total=100,
                top_movers=[],
            )
        )
    session.flush()
    assert "- Significant distribution drifts: 1" in _message(session, project)


def test_weekly_live_coverage_counts_only_main_non_archived_events(digest_rows) -> None:
    session, project, _, event_type, _, main, working = digest_rows
    for index, (branch, status, seen) in enumerate(
        (
            (main, EventStatus.live, NOW),
            (main, EventStatus.draft, None),
            (main, EventStatus.archived, NOW),
            (working, EventStatus.live, NOW),
        )
    ):
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project.id,
                branch_id=branch.id,
                event_type_id=event_type.id,
                name=f"event-{index}",
                status=status,
                last_seen_at=seen,
                description="",
                order=index,
            )
        )
    session.flush()
    assert "- Live coverage: 1/2 events (50.0%)" in _message(session, project)


def test_weekly_dead_count_uses_status_and_thirty_day_cutoff(digest_rows) -> None:
    session, project, _, event_type, _, main, _ = digest_rows
    for index, (status, seen) in enumerate(
        (
            (EventStatus.implemented, None),
            (EventStatus.live, NOW - timedelta(days=31)),
            (EventStatus.live, NOW - timedelta(days=1)),
            (EventStatus.draft, None),
        )
    ):
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project.id,
                branch_id=main.id,
                event_type_id=event_type.id,
                name=f"dead-{index}",
                status=status,
                last_seen_at=seen,
                description="",
                order=index,
            )
        )
    session.flush()
    assert "- Dead implemented events: 2" in _message(session, project)
