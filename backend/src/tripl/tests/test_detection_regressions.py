"""Regression tests for detection.

Each test fails when the fix it names is reverted. One item is a documentation fix
(the release-regression volume-drop bar reuses the project's ``sigma_threshold``,
which the docs now say) and has no behaviour to pin.
"""

from __future__ import annotations

import importlib
import math
import sys
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters.base import AggregateSpec, ColumnInfo
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    SeriesPoint,
    _detect_trend_shift,
    _present_series,
    _rolling_anomaly_at,
    detect_anomalies,
    expand_series,
    forecast_next_buckets,
)
from tripl.models import Base
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event import Event
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.semver import compare_versions, latest_previous_versions, order_versions
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.test_fact_metric_batched_collector import (
    _make_single_metric,
    _seed_fact_table,
    _seed_project_and_ds,
)
from tripl.tests.test_metrics_tasks import _create_scan_config
from tripl.tests.test_paused_demo_collection import (
    _run_dispatcher,
    _seed_scan_config,
    _simulate_backfill_tick,
)
from tripl.worker.tasks.metrics import metric_collect
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.tasks.metrics.detect import (
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
    _collect_breakdown_scope_keys,
)
from tripl.worker.tasks.metrics.signals import _latest_anomaly_per_scope

_HOUR = timedelta(hours=1)
_START = datetime(2026, 7, 1, tzinfo=UTC)


def _bucket(hour: int) -> datetime:
    return _START + _HOUR * hour


# ── the forecast repeats the LATEST cycle ────────────────────


def test_forecast_uses_the_latest_seasonal_cycle_not_the_first() -> None:
    """A daily amplitude ramping 10 -> 100 around a level of 200.

    The next bucket is a daily peak whose true value is ~300 and whose
    same-hour value one day earlier is ~293. The first-cycle lookup
    (``future_index % period``) extrapolated the first day's tiny amplitude and
    forecast ~215.
    """
    length = 330

    def value(hour: int) -> float:
        amplitude = 10.0 + 90.0 * hour / (length - 1)
        return 200.0 + amplitude * math.cos(2 * math.pi * (hour - length) / 24)

    points = [SeriesPoint(bucket=_bucket(hour), count=value(hour)) for hour in range(length)]

    forecast = forecast_next_buckets(points, interval=_HOUR, horizon=1)

    assert len(forecast) == 1
    assert forecast[0].bucket == _bucket(length)
    assert forecast[0].expected_count > 260.0


# ── NaN / inf are "no data", never an anomaly ────────────────


_FRACTIONAL = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=4.0,
    min_expected_count=0.0,
)


def test_present_series_drops_non_finite_values() -> None:
    points = [
        SeriesPoint(bucket=_bucket(0), count=1.0),
        SeriesPoint(bucket=_bucket(1), count=math.nan),
        SeriesPoint(bucket=_bucket(2), count=math.inf),
        SeriesPoint(bucket=_bucket(3), count=-math.inf),
        SeriesPoint(bucket=_bucket(4), count=2.0),
    ]

    kept = _present_series(points, end_exclusive=_bucket(10))

    assert [point.bucket for point in kept] == [_bucket(0), _bucket(4)]


def test_expand_series_excludes_non_finite_buckets_instead_of_keeping_or_zero_filling() -> None:
    points = [
        SeriesPoint(bucket=_bucket(0), count=5.0),
        SeriesPoint(bucket=_bucket(1), count=math.inf),
        SeriesPoint(bucket=_bucket(2), count=5.0),
    ]

    expanded = expand_series(points, interval=_HOUR, end_exclusive=_bucket(3))

    assert [(point.bucket, point.count) for point in expanded] == [
        (_bucket(0), 5.0),
        (_bucket(2), 5.0),
    ]


