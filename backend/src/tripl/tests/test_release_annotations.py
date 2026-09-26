"""Automatic "Release <version>" chart annotations (GitHub #256).

Layers: the pure selection (which versions earn a marker, and at which bucket),
the metrics-worker pass that writes them (idempotent, per project, inert without
an ``app_version_column``, one savepoint per row), the ``collect_metrics`` hook
that runs it, the PostgreSQL ``ON CONFLICT`` inference against the partial
unique index, and the demo, whose scan must show exactly one release marker.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Insert

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.models import Base
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import ChartAnnotationSource
from tripl.models.event import Event
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.demo.builders.alerts import DEMO_RELEASE_VERSION
from tripl.services.demo_service import create_demo_project
from tripl.services.release_annotations import (
    RELEASE_ANNOTATION_COLOR,
    release_annotation_label,
    releases_to_annotate,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.tests.test_metrics_tasks import _create_scan_config
from tripl.worker.tasks.metrics import tasks as metrics
from tripl.worker.tasks.metrics.release_annotations import (
    RELEASE_LABEL_MAX_LENGTH,
    _sync_release_annotations,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
HOURS = 72
ROLLOUT_HOUR = 30


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone on round-trip; compare everything as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hours(n: int) -> list[datetime]:
    return [T0 + timedelta(hours=h) for h in range(n)]


def _rollout_series() -> dict[str, dict[datetime, float]]:
    """1.0.0 live from the first hour; 1.1.0 rolls out at ``ROLLOUT_HOUR``."""
    return {
        "1.0.0": {bucket: 1000 for bucket in _hours(HOURS)},
        "1.1.0": {bucket: 500 for bucket in _hours(HOURS)[ROLLOUT_HOUR:]},
    }


def _totals(series: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
    totals: dict[datetime, float] = {}
    for by_bucket in series.values():
        for bucket, count in by_bucket.items():
            totals[bucket] = totals.get(bucket, 0) + count
    return totals


# ── pure selection ────────────────────────────────────────────────────────────


def test_a_version_absent_at_slice_start_is_marked_at_its_activation() -> None:
    series = _rollout_series()

    assert releases_to_annotate(series, _totals(series)) == {
        "1.1.0": T0 + timedelta(hours=ROLLOUT_HOUR)
    }


def test_the_first_version_ever_seen_is_never_marked() -> None:
    series = {"1.0.0": {bucket: 1000 for bucket in _hours(HOURS)}}

    assert releases_to_annotate(series, _totals(series)) == {}


def test_versions_already_live_when_the_series_begins_are_not_marked() -> None:
    series = {
        "1.0.0": {bucket: 1000 for bucket in _hours(HOURS)},
        "1.1.0": {bucket: 800 for bucket in _hours(HOURS)},
    }

    assert releases_to_annotate(series, _totals(series)) == {}


def test_a_legacy_version_under_the_gate_at_slice_start_is_not_marked() -> None:
    """Live before the slice (at 2%), crossing the gate later is not a release."""
    series = {
        "1.0.0": {bucket: 1000 for bucket in _hours(HOURS)},
        "0.9.0": {
            bucket: (20 if hour < ROLLOUT_HOUR else 400)
            for hour, bucket in enumerate(_hours(HOURS))
        },
    }

    assert releases_to_annotate(series, _totals(series)) == {}


def test_the_baseline_re_crossing_after_a_dip_is_not_marked() -> None:
    """1.0.0 is active for one bucket, dips under the gate, then re-crosses.

    Its activation run starts at the re-cross, after 0.9.0's: the old
    "anything activating after the earliest activation" rule marked it there.
    """
    dip = range(1, 10)
    series = {
        "0.9.0": {bucket: 1000 for bucket in _hours(HOURS)},
        "1.0.0": {
            bucket: (10 if hour in dip else 600) for hour, bucket in enumerate(_hours(HOURS))
        },
    }

    assert releases_to_annotate(series, _totals(series)) == {}


def test_a_genuinely_new_version_is_marked_once_next_to_live_ones() -> None:
    series = {
        "0.9.0": {
            bucket: (20 if hour < ROLLOUT_HOUR else 400)
            for hour, bucket in enumerate(_hours(HOURS))
        },
        "1.0.0": {bucket: 1000 for bucket in _hours(HOURS)},
        "1.1.0": {bucket: 500 for bucket in _hours(HOURS)[ROLLOUT_HOUR:]},
    }

    marked = releases_to_annotate(series, _totals(series))

    assert marked == {"1.1.0": T0 + timedelta(hours=ROLLOUT_HOUR)}


def test_a_slice_starting_without_traffic_marks_nothing() -> None:
    """No traffic in the leading buckets: nothing can be told apart from live."""
    series = _rollout_series()
    leading = set(_hours(2))
    series["1.0.0"] = {b: c for b, c in series["1.0.0"].items() if b not in leading}
    totals = _totals(series)
    for bucket in leading:
        totals[bucket] = 0

    assert releases_to_annotate(series, totals) == {}


def test_the_activation_gate_excludes_dev_builds_and_prereleases() -> None:
    series = _rollout_series()
    # Below 5% share: a tester build that surfaced but never shipped.
    series["1.2.0"] = {bucket: 10 for bucket in _hours(HOURS)[40:]}
    # Enough share, but a SemVer prerelease.
    series["1.3.0-beta.1"] = {bucket: 400 for bucket in _hours(HOURS)[50:]}
    # Enough share, but the scan's own prerelease pattern names it.
    series["1.4.0b1"] = {bucket: 400 for bucket in _hours(HOURS)[60:]}

    marked = releases_to_annotate(series, _totals(series), prerelease_pattern=re.compile(r"b\d+$"))

    assert marked == {"1.1.0": T0 + timedelta(hours=ROLLOUT_HOUR)}


def test_a_version_below_the_volume_floor_is_not_marked() -> None:
    series = {
        "1.0.0": {bucket: 100 for bucket in _hours(10)},
        # ~9% share for 5 buckets, but 50 events in all: under the 200 floor.
        "1.1.0": {bucket: 10 for bucket in _hours(10)[5:]},
    }

    assert releases_to_annotate(series, _totals(series)) == {}


def test_release_label_is_the_version_behind_a_fixed_prefix() -> None:
    assert release_annotation_label("4.12.0") == "Release 4.12.0"


# ── worker pass ───────────────────────────────────────────────────────────────


@pytest.fixture
def sync_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _config(
    project_id: uuid.UUID,
    *,
    scan_config_id: uuid.UUID | None = None,
    app_version_column: str | None = "app_version",
) -> ScanConfig:
    # The pass reads only these attributes; a full ScanConfig needs a data source.
    return cast(
        ScanConfig,
        SimpleNamespace(
            id=scan_config_id or uuid.uuid4(),
            project_id=project_id,
            app_version_column=app_version_column,
            app_version_active_share_min=None,
            app_version_prerelease_pattern=None,
        ),
    )


def _seed_project(session: Session) -> uuid.UUID:
    project_id = uuid.uuid4()
    session.add(Project(id=project_id, name="Releases", slug=f"rel-{project_id.hex[:8]}"))
    session.commit()
    return project_id


def _seed_version_rows(
    session: Session,
    scan_config_id: uuid.UUID,
    series: dict[str, dict[datetime, float]] | None = None,
    *,
    event_id: uuid.UUID | None = None,
) -> None:
    event_id = event_id or uuid.uuid4()
    for version, by_bucket in (series or _rollout_series()).items():
        for bucket, count in by_bucket.items():
            session.add(
                EventMetricBreakdown(
                    scan_config_id=scan_config_id,
                    event_id=event_id,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=False,
                    count=int(count),
                )
            )
    session.commit()


def _annotations(session: Session, project_id: uuid.UUID) -> list[ChartAnnotation]:
    return list(
        session.execute(select(ChartAnnotation).where(ChartAnnotation.project_id == project_id))
        .scalars()
        .all()
    )


def test_worker_creates_one_project_wide_release_annotation(sync_session: Session) -> None:
    project_id = _seed_project(sync_session)
    config = _config(project_id)
    _seed_version_rows(sync_session, config.id)

    created = _sync_release_annotations(sync_session, config)
    sync_session.commit()

    assert created == 1
    [marker] = _annotations(sync_session, project_id)
    assert marker.label == "Release 1.1.0"
    assert marker.source == ChartAnnotationSource.release
    assert marker.scope_type is None
    assert marker.scope_ref is None
    assert marker.url is None
    assert marker.created_by_user_id is None
    assert marker.color == RELEASE_ANNOTATION_COLOR
    assert _utc(marker.bucket) == T0 + timedelta(hours=ROLLOUT_HOUR)


def test_worker_pass_is_idempotent(sync_session: Session) -> None:
    project_id = _seed_project(sync_session)
    config = _config(project_id)
    _seed_version_rows(sync_session, config.id)

    assert _sync_release_annotations(sync_session, config) == 1
    sync_session.commit()
    assert _sync_release_annotations(sync_session, config) == 0
    sync_session.commit()

    assert len(_annotations(sync_session, project_id)) == 1


def test_two_scans_of_one_project_mark_a_release_once(sync_session: Session) -> None:
    project_id = _seed_project(sync_session)
    first = _config(project_id)
    second = _config(project_id)
    _seed_version_rows(sync_session, first.id)
    _seed_version_rows(sync_session, second.id)

    assert _sync_release_annotations(sync_session, first) == 1
    assert _sync_release_annotations(sync_session, second) == 0
    sync_session.commit()

    assert [row.label for row in _annotations(sync_session, project_id)] == ["Release 1.1.0"]


def test_release_uniqueness_ignores_manual_markers_with_the_same_label(
    sync_session: Session,
) -> None:
    """A person's own "Release 1.1.0" note does not block the automatic one."""
    project_id = _seed_project(sync_session)
    config = _config(project_id)
    _seed_version_rows(sync_session, config.id)
    sync_session.add(
        ChartAnnotation(project_id=project_id, bucket=T0, label=release_annotation_label("1.1.0"))
    )
    sync_session.commit()

    assert _sync_release_annotations(sync_session, config) == 1
    sync_session.commit()

    sources = sorted(str(row.source) for row in _annotations(sync_session, project_id))
    assert sources == ["manual", "release"]


