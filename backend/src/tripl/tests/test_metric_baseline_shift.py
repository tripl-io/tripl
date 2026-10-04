"""Outside the band == flagged, even where one row reports many buckets.

The detector reports a baseline for every bucket it scores, but
two passes drop per-bucket rows after scoring: a bucket inside a reported trend
shift (``shifted_buckets``) loses its row to the single trend row, and an
outage's run of zeros is collapsed to one announcement
(``_collapse_outage_runs``). Those buckets sit outside their band with no row,
so the chart would draw an unflagged point outside the band. Their baselines
are withheld instead; the representative flagged bucket keeps its own.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from tripl.core.analyzers import anomaly_detector
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    BaselinePoint,
    DetectedAnomaly,
    DetectionResult,
    SeriesPoint,
    _drawable_baselines,
    detect_anomalies,
)

_HOUR = timedelta(hours=1)
_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)
_DEATH_HOUR = 24 * 28


def _bucket(hour: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hour)


def _business_hours_count(hour: int) -> float:
    return 60.0 if 9 <= hour % 24 < 18 else 0.0


def _dying_series(horizon: int) -> list[SeriesPoint]:
    return [
        SeriesPoint(
            bucket=_bucket(hour),
            count=0.0 if hour >= _DEATH_HOUR else _business_hours_count(hour),
        )
        for hour in range(horizon)
    ]


def _detect(points: list[SeriesPoint], *, start: int, end: int) -> DetectionResult:
    return detect_anomalies(
        points,
        interval=_HOUR,
        evaluation_start=_bucket(start),
        evaluation_end=_bucket(end),
        settings=_SETTINGS,
    )


def _unflagged_outside_band(
    result: DetectionResult, points: Sequence[SeriesPoint]
) -> list[datetime]:
    """Buckets drawn outside their band that carry no anomaly row."""
    actual = {point.bucket: point.count for point in points}
    flagged = {anomaly.bucket for anomaly in result.anomalies}
    return [
        baseline.bucket
        for baseline in result.baselines
        if baseline.bucket not in flagged
        and abs(actual[baseline.bucket] - baseline.expected_count)
        >= _SETTINGS.sigma_threshold * baseline.effective_stddev
    ]


def _without_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anomaly_detector,
        "_drawable_baselines",
        lambda baselines, _primary, _emitted: tuple(baselines),
    )


def test_an_outage_draws_no_band_on_the_buckets_its_announcement_folded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    horizon = _DEATH_HOUR + 24 * 3
    points = _dying_series(horizon)

    result = _detect(points, start=_DEATH_HOUR, end=horizon)

    # One announcement, and it keeps its own band.
    assert len(result.anomalies) == 1
    announced = result.anomalies[0].bucket
    if result.anomalies[0].kind != "trend":
        assert announced in {baseline.bucket for baseline in result.baselines}
    assert _unflagged_outside_band(result, points) == []

    # The filter is what holds the invariant: without it the folded dead
    # working hours are drawn outside the band with no row.
    with monkeypatch.context() as patched:
        _without_the_filter(patched)
        unfiltered = _detect(points, start=_DEATH_HOUR, end=horizon)
    folded = _unflagged_outside_band(unfiltered, points)
    assert folded
    assert announced not in folded
    kept = {baseline.bucket for baseline in result.baselines}
    assert kept == {baseline.bucket for baseline in unfiltered.baselines} - set(folded)


def _baseline(hour: int) -> BaselinePoint:
    return BaselinePoint(bucket=_bucket(hour), expected_count=100.0, effective_stddev=5.0)


def _anomaly(hour: int, *, kind: str = "phase") -> DetectedAnomaly:
    return DetectedAnomaly(
        bucket=_bucket(hour),
        actual_count=0.0,
        expected_count=100.0,
        stddev=5.0,
        z_score=-20.0,
        direction="drop",
        effective_stddev=5.0,
        kind=kind,
    )


def test_drawable_baselines_withholds_only_the_silenced_buckets() -> None:
    baselines = [_baseline(hour) for hour in range(5)]
    # Hours 1-3 were flagged per bucket; hour 1 survived as the representative
    # row and hour 2 was replaced by a trend row at the same bucket. Hour 3 was
    # folded away. Hours 0 and 4 were never flagged.
    primary = [_anomaly(1), _anomaly(2), _anomaly(3)]
    emitted = [_anomaly(1), _anomaly(2, kind="trend")]

    drawable = _drawable_baselines(baselines, primary, emitted)

    assert [point.bucket for point in drawable] == [_bucket(hour) for hour in (0, 1, 2, 4)]

    points = [
        SeriesPoint(bucket=_bucket(hour), count=0.0 if hour in (1, 2, 3) else 100.0)
        for hour in range(5)
    ]
    assert _unflagged_outside_band(
        DetectionResult(anomalies=emitted, baselines=baselines), points
    ) == [_bucket(3)]
    assert (
        _unflagged_outside_band(DetectionResult(anomalies=emitted, baselines=drawable), points)
        == []
    )


def test_drawable_baselines_is_a_no_op_when_nothing_was_dropped() -> None:
    baselines = [_baseline(hour) for hour in range(3)]
    primary = [_anomaly(1)]

    assert _drawable_baselines(baselines, primary, primary) == tuple(baselines)
