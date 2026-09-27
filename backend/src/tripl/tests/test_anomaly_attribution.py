"""Stored "Why did it change?" attributions (F02, #255).

The worker half: the pass stores one row per volume anomaly with the split and
the release context, keeps the app-version column out of the ranking, upserts on
a re-run, loses the row with its anomaly (cascade) and recomputes it after a
replay re-scores the bucket, and stores nothing for a scan with no breakdown
column. The API half: the lazy route, the signals list and the drilldown's
``latest_signal`` carry the attribution and its status, and the membership gate
404s outsiders.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.api.deps import get_current_user
from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.anomaly_detector import DetectedAnomaly, SeriesPoint
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.main import app
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import UserRole
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.user import User
from tripl.tests._members import PASSWORD_HASH_PLACEHOLDER
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_metrics_tasks import (
    _ANOMALY_BASE,
    _seed_alert_rule,
    _seed_anomaly_scan_state,
)
from tripl.worker.tasks import demo_runtime
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.tasks.metrics.attribution import (
    recompute_anomaly_attributions,
    scan_breakdown_columns,
    stored_attribution_payload,
)

# ── worker ───────────────────────────────────────────────────────────────────

_BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_FLAGGED = _BASE + timedelta(hours=4)
_WINDOW = (_BASE, _BASE + timedelta(hours=5))


@pytest.fixture
def sync_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_scan(
    session: Session,
    *,
    platform_column: str | None = "platform",
    app_version_column: str | None = "app_version",
) -> tuple[ScanConfig, Event]:
    project = Project(
        id=uuid.uuid4(), name="Why", slug=f"why-{uuid.uuid4().hex[:8]}", description=""
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
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="page_view",
        display_name="Page View",
        description="",
    )
    session.add_all([project, data_source, event_type])
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        event_type_id=event_type.id,
        name="Events",
        base_query="SELECT 1",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
        platform_column=platform_column,
        app_version_column=app_version_column,
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="Landing Viewed",
        description="",
        status="implemented",
    )
    session.add_all([config, event])
    session.add(
        ProjectAnomalySettings(
            project_id=project.id,
            anomaly_detection_enabled=True,
            baseline_window_buckets=4,
        )
    )
    session.flush()
    return config, event


def _seed_series(
    session: Session,
    config: ScanConfig,
    event: Event,
    *,
    flagged_split: dict[str, int],
) -> None:
    """Four baseline hours at 1000 (ios 600 / android 400), then the flagged hour."""
    for hour in range(5):
        bucket = _BASE + timedelta(hours=hour)
        split = flagged_split if bucket == _FLAGGED else {"ios": 600, "android": 400}
        session.add(
            EventMetric(
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=None,
                bucket=bucket,
                count=sum(split.values()),
            )
        )
        for value, count in split.items():
            session.add(
                EventMetricBreakdown(
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="platform",
                    breakdown_value=value,
                    is_other=False,
                    count=count,
                )
            )
    session.flush()


def _seed_rollout(session: Session, config: ScanConfig, event: Event) -> None:
    """4.12 absent for two hours, then 30% / 40% / 50% of traffic: it activates
    two hours before the flagged bucket."""
    new_counts = {0: 0, 1: 0, 2: 300, 3: 400, 4: 250}
    for hour, new in new_counts.items():
        bucket = _BASE + timedelta(hours=hour)
        total = 500 if bucket == _FLAGGED else 1000
        for version, count in (("4.11", total - new), ("4.12", new)):
            if count <= 0:
                continue
            session.add(
                EventMetricBreakdown(
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=False,
                    count=count,
                )
            )
    session.flush()


def _add_anomaly(session: Session, config: ScanConfig, event: Event) -> MetricAnomaly:
    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=config.id,
        scope_type="event",
        scope_ref=str(event.id),
        event_id=event.id,
        event_type_id=None,
        bucket=_FLAGGED,
        actual_count=500,
        expected_count=1000,
        stddev=50,
        z_score=-10,
        direction="drop",
    )
    session.add(anomaly)
    session.flush()
    return anomaly


def _rows(session: Session) -> list[MetricAnomalyAttribution]:
    return list(session.execute(select(MetricAnomalyAttribution)).scalars())


def test_worker_stores_the_split_and_the_release(sync_session: Session) -> None:
    config, event = _seed_scan(sync_session)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    _seed_rollout(sync_session, config, event)
    anomaly = _add_anomaly(sync_session, config, event)

    written = recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )

    assert written == 1
    (row,) = _rows(sync_session)
    assert row.anomaly_id == anomaly.id
    assert row.delta == pytest.approx(-500)
    # The app-version column feeds the release context only, never the ranking.
    assert [column["column"] for column in row.columns] == ["platform"]
    platform = row.columns[0]
    assert [value["value"] for value in platform["values"]] == ["ios", "android"]
    ios = platform["values"][0]
    assert ios["expected"] == pytest.approx(600)
    assert ios["actual"] == pytest.approx(120)
    assert ios["delta"] == pytest.approx(-480)
    assert platform["explained_share"] == pytest.approx(1.0)
    assert sum(value["delta"] for value in platform["values"]) == pytest.approx(-500)
    assert row.release is not None
    assert row.release["version"] == "4.12"
    assert row.release["previous_version"] == "4.11"
    assert row.release["share"] == pytest.approx(0.5)
    assert datetime.fromisoformat(row.release["reached_at"]) == _BASE + timedelta(hours=2)

    stored = stored_attribution_payload(
        sync_session,
        scan_config_id=config.id,
        scope_type="event",
        scope_ref=str(event.id),
        bucket=_FLAGGED,
    )
    assert stored is not None and stored["delta"] == pytest.approx(-500)


def test_rerun_upserts_and_replay_recomputes(sync_session: Session) -> None:
    config, event = _seed_scan(sync_session, app_version_column=None)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    anomaly = _add_anomaly(sync_session, config, event)
    recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )
    # A second run over the same rows updates in place rather than tripping
    # the unique anomaly_id.
    recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )
    assert len(_rows(sync_session)) == 1

    # A replay re-scores the bucket: the anomaly row is replaced, and its
    # attribution goes with it (ON DELETE CASCADE)...
    sync_session.execute(delete(MetricAnomaly).where(MetricAnomaly.id == anomaly.id))
    sync_session.flush()
    assert _rows(sync_session) == []
    # ...over re-collected data where android, not ios, carries the drop.
    sync_session.execute(
        delete(EventMetricBreakdown).where(EventMetricBreakdown.bucket == _FLAGGED)
    )
    for value, count in (("ios", 480), ("android", 20)):
        sync_session.add(
            EventMetricBreakdown(
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=None,
                bucket=_FLAGGED,
                breakdown_column="platform",
                breakdown_value=value,
                is_other=False,
                count=count,
            )
        )
    replacement = _add_anomaly(sync_session, config, event)

    # Through the collection's own entry point (the savepoint wrapper).
    written = metrics_tasks._recompute_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )

    assert written == 1
    (row,) = _rows(sync_session)
    assert row.anomaly_id == replacement.id
    assert row.release is None
    top = row.columns[0]["values"][0]
    assert (top["value"], top["delta"]) == ("android", pytest.approx(-380))


def test_no_breakdown_columns_stores_nothing(sync_session: Session) -> None:
    config, event = _seed_scan(sync_session, platform_column=None)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    _seed_rollout(sync_session, config, event)
    _add_anomaly(sync_session, config, event)

    # Only the app-version column: it is not a column to split by.
    assert scan_breakdown_columns(sync_session, config) == set()
    written = recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )
    assert written == 0
    assert _rows(sync_session) == []


def test_scope_without_breakdown_series_stores_nothing(sync_session: Session) -> None:
    config, event = _seed_scan(sync_session, app_version_column=None)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    # A project-total anomaly: the scan has only event-level breakdown rows.
    sync_session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type="project_total",
            scope_ref=str(config.id),
            event_id=None,
            event_type_id=None,
            bucket=_FLAGGED,
            actual_count=500,
            expected_count=1000,
            stddev=50,
            z_score=-10,
            direction="drop",
        )
    )
    sync_session.flush()
    written = recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )
    assert written == 0
    assert _rows(sync_session) == []


def test_values_seen_in_under_half_the_baseline_fold_into_other(sync_session: Session) -> None:
    config, event = _seed_scan(sync_session, app_version_column=None)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    # "web" shows up in one baseline hour of four, then carries the flagged hour.
    for bucket, count in ((_BASE, 50), (_FLAGGED, 400)):
        sync_session.add(
            EventMetricBreakdown(
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=None,
                bucket=bucket,
                breakdown_column="platform",
                breakdown_value="web",
                is_other=False,
                count=count,
            )
        )
    sync_session.flush()
    _add_anomaly(sync_session, config, event)

    recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )

    (row,) = _rows(sync_session)
    named = [value["value"] for value in row.columns[0]["values"]]
    assert "web" not in named
    assert "Other" not in named
    assert named[0] == "ios"


# ── demo runtime tick ────────────────────────────────────────────────────────


def test_a_demo_tick_re_attributes_the_anomalies_it_replaced(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed -> tick -> the Why rows are still there.

    The tick deletes and re-inserts the window's anomaly rows, so their
    attributions go with them (ON DELETE CASCADE); ``_recompute_anomalies``
    must re-attribute the new rows or the demo's Why panel empties after the
    first tick."""
    config, event = _seed_scan(sync_session, app_version_column=None)
    _seed_series(sync_session, config, event, flagged_split={"ios": 120, "android": 380})
    seeded = _add_anomaly(sync_session, config, event)
    # Seed: the demo seeder runs the worker's own pass.
    recompute_anomaly_attributions(
        sync_session, config, evaluation_start=_WINDOW[0], evaluation_end=_WINDOW[1]
    )
    assert [row.anomaly_id for row in _rows(sync_session)] == [seeded.id]

    def flag_the_same_bucket(points: list[SeriesPoint], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            anomalies=[
                DetectedAnomaly(
                    bucket=_FLAGGED,
                    actual_count=500,
                    expected_count=1000,
                    stddev=50,
                    z_score=-10,
                    direction="drop",
                    effective_stddev=50,
                )
                for point in points
                if point.bucket == _FLAGGED
            ]
        )

    monkeypatch.setattr(demo_runtime, "detect_anomalies", flag_the_same_bucket)
    # Tick.
    demo_runtime._recompute_anomalies(
        sync_session, config.project_id, config.id, _FLAGGED + timedelta(hours=1)
    )
    sync_session.flush()

    (current,) = sync_session.execute(select(MetricAnomaly)).scalars().all()
    assert current.id != seeded.id
    rows = _rows(sync_session)
    assert [row.anomaly_id for row in rows] == [current.id]
    assert rows[0].columns[0]["column"] == "platform"
    assert rows[0].columns[0]["values"][0]["value"] == "ios"