def test_a_nan_z_score_is_not_an_anomaly() -> None:
    """``abs(nan) < sigma`` is False, so the old gate emitted a NaN-z row."""
    counts = [10.0] * 14 + [math.nan]

    anomaly = _rolling_anomaly_at(
        counts, 14, SeriesPoint(bucket=_bucket(14), count=math.nan), _FRACTIONAL
    )

    assert anomaly is None


def test_a_nan_bucket_in_a_fractional_series_emits_no_row() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=5.0 + hour % 3 / 10) for hour in range(30)]
    points[27] = SeriesPoint(bucket=_bucket(27), count=math.nan)

    anomalies = detect_anomalies(
        points,
        interval=_HOUR,
        evaluation_start=_bucket(25),
        evaluation_end=_bucket(30),
        settings=_FRACTIONAL,
        fill_gaps=False,
    ).anomalies

    assert all(math.isfinite(anomaly.z_score) for anomaly in anomalies)
    assert _bucket(27) not in {anomaly.bucket for anomaly in anomalies}


# ── dotted-numeric versions sort numerically ─────────────────


def test_dotted_numeric_versions_sort_numerically() -> None:
    assert order_versions(["15.10", "15.9"]) == ["15.9", "15.10"]
    assert order_versions(["15.8", "15.7.4"]) == ["15.7.4", "15.8"]
    assert order_versions(["1.2.4", "1.2.3.4", "1.2.3"]) == ["1.2.3", "1.2.3.4", "1.2.4"]
    # A SemVer prerelease still ranks below the bare release it precedes.
    assert compare_versions("15.8.0-rc.1", "15.8") == -1
    # Free text stays below every numbered version.
    assert order_versions(["15.8", "beta"]) == ["beta", "15.8"]


def test_a_two_part_marketing_release_is_the_latest() -> None:
    assert latest_previous_versions(["15.7.3", "15.7.4", "15.8"]) == ("15.8", "15.7.4")
    assert latest_previous_versions(["15.8", "15.7.4"]) == ("15.8", "15.7.4")


# ── trend rows are dated at the raw departure ────────────────


_TREND_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=4.0,
    min_expected_count=0,
)
_TREND_HOURS = 24 * 22  # three full hour-of-week cycles, so period 168 is selectable


def _trend_components(
    trend: list[float],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    return tuple(trend), tuple([0.0] * len(trend)), tuple([0.0] * len(trend))


def test_trend_row_is_anchored_where_the_raw_value_left_its_band() -> None:
    """The centred trend bends four hours before the raw step; the row must not.

    Anchored at the run's first TREND bucket the row was dated on a bucket whose
    raw value (100) sat exactly on its expectation (100).
    """
    run_start = _TREND_HOURS - 10
    departure = _TREND_HOURS - 6
    points = [
        SeriesPoint(bucket=_bucket(hour), count=50.0 if hour >= departure else 100.0)
        for hour in range(_TREND_HOURS)
    ]
    trend = [50.0 if hour >= run_start else 100.0 for hour in range(_TREND_HOURS)]

    result = _detect_trend_shift(
        points,
        _trend_components(trend),
        evaluation_start=_bucket(_TREND_HOURS - 24),
        settings=_TREND_SETTINGS,
        interval=_HOUR,
    )

    rows = [(row.bucket, row.direction) for row in result.anomalies]
    assert rows == [(_bucket(departure), "drop")]
    # The run owns its row, so every bucket of it is still one incident.
    run = frozenset(_bucket(hour) for hour in range(run_start, _TREND_HOURS))
    assert result.shifted_buckets == run


def test_a_run_whose_row_is_gated_out_does_not_silence_its_buckets() -> None:
    """The reconstructed expectation (100) is under ``min_expected_count`` (150),
    so no trend row is written — and the run must then claim no buckets, or every
    per-bucket row in it vanishes with nothing emitted in its place."""
    run_start = _TREND_HOURS - 10
    points = [
        SeriesPoint(bucket=_bucket(hour), count=200.0 if hour >= run_start else 100.0)
        for hour in range(_TREND_HOURS)
    ]
    trend = [200.0 if hour >= run_start else 100.0 for hour in range(_TREND_HOURS)]
    settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=4.0,
        min_expected_count=150,
    )

    result = _detect_trend_shift(
        points,
        _trend_components(trend),
        evaluation_start=_bucket(_TREND_HOURS - 24),
        settings=settings,
        interval=_HOUR,
    )

    assert result.anomalies == []
    assert result.shifted_buckets == frozenset()


