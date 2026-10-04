from datetime import UTC, datetime, timedelta
from math import isclose, pi, sin, sqrt

import pytest

from tripl.core.analyzers import anomaly_detector
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    DetectedAnomaly,
    DetectionResult,
    SeriesPoint,
    _detect_trend_shift,
    _fit_components,
    _phase_anomaly_at,
    _phase_level_window,
    _select_phase_period,
    detect_anomalies,
    expand_series,
    forecast_next_buckets,
    required_history_buckets,
    settling_buckets_for,
)


def _bucket(hour: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hour)


def _daily_pattern_count(hour: int) -> int:
    hour_of_day = hour % 24
    if 9 <= hour_of_day < 12:
        return 60
    if 18 <= hour_of_day < 20:
        return 35
    return 12


SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)


def test_detect_anomalies_returns_empty_for_stable_series() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(7),
        evaluation_end=_bucket(10),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_detects_spike_and_drop() -> None:
    spike_points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    spike_points.append(SeriesPoint(bucket=_bucket(10), count=40))
    spike_anomalies = detect_anomalies(
        spike_points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies

    drop_points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    drop_points.append(SeriesPoint(bucket=_bucket(10), count=0))
    drop_anomalies = detect_anomalies(
        drop_points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies

    assert [anomaly.direction for anomaly in spike_anomalies] == ["spike"]
    assert [anomaly.direction for anomaly in drop_anomalies] == ["drop"]
    assert spike_anomalies[0].bucket == _bucket(10)
    assert drop_anomalies[0].bucket == _bucket(10)


def test_detect_anomalies_uses_effective_stddev_for_flat_baseline() -> None:
    """A real spike on a flat count baseline is scored against the Poisson-aware
    effective_stddev (sqrt(expected)), not the raw stddev of 0. The floored
    denominator is surfaced on the anomaly (contract C1)."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(8)]
    points.append(SeriesPoint(bucket=_bucket(8), count=30))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(8),
        evaluation_end=_bucket(9),
        settings=SETTINGS,
    ).anomalies

    assert len(anomalies) == 1
    assert anomalies[0].stddev == 0  # raw scale of a perfectly flat baseline
    # Poisson floor: sqrt(10) dominates the 1.0 absolute and 0.3 relative floors.
    assert isclose(anomalies[0].effective_stddev, sqrt(10), rel_tol=1e-9)
    assert isclose(anomalies[0].z_score, 20 / sqrt(10), rel_tol=1e-9)
    assert anomalies[0].kind == "rolling"
    assert anomalies[0].direction == "spike"


def test_detect_anomalies_poisson_floor_ignores_low_volume_noise() -> None:
    """a flat 10 +/- 0 count baseline must NOT flag a 10 -> 14
    move. sqrt(10) ~= 3.16, so a 3-sigma bar needs ~9-10 of deviation; +4 is
    Poisson noise, not an anomaly. The old fixed 1.0 floor scored it z=4."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=14))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_detects_fractional_spike_without_zero_fill() -> None:
    """A sub-unit ratio movement (0.5 -> 0.9) must be detectable: fractional
    series swap the 1.0 absolute stddev floor for a magnitude-derived one and
    keep their float values instead of rounding toward 0."""
    fractional_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=0,  # the fractional path always zeroes this gate
    )
    points = [SeriesPoint(bucket=_bucket(hour), count=0.5) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=0.9))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=fractional_settings,
        fill_gaps=False,
    ).anomalies

    assert [anomaly.direction for anomaly in anomalies] == ["spike"]
    assert anomalies[0].actual_count == 0.9
    assert anomalies[0].expected_count == 0.5


def test_detect_anomalies_fractional_noise_stays_quiet() -> None:
    """Micro-wobble below the magnitude-derived floor must not fire."""
    fractional_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=0,
    )
    points = [SeriesPoint(bucket=_bucket(hour), count=0.5) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=0.508))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=fractional_settings,
        fill_gaps=False,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_respects_min_history_gate() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(6)]
    points.append(SeriesPoint(bucket=_bucket(6), count=40))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(6),
        evaluation_end=_bucket(7),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_respects_min_expected_count_gate() -> None:
    low_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=20,
    )
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(8)]
    points.append(SeriesPoint(bucket=_bucket(8), count=0))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(8),
        evaluation_end=_bucket(9),
        settings=low_settings,
    ).anomalies

    assert anomalies == []


def test_sub_threshold_series_early_exits_without_stl_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a count series whose every bucket sits below
    ``min_expected_count`` can never emit (all paths are gated on it), so the
    detector must return [] BEFORE paying for the robust STL/MSTL fit — on real
    projects with hundreds of low-volume events that fit was ~1s per scope per
    scan. The series is long enough to have a phase period, so without the
    early exit ``_fit_components`` WOULD be called."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("_fit_components must not run for a provably-silent series")

    monkeypatch.setattr(anomaly_detector, "_fit_components", _explode)

    high_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=50,
    )
    # 400 hourly buckets of 1-8 events with a relative "spike" inside the
    # evaluation window — max(counts)=8, well under the 2x-headroom skip bound
    # (2*8=16 < 50).
    points = [SeriesPoint(bucket=_bucket(hour), count=1 + hour % 5) for hour in range(399)]
    points.append(SeriesPoint(bucket=_bucket(399), count=8))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(399),
        evaluation_end=_bucket(400),
        settings=high_settings,
    ).anomalies

    assert anomalies == []