# ── collect_metrics: stored before the alert snapshot ────────────────────────


@pytest.fixture
def collect_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    # A file, not :memory:: collect_metrics opens several sessions of its own.
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution_collect.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_collect_metrics_stores_the_attribution_before_the_alert_snapshot(
    collect_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own attribution pass writes the rows BEFORE
    ``_prepare_alert_deliveries`` freezes the snapshot, so the queued alert's
    event item carries its "why" line."""
    with collect_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        # An event-level breakdown column: the attribution pass splits by it.
        # The fake warehouse has no such column, so the collection itself
        # skips it; the breakdown rows are written below instead.
        event.metric_breakdown_columns = ["platform"]
        _seed_alert_rule(session, config)
        session.commit()
        config_id = str(config.id)
        event_id = event.id

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self, *args: object, **kwargs: object
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name"],
                [],
                [
                    (_ANOMALY_BASE + timedelta(hours=8), "Login", 10),
                    (_ANOMALY_BASE + timedelta(hours=9), "Login", 10),
                ],
            )

        def close(self) -> None:
            return None

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with collect_session_factory() as session:
            persisted_event = session.execute(select(Event)).scalar_one()
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    real_recalculate = metrics_tasks._recalculate_metric_anomalies

    def with_platform_split(session: Session, config: ScanConfig, **kwargs: Any) -> int:
        # Stands in for the breakdown half of the collection the fake warehouse
        # does not model: every collected bucket of the event splits ios 60% /
        # android 40%, written after the chunk rewrite and before detection.
        for bucket, count in session.execute(
            select(EventMetric.bucket, EventMetric.count).where(
                EventMetric.scan_config_id == config.id, EventMetric.event_id == event_id
            )
        ).all():
            if not count:
                continue
            for value, share in (("ios", 0.6), ("android", 0.4)):
                session.add(
                    EventMetricBreakdown(
                        scan_config_id=config.id,
                        event_id=event_id,
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="platform",
                        breakdown_value=value,
                        is_other=False,
                        count=round(count * share),
                    )
                )
        session.flush()
        return real_recalculate(session, config, **kwargs)

    stored_before_snapshot: list[int] = []
    real_prepare = metrics_tasks._prepare_alert_deliveries

    def prepare_spy(session: Session, config: ScanConfig, **kwargs: Any) -> list[uuid.UUID]:
        stored_before_snapshot.append(
            len(session.execute(select(MetricAnomalyAttribution)).scalars().all())
        )
        return real_prepare(session, config, **kwargs)

    queued: list[str] = []
    monkeypatch.setattr(metrics_tasks, "is_holding", lambda freshness: False)
    monkeypatch.setattr(metrics_tasks, "_get_sync_session", collect_session_factory)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics_tasks, "_floor_to_interval", lambda dt, delta: _ANOMALY_BASE + timedelta(hours=13)
    )
    monkeypatch.setattr(metrics_tasks, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(metrics_tasks, "generate_events", fake_generate_events)
    monkeypatch.setattr(metrics_tasks, "_recalculate_metric_anomalies", with_platform_split)
    monkeypatch.setattr(metrics_tasks, "_prepare_alert_deliveries", prepare_spy)
    monkeypatch.setattr(
        metrics_tasks.send_alert_delivery, "delay", lambda delivery_id: queued.append(delivery_id)
    )

    result = metrics_tasks.collect_metrics.run(config_id)

    assert result["anomaly_attributions_computed"] >= 1
    assert stored_before_snapshot and stored_before_snapshot[0] >= 1
    assert result["alerts_queued"] >= 1
    with collect_session_factory() as session:
        snapshots = [
            delivery.payload_snapshot
            for delivery in session.execute(select(AlertDelivery)).scalars().all()
        ]
    items = [
        item
        for snapshot in snapshots
        if snapshot is not None
        for item in cast(list[dict[str, object]], snapshot["items"])
    ]
    event_items = [
        item
        for item in items
        if item["scope_type"] == "event" and item["scope_ref"] == str(event_id)
    ]
    assert event_items
    for item in event_items:
        line = item["attribution_line"]
        assert isinstance(line, str)
        assert re.match(r"60% of the (drop|spike) comes from platform = ios \(", line), line


# ── API ──────────────────────────────────────────────────────────────────────

# Recent and hour-aligned, so the seeded anomaly classifies as an open signal.
_OPEN_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

_STORED_COLUMNS = [
    {
        "column": "platform",
        "explained_share": 0.96,
        "values": [
            {"value": "ios", "delta": -3120.0, "expected": 4000.0, "actual": 880.0, "share": 0.92},
            {"value": "web", "delta": -140.0, "expected": 500.0, "actual": 360.0, "share": 0.04},
        ],
    }
]


async def _seed_api(client: AsyncClient, slug: str) -> dict[str, str]:
    created = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert created.status_code == 201, created.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type.json()["id"],
            "name": "Landing Viewed",
            "status": "implemented",
        },
    )
    assert event.status_code == 201, event.text
    data_source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {slug}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source.status_code == 201, data_source.text
    scan = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={"data_source_id": data_source.json()["id"], "name": "Scan", "base_query": "SELECT 1"},
    )
    assert scan.status_code == 201, scan.text
    project = await client.get(f"/api/v1/projects/{slug}")
    ids = {
        "project_id": project.json()["id"],
        "event_id": event.json()["id"],
        "scan_config_id": scan.json()["id"],
        "anomaly_id": str(uuid.uuid4()),
    }
    scan_id = uuid.UUID(ids["scan_config_id"])
    event_id = uuid.UUID(ids["event_id"])
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                scan_config_id=scan_id,
                event_id=event_id,
                event_type_id=None,
                bucket=_OPEN_BUCKET,
                count=880,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.UUID(ids["anomaly_id"]),
                scan_config_id=scan_id,
                scope_type="event",
                scope_ref=ids["event_id"],
                event_id=event_id,
                event_type_id=None,
                bucket=_OPEN_BUCKET,
                actual_count=880,
                expected_count=4270,
                stddev=100,
                z_score=-30,
                direction="drop",
                created_at=_OPEN_BUCKET,
            )
        )
        await session.commit()
    return ids


async def _set_platform_column(scan_config_id: str, column: str | None) -> None:
    async with TestSessionLocal() as session:
        config = await session.get(ScanConfig, uuid.UUID(scan_config_id))
        assert config is not None
        config.platform_column = column
        await session.commit()


async def _store_attribution(anomaly_id: str) -> None:
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomalyAttribution(
                anomaly_id=uuid.UUID(anomaly_id),
                delta=-3390.0,
                columns=_STORED_COLUMNS,
                release={
                    "version": "4.12",
                    "previous_version": "4.11",
                    "share": 0.38,
                    "reached_at": (_OPEN_BUCKET - timedelta(hours=3)).isoformat(),
                },
            )
        )
        await session.commit()


def _url(slug: str, anomaly_id: str) -> str:
    return f"/api/v1/projects/{slug}/anomalies/{anomaly_id}/attribution"


async def test_route_reports_each_status(client: AsyncClient) -> None:
    ids = await _seed_api(client, "why-route")

    no_columns = await client.get(_url("why-route", ids["anomaly_id"]))
    assert no_columns.status_code == 200, no_columns.text
    assert no_columns.json()["attribution_status"] == "no_breakdown_columns"
    assert no_columns.json()["attribution"] is None

    await _set_platform_column(ids["scan_config_id"], "platform")
    pending = await client.get(_url("why-route", ids["anomaly_id"]))
    assert pending.json()["attribution_status"] == "not_computed"

    await _store_attribution(ids["anomaly_id"])
    ready = await client.get(_url("why-route", ids["anomaly_id"]))
    body = ready.json()
    assert body["attribution_status"] == "ready"
    attribution = body["attribution"]
    assert attribution["delta"] == -3390.0
    assert attribution["columns"][0]["values"][0]["value"] == "ios"
    assert attribution["headline"] == (
        "92% of the drop comes from platform = ios (−3,120 of −3,390)"
    )
    assert attribution["release"]["version"] == "4.12"
    expected_line = "Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop"
    assert attribution["release_line"] == expected_line


async def test_signals_and_drilldown_carry_the_attribution(client: AsyncClient) -> None:
    ids = await _seed_api(client, "why-signals")
    await _set_platform_column(ids["scan_config_id"], "platform")
    await _store_attribution(ids["anomaly_id"])

    listed = await client.get(
        "/api/v1/projects/why-signals/anomalies/signals", params={"expanded": "true"}
    )
    assert listed.status_code == 200, listed.text
    (signal,) = [item for item in listed.json() if item["scope_type"] == "event"]
    assert signal["anomaly_id"] == ids["anomaly_id"]
    assert signal["attribution_status"] == "ready"
    assert signal["attribution"]["columns"][0]["column"] == "platform"

    drilldown = await client.get(
        f"/api/v1/projects/why-signals/events/{ids['event_id']}/metrics",
        params={
            "from": (_OPEN_BUCKET - timedelta(days=1)).isoformat(),
            "to": (_OPEN_BUCKET + timedelta(hours=1)).isoformat(),
        },
    )
    assert drilldown.status_code == 200, drilldown.text
    latest = drilldown.json()["latest_signal"]
    assert latest is not None
    assert latest["attribution_status"] == "ready"
    assert latest["attribution"]["headline"].startswith("92% of the drop")


async def test_unknown_or_foreign_anomaly_is_404(client: AsyncClient) -> None:
    ids = await _seed_api(client, "why-mine")
    other = await _seed_api(client, "why-theirs")
    assert (await client.get(_url("why-mine", str(uuid.uuid4())))).status_code == 404
    # Another project's anomaly, asked for through this project's slug.
    assert (await client.get(_url("why-mine", other["anomaly_id"]))).status_code == 404
    assert (await client.get(_url("why-mine", ids["anomaly_id"]))).status_code == 200


async def test_non_member_gets_404(client: AsyncClient) -> None:
    ids = await _seed_api(client, "why-outsider")
    async with TestSessionLocal() as session:
        outsider = User(
            id=uuid.uuid4(),
            email=f"outsider-{uuid.uuid4().hex[:8]}@example.com",
            name="Outsider",
            password_hash=PASSWORD_HASH_PLACEHOLDER,
            role=UserRole.editor.value,
        )
        session.add(outsider)
        await session.commit()

    async def _override() -> User:
        return outsider

    app.dependency_overrides[get_current_user] = _override
    try:
        response = await client.get(_url("why-outsider", ids["anomaly_id"]))
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 404