# ── a resumed demo waits for the backfill tick ───────────────


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch14_detection.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def test_a_resumed_demo_is_not_collected_before_the_backfill_tick(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The dispatcher tick wins the race against ``advance_demos``.

    The demo was just opened (active), but its newest bucket is still frozen at
    pause start, eight hours back: a collection now would reach past the
    synthetic adapter's full-volume hours and rewrite them at sampled volume.
    Once the backfill has run, the same demo is dispatched.
    """
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        seeded = _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - timedelta(days=3),
            last_accessed=now,
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert dispatched == []
    assert result == {"checked": 1, "dispatched": 0}

    with sync_session_factory() as session:
        _simulate_backfill_tick(session, seeded.scan_config_id)
        session.commit()

    _result, dispatched_after = _run_dispatcher(sync_session_factory, monkeypatch)
    assert [config_id for config_id, _job in dispatched_after] == [str(seeded.scan_config_id)]


def test_a_resumed_demo_is_collected_when_the_demo_runtime_is_disabled(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """With no tick, nothing ever backfills; waiting for one stalls the demo."""
    monkeypatch.setattr(metrics_schedule.settings, "demo_runtime_enabled", False)
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        seeded = _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - timedelta(days=3),
            last_accessed=now,
        )

    _result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert [config_id for config_id, _job in dispatched] == [str(seeded.scan_config_id)]


def test_the_resume_gate_ignores_an_interval_whose_overlap_spans_the_full_volume(
    memory_session: Session,
) -> None:
    """A 4h demo resumes two buckets (8h) back, past the 6 full-volume hours.

    Every dispatch would reach that far however fresh the data is, so gating it
    would stop scheduled collection for good.
    """
    now = datetime.now(UTC)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    gate = metrics_schedule._demo_resume_window_exceeds_full_volume
    stale = current_hour - timedelta(days=1)

    assert gate(memory_session, uuid.uuid4(), last_bucket=stale, delta=_HOUR, now=now)
    assert not gate(
        memory_session, uuid.uuid4(), last_bucket=stale, delta=timedelta(hours=4), now=now
    )


def test_a_real_project_with_the_same_stale_history_still_dispatches(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        _seed_scan_config(session, now=now, is_demo=False)

    _result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert len(dispatched) == 1


# ── tripl doctor does not FAIL an idle demo ──────────────────


def _scan_checks() -> ModuleType:
    cli_src = Path(__file__).resolve().parents[4] / "cli" / "src"
    if not (cli_src / "tripl_cli").is_dir():
        pytest.skip("cli/ is not checked out next to the backend")
    if str(cli_src) not in sys.path:
        sys.path.insert(0, str(cli_src))
    return importlib.import_module("tripl_cli.diagnostics.scan_checks")


def _api_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _findings(*, is_demo: bool, idle: timedelta) -> list[tuple[str, str]]:
    scan_checks = _scan_checks()
    now = datetime.now(UTC)
    stamp = now - idle
    config = {
        "id": "scan-1",
        "name": "demo events",
        "interval": "1h",
        "time_column": "event_time",
        "data_source_id": "ds-1",
        "created_at": _api_time(now - timedelta(days=30)),
    }
    job = {
        "id": "job-1",
        "scan_config_id": "scan-1",
        "status": "completed",
        "error_message": None,
        "result_summary": {"mode": scan_checks.DISPATCHER_MODE, "time_to": stamp.isoformat()},
        "created_at": _api_time(stamp),
        "started_at": _api_time(stamp),
        "completed_at": _api_time(stamp),
        "updated_at": _api_time(stamp),
    }
    findings = scan_checks._config_findings(
        "demo", config, [job], None, 200, now, None, is_demo=is_demo
    )
    return [(finding.code, str(finding.severity)) for finding in findings]


def test_doctor_reports_nothing_for_a_long_idle_demo() -> None:
    assert _findings(is_demo=True, idle=timedelta(days=3)) == []


def test_doctor_still_fails_a_dead_scheduler_on_a_real_project() -> None:
    codes = [code for code, _severity in _findings(is_demo=False, idle=timedelta(days=3))]

    assert "scan_not_dispatched" in codes


# ── top-N is ranked once over the caller's whole window ──────


def _platform_rows() -> list[dict[str, object]]:
    """Week 1 ranks {a, c}; week 2 ranks {a, b}; the whole window ranks {a, b}."""
    rows: list[dict[str, object]] = []
    for platform, count in (("a", 10), ("c", 9), ("b", 1)):
        rows.extend(
            {"event_time": _START + timedelta(days=1, minutes=i), "platform": platform}
            for i in range(count)
        )
    for platform, count in (("a", 10), ("b", 9)):
        rows.extend(
            {"event_time": _START + timedelta(days=9, minutes=i), "platform": platform}
            for i in range(count)
        )
    return rows


def _explicit_values(
    adapter: SyntheticAdapter, chunk_from: datetime, chunk_to: datetime
) -> set[object]:
    _cols, _json, rows = adapter.get_time_bucketed_breakdown_counts(
        "SELECT * FROM events",
        "event_time",
        "1d",
        "platform",
        [],
        [],
        None,
        chunk_from,
        chunk_to,
        values_limit=3,
    )
    return {row[1] for row in rows if not row[2]}


def test_every_chunk_keeps_the_whole_windows_top_values(monkeypatch: MonkeyPatch) -> None:
    adapter = SyntheticAdapter(history_days=2)
    rows = _platform_rows()
    monkeypatch.setattr(adapter, "_scan_rows", lambda base_query, table: rows)
    week_one = (_START, _START + timedelta(days=7))
    week_two = (_START + timedelta(days=7), _START + timedelta(days=14))

    # Per chunk, 'c' was explicit in week 1 and folded into Other in week 2.
    assert _explicit_values(adapter, *week_one) == {"a", "c"}

    with adapter.top_n_ranking_window(_START, _START + timedelta(days=14)):
        assert _explicit_values(adapter, *week_one) == {"a", "b"}
        assert _explicit_values(adapter, *week_two) == {"a", "b"}

    # Outside the block the call's own window is ranked again.
    assert _explicit_values(adapter, *week_one) == {"a", "c"}


def test_sql_adapters_run_the_top_n_pre_query_once_over_the_whole_window() -> None:
    adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
    calls: list[tuple[datetime, datetime]] = []

    def _query(
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        calls.append((time_from, time_to))
        return {column: ["a"] for column in breakdown_columns}

    adapter._query_top_breakdown_values_multi = _query  # type: ignore[method-assign]
    whole = (_START, _START + timedelta(days=14))
    with adapter.top_n_ranking_window(*whole):
        for day in (0, 7):
            chunk_from = _START + timedelta(days=day)
            chunk_to = chunk_from + timedelta(days=7)
            ranked = adapter._top_breakdown_values_multi(
                "SELECT 1", "t", ["platform"], chunk_from, chunk_to, 2
            )
            assert ranked == {"platform": ["a"]}

    assert calls == [whole]


def test_a_nested_ranking_window_ranks_its_own_window_once_per_block() -> None:
    """The batched fact path re-enters the block per scan with its metric's window.

    Each window must be ranked on its own (not the outer window's set) and
    still only once across every chunk of the outer block.
    """
    adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
    calls: list[tuple[datetime, datetime]] = []

    def _query(
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        calls.append((time_from, time_to))
        return {column: [time_from.isoformat()] for column in breakdown_columns}

    adapter._query_top_breakdown_values_multi = _query  # type: ignore[method-assign]
    covering = (_START, _START + timedelta(days=3))
    first = (_START, _START + timedelta(days=2))
    second = (_START + timedelta(days=1), _START + timedelta(days=3))
    with adapter.top_n_ranking_window(*covering):
        for day in range(3):
            chunk_from = _START + timedelta(days=day)
            chunk_to = chunk_from + timedelta(days=1)
            for window in (first, second):
                with adapter.top_n_ranking_window(*window):
                    ranked = adapter._top_breakdown_values_multi(
                        "SELECT 1", "t", ["platform"], chunk_from, chunk_to, 2
                    )
                assert ranked == {"platform": [window[0].isoformat()]}

    assert calls == [first, second]


class _RankingRecorder:
    """Stands in for ``rank_top_n_once`` and remembers which window is open.

    A fake adapter reads :attr:`current` on every warehouse call, so a test sees
    the ranking window each chunk query actually ran under; deleting a chunk
    loop's ``with rank_top_n_once(...)`` leaves it ``None``.
    """

    def __init__(self) -> None:
        self._open: list[tuple[datetime, datetime]] = []

    @property
    def current(self) -> tuple[datetime, datetime] | None:
        return self._open[-1] if self._open else None

    def __call__(
        self, adapter: object, time_from: datetime, time_to: datetime
    ) -> AbstractContextManager[None]:
        @contextmanager
        def _block() -> Iterator[None]:
            self._open.append((time_from, time_to))
            try:
                yield
            finally:
                self._open.pop()

        return _block()


class _RecordingAdapter:
    """A warehouse with no rows that logs ``(method, chunk, ranking window)``."""

    def __init__(self, recorder: _RankingRecorder, columns: tuple[str, ...]) -> None:
        self._recorder = recorder
        self._columns = columns
        self.calls: list[tuple[str, datetime, datetime, tuple[datetime, datetime] | None]] = []

    def _record(self, method: str, time_from: datetime, time_to: datetime) -> None:
        self.calls.append((method, time_from, time_to, self._recorder.current))

    def ranked(self, method: str) -> list[tuple[datetime, datetime, object]]:
        """``(chunk_from, chunk_to, ranking window)`` per call, in chunk order."""
        return sorted(
            (
                (time_from, time_to, window)
                for name, time_from, time_to, window in self.calls
                if name == method
            ),
            key=lambda call: (call[0], call[1]),
        )

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [ColumnInfo(name=name, type_name="String") for name in self._columns]

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
        self._record("counts", time_from, time_to)
        return (list(regular_columns), [], [])

    def get_time_bucketed_aggregate(
        self, *args: object, **kwargs: object
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._record("aggregate", cast(datetime, args[8]), cast(datetime, args[9]))
        return ([], [], [])

    def get_time_bucketed_aggregate_breakdown(
        self, *args: object, **kwargs: object
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._record("aggregate_breakdown", cast(datetime, args[9]), cast(datetime, args[10]))
        return ([], [], [])

    def get_time_bucketed_multi_aggregate(
        self, *args: object, **kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        specs = cast(list[AggregateSpec], args[3])
        self._record("multi_aggregate", cast(datetime, args[4]), cast(datetime, args[5]))
        return (["bucket", *[spec.key for spec in specs]], [])

    def get_time_bucketed_multi_aggregate_breakdown(
        self, *args: object, **kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        specs = cast(list[AggregateSpec], args[4])
        self._record("multi_aggregate_breakdown", cast(datetime, args[5]), cast(datetime, args[6]))
        keys = [spec.key for spec in specs]
        return (["bucket", "breakdown_value", "is_other", *keys], [])

    def close(self) -> None:
        return None


def _hour(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=UTC)


def _patch_fact_collection(
    monkeypatch: MonkeyPatch,
    factory: sessionmaker[Session],
    adapter: _RecordingAdapter,
    recorder: _RankingRecorder,
) -> None:
    monkeypatch.setattr(metric_collect, "_get_sync_session", factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "rank_top_n_once", recorder)


def test_batched_fact_breakdowns_rank_over_each_metrics_own_window(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """Two metrics share a fact table, a breakdown column and a limit.

    Their windows differ, as when one of them lags. Each metric's breakdown must
    be ranked over its own window in every chunk, as the per-metric collector
    does, not over the group's covering window.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        metrics = [
            _make_single_metric(
                session,
                project,
                fact_table,
                aggregation=MetricAggregation.sum,
                config={"measure_column": "amount"},
                breakdown_columns=["country"],
                breakdown_values_limit=3,
                replay_chunk_interval="1h",
            )
            for _ in range(2)
        ]
        lagging, current = (metric.id for metric in metrics)
    windows = {lagging: (_hour(9), _hour(11)), current: (_hour(10), _hour(12))}

    recorder = _RankingRecorder()
    adapter = _RecordingAdapter(recorder, ("ts", "amount", "user_id", "country"))
    _patch_fact_collection(monkeypatch, sync_session_factory, adapter, recorder)
    monkeypatch.setattr(
        metric_collect,
        "_effective_value_window",
        lambda session, *, metric_definition_id, **kwargs: windows[metric_definition_id],
    )

    metric_collect.collect_fact_metrics_batch.run([str(lagging), str(current)])

    calls = adapter.ranked("multi_aggregate_breakdown")
    assert len(calls) == 4
    assert set(calls) == {
        (_hour(9), _hour(10), windows[lagging]),
        (_hour(10), _hour(11), windows[lagging]),
        (_hour(10), _hour(11), windows[current]),
        (_hour(11), _hour(12), windows[current]),
    }


def test_per_metric_fact_collectors_rank_every_chunk_over_the_whole_window(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The single and ratio oracles hold one ranking window across their chunks."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        single = _make_single_metric(
            session,
            project,
            fact_table,
            aggregation=MetricAggregation.sum,
            config={"measure_column": "amount"},
            breakdown_columns=["country"],
            breakdown_values_limit=3,
            replay_chunk_interval="1h",
        )
        ratio = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"fact-ratio-{uuid.uuid4().hex[:6]}",
            display_name="Conversion",
            kind=MetricKind.fact,
            composition=MetricComposition.ratio,
            aggregation=MetricAggregation.count,
            fact_table_id=fact_table.id,
            config={
                "numerator": {"fact_table_id": str(fact_table.id), "aggregation": "count"},
                "denominator": {
                    "fact_table_id": str(fact_table.id),
                    "aggregation": "count_distinct",
                    "distinct_column": "user_id",
                },
            },
            interval=ScanInterval.h1,
            status=MetricStatus.active,
            breakdown_columns=["country"],
            breakdown_values_limit=3,
            replay_chunk_interval="1h",
        )
        session.add(ratio)
        session.commit()
        single_id, ratio_id = single.id, ratio.id
    whole = (_hour(9), _hour(12))

    recorder = _RankingRecorder()
    adapter = _RecordingAdapter(recorder, ("ts", "amount", "user_id", "country"))
    _patch_fact_collection(monkeypatch, sync_session_factory, adapter, recorder)
    monkeypatch.setattr(metric_collect, "_resolve_value_window", lambda *a, **k: whole)

    metric_collect.collect_metric_definitions.run(str(single_id))
    assert adapter.ranked("aggregate_breakdown") == [
        (_hour(hour), _hour(hour + 1), whole) for hour in (9, 10, 11)
    ]

    metric_collect.collect_metric_definitions.run(str(ratio_id))
    assert adapter.ranked("multi_aggregate_breakdown") == [
        (_hour(hour), _hour(hour + 1), whole) for hour in (9, 10, 11)
    ]