def test_max_count_at_threshold_is_not_early_exited() -> None:
    """A series whose max equals the threshold is far inside the 2x-headroom
    skip bound (2*10=20 >= 10), so full detection runs and the boundary
    drop-to-zero still flags."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=0))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,  # min_expected_count=10 == max(counts)
    ).anomalies

    assert [anomaly.direction for anomaly in anomalies] == ["drop"]


def test_headroom_preserves_trend_shift_above_max_counts() -> None:
    """Adversarial-review counterexample: the deseasonalized STL
    trend is NOT bounded by max(counts). 10 days of day/night seasonality (45
    by day, 1 by night) followed by 2 days pinned flat at 45 — the troughs
    vanish, a genuine sustained level shift. The deseasonalized trend rises to
    ~54 while no observed count exceeds 45, so with min_expected_count=50 a
    naive max(counts)-based skip would silence a real trend signal. The 2x
    headroom keeps this series on the full path (2*45=90 >= 50).

    The observable is the trend detector RECOGNIZING the shift, not a persisted
    row: every per-bucket expectation on this series is under 45, and since
    the volume-gate fix the trend path applies the project's volume gate to the value
    it reports — exactly as the phase and rolling paths always have. What
    the gate must never do is skip the series before the detector sees it."""
    points = []
    for hour in range(12 * 24):
        is_flat_tail = hour >= 10 * 24
        is_daytime = hour % 24 < 12
        count = 45 if (is_flat_tail or is_daytime) else 1
        points.append(SeriesPoint(bucket=_bucket(hour), count=count))

    high_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=50,
    )
    assert not anomaly_detector.is_provably_silent(45, high_settings.min_expected_count)

    expanded = expand_series(points, interval=timedelta(hours=1), end_exclusive=_bucket(12 * 24))
    components = _fit_components([point.count for point in expanded], interval=timedelta(hours=1))
    assert components is not None
    result = _detect_trend_shift(
        expanded,
        components,
        evaluation_start=_bucket(10 * 24),
        settings=high_settings,
        interval=timedelta(hours=1),
    )

    assert result.shifted_buckets  # the deseasonalized trend shift is still seen


def test_detect_anomalies_zero_fills_gaps_after_first_seen_bucket() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(8)]

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(8),
        evaluation_end=_bucket(9),
        settings=SETTINGS,
    ).anomalies

    assert len(anomalies) == 1
    assert anomalies[0].bucket == _bucket(8)
    assert anomalies[0].actual_count == 0
    assert anomalies[0].direction == "drop"


def test_detect_anomalies_respects_repeating_daily_pattern_with_stl() -> None:
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_daily_pattern_count(hour))
        for hour in range(24 * 10)
    ]

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(24 * 10 - 1),
        evaluation_end=_bucket(24 * 10),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_detects_spike_on_top_of_repeating_daily_pattern() -> None:
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_daily_pattern_count(hour))
        for hour in range(24 * 10)
    ]
    anomaly_hour = 24 * 10 - 15  # 09:00 in the last day
    points[anomaly_hour] = SeriesPoint(bucket=_bucket(anomaly_hour), count=160)

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(anomaly_hour),
        evaluation_end=_bucket(24 * 10),
        settings=SETTINGS,
    ).anomalies

    spike_anomaly = next(
        anomaly for anomaly in anomalies if anomaly.bucket == _bucket(anomaly_hour)
    )
    assert spike_anomaly.direction == "spike"
    assert spike_anomaly.expected_count > 30


def test_detect_anomalies_skips_micro_deviation_on_high_volume_flat_baseline() -> None:
    """Bug fix: a 1% wobble on a 1000-count baseline should not be a 10-sigma
    anomaly. The relative stddev floor (3% of expected) clamps the z-score so
    only meaningful relative changes trip the threshold.
    """
    points = [SeriesPoint(bucket=_bucket(hour), count=1000) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=1010))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_detect_anomalies_still_flags_meaningful_high_volume_change() -> None:
    """Sanity check that the relative floor doesn't suppress real spikes on
    high-volume series — a 30% jump on a 1000-baseline still trips."""
    points = [SeriesPoint(bucket=_bucket(hour), count=1000) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=1300))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies

    assert len(anomalies) == 1
    assert anomalies[0].direction == "spike"


def test_detect_anomalies_detects_sustained_growth_on_top_of_daily_pattern() -> None:
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_daily_pattern_count(hour))
        for hour in range(24 * 10)
    ]
    growth_hours = [24 * 10 - 15, 24 * 10 - 14, 24 * 10 - 13]  # 09:00, 10:00, 11:00
    for hour in growth_hours:
        points[hour] = SeriesPoint(bucket=_bucket(hour), count=_daily_pattern_count(hour) + 35)

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(24 * 10 - 24),
        evaluation_end=_bucket(24 * 10),
        settings=SETTINGS,
    ).anomalies

    anomaly_buckets = {anomaly.bucket for anomaly in anomalies}
    assert _bucket(growth_hours[-1]) in anomaly_buckets
    sustained_anomaly = next(
        anomaly for anomaly in anomalies if anomaly.bucket == _bucket(growth_hours[-1])
    )
    assert sustained_anomaly.direction == "spike"
    assert sustained_anomaly.actual_count == _daily_pattern_count(growth_hours[-1]) + 35


def test_forecast_next_buckets_returns_empty_without_enough_history() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(5)]

    assert forecast_next_buckets(points, interval=timedelta(hours=1), horizon=1) == []


def test_forecast_next_buckets_predicts_seasonal_pattern() -> None:
    # Two full weeks of an hourly daily-shape series; the next bucket should
    # follow the same hour-of-day pattern.
    hours = 24 * 14
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_daily_pattern_count(hour)) for hour in range(hours)
    ]

    forecast = forecast_next_buckets(points, interval=timedelta(hours=1), horizon=3)

    assert len(forecast) == 3
    next_hours = [hours, hours + 1, hours + 2]
    for prediction, hour in zip(forecast, next_hours, strict=True):
        assert prediction.bucket == _bucket(hour)
        # Within 25% of the historical value at the matching hour-of-day —
        # STL trend wobble can pull the absolute value a bit either way.
        target = _daily_pattern_count(hour)
        assert abs(prediction.expected_count - target) <= max(target * 0.25, 5)
        assert prediction.stddev >= 0


def test_forecast_next_buckets_handles_zero_horizon() -> None:
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(50)]
    assert forecast_next_buckets(points, interval=timedelta(hours=1), horizon=0) == []


def _weekly_pattern_count(hour: int) -> int:
    """Deterministic hourly series with a daily shape and a weekend dip — the
    kind of strongly seasonal volume that made the old detector fire every
    cycle at the same phase."""
    hour_of_day = hour % 24
    day_of_week = (hour // 24) % 7
    if 9 <= hour_of_day < 12:
        level = 600
    elif 12 <= hour_of_day < 18:
        level = 450
    elif 18 <= hour_of_day < 22:
        level = 300
    else:
        level = 120  # overnight trough
    if day_of_week in (5, 6):
        level = int(level * 0.6)  # weekend
    return level


def _four_weeks() -> list[SeriesPoint]:
    return [
        SeriesPoint(bucket=_bucket(hour), count=_weekly_pattern_count(hour))
        for hour in range(24 * 28)
    ]


def test_required_history_buckets_covers_longest_seasonal_cycle() -> None:
    # Hourly: 3 weeks (hour-of-week * 3) dominates the 14-bucket rolling window.
    assert required_history_buckets(timedelta(hours=1), SETTINGS) == 24 * 7 * 3
    assert required_history_buckets(timedelta(hours=6), SETTINGS) == 4 * 7 * 3
    assert required_history_buckets(timedelta(days=1), SETTINGS) == 7 * 3


def test_phase_baseline_no_false_positive_on_recurring_seasonal_trough() -> None:
    """Regression: a strongly seasonal series with recurring daily troughs and a
    weekend dip must not flag the trough as a drop every cycle. The whole last
    day is evaluated; a clean series must yield zero anomalies."""
    points = _four_weeks()

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(24 * 28 - 24),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_phase_baseline_catches_real_drop_at_trough() -> None:
    points = _four_weeks()
    trough_hour = 24 * 28 - 20  # 04:00 on the last day, normally the daily low
    points[trough_hour] = SeriesPoint(
        bucket=_bucket(trough_hour), count=_weekly_pattern_count(trough_hour) // 4
    )

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(trough_hour),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    drop = next(anomaly for anomaly in anomalies if anomaly.bucket == _bucket(trough_hour))
    assert drop.direction == "drop"


def test_phase_baseline_catches_spike_at_peak() -> None:
    points = _four_weeks()
    peak_hour = 24 * 28 - 14  # 10:00 on the last day, normally the daily peak
    points[peak_hour] = SeriesPoint(
        bucket=_bucket(peak_hour), count=_weekly_pattern_count(peak_hour) * 3
    )

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(peak_hour),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    spike = next(anomaly for anomaly in anomalies if anomaly.bucket == _bucket(peak_hour))
    assert spike.direction == "spike"


def test_hybrid_detects_sustained_level_shift_on_seasonal_series() -> None:
    """A sustained +35% step on top of the seasonal pattern should surface as the
    shift begins, via either the per-bucket phase baseline or the deseasonalized
    trend-shift detector. It is evaluated across the shift region (not just the
    final bucket): once the phase baseline re-levels to the new level
    the tail buckets stop flagging, which is the point — a level shift is ONE
    incident, not one flag per bucket."""
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_weekly_pattern_count(hour))
        for hour in range(24 * 28)
    ]
    shift_start = 24 * 23  # last 5 days run 35% hot
    for hour in range(shift_start, 24 * 28):
        points[hour] = SeriesPoint(
            bucket=_bucket(hour), count=int(_weekly_pattern_count(hour) * 1.35)
        )

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(shift_start),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    assert any(anomaly.direction == "spike" for anomaly in anomalies)


def test_phase_baseline_relevels_sustained_shift_instead_of_flagging_every_bucket() -> None:
    """a level that stepped up ~6x two cycles ago and has been stable
    since must NOT flag every bucket. On a real production instance such an event showed 166
    of 167 hourly buckets flagged because the same-phase median stayed anchored to
    the pre-shift level. Re-leveling the phase expectation to the current level
    fixes it: a whole day of the stable-high plateau yields no per-bucket flags."""
    points = []
    for hour in range(24 * 28):
        base = _weekly_pattern_count(hour)
        # Weeks 3 and 4 (the last 14 days) run at ~6x the earlier level, stably.
        multiplier = 6 if hour >= 24 * 14 else 1
        points.append(SeriesPoint(bucket=_bucket(hour), count=base * multiplier))

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(24 * 28 - 24),  # the whole last day
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_phase_baseline_poisson_floor_ignores_low_count_wobble() -> None:
    """on a low-count seasonal series the phase path must not flag a
    Poisson wobble. Baseline ~11/bucket; a +3 move (11 -> 14) is ~0.9 sigma under
    sqrt(11) spread, not an anomaly. Before the phase Poisson floor this scored
    z=3 against the 1.0 absolute floor and flagged every hour."""
    points = [SeriesPoint(bucket=_bucket(hour), count=11) for hour in range(24 * 28)]
    last = 24 * 28 - 1
    points[last] = SeriesPoint(bucket=_bucket(last), count=14)

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(last),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_covered_buckets_gap_is_not_flagged_as_drop() -> None:
    """Contract C2: a collection gap (a bucket the scan never
    observed) must be EXCLUDED from evaluation, not zero-filled into a fake
    'drop'. Without covered_buckets the same missing bucket zero-fills and flags
    (see test_detect_anomalies_zero_fills_gaps_after_first_seen_bucket)."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(8)]
    covered = {_bucket(hour) for hour in range(8)}  # bucket 8 was never scanned

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(8),
        evaluation_end=_bucket(9),
        settings=SETTINGS,
        covered_buckets=covered,
    ).anomalies

    assert anomalies == []


