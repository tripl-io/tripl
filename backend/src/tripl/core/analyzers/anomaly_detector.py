from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from math import ceil, isfinite, sqrt
from statistics import fmean, median

import numpy as np
from statsmodels.tsa.seasonal import MSTL, STL

from tripl.models.domain_enums import MetricScopeType

SCOPE_PROJECT_TOTAL = MetricScopeType.project_total.value
SCOPE_EVENT_TYPE = MetricScopeType.event_type.value
SCOPE_EVENT = MetricScopeType.event.value
SCOPE_METRIC = MetricScopeType.metric.value
# Seasonal periods (in buckets) used by the STL/MSTL decomposition for the
# trend-shift detector and the forecast. Ascending order matters: MSTL drops
# periods longer than half the series and keeps the shortest survivors first.
_SEASONAL_PERIODS_BY_INTERVAL_SECONDS: dict[int, tuple[int, ...]] = {
    15 * 60: (96, 96 * 7),
    60 * 60: (24, 24 * 7),
    6 * 60 * 60: (4, 4 * 7),
    24 * 60 * 60: (7,),
}
_MIN_CYCLES_PER_PERIOD = 2
# Phase (seasonal) baseline periods in *descending* length order. Each bucket is
# compared against the same position in the longest cycle we have enough history
# for — hour-of-week first, then hour-of-day. This judges a sharp seasonal trough
# against past troughs (not against a smoothed fit or a flat rolling mean), which
# is what kills the phase-locked false positives the old STL/rolling paths emit.
_PHASE_PERIODS_BY_INTERVAL_SECONDS: dict[int, tuple[int, ...]] = {
    15 * 60: (96 * 7, 96),
    60 * 60: (24 * 7, 24),
    6 * 60 * 60: (4 * 7, 4),
    24 * 60 * 60: (7,),
}
# Minimum number of complete same-phase cycles required before a phase period is
# usable. Below this we fall back to the next-shorter period, then to the plain
# rolling baseline (for brand-new series).
_MIN_PHASE_CYCLES = 3
# Lower bound on stddev relative to expected magnitude. Without it a perfectly
# stable series (stddev=0) treats any micro-deviation as a multi-sigma event —
# e.g. baseline=1000±0, current=1010 would score z=10. A 3% floor anchors the
# z-scale to "noticeable change relative to volume" instead of pure 1.0-units.
_RELATIVE_STDDEV_FLOOR_RATIO = 0.03
# Per-bucket phase baseline is noisier (single point vs a cycle median), so it
# uses a wider relative floor than the rolling baseline.
_PHASE_STDDEV_FLOOR_RATIO = 0.05
# The trend-shift detector compares deseasonalized levels averaged over a full
# cycle. Its stddev floor is aligned with the per-bucket phase floor (0.05) so a
# tight residual scale can't inflate small level wobble into a multi-sigma shift.
# The separate _TREND_MIN_RELATIVE_SHIFT gate below enforces a visible effect
# size; the old 0.01 floor (with no effect-size gate) flagged 8-49% of buckets on
# smooth seasonal series that never visibly drifted.
_TREND_STDDEV_FLOOR_RATIO = 0.05
# Minimum fractional trend-level change (relative to the larger of the pre-shift
# and current trend levels) required before the trend-shift detector flags a
# bucket. This is ANDed with the sigma threshold: a shift must be BOTH
# statistically significant AND a visible fraction of the level. Without it, a
# few-percent daily sinusoid tripped a flood of "trend shift" rows that deviated
# <10% from expected.
_TREND_MIN_RELATIVE_SHIFT = 0.15
# Fractional series derive their absolute stddev floor from the series' own
# robust magnitude instead of the count-shaped 1.0: a ratio living around 0.5
# gets a ~0.02 floor, so a 0.5 -> 0.8 movement scores as the multi-sigma event
# it is, while micro-wobble below a few percent of the level stays suppressed.
# Widened from 0.01 -> 0.04: a 1% floor still let Poisson-like
# jitter on ratios trip the sigma bar; ~4% of the level is a better "noticeable
# fractional change" anchor for averages/ratios/sql levels.
_FRACTIONAL_STDDEV_FLOOR_RATIO = 0.04
_FRACTIONAL_STDDEV_FLOOR_EPSILON = 1e-9
# Minimum length of a strictly monotonic run (ending at the evaluated bucket)
# that marks a bucket as part of a *sustained fractional trend*. Such buckets are
# exempted from per-bucket fractional flagging and deferred to the trend-shift
# detector, so a smooth ratio ramp surfaces as one trend row
# instead of a flag on every rung.
_MONOTONIC_TREND_MIN_RUN = 4


def settling_buckets_for(interval: timedelta, delay: timedelta) -> int:
    """Trailing buckets to withhold from anomaly emission for an ingestion ``delay``.

    A warehouse keeps delivering rows for a bucket well after that bucket's clock
    interval closes, so the newest bucket(s) of a freshly collected series read
    low purely because the scan ran early. ``delay`` is the
    wall-clock allowance the operator gives ingestion; this converts it to whole
    buckets of the series' own grid (rounding up, so any positive allowance
    withholds at least one bucket).
    """
    if delay <= timedelta(0) or interval <= timedelta(0):
        return 0
    return ceil(delay / interval)


def _fractional_stddev_floor(counts: Sequence[float]) -> float:
    """Magnitude-derived absolute stddev floor for fractional series."""
    magnitudes = [abs(value) for value in counts]
    if not magnitudes:
        return _FRACTIONAL_STDDEV_FLOOR_EPSILON
    magnitude = median(magnitudes)
    if magnitude == 0.0:
        # More than half the history is zero. A platform-parity ratio for an
        # event that only fires on one platform looks exactly like this, and the
        # parity path deliberately runs with min_expected_count=0, so nothing
        # else gates it. Taking the median literally collapsed the floor to
        # 1e-9, and any bucket where the other platform emitted even once scored
        # z ~ 1e8 — a flood of false parity anomalies on a routine tracking plan.
        # Fall back to the series' own peak so the floor still
        # reflects its scale instead of machine epsilon.
        magnitude = max(magnitudes)
    return max(magnitude * _FRACTIONAL_STDDEV_FLOOR_RATIO, _FRACTIONAL_STDDEV_FLOOR_EPSILON)


@dataclass(frozen=True)
class AnomalyDetectionSettings:
    baseline_window_buckets: int
    min_history_buckets: int
    sigma_threshold: float
    min_expected_count: float


@dataclass(frozen=True)
class SeriesPoint:
    """One bucket of the analyzed series. ``count`` carries whole counts for
    volume series and fractional values (ratios/averages/sql levels) for
    catalog metrics — the detector is scale-aware either way."""

    bucket: datetime
    count: float


@dataclass(frozen=True)
class DetectedAnomaly:
    bucket: datetime
    actual_count: float
    expected_count: float
    stddev: float
    z_score: float
    direction: str
    # The FLOORED stddev actually used in the z denominator (not the raw
    # ``stddev`` scale). The chart band = expected +- sigma_threshold *
    # effective_stddev, so "outside the band" equals "flagged" (contract C1/C4).
    effective_stddev: float = 0.0
    # Which detection path produced this row: one of
    # "phase" | "rolling" | "trend" | "fractional" (contract C1).
    kind: str = "phase"


@dataclass(frozen=True)
class ForecastPoint:
    bucket: datetime
    expected_count: float
    stddev: float


@dataclass(frozen=True)
class SuppressedRange:
    """A half-open bucket range this pass deliberately declined to score.

    Not "nothing was found here" — "an earlier pass already had its say here and
    this one is not entitled to overwrite it". A caller that rewrites a window by
    DELETE-then-INSERT must exclude these ranges from the DELETE, or it destroys
    the earlier verdict and replaces it with nothing.
    """

    start: datetime
    end: datetime


