"""Small-sample guard for the distribution-drift PSI analyzer.

Raw PSI is sample-size driven: under the null hypothesis (both windows drawn
from the same mix) ``n_eff * PSI`` follows chi2(K-1). The fixed 0.1 / 0.25 bands
therefore called a sparse per-event-type scope "significant" most of the time,
purely because it had drawn a handful of events. ``compute_psi`` now floors an
absent category at half an observation *of its own window* and subtracts the
noise a window of that size and value count scores on its own before banding.

``compute_psi`` is pure arithmetic over two dicts, so every vector below is
hand-written and deterministic — no RNG, no session, no SQLite caveat.
"""

from __future__ import annotations

from tripl.core.analyzers.distribution_drift import (
    PSI_BAND_MINOR,
    PSI_BAND_SIGNIFICANT,
    compute_psi,
)

# 14 buckets of a uniform 20-value mix: the shape a per-event-type breakdown
# baseline has when the field is high-cardinality and the scope is low-volume.
_UNIFORM_20_BASELINE = {f"v{index}": 35 for index in range(20)}

# A plausible 50-event draw from that very mix (18 of the 20 values seen,
# v18/v19 simply did not come up). Zero real drift.
_UNIFORM_20_SAMPLE_OF_50 = {
    "v0": 5,
    "v1": 1,
    "v2": 4,
    "v3": 2,
    "v4": 3,
    "v5": 1,
    "v6": 4,
    "v7": 2,
    "v8": 3,
    "v9": 2,
    "v10": 6,
    "v11": 1,
    "v12": 3,
    "v13": 2,
    "v14": 4,
    "v15": 2,
    "v16": 1,
    "v17": 4,
}


def test_small_current_window_is_not_significant() -> None:
    # Before the guard this scored 0.8991 / "significant" and became an alert
    # candidate, on a window that had not drifted at all.
    result = compute_psi(_UNIFORM_20_BASELINE, _UNIFORM_20_SAMPLE_OF_50)
    assert result.current_total == 50
    assert result.band == "stable", (result.psi, result.psi_raw, result.psi_noise_floor)
    assert result.psi == 0.0
    # The raw divergence is real but entirely inside the noise this window size
    # produces on its own.
    assert result.psi_raw > 0.0
    assert result.psi_noise_floor > result.psi_raw


def test_same_shares_are_drift_once_the_window_is_large_enough() -> None:
    # Identical observed proportions, 20x the events. The noise floor scales
    # with n, so this is a correction and not a blanket ban on small windows.
    scaled_current = {value: count * 20 for value, count in _UNIFORM_20_SAMPLE_OF_50.items()}
    small = compute_psi(_UNIFORM_20_BASELINE, _UNIFORM_20_SAMPLE_OF_50)
    large = compute_psi(_UNIFORM_20_BASELINE, scaled_current)
    assert small.band == "stable"
    assert large.band == "significant"
    assert large.psi_noise_floor < small.psi_noise_floor


def test_single_absent_category_does_not_dominate_a_small_window() -> None:
    # Every value present keeps its exact baseline share; only v19 is missing
    # from the current window. The old constant 1e-4 floor turned that single
    # absence into psi 0.3127 / "significant" on its own.
    baseline = {f"v{index}": 100 for index in range(20)}
    current = {f"v{index}": 10 for index in range(19)}
    result = compute_psi(baseline, current)
    assert result.band == "stable", (result.psi, result.psi_raw, result.psi_noise_floor)
    assert result.psi_raw < PSI_BAND_SIGNIFICANT


def test_bands_are_unchanged_at_production_volume() -> None:
    # The vector the bands were calibrated on: a 50/50 -> 90/10 platform shift
    # over 1000 events per window. The correction must be negligible here.
    result = compute_psi({"ios": 500, "android": 500}, {"ios": 900, "android": 100})
    assert result.band == "significant"
    assert result.psi > 0.85
    assert result.psi_noise_floor < 0.01
    assert result.psi < result.psi_raw


def test_real_drift_still_fires_on_a_small_window() -> None:
    # A 50-event window that genuinely went 50/50 -> 90/10 is far outside the
    # noise floor for a two-value field, so it still alerts.
    result = compute_psi({"ios": 3500, "android": 3500}, {"ios": 45, "android": 5})
    assert result.current_total == 50
    assert result.band == "significant"


def test_top_mover_contributions_are_never_negative() -> None:
    # (current - baseline) and log(current / baseline) always share a sign, so a
    # contribution cannot be negative. Direction is read from the two shares.
    result = compute_psi({"ios": 500, "android": 500}, {"ios": 900, "android": 100})
    assert result.top_movers
    assert all(shift.contribution >= 0.0 for shift in result.top_movers)
    dropped = next(shift for shift in result.top_movers if shift.value == "android")
    assert dropped.current_share < dropped.baseline_share


def test_empty_baseline_is_stable() -> None:
    # Previously the current keys were scored against a 1e-4 baseline and came
    # back as a large PSI; the worker only guards the other direction.
    result = compute_psi({}, {"ios": 5})
    assert result.psi == 0.0
    assert result.band == "stable"
    assert result.baseline_total == 0
    assert result.current_total == 5
    assert result.top_movers == []


def test_empty_current_window_is_stable() -> None:
    result = compute_psi({"ios": 5}, {})
    assert result.psi == 0.0
    assert result.band == "stable"
    assert result.top_movers == []


def test_reported_psi_always_matches_its_band() -> None:
    # The persisted score and band must never disagree in the UI, which renders
    # them side by side.
    vectors = [
        (_UNIFORM_20_BASELINE, _UNIFORM_20_SAMPLE_OF_50),
        ({"ios": 500, "android": 500}, {"ios": 900, "android": 100}),
        ({"ios": 600, "android": 400}, {"ios": 800, "android": 200}),
        ({"existing": 1000}, {"existing": 200, "brand-new": 800}),
    ]
    for baseline, current in vectors:
        result = compute_psi(baseline, current)
        expected = (
            "stable"
            if result.psi < PSI_BAND_MINOR
            else "minor"
            if result.psi < PSI_BAND_SIGNIFICANT
            else "significant"
        )
        assert result.band == expected, (baseline, current, result.psi, result.band)
        assert result.psi == max(result.psi_raw - result.psi_noise_floor, 0.0)