def test_covered_buckets_none_is_byte_identical_zero_fill() -> None:
    """Sanity: passing covered_buckets=None leaves the historical zero-fill drop
    behavior unchanged, so the collection-gap fix is strictly opt-in."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(8)]

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(8),
        evaluation_end=_bucket(9),
        settings=SETTINGS,
        covered_buckets=None,
    ).anomalies

    assert len(anomalies) == 1
    assert anomalies[0].direction == "drop"
    assert anomalies[0].actual_count == 0


def test_covered_buckets_still_flags_real_change_in_covered_bucket() -> None:
    """A bucket that WAS covered still evaluates normally: a real spike inside
    the covered set is flagged even when the fix is active."""
    points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    points.append(SeriesPoint(bucket=_bucket(10), count=40))
    covered = {_bucket(hour) for hour in range(11)}  # every bucket scanned

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
        covered_buckets=covered,
    ).anomalies

    assert [anomaly.direction for anomaly in anomalies] == ["spike"]


def test_expand_series_excludes_uncovered_buckets() -> None:
    """expand_series drops uncovered buckets entirely and zero-fills covered
    ones with no data point (a genuine 'scan ran, zero events')."""
    points = [SeriesPoint(bucket=_bucket(0), count=5), SeriesPoint(bucket=_bucket(2), count=7)]
    covered = {_bucket(0), _bucket(1), _bucket(2)}  # bucket 3 uncovered

    expanded = expand_series(
        points,
        interval=timedelta(hours=1),
        end_exclusive=_bucket(4),
        covered_buckets=covered,
    )

    assert [point.bucket for point in expanded] == [_bucket(0), _bucket(1), _bucket(2)]
    assert [point.count for point in expanded] == [5, 0, 7]  # bucket 1 zero-filled


def test_effective_stddev_and_kind_populated_on_every_path() -> None:
    """Contract C1: effective_stddev is the positive floored denominator and
    kind is one of the four labels, on both the rolling and phase paths."""
    allowed_kinds = {"phase", "rolling", "trend", "fractional"}

    rolling_points = [SeriesPoint(bucket=_bucket(hour), count=10) for hour in range(10)]
    rolling_points.append(SeriesPoint(bucket=_bucket(10), count=40))
    rolling = detect_anomalies(
        rolling_points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(10),
        evaluation_end=_bucket(11),
        settings=SETTINGS,
    ).anomalies
    assert rolling[0].kind == "rolling"
    assert rolling[0].effective_stddev > 0
    # z-score is recomputable from the surfaced effective_stddev.
    expected_z = (rolling[0].actual_count - rolling[0].expected_count) / rolling[0].effective_stddev
    assert isclose(rolling[0].z_score, expected_z, rel_tol=1e-9)

    phase_points = _four_weeks()
    peak_hour = 24 * 28 - 14
    phase_points[peak_hour] = SeriesPoint(
        bucket=_bucket(peak_hour), count=_weekly_pattern_count(peak_hour) * 3
    )
    phase = detect_anomalies(
        phase_points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(peak_hour),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies
    spike = next(anomaly for anomaly in phase if anomaly.bucket == _bucket(peak_hour))
    assert spike.kind in allowed_kinds
    assert spike.effective_stddev > 0


def _smooth_sinusoid_count(hour: int) -> float:
    """A visually-flat daily sinusoid: level ~1000 with a +/-3% swing. The kind
    of series the old trend-shift detector over-flagged — the
    trend never actually drifts, only the seasonal component wobbles."""
    return 1000.0 + 30.0 * sin(2 * pi * (hour % 24) / 24)


def test_trend_shift_ignores_smooth_few_percent_daily_sinusoid() -> None:
    """A smooth few-percent daily sinusoid must raise ~zero trend anomalies: the
    deseasonalized trend is flat, so no bucket clears the relative effect-size
    gate. This is the primary over-flagging case from complaint 2."""
    hours = 24 * 28
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_smooth_sinusoid_count(hour))
        for hour in range(hours)
    ]

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(hours - 24),  # evaluate the whole last day
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
    ).anomalies

    assert anomalies == []


def test_trend_shift_collapses_sustained_shift_to_single_anomaly() -> None:
    """A genuine sustained 30% level shift must surface as exactly ONE trend
    anomaly, not one row per shifted bucket. Exercises the trend-shift detector
    in isolation so the per-bucket phase baseline can't add extra rows."""
    days = 28
    hours = 24 * days
    shift_start = 24 * 23  # last 5 days run 30% hot
    counts = [
        _daily_pattern_count(hour) * (1.30 if hour >= shift_start else 1.0) for hour in range(hours)
    ]
    points = [SeriesPoint(bucket=_bucket(hour), count=counts[hour]) for hour in range(hours)]
    expanded = expand_series(points, interval=timedelta(hours=1), end_exclusive=_bucket(hours))
    components = _fit_components([point.count for point in expanded], interval=timedelta(hours=1))
    assert components is not None

    trend_anomalies = _detect_trend_shift(
        expanded,
        components,
        evaluation_start=_bucket(shift_start),  # window spans the whole shift run
        settings=SETTINGS,
        interval=timedelta(hours=1),
    ).anomalies

    assert len(trend_anomalies) == 1
    assert trend_anomalies[0].direction == "spike"