def test_worker_pass_is_inert_without_an_app_version_column(sync_session: Session) -> None:
    project_id = _seed_project(sync_session)
    config = _config(project_id, app_version_column=None)
    _seed_version_rows(sync_session, config.id)

    assert _sync_release_annotations(sync_session, config) == 0
    sync_session.commit()

    assert _annotations(sync_session, project_id) == []


def test_worker_pass_is_inert_without_version_rows(sync_session: Session) -> None:
    project_id = _seed_project(sync_session)

    assert _sync_release_annotations(sync_session, _config(project_id)) == 0
    assert _annotations(sync_session, project_id) == []


def test_a_version_whose_label_would_overflow_the_column_is_skipped(
    sync_session: Session,
) -> None:
    """Versions hold up to 500 characters, labels 200: skip, never truncate."""
    project_id = _seed_project(sync_session)
    config = _config(project_id)
    long_version = "2." + "1" * RELEASE_LABEL_MAX_LENGTH
    series = _rollout_series()
    series[long_version] = {bucket: 500 for bucket in _hours(HOURS)[50:]}
    _seed_version_rows(sync_session, config.id, series)

    assert _sync_release_annotations(sync_session, config) == 1
    sync_session.commit()

    assert [row.label for row in _annotations(sync_session, project_id)] == ["Release 1.1.0"]


