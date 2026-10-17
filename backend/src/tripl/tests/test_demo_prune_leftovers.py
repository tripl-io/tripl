"""The daily demo prune reaches every row a running demo writes, not only the series.

``test_demo_v5_runtime`` pins the observed series (scan-scoped metrics and
anomalies). These pin the rest a demo accumulates per incident and per visitor
click, each older than the horizon and each the prune used to leave forever:

* catalog-metric anomalies (NULL ``scan_config_id``) and their attributions;
* bell notifications;
* the inbox's correlation states and cached incident summaries whose every
  delivery item is gone, while a decision still in force (an acknowledgement,
  an open-ended mute) stays, because dispatch enforces it with no time bound;
* signal triage that pins a pruned bucket, and long-lapsed mutes, while a mute
  in force stays;
* release chart markers, while a person's own markers stay;
* scan previews and dry runs.

A real project's rows of the same age are never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Imported first and for its side effect: the worker task package is
# import-order sensitive (see test_demo_v5_runtime).
import tripl.worker.celery_app  # noqa: F401
from tripl.models import Base
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind
from tripl.models.incident_summary import IncidentSummary
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.metric_definition import MetricDefinition
from tripl.models.notification import Notification
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_job import ScanJobStatus
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.models.signal_triage import SignalTriage
from tripl.models.user import User
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks import demo_runtime

NOW = datetime(2026, 10, 16, 3, 17, tzinfo=UTC)
CUTOFF = demo_runtime.demo_retention_cutoff(NOW)
OLD = CUTOFF - timedelta(days=2)
RECENT = CUTOFF + timedelta(days=5)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_prune_leftovers.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass
class Seeded:
    """The ids the assertions look up, by what each row stands for."""

    kept: dict[str, uuid.UUID]
    pruned: dict[str, uuid.UUID]


def _seed_project(session: Session, user: User, *, is_demo: bool) -> Seeded:
    tag = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(), name=f"P {tag}", slug=f"p-{tag}", description="", is_demo=is_demo
    )
    # Flushed on its own: no relationship tells the unit of work to insert the
    # project before the source that names it.
    session.add(project)
    session.flush()
    source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {tag}",
        db_type="demo" if is_demo else "postgres",
        host="demo",
        port=0,
        database_name="demo",
        username="",
        password_encrypted="",
        project_id=project.id if is_demo else None,
    )
    session.add(source)
    session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=source.id,
        project_id=project.id,
        name=f"Scan {tag}",
        base_query="SELECT 1",
        time_column="event_time",
        cardinality_threshold=100,
        interval="1h",
    )
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"revenue_{tag}",
        display_name="Revenue",
        kind=MetricKind.sql.value,
        config={},
        data_source_id=source.id,
        interval="1h",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=AlertDestinationType.demo_sink.value,
        name="Sink",
        enabled=True,
    )
    session.add_all([config, metric, destination])
    session.flush()
    rule = AlertRule(
        id=uuid.uuid4(), destination_id=destination.id, name="Watch", message_format="plain"
    )
    session.add(rule)
    session.flush()

    kept: dict[str, uuid.UUID] = {}
    pruned: dict[str, uuid.UUID] = {}

    def anomaly(bucket: datetime) -> MetricAnomaly:
        return MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=None,
            scope_type="metric",
            scope_ref=str(metric.id),
            bucket=bucket,
            actual_count=1,
            expected_count=10,
            stddev=1,
            z_score=-9,
            direction="drop",
        )

    old_anomaly, new_anomaly = anomaly(OLD), anomaly(RECENT)
    session.add_all([old_anomaly, new_anomaly])
    session.flush()
    old_attribution = MetricAnomalyAttribution(
        id=uuid.uuid4(), anomaly_id=old_anomaly.id, delta=-9.0, columns=[]
    )
    session.add(old_attribution)
    pruned |= {"metric anomaly": old_anomaly.id, "its attribution": old_attribution.id}
    kept["recent metric anomaly"] = new_anomaly.id

    # One incident still has a delivery inside the horizon; every other group
    # had only the old one the prune deletes.
    live_group = uuid.uuid4()
    for created, group in ((OLD, uuid.uuid4()), (RECENT, live_group)):
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel=AlertDestinationType.demo_sink.value,
            status=AlertDeliveryStatus.sent.value,
            matched_count=1,
            payload_snapshot={},
            created_at=created,
        )
        session.add(delivery)
        session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="metric",
                scope_ref=str(metric.id),
                scope_name="Revenue",
                bucket=created,
                direction="drop",
                actual_count=1,
                expected_count=10,
                absolute_delta=-9,
                percent_delta=-90,
                correlation_group_id=group,
            )
        )

    def state(group: uuid.UUID, **fields: object) -> AlertCorrelationState:
        row = AlertCorrelationState(
            id=uuid.uuid4(),
            project_id=project.id,
            correlation_group_id=group,
            created_at=OLD,
            updated_at=OLD,
            last_seen_at=OLD,
            **fields,
        )
        session.add(row)
        return row

    pruned["open incident with no items left"] = state(uuid.uuid4(), status="open").id
    pruned["mute that lapsed before the horizon"] = state(
        uuid.uuid4(), status="muted", muted_until=OLD
    ).id
    kept["acknowledged incident"] = state(uuid.uuid4(), status="acknowledged").id
    kept["mute with no end"] = state(uuid.uuid4(), status="muted").id
    kept["incident with a recent item"] = state(live_group, status="open").id

    def summary(group: uuid.UUID) -> IncidentSummary:
        row = IncidentSummary(
            id=uuid.uuid4(),
            project_id=project.id,
            correlation_group_id=group,
            facts_hash="0" * 64,
            facts=[],
            sentences=[],
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(row)
        return row

    pruned["summary of a gone incident"] = summary(uuid.uuid4()).id
    kept["summary of a listed incident"] = summary(live_group).id

    def triage(scope_suffix: str = "", **fields: object) -> SignalTriage:
        row = SignalTriage(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=None,
            scope_type="metric",
            scope_ref=f"{metric.id}{scope_suffix}",
            **fields,
        )
        session.add(row)
        return row

    pruned["acknowledgement of a pruned signal"] = triage(action="acknowledged", bucket=OLD).id
    kept["verdict on a recent signal"] = triage(action="expected", bucket=RECENT).id
    # The mutes sit on scopes of their own: one mute per scope.
    pruned["triage mute that lapsed long ago"] = triage("-a", action="muted", muted_until=OLD).id
    kept["triage mute with no end"] = triage("-b", action="muted").id

    def marker(source: str, bucket: datetime, label: str) -> ChartAnnotation:
        row = ChartAnnotation(
            id=uuid.uuid4(), project_id=project.id, bucket=bucket, label=label, source=source
        )
        session.add(row)
        return row

    pruned["old release marker"] = marker("release", OLD, "Release 6.1.0").id
    kept["recent release marker"] = marker("release", RECENT, "Release 6.2.0").id
    kept["a person's old marker"] = marker("manual", OLD, "Outage").id

    for model, label in ((ScanPreviewJob, "preview"), (ScanDryRunJob, "dry run")):
        for when, bucket in ((OLD, pruned), (RECENT, kept)):
            job = model(
                id=uuid.uuid4(),
                project_id=project.id,
                data_source_id=source.id,
                base_query="SELECT 1",
                status=ScanJobStatus.completed.value,
                created_at=when,
                updated_at=when,
                completed_at=when,
            )
            session.add(job)
            bucket[f"{'old' if when is OLD else 'recent'} {label}"] = job.id

    for when, bucket in ((OLD, pruned), (RECENT, kept)):
        note = Notification(
            id=uuid.uuid4(),
            user_id=user.id,
            project_id=project.id,
            kind="signal",
            entity_type="metric",
            entity_id=metric.id,
            title="Revenue dropped",
            url=f"/p/{project.slug}/metrics/{metric.id}",
            created_at=when,
        )
        session.add(note)
        bucket[f"{'old' if when is OLD else 'recent'} notification"] = note.id

    session.commit()
    return Seeded(kept=kept, pruned=pruned)


_MODELS: dict[str, type[Base]] = {
    "metric anomaly": MetricAnomaly,
    "recent metric anomaly": MetricAnomaly,
    "its attribution": MetricAnomalyAttribution,
    "open incident with no items left": AlertCorrelationState,
    "mute that lapsed before the horizon": AlertCorrelationState,
    "acknowledged incident": AlertCorrelationState,
    "mute with no end": AlertCorrelationState,
    "incident with a recent item": AlertCorrelationState,
    "summary of a gone incident": IncidentSummary,
    "summary of a listed incident": IncidentSummary,
    "acknowledgement of a pruned signal": SignalTriage,
    "verdict on a recent signal": SignalTriage,
    "triage mute that lapsed long ago": SignalTriage,
    "triage mute with no end": SignalTriage,
    "old release marker": ChartAnnotation,
    "recent release marker": ChartAnnotation,
    "a person's old marker": ChartAnnotation,
    "old preview": ScanPreviewJob,
    "recent preview": ScanPreviewJob,
    "old dry run": ScanDryRunJob,
    "recent dry run": ScanDryRunJob,
    "old notification": Notification,
    "recent notification": Notification,
}


def _present(session: Session, rows: dict[str, uuid.UUID]) -> set[str]:
    # An id read before its flush is None, and a None id is "absent" for free.
    assert all(row_id is not None for row_id in rows.values())
    return {
        name
        for name, row_id in rows.items()
        if session.execute(
            select(_MODELS[name].id).where(_MODELS[name].id == row_id)  # type: ignore[attr-defined]
        ).first()
    }


def test_the_prune_reaches_every_row_a_demo_accumulates(
    factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    with factory() as session:
        user = User(id=uuid.uuid4(), email="visitor@example.com", password_hash="!seed")
        session.add(user)
        session.commit()
        demo = _seed_project(session, user, is_demo=True)
        real = _seed_project(session, user, is_demo=False)

    monkeypatch.setattr(demo_runtime, "_get_sync_session", factory)
    result = demo_runtime.prune_demo_history.run(now=NOW)
    assert result == {"pruned": 1, "failed": 0, "cutoff": CUTOFF.isoformat()}

    with factory() as session:
        assert _present(session, demo.pruned) == set(), "older than the horizon, left behind"
        assert _present(session, demo.kept) == set(demo.kept), "still meant to apply, deleted"
        # A real project of the same age is never pruned, by this sweep at least.
        assert _present(session, real.pruned) == set(real.pruned)
        assert _present(session, real.kept) == set(real.kept)