def test_trend_shift_still_flags_sharp_spike_via_phase_detector() -> None:
    """The trend-path changes must not weaken the sharp-spike path: a single
    isolated spike is still caught (by the phase/rolling detector)."""
    points = [
        SeriesPoint(bucket=_bucket(hour), count=_weekly_pattern_count(hour))
        for hour in range(24 * 28)
    ]
    peak_hour = 24 * 28 - 14  # 10:00 on the last day, normally the daily peak
    points[peak_hour] = SeriesPoint(
        bucket=_bucket(peak_hour), count=_weekly_pattern_count(peak_hour) * 4
    )

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(peak_hour),
        evaluation_end=_bucket(24 * 28),
        settings=SETTINGS,
    ).anomalies

    spike = next(anomaly for anomaly in anomalies if anomaly.bucket == _bucket(peak_hour))
    assert spike.direction == "spike"


def test_trend_shift_direction_matches_actual_vs_expected() -> None:
    """a trend-shift row's stored direction is derived from the
    ACTUAL point vs its reconstructed expected level, so it can never contradict
    the point. A sustained DOWNWARD shift reads 'drop' and every emitted row
    satisfies direction == 'spike' iff actual >= expected."""
    days = 28
    hours = 24 * days
    # A full week cold: shorter shifts are attenuated by the week-over-week STL
    # trend comparison + the anti-over-flagging gates (0.05 floor, 15% min shift) —
    # intended; this test only needs one real trend row to check direction.
    shift_start = 24 * 21  # last 7 days run 30% cold
    counts = [
        _daily_pattern_count(hour) * (0.70 if hour >= shift_start else 1.0) for hour in range(hours)
    ]
    points = [SeriesPoint(bucket=_bucket(hour), count=counts[hour]) for hour in range(hours)]
    expanded = expand_series(points, interval=timedelta(hours=1), end_exclusive=_bucket(hours))
    components = _fit_components([point.count for point in expanded], interval=timedelta(hours=1))
    assert components is not None

    trend_anomalies = _detect_trend_shift(
        expanded,
        components,
        evaluation_start=_bucket(shift_start),
        settings=SETTINGS,
        interval=timedelta(hours=1),
    ).anomalies

    assert len(trend_anomalies) >= 1
    for anomaly in trend_anomalies:
        assert anomaly.kind == "trend"
        assert anomaly.effective_stddev > 0
        expected_direction = "spike" if anomaly.actual_count >= anomaly.expected_count else "drop"
        assert anomaly.direction == expected_direction
    assert trend_anomalies[0].direction == "drop"


# A zero floor is what makes the trend path's clamped-to-zero expectation
# reachable at all: for any positive floor the volume gate on the REPORTED value
# already rejects it. This is the acme-ios configuration the 14
# production rows came from.
ZERO_FLOOR_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=4.0,
    min_expected_count=0,
)