def test_a_chunked_scan_collection_ranks_every_chunk_over_the_whole_window(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.replay_chunk_interval = "1h"
        job = ScanJob(id=uuid.uuid4(), scan_config_id=config.id, status=ScanJobStatus.pending.value)
        session.add(job)
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=config.event_type_id,
                name="event_name",
                display_name="Event name",
                field_type="string",
                is_required=False,
                description="",
            )
        )
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=config.project_id,
                event_type_id=config.event_type_id,
                name="event_name=Login",
                description="",
                status="implemented",
            )
        )
        session.commit()
        config_id, job_id = str(config.id), str(job.id)

    recorder = _RankingRecorder()
    adapter = _RecordingAdapter(recorder, ("time", "event_name"))
    monkeypatch.setattr(metrics_tasks, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metrics_tasks, "rank_top_n_once", recorder)
    monkeypatch.setattr(
        metrics_tasks,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 11), True),
    )
    monkeypatch.setattr(
        metrics_tasks,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics_tasks,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    metrics_tasks.collect_metrics.run(config_id, job_id)

    chunks = adapter.ranked("counts")
    assert len(chunks) == 3
    whole = (chunks[0][0], chunks[-1][1])
    assert [window for _from, _to, window in chunks] == [whole, whole, whole]


# ── scope discovery narrows and de-duplicates in SQL ───────────


@pytest.fixture
def memory_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _anomaly(scope_ref: str, bucket: datetime) -> MetricAnomaly:
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=None,
        scope_type=SCOPE_METRIC,
        scope_ref=scope_ref,
        bucket=bucket,
        actual_count=1.0,
        expected_count=2.0,
        stddev=0.5,
        z_score=-4.5,
        direction="drop",
    )