@dataclass(frozen=True)
class BaselinePoint:
    """The band one bucket was judged against, flagged or not.

    ``effective_stddev`` is the floored stddev the z-score divides by, so the
    chart band ``expected_count ± sigma_threshold * effective_stddev`` is the
    detector's own decision boundary for the bucket.
    """

    bucket: datetime
    expected_count: float
    effective_stddev: float


@dataclass(frozen=True)
class DetectionResult:
    """What one detection pass has to say about its evaluation window.

    ``anomalies`` are the rows to store. ``suppressed_ranges`` are the parts of
    the window whose stored rows must be left alone (see ``SuppressedRange``);
    they are deliberately NOT derivable from ``anomalies``, since the whole point
    is that the pass emits nothing there.
    """

    anomalies: list[DetectedAnomaly]
    suppressed_ranges: tuple[SuppressedRange, ...] = ()
    # The per-bucket (phase / rolling / fractional) baseline of every bucket
    # this pass SCORED — cleared the volume gate inside the emission window —
    # whether or not it was flagged, so the chart can draw a continuous band.
    # Buckets the pass skipped (history too short, below the
    # volume floor, settling, a fractional ramp, a provably silent series)
    # carry none, and so do buckets whose own flag was dropped in favour of the
    # one row reporting a trend shift or outage (see ``_drawable_baselines``).
    # Trend rows keep their own expectation on the anomaly row.
    baselines: tuple[BaselinePoint, ...] = ()


def expand_series(
    points: list[SeriesPoint],
    *,
    interval: timedelta,
    end_exclusive: datetime,
    covered_buckets: set[datetime] | None = None,
) -> list[SeriesPoint]:
    """Zero-fill missing buckets onto the interval grid.

    ``covered_buckets`` (contract C2) marks the buckets a scan
    actually observed. When provided, a bucket NOT in the set is EXCLUDED from
    the expanded series entirely rather than zero-filled, so a collection gap no
    longer reads as a real drop to zero (fake ``z << -3`` "drop" anomalies). A
    covered bucket with no data point is still zero-filled — that is a genuine
    "scan ran, zero events" observation. When ``None`` the behavior is
    byte-identical to the historical unconditional zero-fill.

    A NaN or infinite point is EXCLUDED the same way an uncovered bucket is —
    neither zero-filled nor kept — so it can neither become a fake zero nor
    poison every mean, band and z-score it touches.
    """
    if not points:
        return []

    counts_by_bucket = {point.bucket: point.count for point in points}
    non_finite = {bucket for bucket, count in counts_by_bucket.items() if not isfinite(count)}
    bucket = min(counts_by_bucket)
    expanded: list[SeriesPoint] = []
    while bucket < end_exclusive:
        if bucket in non_finite or (covered_buckets is not None and bucket not in covered_buckets):
            bucket += interval
            continue
        expanded.append(SeriesPoint(bucket=bucket, count=counts_by_bucket.get(bucket, 0)))
        bucket += interval
    return expanded


def _select_seasonal_periods(interval: timedelta, series_length: int) -> tuple[int, ...]:
    interval_seconds = int(interval.total_seconds())
    candidates = _SEASONAL_PERIODS_BY_INTERVAL_SECONDS.get(interval_seconds, ())
    return tuple(
        period for period in candidates if series_length >= period * _MIN_CYCLES_PER_PERIOD
    )


def _rolling_stats(values: Sequence[float]) -> tuple[float, float]:
    mean_value = fmean(values)
    variance = fmean((value - mean_value) ** 2 for value in values)
    return mean_value, sqrt(variance)


def _robust_scale(values: Sequence[float]) -> float:
    if not values:
        return 0.0

    center = median(values)
    absolute_deviations = [abs(value - center) for value in values]
    mad = median(absolute_deviations)
    if mad > 0:
        return 1.4826 * mad

    _mean, stddev = _rolling_stats(values)
    return stddev


def _effective_stddev(
    stddev: float,
    expected_count: float,
    *,
    ratio: float = _RELATIVE_STDDEV_FLOOR_RATIO,
    absolute_floor: float = 1.0,
    poisson: bool = False,
) -> float:
    """Stddev clamped from below so flat baselines don't produce huge z-scores.

    The floor is the larger of (``absolute_floor``, ``ratio`` * expected_count).
    Count-shaped series keep the historical 1.0 absolute floor (sub-unit
    deviations on volumes are noise); fractional series pass a floor derived
    from their own magnitude, otherwise a 1.0 floor would flatten every
    sub-unit ratio movement to z~0. ``ratio`` lets each detector
    pick its own tightness (wider for the noisy per-bucket phase baseline,
    tighter for the averaged trend).

    ``poisson`` (COUNT-shaped per-bucket detection only) adds a
    ``sqrt(expected_count)`` term to the floor: a count process of rate N has
    natural spread ~sqrt(N), so a flat baseline of N needs ~sigma*sqrt(N)
    deviation to flag regardless of ``min_expected_count``. This kills
    Poisson-noise flags on low-volume events (e.g. 10 -> 14) that a fixed 1.0
    floor turned into a 4-sigma "spike". It is NOT applied to the averaged
    trend path, which has its own relative effect-size gate.
    """
    relative_floor = max(abs(expected_count) * ratio, absolute_floor)
    if poisson and expected_count > 0:
        relative_floor = max(relative_floor, sqrt(expected_count))
    return max(stddev, relative_floor)


def _clears_volume_gate(
    expected: float,
    settings: AnomalyDetectionSettings,
    *,
    signed: bool,
) -> bool:
    """Whether an expectation is substantial enough for this series to be scored.

    ``min_expected_count`` is a floor on VOLUME, and for a series that can go
    negative volume is the MAGNITUDE: a level of -100 is exactly as substantial
    as one of +100. Comparing the signed value against a non-negative floor
    rejected that whole class on every scoring path, so a ``fact`` sum/avg/min/max
    over a signed column, or any ``sql`` metric sitting below zero, stayed listed
    as monitored and could never signal.

    A series that never goes negative keeps the historical signed comparison
    verbatim, so no count-shaped behaviour moves — which is why ``signed`` is
    decided once, from the series itself, in :func:`detect_anomalies`.
    """
    return (abs(expected) if signed else expected) >= settings.min_expected_count


def _continues_monotonic_trend(counts: Sequence[float], idx: int) -> bool:
    """True when ``counts[idx]`` extends a strictly monotonic run of at least
    ``_MONOTONIC_TREND_MIN_RUN`` buckets ending at ``idx``.

    Used only by the fractional per-bucket path: a bucket riding a smooth,
    sustained ramp is part of a *trend*, not a point anomaly, so it is deferred
    to the trend-shift detector instead of being flagged on
    every rung. A flat baseline with a single jump is NOT monotonic here (equal
    neighbours break the strict run), so genuine step spikes still flag.
    """
    if idx < _MONOTONIC_TREND_MIN_RUN:
        return False
    window = counts[idx - _MONOTONIC_TREND_MIN_RUN : idx + 1]
    increasing = all(earlier < later for earlier, later in zip(window, window[1:], strict=False))
    decreasing = all(earlier > later for earlier, later in zip(window, window[1:], strict=False))
    return increasing or decreasing


# The robust MSTL fit below is the single most expensive thing a metrics scan
# does: ~1.4s per scope over the ~530-bucket history an hourly grid loads, and it
# is 97% of the wall time of a collection once the provably-silent scopes are
# skipped (measured on the demo's in-memory warehouse). It is also
# a PURE function of (series, grid) — and identical series are routine on real
# projects: a scan whose platform column carries a single value (an iOS-only or
# Android-only scan) produces a platform-parity ratio of exactly
# 1.0 for EVERY scope, and the parity path has no volume gate to skip them, so
# the same fit was recomputed once per scope per run. Memoizing on the full
# series makes those runs collapse to one fit while staying byte-identical to the
# uncached path: a series that differs anywhere is a different key.
#
# 64 entries of three ~530-float tuples is well under a megabyte.
_FIT_CACHE_MAX_ENTRIES = 64