def test_trend_shift_emits_only_when_something_moved() -> None:
    """a bucket with neither an expectation nor any traffic is not
    a spike, it is no movement, and no row may be written for it. Only the PAIR
    is degenerate — the three other corners of the same boundary still emit.

    Hand-built components so every corner is exact and costs no MSTL fit.
    The reconstructed expectation at the anchor is ``trend[anchor - 168] +
    seasonal[anchor]`` = ``-6.0 + seasonal``, which the clamp at the emit site
    turns into exactly 0.0 for the low-seasonal cases; the negative pre-shift
    level is what a real STL fit produces inside a run of dead buckets."""
    hours = 24 * 22  # 3 full hour-of-week cycles, so period 168 is selectable
    anchor = hours - 1

    def trend_rows(seasonal_at_anchor: float, anchor_count: float) -> list[DetectedAnomaly]:
        points = [SeriesPoint(bucket=_bucket(hour), count=1.0) for hour in range(hours)]
        points[anchor] = SeriesPoint(bucket=_bucket(anchor), count=anchor_count)
        trend = [1.0] * hours
        trend[anchor] = 0.5
        trend[anchor - 168] = -6.0
        seasonal = [0.0] * hours
        seasonal[anchor] = seasonal_at_anchor
        return _detect_trend_shift(
            points,
            (tuple(trend), tuple(seasonal), tuple([0.0] * hours)),
            evaluation_start=_bucket(anchor - 1),
            settings=ZERO_FLOOR_SETTINGS,
            interval=timedelta(hours=1),
        ).anomalies

    # Nothing expected and nothing arrived: the pre-fix code read ``0 >= 0`` as a
    # spike and wrote "0 actual vs 0 expected".
    assert trend_rows(0.5, 0.0) == []

    # ...but traffic against a zero expectation is a real spike from nothing and
    # must survive. This assertion is what keeps the fix honest.
    spike_from_nothing = trend_rows(0.5, 40.0)
    assert [
        (row.direction, row.actual_count, row.expected_count) for row in spike_from_nothing
    ] == [("spike", 40.0, 0.0)]

    # A NEGATIVE actual against the same clamped-zero expectation is a real drop.
    # The ``point.count == 0.0`` half stays as hypothetical as it always was: it
    # can only bite alongside ``expected_count == 0.0``, and the only lanes that
    # let a zero expectation past the volume gate (count-shaped scopes, the
    # platform-parity ratio) are non-negative by construction, so ``signed`` is
    # False there and an actual below zero cannot arise. What the signed-series change made
    # live is the EXPECTED half — ``== 0.0`` rather than ``<= 0.0`` — because a
    # signed series' reconstruction is no longer clamped up to zero. This harness
    # calls ``_detect_trend_shift`` directly, which defaults to ``signed=False``,
    # so what it pins is the COUNT path; the signed lane is pinned in
    # ``test_metric_scope_purge.py`` by
    # ``test_trend_shift_reports_a_signed_expectation`` (the expectation VALUE)
    # and ``test_trend_shift_emits_an_empty_bucket_against_a_negative_expectation``
    # (the guard spelling itself).
    assert [(row.direction, row.expected_count) for row in trend_rows(0.5, -3.0)] == [("drop", 0.0)]

    # Both halves of the boundary against a REAL expectation are untouched.
    assert [(row.direction, row.expected_count) for row in trend_rows(10.0, 0.0)] == [("drop", 4.0)]
    assert [(row.direction, row.expected_count) for row in trend_rows(10.0, 40.0)] == [
        ("spike", 4.0)
    ]


def test_trend_shift_does_not_write_a_zero_expectation_against_an_empty_bucket() -> None:
    """The same shape through the real MSTL fit, which is how production got its
    14 rows. A scope busy 09:00-18:00 with genuinely empty nights,
    dead for 8 days, then alive again: inside the dead run the deseasonalized
    trend overshoots NEGATIVE, so one seasonal period later the per-bucket
    reconstruction clamps to exactly 0.0 — and the bucket it anchors on is a
    night bucket that is legitimately empty. Pre-fix that wrote one row reading
    "spike, 0.0 actual vs 0.0 expected" at z=+4.03.

    Asserts the property and not the bucket: the anchor moved between hours
    across neighbouring fixture shapes, so it is a function of the fit."""
    hours = 24 * 37
    points = []
    for hour in range(hours):
        is_alive = hour // 24 < 21 or hour // 24 >= 29  # 21 days alive, 8 dead, 8 alive again
        is_open = 9 <= hour % 24 < 18
        count = 40.0 if is_alive and is_open else 0.0
        points.append(SeriesPoint(bucket=_bucket(hour), count=count))

    def is_zero_versus_zero(row: DetectedAnomaly) -> bool:
        return row.expected_count == 0.0 and row.actual_count == 0.0

    evaluation_start = _bucket(hours - (24 * 8 + 12))  # covers the whole return to life
    expanded = expand_series(points, interval=timedelta(hours=1), end_exclusive=_bucket(hours))
    components = _fit_components([point.count for point in expanded], interval=timedelta(hours=1))
    assert components is not None

    trend_result = _detect_trend_shift(
        expanded,
        components,
        evaluation_start=evaluation_start,
        settings=ZERO_FLOOR_SETTINGS,
        interval=timedelta(hours=1),
    )

    # The fixture must still reach the emit site, or a future refit that stops
    # producing a shifted run would let this test pass while proving nothing.
    assert trend_result.shifted_buckets
    assert [row for row in trend_result.anomalies if is_zero_versus_zero(row)] == []

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=evaluation_start,
        evaluation_end=_bucket(hours),
        settings=ZERO_FLOOR_SETTINGS,
    ).anomalies

    assert [row for row in anomalies if is_zero_versus_zero(row)] == []
    # ...and the incident itself is still announced: the per-bucket phase path
    # reports the return to life, so suppressing the degenerate row loses nothing.
    assert [row for row in anomalies if row.kind == "phase" and row.actual_count > 0]


# --------------------------------------------------------------------------
# Ingestion settling
# --------------------------------------------------------------------------


def test_settling_buckets_for_converts_wall_clock_allowance_per_grid() -> None:
    """The allowance is wall-clock; each grid rounds it UP to whole buckets, so
    any positive allowance withholds at least one bucket."""
    allowance = timedelta(hours=2)

    assert settling_buckets_for(timedelta(minutes=15), allowance) == 8
    assert settling_buckets_for(timedelta(hours=1), allowance) == 2
    assert settling_buckets_for(timedelta(hours=6), allowance) == 1
    assert settling_buckets_for(timedelta(days=1), allowance) == 1
    assert settling_buckets_for(timedelta(hours=1), timedelta(0)) == 0


def _still_filling_peak_series() -> tuple[list[SeriesPoint], int]:
    """28 days of the daily pattern whose LAST bucket is a peak hour that the
    warehouse has only partly delivered (20 of the usual 60)."""
    hours = 24 * 28 - 12  # last index lands on hour-of-day 11, a pattern peak
    counts = [float(_daily_pattern_count(hour)) for hour in range(hours)]
    counts[-1] = 20.0
    return [SeriesPoint(bucket=_bucket(hour), count=counts[hour]) for hour in range(hours)], hours


def test_settling_allowance_suppresses_drop_on_still_filling_newest_bucket() -> None:
    """the newest bucket is scored while the warehouse is still
    delivering it, so a partly-filled 20-of-60 peak reads as a 5-sigma drop that
    evaporates once the rows land. One settling bucket holds the scoring back."""
    points, hours = _still_filling_peak_series()

    unsettled = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(hours - 1),
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
        settling_buckets=0,
    ).anomalies
    settled = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(hours - 1),
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
        settling_buckets=1,
    ).anomalies

    assert [anomaly.direction for anomaly in unsettled] == ["drop"]  # today's behavior
    assert settled == []  # the still-filling bucket is scored by a later scan


def test_settling_allowance_keeps_the_series_complete() -> None:
    """Only EMISSION is held back: a withheld newest bucket still feeds the
    baseline, so an older bucket inside the same window is scored exactly as it
    would be without the allowance."""
    points, hours = _still_filling_peak_series()
    # Break an older bucket too — it must surface either way.
    broken = hours - 5
    points[broken] = SeriesPoint(bucket=_bucket(broken), count=0.0)

    without = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(broken),
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
        settling_buckets=0,
    ).anomalies
    with_allowance = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(broken),
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
        settling_buckets=1,
    ).anomalies

    older = next(anomaly for anomaly in without if anomaly.bucket == _bucket(broken))
    still_older = next(anomaly for anomaly in with_allowance if anomaly.bucket == _bucket(broken))
    assert isclose(still_older.z_score, older.z_score, rel_tol=1e-9)
    assert all(anomaly.bucket != _bucket(hours - 1) for anomaly in with_allowance)


