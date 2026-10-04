"""Source freshness and scan lag (#269).

Covers the backend core seams:

* the pure helper ``services.source_freshness`` (status table, thresholds);
* the metrics worker recording ``last_event_at`` / ``last_collection_at``
  (a replay only moves the watermark when it reaches past it);
* the detector HOLDING new drop-direction volume anomalies while a source is
  late (a spike still fires, a stored drop survives, nothing is held when fresh);
* ``collect_metrics`` end to end: a delay is held, and the recovery run
  re-collects the delayed buckets so they are scored against the late rows;
* the demo runtime tick stamping freshness facts and honouring the hold;
* the API: ``GET /projects/{slug}/source-freshness`` and ``freshness`` on scans;

plus the issue's "done when": delaying the demo source yields ONE freshness
alert candidate and no new volume drop for its scopes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.anomaly_detector import settling_buckets_for
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.services.demo_service import create_demo_project
from tripl.services.source_freshness import (
    DEFAULT_SETTLING,
    advance_last_event_at,
    compute_freshness,
    evaluate_freshness,
    format_duration,
    is_holding,
    late_threshold,
    overdue_threshold,
)
from tripl.tests import test_demo_runtime as demo_runtime_tests
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_metrics_tasks import _create_scan_config

# Import through the package first so the task graph initialises in order (see
# test_demo_alert_sink.py): ``signals`` is reached via ``tasks.metrics``.
from tripl.worker.tasks import demo_runtime
from tripl.worker.tasks import metrics as _metrics_package  # noqa: F401
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics import signals as metrics_signals
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.tasks.metrics.freshness import record_collection_freshness

_HOUR = timedelta(hours=1)
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@dataclass
class _Subject:
    interval: str | None
    time_column: str | None
    last_event_at: datetime | None
    last_collection_at: datetime | None


# --------------------------------------------------------------------------- #
# Pure helper
# --------------------------------------------------------------------------- #


def test_thresholds() -> None:
    # min(3 x interval, (k + 2) x interval), k = settling_buckets_for(...).
    # Hourly on the default 2h settling: k = 2, min(3h, 4h) = 3h.
    assert late_threshold(_HOUR) == timedelta(hours=3)
    # A long settling window cannot push it past three intervals: min(3h, 8h).
    assert late_threshold(_HOUR, timedelta(hours=6)) == timedelta(hours=3)
    # No settling: the first missing bucket is scored as soon as it closes, so
    # late starts at 2 intervals (k = 0).
    assert late_threshold(_HOUR, timedelta(0)) == timedelta(hours=2)
    # Any positive allowance withholds at least one bucket (k rounds up).
    assert late_threshold(_HOUR, timedelta(minutes=30)) == timedelta(hours=3)
    # Daily: k = 1 on the 2h settling, min(3d, 3d); 2d without settling.
    assert late_threshold(timedelta(days=1)) == timedelta(days=3)
    assert late_threshold(timedelta(days=1), timedelta(0)) == timedelta(days=2)
    # A fine grid: 15m with 2h settling withholds 8 buckets, the cap wins.
    assert late_threshold(timedelta(minutes=15)) == timedelta(minutes=45)
    assert overdue_threshold(_HOUR) == timedelta(hours=2)
    assert timedelta(hours=2) == DEFAULT_SETTLING


def test_late_threshold_is_never_after_the_first_missing_bucket_emits() -> None:
    # ``last_event_at`` = start of bucket B. Bucket B + 1 closes at B + 2i and the
    # detector emits it ``k`` buckets later; late must start no later than that.
    for interval in (timedelta(minutes=15), _HOUR, timedelta(days=1)):
        for settling in (timedelta(0), timedelta(minutes=30), _HOUR * 2, _HOUR * 30):
            k = settling_buckets_for(interval, settling)
            assert late_threshold(interval, settling) <= (k + 2) * interval


@pytest.mark.parametrize(
    ("interval", "time_column", "event_age", "collection_age", "expected"),
    [
        # Manual scans (no interval) and scans with no time column: unknown.
        (None, "time", timedelta(hours=1), timedelta(minutes=5), "unknown"),
        ("1h", None, timedelta(hours=1), timedelta(minutes=5), "unknown"),
        # Nothing recorded yet.
        ("1h", "time", None, None, "unknown"),
        # Collected, but no event ever observed, and the scan is on time.
        ("1h", "time", None, timedelta(minutes=5), "unknown"),
        # Steady state: newest full bucket 1-2h old.
        ("1h", "time", timedelta(hours=2), timedelta(minutes=5), "fresh"),
        # Just under the allowance is fresh; reaching it is late (``>=``).
        ("1h", "time", timedelta(hours=2, minutes=59), timedelta(minutes=5), "fresh"),
        ("1h", "time", timedelta(hours=3), timedelta(minutes=5), "late"),
        ("1h", "time", timedelta(hours=7), timedelta(minutes=5), "late"),
        # The scan stopped running: overdue, and overdue wins over late.
        ("1h", "time", timedelta(hours=1), timedelta(hours=3), "overdue"),
        ("1h", "time", timedelta(hours=9), timedelta(hours=3), "overdue"),
        # Daily scans: a day-old newest bucket is fresh, four days is late.
        ("1d", "time", timedelta(days=1, hours=6), timedelta(hours=6), "fresh"),
        ("1d", "time", timedelta(days=4), timedelta(hours=6), "late"),
        ("1d", "time", timedelta(days=3), timedelta(hours=6), "late"),
        ("1d", "time", timedelta(days=1), timedelta(days=3), "overdue"),
    ],
)
def test_compute_freshness_status_table(
    interval: str | None,
    time_column: str | None,
    event_age: timedelta | None,
    collection_age: timedelta | None,
    expected: str,
) -> None:
    subject = _Subject(
        interval=interval,
        time_column=time_column,
        last_event_at=_NOW - event_age if event_age is not None else None,
        last_collection_at=_NOW - collection_age if collection_age is not None else None,
    )
    freshness = compute_freshness(subject, _NOW)
    assert freshness.status == expected
    assert is_holding(freshness) is (expected in {"late", "overdue"})


def test_compute_freshness_fields_and_naive_inputs() -> None:
    # SQLite hands back naive datetimes; they are read as UTC.
    subject = _Subject(
        interval="1h",
        time_column="time",
        last_event_at=(_NOW - timedelta(hours=7)).replace(tzinfo=None),
        last_collection_at=(_NOW - timedelta(minutes=5)).replace(tzinfo=None),
    )
    freshness = compute_freshness(subject, _NOW)
    assert freshness.status == "late"
    assert freshness.lag_seconds == 7 * 3600
    assert freshness.last_event_at == _NOW - timedelta(hours=7)
    assert freshness.last_event_at is not None and freshness.last_event_at.tzinfo is not None
    assert freshness.last_collection_at == _NOW - timedelta(minutes=5)
    assert freshness.expected_by == _NOW - timedelta(hours=4)


def test_project_settling_moves_the_late_allowance_within_the_cap() -> None:
    two_hours = _Subject("1h", "time", _NOW - timedelta(hours=2), _NOW - timedelta(minutes=5))
    # Default settling (2h): 3h allowance, a 2h-old newest event is fresh.
    assert compute_freshness(two_hours, _NOW).status == "fresh"
    # No settling: the detector would score the missing bucket already.
    assert compute_freshness(two_hours, _NOW, settling=timedelta(0)).status == "late"
    # A long settling window does not stretch past three intervals.
    three_hours = _Subject("1h", "time", _NOW - timedelta(hours=3), _NOW - timedelta(minutes=5))
    assert compute_freshness(three_hours, _NOW, settling=timedelta(hours=6)).status == "late"


@pytest.mark.parametrize(
    ("event_age", "expected"),
    [
        (timedelta(hours=1, minutes=59), "fresh"),
        (timedelta(hours=2), "late"),
        (timedelta(hours=5), "late"),
    ],
)
def test_zero_settling_status_table(event_age: timedelta, expected: str) -> None:
    freshness = evaluate_freshness(
        interval_code="1h",
        last_event_at=_NOW - event_age,
        last_collection_at=_NOW - timedelta(minutes=5),
        now=_NOW,
        settling=timedelta(0),
    )
    assert freshness.status == expected
    assert is_holding(freshness) is (expected == "late")
    assert freshness.expected_by == _NOW - event_age + timedelta(hours=2)


def test_evaluate_unscheduled_is_unknown_even_when_old() -> None:
    freshness = evaluate_freshness(
        interval_code="1h",
        last_event_at=_NOW - timedelta(days=9),
        last_collection_at=_NOW - timedelta(days=9),
        now=_NOW,
        scheduled=False,
    )
    assert freshness.status == "unknown"
    assert freshness.expected_by is None


def test_advance_last_event_at_never_rewinds() -> None:
    newer = _NOW
    older = _NOW - timedelta(hours=5)
    assert advance_last_event_at(None, older) == older
    assert advance_last_event_at(newer, older) == newer
    assert advance_last_event_at(older, newer) == newer
    assert advance_last_event_at(newer, None) == newer
    assert advance_last_event_at(None, None) is None


def test_format_duration() -> None:
    assert format_duration(timedelta(minutes=45)) == "45m"
    assert format_duration(timedelta(hours=7, minutes=20)) == "7h"
    assert format_duration(timedelta(days=3)) == "3d"


# --------------------------------------------------------------------------- #
# Worker: recording + holding (own sync SQLite engine)
# --------------------------------------------------------------------------- #

_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=48)
_HISTORY = 30


def _bucket(index: int) -> datetime:
    return _BASE + _HOUR * index


_EVAL_START = _bucket(_HISTORY - 4)
_EVAL_END = _bucket(_HISTORY)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'source_freshness.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed(session: Session) -> tuple[ScanConfig, EventType, EventType]:
    """One config, two event types: ``spiky`` spikes and ``droppy`` drops on the head."""
    project = Project(
        id=uuid.uuid4(),
        name="Freshness",
        slug=f"freshness-{uuid.uuid4().hex[:8]}",
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
        # Only the per-type scopes: the project total would sum the spike and
        # the drop into something less legible.
        detect_project_total=False,
        detect_events=False,
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=10,
    )
    session.add_all([project, data_source, config, settings])
    session.flush()
    spiky = EventType(
        id=uuid.uuid4(), project_id=project.id, name="spiky", display_name="Spiky", description=""
    )
    droppy = EventType(
        id=uuid.uuid4(), project_id=project.id, name="droppy", display_name="Droppy", description=""
    )
    session.add_all([spiky, droppy])
    session.flush()
    rows: list[EventMetric] = []
    for index in range(_HISTORY):
        head = index == _HISTORY - 1
        rows.append(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_type_id=spiky.id,
                bucket=_bucket(index),
                count=400 if head else 100 + index % 3,
            )
        )
        rows.append(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_type_id=droppy.id,
                bucket=_bucket(index),
                count=5 if head else 100 + index % 3,
            )
        )
    session.add_all(rows)
    session.commit()
    return config, spiky, droppy


def _anomalies(session: Session, config: ScanConfig) -> list[MetricAnomaly]:
    return list(
        session.execute(
            select(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id)
        ).scalars()
    )


def _recalculate(session: Session, config: ScanConfig, *, hold: bool) -> int:
    held: list[int] = []
    metrics_detect._recalculate_metric_anomalies(
        session,
        config,
        evaluation_start=_EVAL_START,
        evaluation_end=_EVAL_END,
        hold_drops=hold,
        held=held,
    )
    session.commit()
    return sum(held)


def test_recording_stamps_newest_bucket_and_collection_time(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _, _ = _seed(session)
        collected_at = datetime.now(UTC)
        freshness = record_collection_freshness(
            session,
            config,
            window_from=_bucket(0),
            window_to=_EVAL_END,
            collected_at=collected_at,
            is_replay=False,
        )
        session.commit()
        session.refresh(config)

        assert config.last_event_at == _bucket(_HISTORY - 1)
        assert config.last_collection_at == collected_at
        assert freshness.status == "late"  # the seeded head is ~19h old
        assert freshness.last_event_at == _bucket(_HISTORY - 1)


def test_recording_ignores_empty_buckets_and_never_rewinds(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _, _ = _seed(session)
        # The newest bucket arrives empty (a zero-count row): not an event.
        session.execute(
            update(EventMetric)
            .where(EventMetric.scan_config_id == config.id, EventMetric.bucket == _bucket(29))
            .values(count=0)
        )
        session.commit()
        record_collection_freshness(
            session,
            config,
            window_from=_bucket(0),
            window_to=_EVAL_END,
            collected_at=datetime.now(UTC),
            is_replay=False,
        )
        assert config.last_event_at == _bucket(28)

        # A replay of an older window cannot move it back, nor move the
        # collection clock (a replay says nothing about the schedule).
        before_collection = config.last_collection_at
        record_collection_freshness(
            session,
            config,
            window_from=_bucket(0),
            window_to=_bucket(10),
            collected_at=datetime.now(UTC) + timedelta(hours=1),
            is_replay=True,
        )
        assert config.last_event_at == _bucket(28)
        assert config.last_collection_at == before_collection


def test_replay_advances_the_watermark_only_past_it(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _, _ = _seed(session)
        config.last_event_at = _bucket(20)
        session.flush()
        # A backfill of data older than the watermark (even one whose rows reach
        # newer buckets) must not move it: that says nothing about the live feed.
        record_collection_freshness(
            session,
            config,
            window_from=_bucket(0),
            window_to=_bucket(20),
            collected_at=datetime.now(UTC),
            is_replay=True,
        )
        assert config.last_event_at == _bucket(20)
        # A replay reaching past the watermark is how a late source recovers.
        record_collection_freshness(
            session,
            config,
            window_from=_bucket(15),
            window_to=_EVAL_END,
            collected_at=datetime.now(UTC),
            is_replay=True,
        )
        assert config.last_event_at == _bucket(_HISTORY - 1)
        assert config.last_collection_at is None

        # No watermark yet: a replay leaves it for the first live collection.
        config.last_event_at = None
        record_collection_freshness(
            session,
            config,
            window_from=_bucket(0),
            window_to=_EVAL_END,
            collected_at=datetime.now(UTC),
            is_replay=True,
        )
        assert config.last_event_at is None


def test_without_hold_both_directions_fire(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, spiky, droppy = _seed(session)
        held = _recalculate(session, config, hold=False)

        by_type = {row.event_type_id: row.direction for row in _anomalies(session, config)}
        assert by_type == {spiky.id: "spike", droppy.id: "drop"}
        assert held == 0


def test_late_config_holds_drops_but_a_spike_still_fires(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, spiky, _ = _seed(session)
        held = _recalculate(session, config, hold=True)

        rows = _anomalies(session, config)
        assert [(row.event_type_id, row.direction) for row in rows] == [(spiky.id, "spike")]
        assert held == 1


def test_holding_keeps_drops_already_stored_and_releases_on_recovery(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _, droppy = _seed(session)
        # A drop recorded while the source was on time stands during the delay.
        _recalculate(session, config, hold=False)
        _recalculate(session, config, hold=True)
        drops = [row for row in _anomalies(session, config) if row.direction == "drop"]
        assert [row.event_type_id for row in drops] == [droppy.id]

        # Held, not lost: once fresh again the series is scored normally.
        session.execute(delete(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id))
        session.commit()
        _recalculate(session, config, hold=True)
        assert all(row.direction != "drop" for row in _anomalies(session, config))
        _recalculate(session, config, hold=False)
        assert any(row.direction == "drop" for row in _anomalies(session, config))


# --------------------------------------------------------------------------- #
# Worker: collect_metrics end to end (delay -> late -> recovery)
# --------------------------------------------------------------------------- #

# The collection clock: ``collect_metrics`` collects up to ``_CLOCK``. Pinned
# through the resolver's declared test seam (``_floor_to_interval``) so the
# window is deterministic; freshness itself reads the real clock, which sits
# within the hour after ``_CLOCK``.
_CLOCK = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
# Stored history ends here; the load is late for everything after it.
_LAST_ON_TIME = _CLOCK - timedelta(hours=8)
_HISTORY_HOURS = 40


def _clock_bucket(hours_before: int) -> datetime:
    return _CLOCK - _HOUR * hours_before


class _Warehouse:
    """Hourly ``Login`` counts the fake adapter serves, filtered by the window."""

    def __init__(self) -> None:
        self.counts: dict[datetime, int] = {}
        self.windows: list[tuple[datetime, datetime]] = []

    def land(self, first: datetime, last: datetime) -> None:
        bucket = first
        while bucket <= last:
            self.counts[bucket] = 100 + (bucket.hour % 3)
            bucket += _HOUR

    def rows(self, time_from: datetime, time_to: datetime) -> list[tuple[object, ...]]:
        start, end = to_utc(time_from), to_utc(time_to)
        self.windows.append((start, end))
        return [
            (bucket, "Login", count)
            for bucket, count in sorted(self.counts.items())
            if start <= bucket < end
        ]


def _seed_collect_scenario(session: Session) -> tuple[ScanConfig, Event]:
    config = _create_scan_config(session, with_event_type=True)
    assert config.event_type_id is not None
    session.add(
        ProjectAnomalySettings(
            project_id=config.project_id,
            anomaly_detection_enabled=True,
            detect_metrics=False,
            baseline_window_buckets=14,
            min_history_buckets=7,
            sigma_threshold=3.0,
            min_expected_count=10,
        )
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=config.event_type_id,
        name="event_name=Login",
        description="",
        status="implemented",
    )
    session.add(event)
    for hours_before in range(_HISTORY_HOURS, 7, -1):
        bucket = _clock_bucket(hours_before)
        count = 100 + (bucket.hour % 3)
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    count=count,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=config.event_type_id,
                    bucket=bucket,
                    count=count,
                ),
            ]
        )
    # The previous scheduled run saw the newest on-time bucket, minutes ago.
    config.last_event_at = _LAST_ON_TIME
    config.last_collection_at = datetime.now(UTC) - timedelta(minutes=10)
    session.commit()
    return config, event


def _drops_since(session: Session, config_id: uuid.UUID, since: datetime) -> list[MetricAnomaly]:
    return [
        row
        for row in session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == config_id,
                MetricAnomaly.direction == "drop",
            )
        ).scalars()
        if to_utc(row.bucket) >= since
    ]


def test_collect_metrics_holds_a_delay_and_rescores_it_against_the_late_rows(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sync_session_factory() as session:
        config, event = _seed_collect_scenario(session)
        config_id, event_id = config.id, event.id

    warehouse = _Warehouse()
    warehouse.land(_clock_bucket(_HISTORY_HOURS), _LAST_ON_TIME)

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
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
            return (["event_name"], [], warehouse.rows(time_from, time_to))

        def close(self) -> None:
            return None

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as inner:
            persisted = inner.get(Event, event_id)
            assert persisted is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted},
            )

    monkeypatch.setattr(metrics_tasks, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(metrics_tasks, "_floor_to_interval", lambda dt, delta: _CLOCK)
    monkeypatch.setattr(metrics_tasks, "analyze_cardinality", lambda *a, **k: object())
    monkeypatch.setattr(metrics_tasks, "generate_events", fake_generate_events)

    first_missing = _LAST_ON_TIME + _HOUR

    # ---- The delay: the scan runs, the newest seven hours are not there. ----
    delayed = metrics_tasks.collect_metrics.run(str(config_id))
    assert delayed["freshness_status"] == "late"
    held = delayed["signals_held"]
    assert isinstance(held, int) and held > 0  # the empty hours really read as drops
    with sync_session_factory() as session:
        assert _drops_since(session, config_id, first_missing) == []
        stored = session.get(ScanConfig, config_id)
        assert stored is not None
        assert to_utc(stored.last_event_at) == _LAST_ON_TIME

    # ---- Recovery: the late rows land, including buckets > 2 intervals old. ----
    warehouse.land(first_missing, _clock_bucket(1))
    warehouse.windows.clear()
    recovered = metrics_tasks.collect_metrics.run(str(config_id))
    assert recovered["freshness_status"] == "fresh"
    assert recovered["signals_held"] == 0
    # The run reached back to the first bucket of the delay, not just the usual
    # two-bucket resume overlap, so every delayed hour was re-collected...
    assert min(start for start, _ in warehouse.windows) == first_missing
    with sync_session_factory() as session:
        stored = session.get(ScanConfig, config_id)
        assert stored is not None
        assert to_utc(stored.last_event_at) == _clock_bucket(1)
        landed = {
            to_utc(bucket)
            for bucket in session.execute(
                select(EventMetric.bucket).where(
                    EventMetric.scan_config_id == config_id,
                    EventMetric.event_id == event_id,
                    EventMetric.bucket >= first_missing,
                    EventMetric.count > 0,
                )
            ).scalars()
        }
        assert landed == {first_missing + _HOUR * i for i in range(7)}
        # ...and scored against the late rows: no drop for any of them.
        assert _drops_since(session, config_id, first_missing) == []


def test_widening_is_capped_and_only_applies_while_holding(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _ = _seed_collect_scenario(session)
        time_to = _CLOCK
        usual_from = _CLOCK - _HOUR * 2
        widen = metrics_tasks._widen_for_held_buckets

        # Late: back to the first bucket after the previous newest event.
        assert widen(session, config, delta=_HOUR, time_from=usual_from, time_to=time_to) == (
            _LAST_ON_TIME + _HOUR
        )
        # Never earlier than the first-run backfill cap.
        config.last_event_at = _CLOCK - timedelta(days=30)
        cap = time_to - _HOUR * metrics_tasks.SCHEDULED_BACKFILL_BUCKETS
        assert widen(session, config, delta=_HOUR, time_from=usual_from, time_to=time_to) == cap
        # Never narrows a window the resolver already opened wider.
        wide_from = cap - _HOUR * 5
        assert widen(session, config, delta=_HOUR, time_from=wide_from, time_to=time_to) == (
            wide_from
        )
        # Fresh or never collected: untouched.
        config.last_event_at = _CLOCK - _HOUR
        assert widen(session, config, delta=_HOUR, time_from=usual_from, time_to=time_to) == (
            usual_from
        )
        config.last_event_at = None
        assert widen(session, config, delta=_HOUR, time_from=usual_from, time_to=time_to) == (
            usual_from
        )


# --------------------------------------------------------------------------- #
# Demo runtime tick
# --------------------------------------------------------------------------- #


def test_demo_tick_stamps_freshness_facts(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_now = demo_runtime_tests._floor(datetime.now(UTC)) - timedelta(days=1)
    with sync_session_factory() as session:
        seeded = demo_runtime_tests._seed_demo(session, seed_now=seed_now, history_hours=72)
        stale = session.get(ScanConfig, seeded.scan_config_id)
        assert stale is not None
        stale.last_event_at = seed_now - timedelta(hours=1)
        stale.last_collection_at = seed_now
        session.commit()

    # Inside the idle-pause window, so the tick advances the demo.
    tick = seed_now + timedelta(hours=3)
    result = demo_runtime_tests._run_tick(sync_session_factory, monkeypatch, tick)
    assert result["advanced"] == 1

    with sync_session_factory() as session:
        cfg = session.get(ScanConfig, seeded.scan_config_id)
        assert cfg is not None
        assert to_utc(cfg.last_event_at) == demo_runtime_tests._floor(tick) - _HOUR
        assert to_utc(cfg.last_collection_at) == tick
        assert compute_freshness(cfg, tick).status == "fresh"


def test_demo_recompute_respects_the_hold(
    sync_session_factory: sessionmaker[Session],
) -> None:
    seed_now = demo_runtime_tests._floor(datetime.now(UTC))
    with sync_session_factory() as session:
        seeded = demo_runtime_tests._seed_demo(session, seed_now=seed_now, history_hours=72)
        # The newest six hours of one series never landed: they read as drops.
        session.execute(
            update(EventMetric)
            .where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.bucket >= seed_now - timedelta(hours=6),
            )
            .values(count=1)
        )
        session.commit()

        demo_runtime._recompute_anomalies(
            session, seeded.project_id, seeded.scan_config_id, seed_now, hold_drops=True
        )
        session.flush()
        held_drops = session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == seeded.scan_config_id,
                MetricAnomaly.direction == "drop",
            )
        ).all()
        assert held_drops == []

        demo_runtime._recompute_anomalies(
            session, seeded.project_id, seeded.scan_config_id, seed_now, hold_drops=False
        )
        session.flush()
        released = session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == seeded.scan_config_id,
                MetricAnomaly.direction == "drop",
            )
        ).all()
        assert released != []


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


async def test_source_freshness_route_and_scan_responses(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Fresh API", "slug": "fresh-api", "description": ""},
    )
    assert resp.status_code == 201
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Fresh CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    data_source_id = resp.json()["id"]

    created: dict[str, str] = {}
    for name, interval in (("Hourly", "1h"), ("Manual", None)):
        payload: dict[str, object] = {
            "data_source_id": data_source_id,
            "name": name,
            "base_query": "SELECT * FROM events",
            "time_column": "event_time",
        }
        if interval is not None:
            payload["interval"] = interval
        resp = await client.post("/api/v1/projects/fresh-api/scans", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Nothing collected yet.
        assert body["freshness"]["status"] == "unknown"
        assert body["last_event_at"] is None
        created[name] = body["id"]

    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        await session.execute(
            update(ScanConfig)
            .where(ScanConfig.id == uuid.UUID(created["Hourly"]))
            .values(
                last_event_at=now - timedelta(hours=7),
                last_collection_at=now - timedelta(minutes=5),
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/fresh-api/source-freshness")
    assert resp.status_code == 200
    items = {item["name"]: item for item in resp.json()}
    assert set(items) == {"Hourly", "Manual"}
    hourly = items["Hourly"]
    assert hourly["id"] == created["Hourly"]
    assert hourly["data_source_id"] == data_source_id
    assert hourly["freshness"]["status"] == "late"
    assert 7 * 3600 <= hourly["freshness"]["lag_seconds"] < 7 * 3600 + 120
    assert hourly["freshness"]["expected_by"] is not None
    assert items["Manual"]["freshness"]["status"] == "unknown"

    resp = await client.get("/api/v1/projects/fresh-api/scans")
    assert resp.status_code == 200
    listed = {row["name"]: row for row in resp.json()}
    assert listed["Hourly"]["freshness"]["status"] == "late"
    assert listed["Hourly"]["last_event_at"] is not None

    resp = await client.get(f"/api/v1/projects/fresh-api/scans/{created['Hourly']}")
    assert resp.status_code == 200
    assert resp.json()["freshness"]["status"] == "late"

    resp = await client.patch(
        f"/api/v1/projects/fresh-api/scans/{created['Hourly']}", json={"name": "Hourly 2"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["freshness"]["status"] == "late"

    resp = await client.get("/api/v1/projects/no-such-project/source-freshness")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Done when: delaying the demo source
# --------------------------------------------------------------------------- #

# How long the simulated warehouse load is late, in hourly buckets.
_DELAY_BUCKETS = 7


async def test_demo_source_is_fresh_by_default_and_a_delay_raises_one_alert() -> None:
    async with TestSessionLocal() as session:
        demo = await create_demo_project(session)
        config = (
            await session.execute(select(ScanConfig).where(ScanConfig.project_id == demo.id))
        ).scalar_one()
        config_id = config.id

    def _delay_and_detect(sync_session: Session) -> dict[str, object]:
        now = datetime.now(UTC)
        cfg = sync_session.get(ScanConfig, config_id)
        assert cfg is not None
        assert compute_freshness(cfg, now).status == "fresh"
        assert metrics_signals._get_source_freshness_candidates(sync_session, cfg, now) == {}

        # The warehouse load is late: the newest ``_DELAY_BUCKETS`` hours have
        # not landed. The scan still runs and finds them empty.
        assert cfg.last_event_at is not None
        newest = cfg.last_event_at
        delay_from = newest - _HOUR * (_DELAY_BUCKETS - 1)
        sync_session.execute(
            delete(EventMetric).where(
                EventMetric.scan_config_id == config_id,
                EventMetric.bucket >= delay_from,
            )
        )
        cfg.last_event_at = delay_from - _HOUR
        sync_session.flush()
        drops_before = set(
            sync_session.execute(
                select(MetricAnomaly.id).where(
                    MetricAnomaly.scan_config_id == config_id,
                    MetricAnomaly.direction == "drop",
                    MetricAnomaly.bucket >= delay_from,
                )
            ).scalars()
        )

        freshness = record_collection_freshness(
            sync_session,
            cfg,
            window_from=delay_from - _HOUR * 2,
            window_to=newest + _HOUR,
            collected_at=now,
            is_replay=False,
        )
        assert freshness.status == "late"
        held: list[int] = []
        metrics_detect._recalculate_metric_anomalies(
            sync_session,
            cfg,
            evaluation_start=delay_from,
            evaluation_end=newest + _HOUR,
            hold_drops=is_holding(freshness),
            held=held,
        )
        sync_session.flush()

        drops_after = set(
            sync_session.execute(
                select(MetricAnomaly.id).where(
                    MetricAnomaly.scan_config_id == config_id,
                    MetricAnomaly.direction == "drop",
                    MetricAnomaly.bucket >= delay_from,
                )
            ).scalars()
        )
        volume_candidates = metrics_signals._get_latest_active_anomalies(sync_session, cfg)
        freshness_candidates = metrics_signals._get_source_freshness_candidates(
            sync_session, cfg, now
        )
        return {
            "delay_from": delay_from,
            "held": sum(held),
            "new_drops": drops_after - drops_before,
            "volume_drop_candidates": [
                anomaly
                for anomaly in volume_candidates.values()
                if anomaly.direction == "drop"
                and anomaly.scope_type in {"project_total", "event_type", "event"}
                and anomaly.bucket >= delay_from
            ],
            "freshness_candidates": freshness_candidates,
        }

    async with TestSessionLocal() as session:
        outcome = await session.run_sync(_delay_and_detect)
        await session.rollback()

    # No new volume drop for any of the demo scan's scopes...
    assert outcome["new_drops"] == set()
    assert outcome["volume_drop_candidates"] == []
    # ...because they were held (the delay really did read as drops)...
    assert isinstance(outcome["held"], int) and outcome["held"] > 0
    # ...and exactly ONE freshness candidate stands in for them.
    candidates = outcome["freshness_candidates"]
    assert isinstance(candidates, dict)
    assert len(candidates) == 1
    ((scope_type, scope_ref),) = candidates
    assert scope_type == "source_freshness"
    assert scope_ref == str(config_id)
    (candidate,) = candidates.values()
    assert candidate.direction == "drop"
    assert candidate.sample_value is not None
    assert "newest event" in candidate.sample_value