_FitComponents = tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]


@lru_cache(maxsize=_FIT_CACHE_MAX_ENTRIES)
def _fit_components_cached(
    counts: tuple[float, ...],
    interval: timedelta,
) -> _FitComponents | None:
    periods = _select_seasonal_periods(interval, len(counts))
    if not periods:
        return None

    # A FLAT series decomposes exactly: LOESS reproduces a constant, so STL on
    # ``y == c`` gives trend == c, seasonal == 0, residuals == 0 analytically.
    # statsmodels computes that same answer to ~1e-13 by running fifteen robust
    # iterations of three LOESS passes per period, at ~1.4s a go. Flat series are
    # routine here rather than a curiosity: a scan whose platform column carries
    # a single value gives EVERY scope a platform-parity ratio of exactly 1.0.
    # This is a shortcut, not an approximation — and a flat trend
    # can never trip the shift detector either way (level change is 0).
    if len(set(counts)) == 1:
        level = float(counts[0])
        flat = (0.0,) * len(counts)
        return (level,) * len(counts), flat, flat

    values = np.asarray(counts, dtype=float)

    try:
        if len(periods) == 1:
            result = STL(values, period=periods[0], robust=True).fit()
            seasonal = np.asarray(result.seasonal, dtype=float)
        else:
            result = MSTL(values, periods=periods, stl_kwargs={"robust": True}).fit()
            seasonal_components = np.asarray(result.seasonal, dtype=float)
            seasonal = (
                seasonal_components
                if seasonal_components.ndim == 1
                else seasonal_components.sum(axis=1)
            )
        trend = np.asarray(result.trend, dtype=float)
        residuals = np.asarray(result.resid, dtype=float)
    except Exception:
        return None

    return tuple(trend.tolist()), tuple(seasonal.tolist()), tuple(residuals.tolist())


def _fit_components(
    counts: Sequence[float],
    *,
    interval: timedelta,
) -> _FitComponents | None:
    """STL/MSTL decomposition returning (trend, seasonal, residuals).

    Used only by the trend-shift detector and to reconstruct the pre-shift
    expected level. Returns None when there isn't enough history to fit.

    The components are immutable tuples because they are shared out of a cache
    keyed on the exact input series (see ``_fit_components_cached``); callers
    read and slice them, never mutate.
    """
    return _fit_components_cached(tuple(counts), interval)