def _day(index: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)


def test_settling_allowance_holds_back_newest_present_fractional_bucket() -> None:
    """a 1d ratio metric whose newest stored bucket reads 0 because
    the inputs that define it are not in yet. The bucket is two grid slots behind
    ``evaluation_end``, so the allowance must be measured from the end of the
    PRESENT series, not from the grid."""
    ratios = [0.11 + 0.004 * ((index * 7) % 5 - 2) for index in range(30)]
    ratios[-1] = 0.0  # inputs for the newest bucket have not landed
    points = [SeriesPoint(bucket=_day(index), count=ratios[index]) for index in range(30)]
    fractional_settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=3.0,
        min_expected_count=1e-6,
    )

    kwargs = {
        "interval": timedelta(days=1),
        "evaluation_start": _day(28),
        "evaluation_end": _day(31),  # grid runs two slots past the newest point
        "settings": fractional_settings,
        "fill_gaps": False,
    }
    unsettled = detect_anomalies(points, settling_buckets=0, **kwargs).anomalies
    settled = detect_anomalies(points, settling_buckets=1, **kwargs).anomalies

    assert [anomaly.direction for anomaly in unsettled] == ["drop"]
    assert settled == []


# --------------------------------------------------------------------------
# One incident, one signal
# --------------------------------------------------------------------------


def _stepped_daily_counts(*, total_hours: int, step_hour: int, factor: float) -> list[float]:
    """A high-volume daily pattern that steps to ``factor`` once and stays there."""
    return [
        _daily_pattern_count(hour) * 10 * (factor if hour >= step_hour else 1.0)
        for hour in range(total_hours)
    ]


def _simulate_hourly_scans(
    counts: list[float],
    *,
    first_end_hour: int,
    last_end_hour: int,
    reeval_buckets: int = 30,
) -> tuple[dict[datetime, DetectedAnomaly], list[int]]:
    """Replay successive hourly scans with the production window arithmetic.

    Each scan evaluates ``[end - reeval_buckets, end)`` and persists with the
    production delete semantics (``_replace_scope_anomalies`` clears only that
    window before re-inserting). Returns the final stored rows and the row count
    observed after every scan.
    """
    persisted: dict[datetime, DetectedAnomaly] = {}
    sizes: list[int] = []
    for end_hour in range(first_end_hour, last_end_hour):
        evaluation_start = _bucket(end_hour - reeval_buckets)
        evaluation_end = _bucket(end_hour)
        found = detect_anomalies(
            [SeriesPoint(bucket=_bucket(hour), count=counts[hour]) for hour in range(end_hour)],
            interval=timedelta(hours=1),
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            settings=SETTINGS,
        ).anomalies
        persisted = {
            bucket: anomaly
            for bucket, anomaly in persisted.items()
            if not evaluation_start <= bucket < evaluation_end
        }
        persisted.update({anomaly.bucket: anomaly for anomaly in found})
        sizes.append(len(persisted))
    return persisted, sizes


def test_sustained_level_change_is_one_row_not_one_per_scan() -> None:
    """the run-collapse invariant only ever held inside a single
    invocation. The evaluation window slides one bucket per hourly scan and the
    delete window slides with it, so a window-anchored trend row was written one
    bucket further along every run and never revisited — one incident accumulated
    one row per scan (90 scans over this series left 17 rows, 14 of them trend).
    Anchored at the run's true start the same incident stays a single row."""
    step_hour = 24 * 9
    total_hours = 24 * 13
    counts = _stepped_daily_counts(total_hours=total_hours, step_hour=step_hour, factor=0.5)

    persisted, sizes = _simulate_hourly_scans(
        counts, first_end_hour=step_hour + 6, last_end_hour=total_hours
    )

    assert len(persisted) == 1
    row = next(iter(persisted.values()))
    assert row.kind == "trend"
    assert row.direction == "drop"
    # Dated when the level moved, not when the last scan ran.
    assert _bucket(step_hour) <= row.bucket <= _bucket(step_hour + 24)
    # No scan ever accumulated a wall of rows, and the tail is perfectly flat:
    # an ongoing condition stops re-entering the list as new.
    assert max(sizes) <= 6
    assert sizes[-20:] == [sizes[-1]] * 20


def test_phase_expectation_relevels_within_one_short_cycle() -> None:
    """with the level averaged over the FULL phase period, an
    hour-of-week baseline anchors ``median(factors) * current_level`` to a 7-day
    trailing mean, so a step change keeps clearing the sigma bar for days
    (measured: still flagging 71h later, and 106-152h on production series).
    Averaging over the shortest seasonal cycle re-levels within that cycle."""
    shift_hour = 24 * 25
    counts = [
        float(_weekly_pattern_count(hour)) * (0.5 if hour >= shift_hour else 1.0)
        for hour in range(24 * 28)
    ]
    interval = timedelta(hours=1)

    slots = list(range(len(counts)))  # contiguous grid: slot == list index

    def phase_at(offset: int, *, level_window: int | None = None) -> DetectedAnomaly | None:
        idx = shift_hour + offset
        period = _select_phase_period(interval, slots[idx])
        assert period == 24 * 7  # the hour-of-week baseline is what regressed
        return _phase_anomaly_at(
            counts,
            slots,
            idx,
            SeriesPoint(bucket=_bucket(idx), count=counts[idx]),
            period,
            SETTINGS,
            level_window=level_window
            if level_window is not None
            else _phase_level_window(interval, period),
            poisson=True,
        )

    # The incident itself is still reported.
    assert phase_at(1) is not None
    # A day later the baseline has followed the new level — no re-fire.
    for offset in (24, 30, 48, 71):
        assert phase_at(offset) is None
        # ... whereas the full-period level window kept flagging the same
        # unchanged level for days. This is the regression being fixed.
        assert phase_at(offset, level_window=24 * 7) is not None


