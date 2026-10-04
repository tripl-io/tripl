"""Per-bucket baseline band for every scored bucket.

``metric_anomalies`` holds flagged buckets only, so the chart drew its expected
value and band on anomalies alone. The detector now reports the baseline of
every bucket it scores, the metrics worker persists it in ``metric_baselines``,
and the drilldown responses attach it to each point as ``baseline_expected`` /
``baseline_stddev``.

Covers the three seams: the detector's ``DetectionResult.baselines``, the
worker's persistence (idempotent, cleared with detection), and the response
builder plus the three drilldown routes that attach it. Also the lifecycle
paths that must not strand a band: the danger-zone anomaly reset, deleting an
event, and merging one into a group.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers._event_generator_merge import _delete_event_anomalies
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    BaselinePoint,
    SeriesPoint,
    detect_anomalies,
)
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event_metric import EventMetricPoint
from tripl.services.metrics_service import _build_metric_points
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.worker.tasks.metrics import detect as metrics_detect

_HOUR = timedelta(hours=1)
# Recent, so the 180-day anomaly retention never ages the rows out mid-test.
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=48)
# 30 hourly buckets: too short for a phase period, so every bucket takes the
# rolling path, whose baseline is easy to reason about.
_HISTORY = 30
_SPIKE = 400
_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10.0,
)


def _counts() -> list[int]:
    # ~100 with a little jitter, and a spike on the newest bucket.
    return [100 + (index % 3) for index in range(_HISTORY - 1)] + [_SPIKE]


def _bucket(index: int) -> datetime:
    return _BASE + _HOUR * index


def _series() -> list[SeriesPoint]:
    return [SeriesPoint(bucket=_bucket(i), count=count) for i, count in enumerate(_counts())]


# The newest four buckets: three quiet ones and the spike.
_EVAL_START = _bucket(_HISTORY - 4)
_EVAL_END = _bucket(_HISTORY)


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #


def test_detector_reports_a_baseline_for_every_scored_bucket() -> None:
    result = detect_anomalies(
        _series(),
        interval=_HOUR,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
        settings=_SETTINGS,
    )

    assert [baseline.bucket for baseline in result.baselines] == [
        _bucket(index) for index in range(_HISTORY - 4, _HISTORY)
    ]
    # Only the spike is flagged, but every bucket carries a band.
    assert [anomaly.bucket for anomaly in result.anomalies] == [_bucket(_HISTORY - 1)]
    quiet_counts = _counts()[_HISTORY - 4 : _HISTORY - 1]
    for baseline, actual in zip(result.baselines[:-1], quiet_counts, strict=True):
        assert 99.0 <= baseline.expected_count <= 103.0
        assert baseline.effective_stddev > 0
        # A quiet bucket sits inside its own band: not flagged, and the band says so.
        low = baseline.expected_count - _SETTINGS.sigma_threshold * baseline.effective_stddev
        high = baseline.expected_count + _SETTINGS.sigma_threshold * baseline.effective_stddev
        assert low <= actual <= high


def test_flagged_bucket_baseline_matches_its_anomaly_row() -> None:
    result = detect_anomalies(
        _series(),
        interval=_HOUR,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
        settings=_SETTINGS,
    )

    anomaly = result.anomalies[0]
    baseline = result.baselines[-1]
    assert baseline.bucket == anomaly.bucket
    assert baseline.expected_count == pytest.approx(anomaly.expected_count)
    assert baseline.effective_stddev == pytest.approx(anomaly.effective_stddev)


def test_settling_buckets_carry_no_baseline() -> None:
    result = detect_anomalies(
        _series(),
        interval=_HOUR,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
        settings=_SETTINGS,
        settling_buckets=1,
    )

    assert _bucket(_HISTORY - 1) not in {baseline.bucket for baseline in result.baselines}
    assert len(result.baselines) == 3


def test_a_series_under_the_volume_floor_stores_no_baseline() -> None:
    quiet = [SeriesPoint(bucket=_bucket(i), count=2) for i in range(_HISTORY)]

    result = detect_anomalies(
        quiet,
        interval=_HOUR,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
        settings=_SETTINGS,
    )

    assert result.baselines == ()


# --------------------------------------------------------------------------- #
# Worker persistence
# --------------------------------------------------------------------------- #


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'baseline_band.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_scope(session: Session) -> tuple[ScanConfig, EventType]:
    project = Project(
        id=uuid.uuid4(),
        name="Baseline Band",
        slug=f"baseline-band-{uuid.uuid4().hex[:8]}",
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
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        detect_metrics=False,
        baseline_window_buckets=_SETTINGS.baseline_window_buckets,
        min_history_buckets=_SETTINGS.min_history_buckets,
        sigma_threshold=_SETTINGS.sigma_threshold,
        min_expected_count=int(_SETTINGS.min_expected_count),
    )
    session.add_all([project, data_source, config, settings])
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="page",
        display_name="Page",
        description="",
    )
    session.add(event_type)
    session.flush()
    session.add_all(
        [
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=None,
                event_type_id=event_type.id,
                bucket=_bucket(index),
                count=count,
            )
            for index, count in enumerate(_counts())
        ]
    )
    session.commit()
    return config, event_type


def _baselines(session: Session, config: ScanConfig, scope_type: str) -> list[MetricBaseline]:
    return list(
        session.execute(
            select(MetricBaseline)
            .where(
                MetricBaseline.scan_config_id == config.id,
                MetricBaseline.scope_type == scope_type,
            )
            .order_by(MetricBaseline.bucket)
        ).scalars()
    )


def _recalculate(session: Session, config: ScanConfig) -> None:
    metrics_detect._recalculate_metric_anomalies(
        session,
        config,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
    )
    session.commit()


def test_worker_persists_a_baseline_for_every_scored_bucket(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, event_type = _seed_scope(session)
        _recalculate(session, config)

        rows = _baselines(session, config, "event_type")
        assert [row.bucket for row in rows] == [
            _bucket(index) for index in range(_HISTORY - 4, _HISTORY)
        ]
        assert {row.scope_ref for row in rows} == {str(event_type.id)}

        # The project total sums the one event type, so it scores the same buckets.
        assert len(_baselines(session, config, "project_total")) == 4

        anomaly = session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type == "event_type",
            )
        ).scalar_one()
        flagged = next(row for row in rows if row.bucket == anomaly.bucket)
        assert flagged.expected_count == pytest.approx(anomaly.expected_count)
        assert flagged.effective_stddev == pytest.approx(anomaly.effective_stddev)


def test_worker_rescoring_the_window_does_not_duplicate_baselines(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _ = _seed_scope(session)
        _recalculate(session, config)
        _recalculate(session, config)

        assert len(_baselines(session, config, "event_type")) == 4


def test_turning_detection_off_clears_the_baselines(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _ = _seed_scope(session)
        _recalculate(session, config)
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings.anomaly_detection_enabled = False
        session.commit()

        _recalculate(session, config)

        assert _baselines(session, config, "event_type") == []
        assert _baselines(session, config, "project_total") == []


def test_disabling_one_scope_clears_only_its_baselines(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _ = _seed_scope(session)
        _recalculate(session, config)
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings.detect_event_types = False
        session.commit()

        _recalculate(session, config)

        assert _baselines(session, config, "event_type") == []
        assert len(_baselines(session, config, "project_total")) == 4


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #


def test_points_carry_the_stored_baseline_and_omit_it_elsewhere() -> None:
    quiet, flagged = _bucket(0), _bucket(1)
    points = _build_metric_points(
        interval="1h",
        metric_rows=[(quiet, 100), (flagged, 400)],
        anomalies=[],
        # A baseline for a bucket the series does not have adds no point.
        baselines={quiet: (101.0, 10.0), _bucket(5): (1.0, 1.0)},
    )

    assert [point.bucket for point in points] == [quiet, flagged]
    assert points[0].baseline_expected == 101.0
    assert points[0].baseline_stddev == 10.0
    assert points[1].baseline_expected is None

    # NULL baselines stay out of the payload rather than adding two nulls a point.
    dumped = [point.model_dump(mode="json") for point in points]
    assert dumped[0]["baseline_expected"] == 101.0
    assert "baseline_expected" not in dumped[1]
    assert "baseline_stddev" not in dumped[1]


def test_point_schema_defaults_to_no_baseline() -> None:
    point = EventMetricPoint(bucket=_bucket(0), count=1)

    assert point.baseline_expected is None
    assert "baseline_expected" not in point.model_dump(mode="json")


@pytest.mark.asyncio
async def test_project_total_route_serves_the_stored_baseline(client: AsyncClient) -> None:
    slug = f"baseline-route-{uuid.uuid4().hex[:8]}"
    project_resp = await client.post("/api/v1/projects", json={"name": "Band", "slug": slug})
    assert project_resp.status_code == 201, project_resp.text
    project_id = uuid.UUID(project_resp.json()["id"])
    type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page", "display_name": "Page"},
    )
    event_type_id = uuid.UUID(type_resp.json()["id"])
    scored, unscored = _bucket(_HISTORY - 2), _bucket(_HISTORY - 1)

    async with TestSessionLocal() as session:
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
        config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="Scan",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all(
            [
                data_source,
                config,
                ProjectAnomalySettings(project_id=project_id, anomaly_detection_enabled=True),
            ]
        )
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=event_type_id,
                    bucket=bucket,
                    count=100,
                )
                for bucket in (scored, unscored)
            ]
        )
        session.add(
            MetricBaseline(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type="project_total",
                scope_ref=str(config.id),
                bucket=scored,
                expected_count=98.0,
                effective_stddev=10.0,
            )
        )
        await session.commit()
        config_id = config.id

    resp = await client.get(
        f"/api/v1/projects/{slug}/metrics/total", params={"scan_config_id": str(config_id)}
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 2
    assert data[0]["baseline_expected"] == 98.0
    assert data[0]["baseline_stddev"] == 10.0
    # No row: the band is simply absent there, and the anomaly fields untouched.
    assert "baseline_expected" not in data[1]
    assert data[1]["expected_count"] is None


# --------------------------------------------------------------------------- #
# Event and event-type drilldown routes
# --------------------------------------------------------------------------- #


async def _seed_route_scope(client: AsyncClient, prefix: str) -> dict[str, object]:
    """A project with one event type, one event and a scan config that counted both.

    Returns the ids the routes and assertions need. Two buckets per series: the
    first carries a stored baseline in every test that seeds one, the second
    never does, so each route test also proves the band stays off unscored points.
    """
    slug = f"{prefix}-{uuid.uuid4().hex[:8]}"
    project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project_resp.status_code == 201, project_resp.text
    project_id = uuid.UUID(project_resp.json()["id"])
    type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page", "display_name": "Page"},
    )
    assert type_resp.status_code == 201, type_resp.text
    event_type_id = uuid.UUID(type_resp.json()["id"])
    field_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": "action", "display_name": "Action", "field_type": "string"},
    )
    assert field_resp.status_code == 201, field_resp.text
    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": str(event_type_id),
            "name": "signup",
            "field_values": [{"field_definition_id": field_resp.json()["id"], "value": "signup"}],
        },
    )
    assert event_resp.status_code == 201, event_resp.text
    event_id = uuid.UUID(event_resp.json()["id"])
    scored, unscored = _bucket(_HISTORY - 2), _bucket(_HISTORY - 1)

    async with TestSessionLocal() as session:
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
        config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="Scan",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all(
            [
                data_source,
                config,
                ProjectAnomalySettings(project_id=project_id, anomaly_detection_enabled=True),
            ]
        )
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=row_event_id,
                    # An event row carries no type id: the type row owns the
                    # (config, type, bucket) key.
                    event_type_id=None if row_event_id else event_type_id,
                    bucket=bucket,
                    count=100,
                )
                for bucket in (scored, unscored)
                for row_event_id in (event_id, None)
            ]
        )
        await session.commit()
        config_id = config.id

    return {
        "slug": slug,
        "event_type_id": event_type_id,
        "event_id": event_id,
        "config_id": config_id,
        "scored": scored,
        "unscored": unscored,
    }


async def _add_baselines(config_id: object, rows: list[tuple[str, str, datetime, float]]) -> None:
    """Persist ``(scope_type, scope_ref, bucket, expected_count)`` rows, stddev 10."""
    assert isinstance(config_id, uuid.UUID)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                MetricBaseline(
                    id=uuid.uuid4(),
                    scan_config_id=config_id,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    bucket=bucket,
                    expected_count=expected,
                    effective_stddev=10.0,
                )
                for scope_type, scope_ref, bucket, expected in rows
            ]
        )
        await session.commit()


async def _baseline_rows(config_id: object) -> list[MetricBaseline]:
    async with TestSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(MetricBaseline)
                    .where(MetricBaseline.scan_config_id == config_id)
                    .order_by(MetricBaseline.scope_type, MetricBaseline.bucket)
                )
            ).scalars()
        )


def _point_at(data: list[dict[str, object]], bucket: datetime) -> dict[str, object]:
    wanted = bucket.astimezone(UTC)
    for point in data:
        raw = point["bucket"]
        assert isinstance(raw, str)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed == wanted:
            return point
    raise AssertionError(f"no point at {bucket.isoformat()} in {data!r}")


@pytest.mark.asyncio
async def test_event_route_serves_the_stored_baseline(client: AsyncClient) -> None:
    scope = await _seed_route_scope(client, "baseline-event")
    event_id, event_type_id = scope["event_id"], scope["event_type_id"]
    scored, unscored = scope["scored"], scope["unscored"]
    assert isinstance(scored, datetime) and isinstance(unscored, datetime)
    await _add_baselines(
        scope["config_id"],
        [
            ("event", str(event_id), scored, 97.0),
            # Same bucket, other scope: must not bleed into the event's series.
            ("event_type", str(event_type_id), scored, 55.0),
        ],
    )

    resp = await client.get(f"/api/v1/projects/{scope['slug']}/events/{event_id}/metrics")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert _point_at(data, scored)["baseline_expected"] == 97.0
    assert _point_at(data, scored)["baseline_stddev"] == 10.0
    assert "baseline_expected" not in _point_at(data, unscored)


@pytest.mark.asyncio
async def test_event_type_route_serves_the_stored_baseline(client: AsyncClient) -> None:
    scope = await _seed_route_scope(client, "baseline-type")
    event_id, event_type_id = scope["event_id"], scope["event_type_id"]
    scored, unscored = scope["scored"], scope["unscored"]
    assert isinstance(scored, datetime) and isinstance(unscored, datetime)
    await _add_baselines(
        scope["config_id"],
        [
            ("event_type", str(event_type_id), scored, 96.0),
            ("event", str(event_id), scored, 55.0),
        ],
    )

    resp = await client.get(f"/api/v1/projects/{scope['slug']}/event-types/{event_type_id}/metrics")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert _point_at(data, scored)["baseline_expected"] == 96.0
    assert _point_at(data, scored)["baseline_stddev"] == 10.0
    assert "baseline_expected" not in _point_at(data, unscored)


# --------------------------------------------------------------------------- #
# Lifecycle: reset, event delete, merge
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_anomaly_reset_deletes_baselines_in_the_period_only(client: AsyncClient) -> None:
    scope = await _seed_route_scope(client, "baseline-reset")
    scored, unscored = scope["scored"], scope["unscored"]
    assert isinstance(scored, datetime) and isinstance(unscored, datetime)
    ref = str(scope["event_type_id"])
    await _add_baselines(
        scope["config_id"],
        [("event_type", ref, scored, 100.0), ("event_type", ref, unscored, 100.0)],
    )
    url = f"/api/v1/projects/{scope['slug']}/danger/reset-anomalies"
    # Half-open: ``after <= bucket < before`` keeps only the scored bucket in range.
    period = {"after": scored.isoformat(), "before": unscored.isoformat()}

    preview = await client.post(url, json={**period, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["metric_baselines"] == 1
    assert len(await _baseline_rows(scope["config_id"])) == 2

    real = await client.post(url, json=period)
    assert real.status_code == 200, real.text
    assert real.json()["metric_baselines"] == 1
    assert [row.bucket for row in await _baseline_rows(scope["config_id"])] == [unscored]

    again = await client.post(url, json=period)
    assert again.json()["metric_baselines"] == 0


@pytest.mark.asyncio
async def test_anomaly_reset_leaves_other_projects_baselines(client: AsyncClient) -> None:
    mine = await _seed_route_scope(client, "baseline-reset-mine")
    theirs = await _seed_route_scope(client, "baseline-reset-theirs")
    for scope in (mine, theirs):
        scored = scope["scored"]
        assert isinstance(scored, datetime)
        await _add_baselines(
            scope["config_id"], [("event_type", str(scope["event_type_id"]), scored, 1.0)]
        )

    resp = await client.post(f"/api/v1/projects/{mine['slug']}/danger/reset-anomalies", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["metric_baselines"] == 1
    assert await _baseline_rows(mine["config_id"]) == []
    assert len(await _baseline_rows(theirs["config_id"])) == 1


@pytest.mark.asyncio
async def test_deleting_an_event_deletes_only_its_baselines(client: AsyncClient) -> None:
    scope = await _seed_route_scope(client, "baseline-delete")
    event_id, event_type_id = scope["event_id"], scope["event_type_id"]
    scored = scope["scored"]
    assert isinstance(scored, datetime)
    await _add_baselines(
        scope["config_id"],
        [
            ("event", str(event_id), scored, 100.0),
            ("event_type", str(event_type_id), scored, 100.0),
            # scope_ref is polymorphic: a non-event scope sharing the string stays.
            ("project_total", str(event_id), scored, 100.0),
        ],
    )

    resp = await client.delete(f"/api/v1/projects/{scope['slug']}/events/{event_id}")

    assert resp.status_code == 204, resp.text
    rows = await _baseline_rows(scope["config_id"])
    remaining = {(row.scope_type, row.scope_ref) for row in rows}
    assert remaining == {
        ("event_type", str(event_type_id)),
        ("project_total", str(event_id)),
    }


def test_merge_cleanup_deletes_both_events_baselines(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, event_type = _seed_scope(session)
        source_id, target_id, bystander_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session.add_all(
            [
                MetricBaseline(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    bucket=_bucket(0),
                    expected_count=1.0,
                    effective_stddev=1.0,
                )
                for scope_type, scope_ref in (
                    ("event", str(source_id)),
                    ("event", str(target_id)),
                    ("event", str(bystander_id)),
                    ("event_type", str(event_type.id)),
                )
            ]
        )
        session.commit()

        _delete_event_anomalies(session, event_ids=[source_id, target_id])
        session.commit()

        assert {row.scope_ref for row in _baselines(session, config, "event")} == {
            str(bystander_id)
        }
        assert len(_baselines(session, config, "event_type")) == 1


# --------------------------------------------------------------------------- #
# PostgreSQL: the upsert's named constraint
# --------------------------------------------------------------------------- #


@pytest.fixture
def pg_session_factory() -> Iterator[sessionmaker[Session]]:
    engine = _engine_or_skip()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.postgres
def test_baseline_upsert_resolves_its_constraint_on_postgres(
    pg_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ON CONFLICT ON CONSTRAINT uq_metric_baseline_scope_bucket`` on real PostgreSQL.

    SQLite takes the ``index_elements`` branch, so only this proves the PG
    branch names a constraint that exists. The delete that precedes the insert
    normally empties the window first; it is neutralised on the second call so
    the insert genuinely collides and the DO UPDATE branch has to run.
    """
    with pg_session_factory() as session:
        config, event_type = _seed_scope(session)
        scope_ref = str(event_type.id)
        buckets = [_bucket(index) for index in range(_HISTORY - 4, _HISTORY)]

        def replace(expected: float) -> None:
            metrics_detect._replace_scope_baselines(
                session,
                scan_config_id=config.id,
                scope_type="event_type",
                scope_ref=scope_ref,
                evaluation_start=_EVAL_START,
                evaluation_end=_EVAL_END,
                baselines=[
                    BaselinePoint(bucket=bucket, expected_count=expected, effective_stddev=5.0)
                    for bucket in buckets
                ],
            )
            session.commit()

        replace(100.0)
        assert [row.expected_count for row in _baselines(session, config, "event_type")] == [
            100.0
        ] * 4

        # A no-op in place of the window delete: the rows stay, so every insert conflicts.
        monkeypatch.setattr(metrics_detect, "delete", lambda model: select(model).limit(0))
        replace(250.0)
        session.expire_all()

        rows = _baselines(session, config, "event_type")
        assert [row.bucket for row in rows] == buckets
        assert [row.expected_count for row in rows] == [250.0] * 4