def test_one_failing_insert_does_not_drop_the_other_markers(
    sync_session: Session, monkeypatch: MonkeyPatch
) -> None:
    project_id = _seed_project(sync_session)
    config = _config(project_id)
    series = _rollout_series()
    series["1.2.0"] = {bucket: 500 for bucket in _hours(HOURS)[50:]}
    _seed_version_rows(sync_session, config.id, series)

    real_execute = sync_session.execute
    failed: list[bool] = []

    def execute_failing_first_insert(statement: object, *args: object, **kwargs: object):
        if isinstance(statement, Insert) and not failed:
            failed.append(True)
            raise OperationalError("INSERT", {}, Exception("disk I/O error"))
        return real_execute(statement, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(sync_session, "execute", execute_failing_first_insert)

    # 1.1.0 (hour 30) is inserted first and fails; 1.2.0 (hour 50) still lands.
    assert _sync_release_annotations(sync_session, config) == 1
    monkeypatch.undo()
    sync_session.commit()

    assert failed == [True]
    assert [row.label for row in _annotations(sync_session, project_id)] == ["Release 1.2.0"]


# ── collect_metrics hook ──────────────────────────────────────────────────────


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    # A file, not :memory:: collect_metrics opens several sessions of its own.
    engine = create_engine(f"sqlite:///{tmp_path / 'release_annotations.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def test_collect_metrics_runs_the_release_annotation_pass(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The hook in ``collect_metrics``: stored history plus this run's bucket
    show 1.1.0 rolling out after 1.0.0, so the run writes its marker and says so
    in its summary."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.app_version_column = "app_version"
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
        project_id = config.project_id
        login_event_id = login_event.id
        # History earlier runs stored: 40 hours, 1.1.0 rolling out at hour 20.
        for hour, bucket in enumerate(_hours(40)):
            for version, count in (("1.0.0", 1000), ("1.1.0", 500 if hour >= 20 else 0)):
                if count:
                    session.add(
                        EventMetricBreakdown(
                            scan_config_id=config.id,
                            event_id=login_event_id,
                            bucket=bucket,
                            breakdown_column="app_version",
                            breakdown_value=version,
                            is_other=False,
                            count=count,
                        )
                    )
        session.commit()

    window_from = (T0 + timedelta(hours=40)).replace(tzinfo=None)
    window_to = window_from + timedelta(hours=1)

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="app_version", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self, *args: object, **kwargs: object
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name", "app_version"],
                [],
                [
                    (window_from, "Login", "1.0.0", 1000),
                    (window_from, "Login", "1.1.0", 500),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self, *args: object, **kwargs: object
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (["event_name", "app_version"], [], [])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (window_from, window_to, False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=2,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert result["release_annotations_created"] == 1
    with sync_session_factory() as session:
        [marker] = _annotations(session, project_id)
    assert marker.label == "Release 1.1.0"
    assert marker.source == ChartAnnotationSource.release
    assert _utc(marker.bucket) == T0 + timedelta(hours=20)


# ── PostgreSQL: ON CONFLICT against the partial unique index ──────────────────


@pytest.fixture
def pg_session() -> Iterator[Session]:
    engine = _engine_or_skip()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.postgres
def test_release_sync_on_postgres_infers_the_partial_unique_index(pg_session: Session) -> None:
    """``ON CONFLICT (project_id, label) WHERE source = 'release'`` must match
    ``uq_chart_annotation_release_label`` on PostgreSQL, or the insert errors
    ("no unique or exclusion constraint matching") instead of doing nothing.
    SQLite is lenient about that inference, so only a real server proves it."""
    project = Project(id=uuid.uuid4(), name="Releases PG", slug=f"rel-pg-{uuid.uuid4().hex[:8]}")
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
        id=uuid.uuid4(), project_id=project.id, name="app", display_name="App", description=""
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="event_name=Login",
        description="",
        status="implemented",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name, app_version FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
        app_version_column="app_version",
    )
    pg_session.add_all([project, data_source])
    pg_session.flush()
    pg_session.add(event_type)
    pg_session.flush()
    pg_session.add_all([event, config])
    pg_session.commit()
    _seed_version_rows(pg_session, config.id, event_id=event.id)

    assert _sync_release_annotations(pg_session, config) == 1
    pg_session.commit()
    assert _sync_release_annotations(pg_session, config) == 0
    pg_session.commit()

    assert [row.label for row in _annotations(pg_session, project.id)] == ["Release 1.1.0"]


# ── demo ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_demo_shows_exactly_one_release_annotation() -> None:
    async with TestSessionLocal() as session:
        created = await create_demo_project(session)
        project = await session.scalar(select(Project).where(Project.slug == created.slug))
        assert project is not None
        releases = (
            (
                await session.execute(
                    select(ChartAnnotation).where(
                        ChartAnnotation.project_id == project.id,
                        ChartAnnotation.source == ChartAnnotationSource.release.value,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert [row.label for row in releases] == [release_annotation_label(DEMO_RELEASE_VERSION)]
    assert releases[0].scope_type is None