def test_trend_rows_never_report_expectation_below_the_volume_gate() -> None:
    """the trend path gated on the deseasonalized trend but
    persisted a different quantity as ``expected_count`` — a per-bucket
    reconstruction that could fall below (even to 0.00 on) a project whose
    configured floor is 50. Every surfaced row must clear the floor the user set,
    as the phase and rolling paths already do."""
    start = datetime(2026, 7, 1, tzinfo=UTC)
    settings = AnomalyDetectionSettings(
        baseline_window_buckets=14,
        min_history_buckets=7,
        sigma_threshold=4.0,
        min_expected_count=50,
    )
    for low, high, shift_day in ((10, 800, 20), (5, 900, 21), (20, 600, 22), (40, 400, 25)):
        points = [
            SeriesPoint(
                bucket=start + timedelta(hours=index),
                count=(low if index < 24 * shift_day else high)
                * (1.0 + 0.15 * ((index % 24) / 24.0))
                + (index * 7919 % 11) / 10.0,
            )
            for index in range(24 * 30)
        ]

        anomalies = detect_anomalies(
            points,
            interval=timedelta(hours=1),
            evaluation_start=start + timedelta(hours=24 * (shift_day - 1)),
            evaluation_end=start + timedelta(hours=24 * 30),
            settings=settings,
        ).anomalies

        assert any(anomaly.kind == "trend" for anomaly in anomalies)
        assert all(anomaly.expected_count >= settings.min_expected_count for anomaly in anomalies)


def test_identical_series_share_one_stl_fit() -> None:
    """The robust MSTL fit is the dominant scan cost and a PURE
    function of (series, grid), so identical series must be fitted once.

    This is not hypothetical: a scan whose platform column carries a single
    value (an iOS-only scan) produces a platform-parity ratio of exactly 1.0 for
    every scope, and the parity path has no volume gate to skip them — so the
    same decomposition was recomputed once per scope, every run.
    """
    interval = timedelta(hours=1)
    counts = [float(_daily_pattern_count(hour)) for hour in range(24 * 24)]

    anomaly_detector._fit_components_cached.cache_clear()
    first = _fit_components(counts, interval=interval)
    assert first is not None
    assert anomaly_detector._fit_components_cached.cache_info().misses == 1

    # An equal-but-distinct list is the same key: one fit, shared result.
    second = _fit_components(list(counts), interval=interval)
    assert second is first
    assert anomaly_detector._fit_components_cached.cache_info().hits == 1

    # A series that differs anywhere is a different key — no stale reuse.
    changed = list(counts)
    changed[17] += 1.0
    third = _fit_components(changed, interval=interval)
    assert third is not None
    assert third != first
    assert anomaly_detector._fit_components_cached.cache_info().misses == 2

    # ...and so is the same series on a different grid.
    _fit_components(counts, interval=timedelta(minutes=15))
    assert anomaly_detector._fit_components_cached.cache_info().misses == 3


def test_flat_series_decomposition_matches_the_fitted_one() -> None:
    """The flat-series shortcut must reproduce what STL computes, not approximate it.

    A single-platform scan makes every scope's parity ratio exactly 1.0;
    those fits are skipped analytically, so pin the equivalence
    against the real fit rather than trusting the algebra.
    """
    from statsmodels.tsa.seasonal import MSTL

    interval = timedelta(hours=1)
    counts = [1.0] * (24 * 24)

    anomaly_detector._fit_components_cached.cache_clear()
    trend, seasonal, residuals = _fit_components(counts, interval=interval)  # type: ignore[misc]
    assert anomaly_detector._fit_components_cached.cache_info().misses == 1

    fitted = MSTL(counts, periods=(24, 168), stl_kwargs={"robust": True}).fit()
    fitted_seasonal = fitted.seasonal
    fitted_seasonal = fitted_seasonal if fitted_seasonal.ndim == 1 else fitted_seasonal.sum(axis=1)
    assert all(abs(a - b) < 1e-9 for a, b in zip(trend, fitted.trend, strict=True))
    assert all(abs(a - b) < 1e-9 for a, b in zip(seasonal, fitted_seasonal, strict=True))
    assert all(abs(a - b) < 1e-9 for a, b in zip(residuals, fitted.resid, strict=True))

    # And a flat series still raises no anomaly, shortcut or not.
    points = [SeriesPoint(bucket=_bucket(hour), count=1.0) for hour in range(24 * 24)]
    assert (
        detect_anomalies(
            points,
            interval=interval,
            evaluation_start=_bucket(24 * 23),
            evaluation_end=_bucket(24 * 24),
            settings=AnomalyDetectionSettings(
                baseline_window_buckets=14,
                min_history_buckets=7,
                sigma_threshold=3.0,
                min_expected_count=0,
            ),
            fill_gaps=False,
        ).anomalies
        == []
    )


# --------------------------------------------------------------------------
# Phase is a position on the time grid, not a position in the list
# --------------------------------------------------------------------------


def test_uncovered_bucket_does_not_rotate_the_seasonal_phase() -> None:
    """A single missing collection must not change the verdict on a clean series.

    ``expand_series`` drops a bucket the scan never covered, so every later
    bucket moves one position down the list. Selecting same-phase partners by
    list position then compares 17:00 against the previous day's 16:00, and the
    seasonal step between the two hours is scored as a real event. On acme-ios
    with the live settings, deleting one bucket turned 0 anomalies into 2 spikes
    (08-02 05:00 z=+4.7, 08-03 05:00 z=+4.3); on this series it produced a
    z=+35 spike at 09:00 plus three drops at the other shape boundaries.
    """
    hours = 24 * 28
    hole = 24 * 26 + 17  # inside the last three days, so partners straddle it
    points = [point for point in _four_weeks() if point.bucket != _bucket(hole)]
    covered = {_bucket(hour) for hour in range(hours) if hour != hole}

    anomalies = detect_anomalies(
        points,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(hours - 24),  # the whole last day
        evaluation_end=_bucket(hours),
        settings=SETTINGS,
        covered_buckets=covered,
    ).anomalies

    assert anomalies == []

    # ...and the quiet is not bought by going blind: with the same hole present,
    # a 4x spike at the daily peak is still flagged.
    peak_hour = hours - 14  # 10:00 on the last day, normally the daily peak
    spiked = [
        SeriesPoint(
            bucket=_bucket(hour),
            count=_weekly_pattern_count(hour) * (4 if hour == peak_hour else 1),
        )
        for hour in range(hours)
        if hour != hole
    ]

    spike_anomalies = detect_anomalies(
        spiked,
        interval=timedelta(hours=1),
        evaluation_start=_bucket(peak_hour),
        evaluation_end=_bucket(peak_hour + 1),
        settings=SETTINGS,
        covered_buckets=covered,
    ).anomalies

    assert [anomaly.direction for anomaly in spike_anomalies] == ["spike"]


# --------------------------------------------------------------------------
# A silent scope announces once
# --------------------------------------------------------------------------


def _business_hours_count(hour: int) -> float:
    """Emits only during the working day, so its own baseline is full of zeros.

    This is the shape that makes "anchor on the first empty bucket" wrong: the
    run of empty buckets opens on an ordinary evening zero, which is never
    anomalous, so anchoring there drops every anomalous bucket in the run and
    the event dies in silence.
    """
    return 60.0 if 9 <= hour % 24 < 18 else 0.0


_DEATH_HOUR = 24 * 28