def _grid_slots(points: Sequence[SeriesPoint], interval: timedelta) -> list[int]:
    """Position of each point on the interval grid, counted from the first point.

    The analyzed list is NOT the grid: ``expand_series`` omits buckets a scan
    never covered and ``_present_series`` omits buckets that carry no value, so
    the list index of a bucket shifts by one for every hole before it. Phase
    (same hour-of-day / hour-of-week) partners must be selected on the grid
    position, never on the list index — with one hourly collection missing, an
    index-keyed baseline compares 05:00 against the previous day's 04:00 and
    scores the resulting mismatch as a real event. Replayed over the real
    acme-ios project_total series with that project's live settings, marking a
    single bucket uncovered turned 0 anomalies into 2 spikes (08-04 04:00 z=+4.2,
    08-04 05:00 z=+4.8) — the same two rows for every one of five hole positions
    between 4h and 100h before the window, because what fires is the phase
    rotation, not the hole.

    Slots are relative to the first analyzed bucket rather than to an absolute
    epoch: a phase partner is any bucket whose slot distance is a multiple of
    the period, which is the same set whatever origin is chosen, and the count
    of elapsed cycles (``slot // period``) stays meaningful. On a contiguous
    grid ``slots[i] == i``, so nothing about today's behavior changes.
    """
    origin = points[0].bucket
    return [(point.bucket - origin) // interval for point in points]


def _select_phase_period(interval: timedelta, slot: int) -> int | None:
    """Longest phase period with >= _MIN_PHASE_CYCLES cycles of grid elapsed
    before ``slot`` (so the baseline never includes the point itself)."""
    interval_seconds = int(interval.total_seconds())
    for period in _PHASE_PERIODS_BY_INTERVAL_SECONDS.get(interval_seconds, ()):
        if slot // period >= _MIN_PHASE_CYCLES:
            return period
    return None


@dataclass(frozen=True)
class _Baseline:
    """What a per-bucket path expects of one bucket, before the sigma test.

    ``scale`` is the raw robust spread, ``effective_stddev`` the floored one the
    z-score divides by. Built for EVERY bucket that clears the volume gate, so
    the caller can persist the band even where nothing was flagged.
    """

    expected_count: float
    scale: float
    effective_stddev: float


def _score_against_baseline(
    point: SeriesPoint,
    baseline: _Baseline,
    settings: AnomalyDetectionSettings,
    *,
    kind: str,
) -> DetectedAnomaly | None:
    z_score = (point.count - baseline.expected_count) / baseline.effective_stddev
    # ``abs(nan) < sigma`` is False, so without the finiteness test a NaN z
    # slipped through as an "anomaly".
    if not isfinite(z_score) or abs(z_score) < settings.sigma_threshold:
        return None

    return DetectedAnomaly(
        bucket=point.bucket,
        actual_count=point.count,
        expected_count=baseline.expected_count,
        stddev=baseline.scale,
        z_score=z_score,
        direction="spike" if point.count >= baseline.expected_count else "drop",
        effective_stddev=baseline.effective_stddev,
        kind=kind,
    )


def _rolling_baseline_at(
    counts: list[float],
    idx: int,
    settings: AnomalyDetectionSettings,
    *,
    stddev_absolute_floor: float = 1.0,
    poisson: bool = False,
    signed: bool = False,
) -> _Baseline | None:
    window_start = max(0, idx - settings.baseline_window_buckets)
    baseline = counts[window_start:idx]
    if len(baseline) < settings.min_history_buckets:
        return None

    expected_count, stddev = _rolling_stats(baseline)
    if not _clears_volume_gate(expected_count, settings, signed=signed):
        return None

    effective_stddev = _effective_stddev(
        stddev, expected_count, absolute_floor=stddev_absolute_floor, poisson=poisson
    )
    return _Baseline(expected_count=expected_count, scale=stddev, effective_stddev=effective_stddev)


def _rolling_anomaly_at(
    counts: list[float],
    idx: int,
    point: SeriesPoint,
    settings: AnomalyDetectionSettings,
    *,
    stddev_absolute_floor: float = 1.0,
    poisson: bool = False,
    signed: bool = False,
    kind: str = "rolling",
) -> DetectedAnomaly | None:
    """Seasonality-blind rolling-mean z-score. Fallback for series too short to
    have a usable phase period (brand-new scans, very sparse data)."""
    baseline = _rolling_baseline_at(
        counts,
        idx,
        settings,
        stddev_absolute_floor=stddev_absolute_floor,
        poisson=poisson,
        signed=signed,
    )
    if baseline is None:
        return None
    return _score_against_baseline(point, baseline, settings, kind=kind)


def _phase_level_window(interval: timedelta, period: int) -> int:
    """Trailing window (in buckets) whose mean defines "the current level".

    The re-leveled phase expectation is ``median(factors) * current_level``. When
    ``current_level`` averages the FULL phase period, an hour-of-week baseline
    (period 168) anchors the expectation to a 7-DAY trailing mean, so a step
    change takes ~a week to be absorbed and the same incident is re-announced
    every hour for days. Averaging over the SHORTEST seasonal
    cycle instead (a day, for hourly data) keeps the level phase-independent — it
    still spans one whole cycle, so every phase sees the same level — while
    converging on a sustained shift within that single cycle.
    """
    candidates = _PHASE_PERIODS_BY_INTERVAL_SECONDS.get(int(interval.total_seconds()), ())
    return min(period, min(candidates)) if candidates else period


def _same_phase_indices(slots: Sequence[int], idx: int, period: int) -> list[int]:
    """Indices strictly before ``idx`` that sit at the same phase of the grid."""
    phase = slots[idx] % period
    return [j for j in range(idx) if slots[j] % period == phase]


def _trailing_window(
    counts: Sequence[float], slots: Sequence[int], idx: int, span: int
) -> list[float]:
    """Counts within ``span`` grid slots ending at (and including) ``idx``.

    Sliced by grid distance rather than by list position so a hole in the series
    shortens the window instead of silently stretching it over more real time.
    """
    return list(counts[bisect_left(slots, slots[idx] - span + 1) : idx + 1])


def _seasonal_factors(
    counts: list[float],
    slots: Sequence[int],
    idx: int,
    period: int,
    level_window: int,
    *,
    signed: bool = False,
) -> tuple[list[float], float]:
    """Level-normalized seasonal factors for ``idx``'s phase, and the current level.

    Each prior same-phase count is divided by the mean level of its own trailing
    ``level_window`` buckets, yielding a factor that captures the seasonal SHAPE
    at this phase independent of the overall level. ``current_level`` is the mean
    of the trailing ``level_window`` buckets immediately before ``idx`` — the
    same window, so numerator and denominator stay consistent. Multiplying the
    median factor by ``current_level`` gives a seasonal expectation that tracks a
    sustained level shift instead of lagging it; see
    ``_phase_level_window`` for why the window is one SHORT cycle rather than the
    full phase period. Cycles whose level is 0 (all-zero history) contribute no
    factor.

    ``signed`` short-circuits the whole normalization to the degenerate
    fallback below — the raw same-phase median, a correct if less adaptive
    baseline — and leaves sustained level shifts to the trend path.
    The per-cycle ``level > 0`` test cannot stand in for it: it
    only excludes a partner whose trailing mean is non-POSITIVE, and a series
    that straddles zero (small positive buckets plus one deep negative one) keeps
    every trailing mean positive-but-tiny, so ``counts[j] / level`` explodes.
    That produced an expectation of -726 on a series whose observed range was
    [-220, +11], which the magnitude gate in :func:`_clears_volume_gate` then
    admitted as a 6.4-sigma "spike". The divisor has to be far from zero, not
    merely above it, and on a signed series nothing bounds it away.

    Both the partner selection and the two level windows are keyed on the grid
    slot (``_grid_slots``), so a missing bucket cannot rotate the phase.
    """
    if signed:
        return [], 0.0
    factors: list[float] = []
    for j in _same_phase_indices(slots, idx, period):
        cycle = _trailing_window(counts, slots, j, level_window)
        level = fmean(cycle) if cycle else 0.0
        if level > 0:
            factors.append(counts[j] / level)
    current_cycle = counts[bisect_left(slots, slots[idx] - level_window) : idx]
    current_level = fmean(current_cycle) if current_cycle else 0.0
    return factors, current_level


def _phase_baseline_at(
    counts: list[float],
    slots: Sequence[int],
    idx: int,
    period: int,
    settings: AnomalyDetectionSettings,
    *,
    level_window: int,
    stddev_absolute_floor: float = 1.0,
    poisson: bool = False,
    signed: bool = False,
) -> _Baseline | None:
    same_phase = [counts[j] for j in _same_phase_indices(slots, idx, period)]
    if not same_phase:
        return None

    factors, current_level = _seasonal_factors(
        counts, slots, idx, period, level_window, signed=signed
    )
    if factors and current_level > 0:
        expected_count = median(factors) * current_level
        scale = _robust_scale(factors) * current_level
    else:
        expected_count = median(same_phase)
        scale = _robust_scale(same_phase)

    if not _clears_volume_gate(expected_count, settings, signed=signed):
        return None

    effective_stddev = _effective_stddev(
        scale,
        expected_count,
        ratio=_PHASE_STDDEV_FLOOR_RATIO,
        absolute_floor=stddev_absolute_floor,
        poisson=poisson,
    )
    return _Baseline(expected_count=expected_count, scale=scale, effective_stddev=effective_stddev)


def _phase_anomaly_at(
    counts: list[float],
    slots: Sequence[int],
    idx: int,
    point: SeriesPoint,
    period: int,
    settings: AnomalyDetectionSettings,
    *,
    level_window: int,
    stddev_absolute_floor: float = 1.0,
    poisson: bool = False,
    signed: bool = False,
    kind: str = "phase",
) -> DetectedAnomaly | None:
    """Compare a bucket to the robust distribution of the same phase (e.g. same
    hour-of-week) over prior cycles. median + MAD are robust to past anomalies
    and to sharp seasonal shapes, so recurring troughs/peaks score ~0 instead of
    tripping every cycle.

    The expectation is re-leveled to the CURRENT level: the plain
    same-phase median lags a sustained level shift because its history spans up
    to ``_MIN_PHASE_CYCLES`` cycles, so a stepped-but-stable series would flag
    every bucket for ~1.5 cycles. Normalizing each same-phase count by its own
    cycle level and re-applying the median factor to the current level tracks the
    shift, while a genuine one-bucket spike still stands out (the current level,
    a trailing full short cycle, barely moves). Degenerate all-zero history falls
    back to the raw same-phase median, so brand-new series behave as before — and
    so does a SIGNED series, whose level is not a safe divisor at all (see
    :func:`_seasonal_factors`)."""
    baseline = _phase_baseline_at(
        counts,
        slots,
        idx,
        period,
        settings,
        level_window=level_window,
        stddev_absolute_floor=stddev_absolute_floor,
        poisson=poisson,
        signed=signed,
    )
    if baseline is None:
        return None
    return _score_against_baseline(point, baseline, settings, kind=kind)


@dataclass(frozen=True)
class TrendShiftResult:
    """Trend-shift rows to persist, plus every bucket the shift spans.

    ``shifted_buckets`` covers the whole contiguous run — including the buckets
    no row is emitted for — of every run that OWNS a trend row (emitted now, by
    an earlier scan, or once it settles); a run whose row is gated out claims
    nothing. ``detect_anomalies`` uses it to suppress the
    per-bucket rows that would otherwise re-announce one level change bucket
    after bucket.
    """

    anomalies: list[DetectedAnomaly]
    shifted_buckets: frozenset[datetime]


def _departs_from_pre_shift(actual: float, reconstructed: float) -> bool:
    """Whether a bucket's RAW value left its pre-shift expectation.

    Uses the trend path's own relative effect-size bar, against the larger of
    the two magnitudes so it stays defined near zero — a bucket inside normal
    noise is not where a level change began.
    """
    reference = max(abs(actual), abs(reconstructed))
    if reference <= 0:
        return False
    return abs(actual - reconstructed) / reference >= _TREND_MIN_RELATIVE_SHIFT


def _detect_trend_shift(
    expanded: list[SeriesPoint],
    components: _FitComponents,
    *,
    evaluation_start: datetime,
    settings: AnomalyDetectionSettings,
    interval: timedelta,
    stddev_absolute_floor: float = 1.0,
    signed: bool = False,
    emission_end: datetime | None = None,
) -> TrendShiftResult:
    """Catch slow/sustained level drifts the per-bucket phase baseline absorbs.

    Operates purely on the *deseasonalized* trend component, so it can never be
    phase-locked: it compares the trend level now against the trend exactly one
    seasonal cycle ago, scaled by the robust spread of residuals.
    """
    trend, seasonal, residuals = components
    slots = _grid_slots(expanded, interval)
    index_by_slot = {slot: index for index, slot in enumerate(slots)}
    period = _select_phase_period(interval, slots[-1])
    if period is None:
        return TrendShiftResult(anomalies=[], shifted_buckets=frozenset())

    anomalies: list[DetectedAnomaly] = []
    shifted_buckets: set[datetime] = set()
    # A sustained shift spans many buckets; we collapse each contiguous shifted
    # run into a SINGLE row, and a run whose anchor lies before
    # ``evaluation_start`` is not re-emitted at all. Anchoring at a fixed point of
    # the incident is what makes the collapse survive ACROSS scans: the
    # evaluation window slides forward one bucket per run and ``_replace_scope_
    # anomalies`` only deletes inside it, so a window-anchored row landed one
    # bucket further along every run and the incident accumulated one row per
    # scan.
    #
    # The anchor is the run's first bucket whose RAW value actually left its
    # pre-shift expectation, not the run's first shifted TREND bucket. The STL
    # trend is centred, so it starts bending hours before the change; anchored
    # at the bend, a run could date its row on a bucket that was inside normal
    # noise, and that start moved with every refit — one scan replaced an
    # incident's drop rows with a single misdated row and the next reverted it.
    # The raw departure does not move between refits.
    #
    # A run only suppresses its buckets' per-bucket rows when it owns the trend
    # row: it emitted one now, its anchor sits before the window (a previous
    # scan owned the emission), or its anchor is still settling (a later scan
    # will). A run whose anchor is gated out, or which never
    # departs at all, leaves its per-bucket rows alone instead of silencing the
    # incident with nothing written in its place.
    runs: list[list[tuple[int, SeriesPoint, float, float, float, float]]] = []
    current_run: list[tuple[int, SeriesPoint, float, float, float, float]] = []
    for idx, point in enumerate(expanded):
        # "One period ago" is a position on the GRID, not ``idx - period``: with
        # a bucket missing, the list offset lands on a neighbouring hour and the
        # comparison is no longer like-with-like. A bucket whose partner was
        # never collected has no comparison to make and is skipped.
        previous_idx = index_by_slot.get(slots[idx] - period)
        if previous_idx is None:
            continue

        pre_shift_level = trend[previous_idx]
        if not _clears_volume_gate(trend[idx], settings, signed=signed):
            if current_run:
                runs.append(current_run)
                current_run = []
            continue

        scale = _robust_scale(residuals[previous_idx:idx])
        effective_stddev = _effective_stddev(
            scale,
            trend[idx],
            ratio=_TREND_STDDEV_FLOOR_RATIO,
            absolute_floor=stddev_absolute_floor,
        )
        level_change = trend[idx] - pre_shift_level
        z_score = level_change / effective_stddev
        # Relative effect-size gate: the level must move by a visible fraction of
        # the level, not just clear the sigma bar. Referenced against the larger
        # of the two levels so it stays well-defined near zero.
        reference_level = max(abs(pre_shift_level), abs(trend[idx]))
        relative_change = abs(level_change) / reference_level if reference_level > 0 else 0.0
        is_shifted = (
            isfinite(z_score)
            and abs(z_score) >= settings.sigma_threshold
            and relative_change >= _TREND_MIN_RELATIVE_SHIFT
        )
        if not is_shifted:
            if current_run:
                runs.append(current_run)
                current_run = []
            continue
        current_run.append((idx, point, pre_shift_level, scale, effective_stddev, z_score))
    if current_run:
        runs.append(current_run)

    for run in runs:
        run_buckets = [member[1].bucket for member in run]
        anchor = next(
            (
                member
                for member in run
                if _departs_from_pre_shift(member[1].count, member[2] + seasonal[member[0]])
            ),
            None,
        )
        if anchor is None:
            continue
        idx, point, pre_shift_level, scale, effective_stddev, z_score = anchor
        if point.bucket < evaluation_start:
            # Emitted (and frozen) by an earlier scan: still the same incident.
            shifted_buckets.update(run_buckets)
            continue
        if emission_end is not None and point.bucket >= emission_end:
            # Still settling: a later scan emits the row, so the run already
            # owns its buckets.
            shifted_buckets.update(run_buckets)
            continue

        # Reconstruct what this bucket would have been without the level shift so
        # the surfaced expected_count stays interpretable per bucket.
        reconstructed = pre_shift_level + seasonal[idx]
        # The clamp exists only because a negative reconstruction is meaningless
        # for a non-negative series. On a signed series it IS the answer, so
        # clamping it to 0.0 would both hide the real expectation and hand the
        # gate below a value the floor rejects.
        expected_count = reconstructed if signed else max(reconstructed, 0.0)
        # The volume gate above tests the deseasonalized trend, but the quantity
        # we PERSIST as expected_count is this per-bucket reconstruction — a
        # different number that can sit below the floor the project configured,
        # so a signal could surface claiming an expectation under the user's
        # min_expected_count. Gate the reported value too.
        if not _clears_volume_gate(expected_count, settings, signed=signed):
            continue
        # ...and a reconstruction that lands AT zero says something else again.
        # The clamp above turns a negative reconstruction into exactly 0.0 and the
        # direction line below then reads ``0 >= 0`` as a SPIKE — a row stating,
        # in the two numbers a reader is shown, that nothing happened. That is
        # what minted the "spike, 0 actual vs 0 expected" rows production carried
        # on a project running ``min_expected_count = 0``, the one floor that lets
        # a zero expectation past the gate above (tripl-wkwv.4 made
        # those rows closable, this stops them being written). Only the PAIR is
        # degenerate, never one half of it: traffic against a zero expectation is
        # a real spike from nothing and still emits, and an empty bucket against a
        # real expectation is still a drop. ``== 0.0`` rather than ``<= 0.0`` on
        # the actual is the conservative spelling, not a live distinction: every
        # series that can reach this line today is non-negative, so the two agree.
        # The only lane running at ``min_expected_count = 0`` on a FRACTIONAL
        # series is platform parity, and its value is a breakdown count over a
        # scope total — two event counts, so it lands in [0, 1]. The fractional
        # series that CAN go negative (a catalog ``fact`` sum/avg/min/max or a
        # ``sql`` metric, i.e. a level over a possibly-signed column) DO reach
        # this line now that the gates above measure magnitude,
        # and they are exactly why the expectation half is ``== 0.0`` rather than
        # ``<= 0.0``: an empty bucket against an expectation of -100 is a real
        # move, and ``<=`` would swallow it. The two spellings agree for every
        # non-negative series, so nothing on the count path changes.
        if expected_count == 0.0 and point.count == 0.0:
            continue
        # Direction is derived from the ACTUAL point vs the reconstructed
        # expected level, not from the sign of the trend z-score.
        # The z-score is computed on the deseasonalized trend delta, which can
        # disagree with where the raw point sits relative to its expected band
        # once the seasonal component is added back — so a point above expected
        # must read "spike" and below "drop" no matter the trend-delta sign.
        anomalies.append(
            DetectedAnomaly(
                bucket=point.bucket,
                actual_count=point.count,
                expected_count=expected_count,
                stddev=scale,
                z_score=z_score,
                direction="spike" if point.count >= expected_count else "drop",
                effective_stddev=effective_stddev,
                kind="trend",
            )
        )
        shifted_buckets.update(run_buckets)

    return TrendShiftResult(anomalies=anomalies, shifted_buckets=frozenset(shifted_buckets))


def _collapse_outage_runs(
    anomalies: list[DetectedAnomaly],
    expanded: list[SeriesPoint],
    *,
    slots: Sequence[int],
    interval: timedelta,
    evaluation_start: datetime,
) -> tuple[list[DetectedAnomaly], tuple[SuppressedRange, ...]]:
    """One row per outage: the bucket where the scope stopped behaving normally.

    A scope that has gone silent stays silent, and every silent bucket scores the
    same way forever, so the per-bucket path re-announces one incident once per
    bucket per scan. Measured at production geometry (hourly, sigma 4.0,
    min_expected_count 50, 504 buckets of history, 30-bucket re-evaluation
    window, 2 settling buckets) one dead event produced 28 rows at 48h of death,
    0 at 72-96h and 28 again at 120-168h, most at z=-20.00 — the exact z the 5%
    relative stddev floor pins a drop-to-zero at. The non-monotonicity is the
    trend-shift detector claiming the run only while the deseasonalized trend
    still clears ``min_expected_count`` and dropping the claim once it decays
    below (and, on a hard death, overshoots negative), so suppression that hangs
    off the live trend level switches itself off exactly as the outage ages.

    The anchor is the first ANOMALOUS bucket of each run of zeros, NOT the first
    zero. That distinction is the whole correctness of this function. Anchoring
    on the first zero is right only for a scope whose baseline never sits at
    zero; for anything nightly-quiet, business-hours or regionally-quiet the run
    begins at a NORMAL zero, which is never anomalous — so the filter would drop
    every anomalous bucket in the run and emit nothing at any scan age, and a
    whole class of events would die silently. That is strictly worse than the
    duplicate rows this replaces, and it is what the first version of this patch
    did.

    Anchoring on the first anomalous bucket also survives the fit: it does not
    ask whether the trend detector still claims the run, so a run is announced
    once no matter how the decomposition drifts as the outage ages. Later scans
    re-derive the same anchor, find it outside their window, and write nothing.

    That last step is why the second return value exists. The anchor is gated on,
    but the announcement is made at the first FLAGGED bucket at or after it,
    which can be strictly LATER — a scope whose anchor phase sits below
    ``min_expected_count`` announces several buckets downstream. The evaluation
    window then advances one bucket per collection, so there is always a pass
    whose window starts after the anchor but still CONTAINS the announced row: it
    skips the run, emits nothing, and a caller that clears its whole window would
    delete the only row the outage ever had, with no replacement and no later
    pass willing to write one. Every run skipped here is therefore reported as a
    ``SuppressedRange`` covering its buckets, and callers must exclude those
    ranges when they clear the window.

    Runs are contiguous in the ANALYZED series, not on the clock: a bucket the
    scan never covered is absent from the list, and an unobserved bucket is not
    evidence the scope came back, so a collection gap cannot split one outage
    into two announcements. An outage older than the loaded history does lose
    sight of its own anchor and re-announces once — that history is 504 buckets
    on the hourly grid, so it takes a three-week outage.

    COUNT-shaped series only. A count of 0 is an unambiguous "emitted nothing";
    for a fractional series 0.0 is a value (a ratio that happens to be zero, and
    possibly not even its minimum), so "the same incident" needs another
    definition there.
    """
    counts = [point.count for point in expanded]
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(expanded):
        if counts[index] > 0:
            index += 1
            continue
        start = index
        while index < len(expanded) and counts[index] <= 0:
            index += 1
        runs.append((start, index))
    if not runs:
        return anomalies, ()

    in_a_run: set[datetime] = set()
    # Buckets kept out of the runs: at most one per run, and only for a run
    # whose anchor this pass is responsible for announcing.
    kept: set[datetime] = set()
    suppressed: list[SuppressedRange] = []
    for start, end in runs:
        run_buckets = {expanded[position].bucket for position in range(start, end)}
        in_a_run |= run_buckets
        anchor = _outage_anchor(expanded, counts, slots, interval, start=start, end=end)
        if anchor < evaluation_start:
            # The announcement belongs to the pass whose window contained the
            # anchor. Writing another one here — at whatever this window happens
            # to start on — is exactly how one outage becomes a row per scan.
            # But the earlier pass may well have announced at a bucket LATER
            # than the anchor (see the docstring), so that row can still be
            # inside this window: hand it back as a range the caller must not
            # clear, or declining to announce silently erases the announcement.
            run_end = expanded[end - 1].bucket + interval
            if run_end > evaluation_start:
                # Runs that end before the window are already safe (a caller only
                # clears from ``evaluation_start``), and reporting them would put
                # one range per nightly-quiet gap in the history on every scan —
                # dozens of clauses that also make stale rows in those gaps
                # unclearable. Only overlapping runs need protecting.
                suppressed.append(SuppressedRange(start=expanded[start].bucket, end=run_end))
            continue
        # The anchor is where the scope stopped behaving normally, which is not
        # necessarily a bucket the detector flagged: the trend detector may have
        # claimed it, or its own phase may be too quiet to score. Announce at the
        # first flagged bucket AT OR AFTER it, or the run is silenced by the very
        # anchor meant to give it a voice.
        candidates = sorted(
            anomaly.bucket
            for anomaly in anomalies
            if anomaly.bucket in run_buckets and anomaly.bucket >= anchor
        )
        if candidates:
            kept.add(candidates[0])
    collapsed = [
        anomaly
        for anomaly in anomalies
        # Anomalies outside any run of empty buckets are untouched — this
        # collapses outages, not spikes.
        if anomaly.bucket not in in_a_run or anomaly.bucket in kept
    ]
    return collapsed, tuple(suppressed)


def _outage_anchor(
    expanded: list[SeriesPoint],
    counts: Sequence[float],
    slots: Sequence[int],
    interval: timedelta,
    *,
    start: int,
    end: int,
) -> datetime:
    """The bucket in ``[start, end)`` where this scope stopped behaving normally.

    The first bucket of the run whose OWN phase was usually non-empty before the
    run began: for a business-hours event the run opens on an ordinary evening
    zero and this walks forward to the first working hour, which is the bucket a
    reader would call the outage.

    Derived from the series, deliberately, and never from the anomaly list. The
    anomalies a pass produces exist only inside its evaluation window, and that
    window slides one bucket per collection — so an anomaly-derived anchor moves
    with it, the previous anchor sits outside the rewritten range and survives,
    and the outage accumulates one row per scan instead of announcing once. That
    is the same weekly pile-up this function exists to remove, only rearranged.
    Same-phase partners are taken from BEFORE the run for the same reason: a long
    outage's own zeros would otherwise vote on whether it is normal to be empty.
    """
    for position in range(start, end):
        period = _select_phase_period(interval, slots[position])
        if period is None:
            # Too little history for a seasonal opinion; nothing distinguishes
            # the buckets of this run, so the first one is the honest anchor.
            break
        partners = [counts[j] for j in _same_phase_indices(slots, position, period) if j < start]
        if partners and median(partners) > 0:
            return expanded[position].bucket
    # No bucket in the run was ever normally busy — a scope that only emits at
    # phases this run does not cover. Announce it at the start rather than not at
    # all: a missed outage is worse than one reported a few buckets early.
    return expanded[start].bucket


def _merge_anomalies(*anomaly_lists: list[DetectedAnomaly]) -> list[DetectedAnomaly]:
    anomalies_by_bucket: dict[datetime, DetectedAnomaly] = {}
    for anomaly_list in anomaly_lists:
        for anomaly in anomaly_list:
            existing = anomalies_by_bucket.get(anomaly.bucket)
            if existing is None or abs(anomaly.z_score) > abs(existing.z_score):
                anomalies_by_bucket[anomaly.bucket] = anomaly
    return [anomalies_by_bucket[bucket] for bucket in sorted(anomalies_by_bucket)]


# Headroom for the silent-series early exit. The trend-shift gate compares the
# STL trend of the DESEASONALIZED series against ``min_expected_count``, and a
# deseasonalized value is ``y - seasonal`` with ``|seasonal|`` bounded by the
# series amplitude, so the trend can exceed max(counts) — up to ~2x in the
# adversarial case (day/night series whose troughs vanish: raw max 45 produced
# a trend of ~54.6 in review). The 2x headroom makes the trend path safe by
# construction: skipping requires 2*max <= threshold, and trend <= ~2*max.
_SILENT_SERIES_HEADROOM = 2.0


def is_provably_silent(max_count: float, min_expected_count: float) -> bool:
    """Whether a count series can be skipped without consulting the detector.

    True when even ``_SILENT_SERIES_HEADROOM * max_count`` stays below the
    ``min_expected_count`` gate that every emission path checks: the rolling
    expectation is a window mean (<= max), the trend gate needs the
    deseasonalized STL trend (<= ~2x max, hence the headroom) to reach the
    threshold, and a spike additionally needs actual > expected. The one known
    loss is the re-leveled phase expectation, ``median(factors) *
    current_level``, which is a *projection* and can exceed any observed count
    by up to the period length on a spike-shaped history — suppressing that
    also suppresses the drop-vs-projection flags it generates, which on a
    series this quiet are noise, not signal (review verdict on tripl-h353).

    A ``min_expected_count`` of 0 disables the skip entirely (counts are
    non-negative, so the strict inequality can never hold).
    """
    return max_count * _SILENT_SERIES_HEADROOM < min_expected_count


def required_history_buckets(interval: timedelta, settings: AnomalyDetectionSettings) -> int:
    """Number of buckets to load before the evaluation window.

    The phase baseline needs `_MIN_PHASE_CYCLES` full cycles of the longest
    seasonal period to compare like-with-like, which is far more than the
    rolling baseline window. Callers must load this much history or the detector
    silently degrades to the seasonality-blind rolling fallback.
    """
    periods = _PHASE_PERIODS_BY_INTERVAL_SECONDS.get(int(interval.total_seconds()), ())
    seasonal = max(periods) * _MIN_PHASE_CYCLES if periods else 0
    return max(settings.baseline_window_buckets, seasonal)


def _present_series(
    points: list[SeriesPoint],
    *,
    end_exclusive: datetime,
) -> list[SeriesPoint]:
    """Sorted, de-duplicated points strictly before ``end_exclusive``.

    Used instead of ``expand_series`` when gaps must NOT be zero-filled (e.g.
    fractional metric series, where a missing bucket means "no data" rather than
    "the value dropped to zero"). Later points win on duplicate buckets. A NaN
    or infinite value is "no data" too and is dropped.
    """
    counts_by_bucket = {
        point.bucket: point.count for point in points if point.bucket < end_exclusive
    }
    counts_by_bucket = {
        bucket: count for bucket, count in counts_by_bucket.items() if isfinite(count)
    }
    return [
        SeriesPoint(bucket=bucket, count=counts_by_bucket[bucket])
        for bucket in sorted(counts_by_bucket)
    ]


def _emission_end(
    expanded: list[SeriesPoint],
    *,
    evaluation_end: datetime,
    settling_buckets: int,
) -> datetime:
    """First bucket that is still settling; rows at or after it are held back.

    Indexed off the END OF THE ANALYZED SERIES rather than off the grid slot
    ``evaluation_end - settling * interval``. The two agree for a zero-filled
    count series, but a sparse fractional series (e.g. a 1d ratio whose newest
    bucket is still missing the inputs that define it) has its
    newest PRESENT bucket held back even when that bucket already sits several
    slots behind the grid's last one.
    """
    if settling_buckets <= 0:
        return evaluation_end
    if len(expanded) <= settling_buckets:
        return expanded[0].bucket
    return expanded[-settling_buckets].bucket


def detect_anomalies(
    points: list[SeriesPoint],
    *,
    interval: timedelta,
    evaluation_start: datetime,
    evaluation_end: datetime,
    settings: AnomalyDetectionSettings,
    fill_gaps: bool = True,
    covered_buckets: set[datetime] | None = None,
    settling_buckets: int = 0,
) -> DetectionResult:
    """Hybrid detector.

    Primary signal: a per-bucket phase (seasonal) baseline — each bucket judged
    against the robust distribution of the same phase in prior cycles. This is
    what eliminates the phase-locked false positives (recurring troughs/peaks).
    Buckets without enough same-phase history fall back to a rolling baseline.

    Secondary signal: a deseasonalized STL trend-shift detector for slow level
    drifts the per-bucket band would absorb. Merged by larger |z|.

    ``fill_gaps`` (default ``True``) zero-fills missing buckets onto the interval
    grid — correct for count-shaped series. Set it ``False`` for fractional
    series (ratios/averages), where a missing bucket means "no data" and must
    not read as a drop to zero. Fractional series also swap the 1.0 absolute
    stddev floor for one derived from their own magnitude, so sub-unit
    movements stay detectable.

    ``covered_buckets`` (contract C2) is threaded into the zero-fill grid: when
    provided, buckets a scan never observed are excluded from evaluation instead
    of zero-filled, so a collection gap does not manufacture a "drop" (only
    meaningful for the ``fill_gaps`` count path). When ``None`` the behavior is
    unchanged.

    ``settling_buckets`` withholds the newest N buckets of the
    series from EMISSION. They stay in the series — baselines, charts and the
    stored metric values are unaffected — only their scoring is deferred to the
    next scan, by which time the warehouse has finished delivering them. Without
    it a bucket that is merely half-delivered reads as a drop that disappears 40
    minutes later. Use ``settling_buckets_for`` to convert an operator's
    wall-clock ingestion allowance into a bucket count.

    Returns a ``DetectionResult``, not a bare list: a pass can also report parts
    of its window that it declined to score and whose stored rows a caller must
    therefore NOT clear (``suppressed_ranges``, see ``_collapse_outage_runs``).
    Callers that only display or count anomalies can ignore that field; callers
    that PERSIST by clearing the window and re-inserting cannot.
    """
    if fill_gaps:
        expanded = expand_series(
            points,
            interval=interval,
            end_exclusive=evaluation_end,
            covered_buckets=covered_buckets,
        )
    else:
        expanded = _present_series(points, end_exclusive=evaluation_end)
    if not expanded:
        return DetectionResult(anomalies=[])

    emission_end = _emission_end(
        expanded, evaluation_end=evaluation_end, settling_buckets=settling_buckets
    )
    is_count_shaped = fill_gaps
    counts = [point.count for point in expanded]
    # Only a fractional lane can carry negative values (a catalog ``fact``
    # sum/avg/min/max or a ``sql`` level over a signed column). A count series is
    # non-negative by construction and a parity ratio lands in [0, 1], so this is
    # False for every count-shaped scope and the volume gates below keep their
    # historical signed comparison there.
    signed = min(counts) < 0.0
    slots = _grid_slots(expanded, interval)

    # Silent-series early exit: when no gate can realistically be
    # cleared, skip the per-bucket loop and the ~1s robust MSTL fit below — on
    # real projects tracking hundreds of low-volume events that fit was the
    # dominant scan cost (~1s per scope per run). See is_provably_silent for
    # the exact bound and the one acknowledged loss class. Count path only:
    # fractional series carry their own magnitude-derived floors.
    if is_count_shaped and is_provably_silent(max(counts), settings.min_expected_count):
        return DetectionResult(anomalies=[])

    stddev_absolute_floor = 1.0 if is_count_shaped else _fractional_stddev_floor(counts)
    # Poisson-aware floor (~sqrt(N)) applies to BOTH count-shaped per-bucket
    # paths. A flat/degenerate baseline's stddev
    # collapses to ~0, so a fixed 1.0 floor turns Poisson jitter (e.g. 10 -> 14)
    # into a 4-sigma flag. The phase path's same-phase MAD was assumed to carry
    # Poisson-scale spread empirically, but low-count seasonal series with few
    # cycles produce a MAD far below sqrt(N) (real production events flagged a +3
    # count wobble every hour), so it now gets the sqrt(N) floor too. High-volume
    # series are unaffected: sqrt(N) sits well below their real spread. The
    # averaged trend path keeps its own relative effect-size gate and is not
    # Poisson-floored. The fractional path keeps its magnitude floor and instead
    # exempts sustained monotonic ramps below.
    per_bucket_kind = "phase" if is_count_shaped else "fractional"
    rolling_kind = "rolling" if is_count_shaped else "fractional"
    primary: list[DetectedAnomaly] = []
    baselines: list[BaselinePoint] = []
    has_phase_period = False

    for idx, point in enumerate(expanded):
        if point.bucket < evaluation_start or point.bucket >= emission_end:
            continue

        # Fractional series: a bucket riding a smooth sustained ramp is a trend,
        # not a point anomaly — defer it to the trend-shift path instead of
        # flagging every rung.
        if not is_count_shaped and _continues_monotonic_trend(counts, idx):
            has_phase_period = (
                has_phase_period or _select_phase_period(interval, slots[idx]) is not None
            )
            continue

        period = _select_phase_period(interval, slots[idx])
        if period is not None:
            has_phase_period = True
            baseline = _phase_baseline_at(
                counts,
                slots,
                idx,
                period,
                settings,
                level_window=_phase_level_window(interval, period),
                stddev_absolute_floor=stddev_absolute_floor,
                poisson=is_count_shaped,
                signed=signed,
            )
            kind = per_bucket_kind
        else:
            baseline = _rolling_baseline_at(
                counts,
                idx,
                settings,
                stddev_absolute_floor=stddev_absolute_floor,
                poisson=is_count_shaped,
                signed=signed,
            )
            kind = rolling_kind
        if baseline is None:
            continue

        # Kept for every scored bucket, flagged or not. A NaN
        # or infinite band cannot be drawn and is not stored.
        if isfinite(baseline.expected_count) and isfinite(baseline.effective_stddev):
            baselines.append(
                BaselinePoint(
                    bucket=point.bucket,
                    expected_count=baseline.expected_count,
                    effective_stddev=baseline.effective_stddev,
                )
            )
        anomaly = _score_against_baseline(point, baseline, settings, kind=kind)
        if anomaly is not None:
            primary.append(anomaly)

    components = _fit_components(counts, interval=interval) if has_phase_period else None
    if components is None:
        merged = _merge_anomalies(primary)
    else:
        trend = _detect_trend_shift(
            expanded,
            components,
            evaluation_start=evaluation_start,
            settings=settings,
            interval=interval,
            stddev_absolute_floor=stddev_absolute_floor,
            signed=signed,
            emission_end=emission_end,
        )
        # Every bucket inside a shifted run describes the SAME incident as the
        # single trend row anchored at that run's start, so its per-bucket row is
        # dropped rather than merged. Without this a sustained
        # level change keeps clearing the per-bucket sigma bar for as long as the
        # baseline takes to re-level, and the incident re-enters the signal list
        # every scan.
        settled_primary = [
            anomaly for anomaly in primary if anomaly.bucket not in trend.shifted_buckets
        ]
        merged = _merge_anomalies(settled_primary, trend.anomalies)

    if not is_count_shaped:
        return DetectionResult(
            anomalies=merged, baselines=_drawable_baselines(baselines, primary, merged)
        )
    collapsed, suppressed_ranges = _collapse_outage_runs(
        merged,
        expanded,
        slots=slots,
        interval=interval,
        evaluation_start=evaluation_start,
    )
    return DetectionResult(
        anomalies=collapsed,
        suppressed_ranges=suppressed_ranges,
        baselines=_drawable_baselines(baselines, primary, collapsed),
    )


def _drawable_baselines(
    baselines: Sequence[BaselinePoint],
    primary: Sequence[DetectedAnomaly],
    emitted: Sequence[DetectedAnomaly],
) -> tuple[BaselinePoint, ...]:
    """The baselines the chart may draw without contradicting the stored rows.

    The chart's contract is "outside the band == flagged". A bucket whose own
    per-bucket anomaly was dropped because it belongs to a reported trend shift
    (``shifted_buckets``) or was folded into an outage's single announcement
    (``_collapse_outage_runs``) sits outside its band with no row of its own, so
    its baseline is withheld: the incident is reported once, by the row the
    pass kept, and the buckets it swallowed show no band. Every other scored
    bucket keeps its band — flagged buckets still carry a row, and unflagged
    ones sit inside the band by construction.
    """
    emitted_buckets = {anomaly.bucket for anomaly in emitted}
    silenced = {anomaly.bucket for anomaly in primary} - emitted_buckets
    if not silenced:
        return tuple(baselines)
    return tuple(point for point in baselines if point.bucket not in silenced)


def forecast_next_buckets(
    points: list[SeriesPoint],
    *,
    interval: timedelta,
    horizon: int = 1,
) -> list[ForecastPoint]:
    """One-step (or N-step) seasonal-naive + trend forecast.

    Fits its OWN STL/MSTL decomposition — the same KIND the anomaly detector
    fits, but a separate model: it calls ``STL(...).fit()`` / ``MSTL(...).fit()``
    directly rather than going through ``_fit_components_cached``, so it shares
    neither that cache nor its flat-series shortcut. A caller on the request path
    must therefore bound the series it hands in and expect to pay for a real fit;
    callers that do not read the result should not ask for one at all.

    It then extrapolates: trend continues with the slope of the last full
    seasonal period, and the seasonal component repeats the most recent cycle
    at the matching phase. Stddev
    comes from the robust scale of residuals so the UI can render a band of the
    same width as the historical anomaly band.

    Returns an empty list when there isn't enough history to fit a model.
    """
    if not points or horizon < 1:
        return []

    # A NaN/inf point would turn the whole STL fit into NaN.
    sorted_points = sorted(
        (point for point in points if isfinite(point.count)), key=lambda point: point.bucket
    )
    if not sorted_points:
        return []
    last_bucket = sorted_points[-1].bucket
    counts = [point.count for point in sorted_points]

    periods = _select_seasonal_periods(interval, len(counts))
    if not periods:
        return []

    values = np.asarray(counts, dtype=float)

    try:
        if len(periods) == 1:
            stl_result = STL(values, period=periods[0], robust=True).fit()
            seasonal_columns = np.asarray(stl_result.seasonal, dtype=float)[:, np.newaxis]
            trend = np.asarray(stl_result.trend, dtype=float)
            residuals = np.asarray(stl_result.resid, dtype=float)
        else:
            mstl_result = MSTL(values, periods=periods, stl_kwargs={"robust": True}).fit()
            seasonal_raw = np.asarray(mstl_result.seasonal, dtype=float)
            seasonal_columns = (
                seasonal_raw[:, np.newaxis] if seasonal_raw.ndim == 1 else seasonal_raw
            )
            trend = np.asarray(mstl_result.trend, dtype=float)
            residuals = np.asarray(mstl_result.resid, dtype=float)
    except Exception:
        return []

    # MSTL silently drops periods whose length exceeds half the series, so the
    # column count can be smaller than `periods`. The remaining columns line
    # up with the shortest periods we passed in (which matches the ascending
    # order of `_SEASONAL_PERIODS_BY_INTERVAL_SECONDS`).
    effective_periods = periods[: seasonal_columns.shape[1]]
    if not effective_periods:
        return []

    # Slope from the longest surviving period back to "now" — captures the
    # actual direction of the trend instead of bouncing on a 2-point delta.
    window = min(len(trend), max(effective_periods))
    last_trend = float(trend[-1])
    slope = (last_trend - float(trend[-window])) / (window - 1) if window >= 2 else 0.0

    stddev = _robust_scale(residuals.tolist())
    series_length = len(counts)

    forecasts: list[ForecastPoint] = []
    for step in range(1, horizon + 1):
        trend_future = last_trend + slope * step
        seasonal_future = 0.0
        for col, period in enumerate(effective_periods):
            # The same phase as the future bucket, taken from the LATEST cycle
            # rather than the first one: STL's seasonal component drifts over
            # the window, so a first-cycle lookup (``future_index % period``)
            # replays a stale amplitude.
            same_phase_index = series_length - period + ((step - 1) % period)
            seasonal_future += float(seasonal_columns[same_phase_index, col])
        expected = max(trend_future + seasonal_future, 0.0)
        forecasts.append(
            ForecastPoint(
                bucket=last_bucket + interval * step,
                expected_count=expected,
                stddev=stddev,
            )
        )
    return forecasts