def test_latest_anomaly_per_scope_keeps_only_each_scopes_newest_row(
    memory_session: Session,
) -> None:
    rows = [
        _anomaly("metric-a", _bucket(1)),
        _anomaly("metric-a", _bucket(5)),
        _anomaly("metric-a", _bucket(3)),
        _anomaly("metric-b", _bucket(2)),
    ]
    memory_session.add_all(rows)
    memory_session.commit()

    statement = _latest_anomaly_per_scope(MetricAnomaly.scope_type == SCOPE_METRIC)
    latest = memory_session.execute(statement).scalars().all()

    found = sorted((row.scope_ref, row.bucket.replace(tzinfo=UTC)) for row in latest)
    assert found == [("metric-a", _bucket(5)), ("metric-b", _bucket(2))]


def test_breakdown_scope_keys_filter_the_column_in_sql(memory_session: Session) -> None:
    scan_config_id = uuid.uuid4()
    event_type_id = uuid.uuid4()
    for hour in range(3):
        for column, value in (("platform", "ios"), ("country", "de"), ("app_version", "1.0.0")):
            memory_session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=event_type_id,
                    bucket=_bucket(hour),
                    breakdown_column=column,
                    breakdown_value=value,
                    is_other=False,
                    count=10,
                )
            )
    memory_session.commit()
    window = {
        "scan_config_id": scan_config_id,
        "history_from": _bucket(0),
        "evaluation_start": _bucket(0),
        "evaluation_end": _bucket(10),
        "scope_type": SCOPE_PROJECT_TOTAL,
    }

    platform_only = _collect_breakdown_scope_keys(
        memory_session, breakdown_column="platform", **window
    )
    assert platform_only == {(None, None, "platform", "ios", False)}
    without_versions = _collect_breakdown_scope_keys(
        memory_session, app_version_column="app_version", **window
    )
    assert without_versions == {
        (None, None, "platform", "ios", False),
        (None, None, "country", "de", False),
    }