def _dying_business_hours_series(horizon: int) -> list[SeriesPoint]:
    return [
        SeriesPoint(
            bucket=_bucket(hour),
            count=0.0 if hour >= _DEATH_HOUR else _business_hours_count(hour),
        )
        for hour in range(horizon)
    ]


def _scan(points: list[SeriesPoint], *, start: int, end: int) -> list[DetectedAnomaly]:
    return detect_anomalies(
        points[:end],
        interval=timedelta(hours=1),
        evaluation_start=_bucket(start),
        evaluation_end=_bucket(end),
        settings=SETTINGS,
    ).anomalies


def test_a_business_hours_event_that_dies_is_announced_once() -> None:
    horizon = _DEATH_HOUR + 24 * 3
    anomalies = _scan(_dying_business_hours_series(horizon), start=_DEATH_HOUR, end=horizon)

    # One row for three days of silence, and it lands on a WORKING hour — the
    # bucket where the scope stopped doing what it normally does, not the
    # midnight zero that merely happens to open the run.
    assert len(anomalies) == 1
    assert anomalies[0].direction == "drop"
    assert 9 <= anomalies[0].bucket.hour < 18


def test_the_outage_announcement_does_not_move_with_the_evaluation_window() -> None:
    """The property an anomaly-derived anchor cannot have.

    Each collection re-evaluates a trailing window that slides forward one
    bucket at a time, and ``_replace_scope_anomalies`` only rewrites rows inside
    it. An anchor chosen from the anomalies of the current pass therefore moves
    with the window: the previous announcement sits outside the rewritten range
    and survives, a new one is written at the window's first anomalous bucket,
    and the outage accumulates a row per scan — the same pile-up the collapse
    exists to remove. Deriving the anchor from the series makes it stable.
    """
    horizon = _DEATH_HOUR + 24 * 4
    points = _dying_business_hours_series(horizon)

    first = _scan(points, start=_DEATH_HOUR, end=_DEATH_HOUR + 24 * 3)
    assert len(first) == 1
    anchor = first[0].bucket

    # A later scan whose window starts AFTER the anchor must find nothing to
    # announce; the row already on record is the announcement.
    later = _scan(points, start=_DEATH_HOUR + 24, end=horizon)
    assert later == []

    # And a scan that still contains the anchor reports the same bucket, not a
    # fresh one nearer the window's start.
    overlapping = _scan(points, start=_DEATH_HOUR, end=horizon)
    assert [a.bucket for a in overlapping] == [anchor]


# --------------------------------------------------------------------------
# A run this pass declines to announce is reported, not silently dropped
# --------------------------------------------------------------------------

# Production geometry. What matters is that ``min_expected_count`` sits ABOVE the
# scope's own small-hours volume: the anchor's phase then cannot clear any
# emission gate, so the announcement is forced downstream of the anchor.
_QUIET_PHASE_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=4.0,
    min_expected_count=50,
)
_QUIET_NIGHT_DEATH_HOUR = 24 * 28 + 2  # 02:00, the first hour of the thin trickle


def _quiet_night_count(hour: int) -> float:
    """Dead 00:00-02:00, a thin ~20/h trickle 02:00-06:00, then ~500/h all day."""
    hour_of_day = hour % 24
    if hour_of_day < 2:
        return 0.0
    if hour_of_day < 6:
        return 20.0
    return 500.0


def _dying_quiet_night_series(horizon: int) -> list[SeriesPoint]:
    return [
        SeriesPoint(
            bucket=_bucket(hour),
            count=0.0 if hour >= _QUIET_NIGHT_DEATH_HOUR else _quiet_night_count(hour),
        )
        for hour in range(horizon)
    ]


def test_an_outage_skipped_by_this_window_is_reported_as_a_suppressed_range() -> None:
    """Declining to announce must be distinguishable from finding nothing.

    The run is gated on its ANCHOR but announced at the first FLAGGED bucket at or
    after it. Here the anchor's own phase expects ~20 events against a
    ``min_expected_count`` of 50, so no path may flag it and the announcement lands
    hours downstream. Once the window has advanced past the anchor the pass emits
    nothing at all — while the announced row is still inside the window a caller
    would clear. An empty anomaly list alone is indistinguishable from "this window
    is clean", so the run comes back as a ``SuppressedRange``: the caller's cue to
    leave those buckets exactly as it found them.
    """
    horizon = _QUIET_NIGHT_DEATH_HOUR + 24
    points = _dying_quiet_night_series(horizon)
    anchor = _bucket(_QUIET_NIGHT_DEATH_HOUR)

    def scan(start_hour: int) -> DetectionResult:
        return detect_anomalies(
            points,
            interval=timedelta(hours=1),
            evaluation_start=_bucket(start_hour),
            evaluation_end=_bucket(horizon),
            settings=_QUIET_PHASE_SETTINGS,
        )

    announcing = scan(_QUIET_NIGHT_DEATH_HOUR)
    assert len(announcing.anomalies) == 1
    announced = announcing.anomalies[0].bucket
    # Strictly LATER than the anchor — the premise of the whole defect.
    assert announced > anchor
    # The pass that owns the announcement suppresses nothing: it is writing the row
    # itself, so the caller must be free to rewrite the window normally.
    assert announcing.suppressed_ranges == ()

    # Every window position from the bucket after the anchor through the one after
    # the announcement: nothing is emitted, and the announced bucket is covered.
    for start_hour in range(_QUIET_NIGHT_DEATH_HOUR + 1, _QUIET_NIGHT_DEATH_HOUR + 9):
        result = scan(start_hour)
        assert result.anomalies == []
        assert any(
            suppressed.start <= announced < suppressed.end
            for suppressed in result.suppressed_ranges
        ), f"the announced bucket is unprotected when the window starts at hour {start_hour}"


def test_a_revived_scope_that_dies_again_is_announced_again() -> None:
    """Two outages are two incidents, however close together."""
    revival_hour = _DEATH_HOUR + 24
    second_death_hour = revival_hour + 24
    horizon = second_death_hour + 24 * 2
    points = [
        SeriesPoint(
            bucket=_bucket(hour),
            count=(
                _business_hours_count(hour)
                if hour < _DEATH_HOUR or revival_hour <= hour < second_death_hour
                else 0.0
            ),
        )
        for hour in range(horizon)
    ]

    anomalies = _scan(points, start=_DEATH_HOUR, end=horizon)

    # One drop per outage. The revival also scores — a day of silence pulls the
    # same-phase medians down, so the restored traffic reads as a spike — and
    # those buckets carry a real count, sit in no run of empty buckets, and are
    # deliberately left alone: this collapses outages, not everything near one.
    outages = [anomaly for anomaly in anomalies if anomaly.direction == "drop"]
    assert len(outages) == 2
    assert outages[0].bucket < _bucket(revival_hour) <= outages[1].bucket
    assert all(anomaly.actual_count > 0 for anomaly in anomalies if anomaly.direction != "drop")
