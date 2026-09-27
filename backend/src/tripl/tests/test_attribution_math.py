"""Contribution math of the "Why did it change?" panel (F02, #255).

The Done-when of #255: per column, the contributions sum to the delta. Proven
here over hand-built shapes (drops, spikes, mixed signs, new and vanished
values, a breakdown that covers only part of the scope) and a seeded sweep of
random shapes, plus the ranking, the release context and the one-liners.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from tripl.core.analyzers.attribution import (
    OTHER_LABEL,
    ColumnContribution,
    attribution_headline,
    attribution_payload,
    column_contributions,
    rank_columns,
    release_activations,
    release_context,
    release_context_at,
    release_line,
)


def _sum_of(column: ColumnContribution) -> float:
    return math.fsum(item.delta for item in column.contributions)


def _assert_invariants(column: ColumnContribution) -> None:
    assert _sum_of(column) == pytest.approx(column.delta, rel=1e-9, abs=1e-6)
    assert 0.0 <= column.explained_share <= 1.0
    assert len(column.values) <= 3
    assert all(not value.is_other for value in column.values)
    assert sum(1 for item in column.contributions if item.is_other) == 1
    magnitudes = [abs(value.delta) for value in column.values]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_single_value_drop_explains_the_whole_change() -> None:
    column = column_contributions(
        "platform",
        actual_total=500,
        expected_total=1000,
        actual_by_value={"ios": 100, "android": 290, "web": 110},
        baseline_by_value={"ios": 6000, "android": 3000, "web": 1000},
    )
    assert column is not None
    _assert_invariants(column)
    assert column.delta == -500
    top = column.values[0]
    assert (top.value, top.expected, top.actual) == ("ios", 600, 100)
    assert top.delta == pytest.approx(-500)
    assert top.share == pytest.approx(1.0)
    # ios -500 and android -10 point the same way: -510 / -500 clips to 1.
    assert column.explained_share == 1.0
    other = column.contributions[-1]
    assert other.value == OTHER_LABEL
    assert other.delta == pytest.approx(0.0, abs=1e-9)


def test_spike_with_mixed_signs() -> None:
    column = column_contributions(
        "country",
        actual_total=1600,
        expected_total=1000,
        actual_by_value={"us": 1200, "de": 150, "fr": 250},
        baseline_by_value={"us": 500, "de": 300, "fr": 200},
    )
    assert column is not None
    _assert_invariants(column)
    by_value = {item.value: item.delta for item in column.contributions}
    assert by_value["us"] == pytest.approx(700)
    assert by_value["de"] == pytest.approx(-150)
    assert by_value["fr"] == pytest.approx(50)
    # Only the same-sign top values count: (700 + 50) / 600, clipped.
    assert column.explained_share == 1.0
    assert [value.value for value in column.values] == ["us", "de", "fr"]
    assert column.values[1].share == pytest.approx(-150 / 600)


def test_two_values_share_the_drop() -> None:
    column = column_contributions(
        "platform",
        actual_total=700,
        expected_total=1000,
        actual_by_value={"ios": 400, "android": 300},
        baseline_by_value={"ios": 500, "android": 500},
    )
    assert column is not None
    _assert_invariants(column)
    # ios -100, android -200 of a -300 delta: both explain it, sum 1.0.
    assert column.explained_share == pytest.approx(1.0)
    assert [value.value for value in column.values] == ["android", "ios"]


def test_other_absorbs_the_part_of_the_scope_the_breakdown_does_not_cover() -> None:
    # The scope's baseline total is 2000 but the named values cover 1000: half
    # the scope sits in "Other" (a values-limit rollup, or a column only some
    # events carry).
    column = column_contributions(
        "platform",
        actual_total=1500,
        expected_total=2000,
        actual_by_value={"ios": 100, "android": 500},
        baseline_by_value={"ios": 500, "android": 500},
        baseline_total=2000,
    )
    assert column is not None
    _assert_invariants(column)
    by_value = {item.value: item for item in column.contributions}
    assert by_value["ios"].expected == pytest.approx(500)
    assert by_value["ios"].delta == pytest.approx(-400)
    assert by_value["android"].delta == pytest.approx(0)
    assert by_value[OTHER_LABEL].actual == pytest.approx(900)
    assert by_value[OTHER_LABEL].expected == pytest.approx(1000)
    assert by_value[OTHER_LABEL].delta == pytest.approx(-100)
    assert column.explained_share == pytest.approx(0.8)
    # A zero contribution is not a "top value".
    assert [value.value for value in column.values] == ["ios"]


def test_new_and_vanished_values() -> None:
    column = column_contributions(
        "app_store",
        actual_total=1000,
        expected_total=1000,
        actual_by_value={"new_store": 400, "kept": 600},
        baseline_by_value={"gone_store": 400, "kept": 600},
    )
    assert column is not None
    _assert_invariants(column)
    by_value = {item.value: item.delta for item in column.contributions}
    assert by_value["new_store"] == pytest.approx(400)
    assert by_value["gone_store"] == pytest.approx(-400)
    # A flat total explains nothing, whatever moved inside it.
    assert column.delta == 0
    assert column.explained_share == 0.0


def test_total_outage_puts_every_value_at_minus_expected() -> None:
    column = column_contributions(
        "platform",
        actual_total=0,
        expected_total=900,
        actual_by_value={},
        baseline_by_value={"ios": 200, "android": 100},
    )
    assert column is not None
    _assert_invariants(column)
    by_value = {item.value: item.delta for item in column.contributions}
    assert by_value["ios"] == pytest.approx(-600)
    assert by_value["android"] == pytest.approx(-300)
    assert column.explained_share == pytest.approx(1.0)


def test_no_baseline_means_no_attribution() -> None:
    assert (
        column_contributions(
            "platform",
            actual_total=10,
            expected_total=100,
            actual_by_value={"ios": 10},
            baseline_by_value={},
        )
        is None
    )
    assert (
        column_contributions(
            "platform",
            actual_total=10,
            expected_total=100,
            actual_by_value={"ios": 10},
            baseline_by_value={"ios": 5},
            baseline_total=0,
        )
        is None
    )


def test_top_values_are_capped_at_three() -> None:
    column = column_contributions(
        "country",
        actual_total=100,
        expected_total=600,
        actual_by_value={code: 10 * index for index, code in enumerate("abcdef")},
        baseline_by_value={code: 100 for code in "abcdef"},
    )
    assert column is not None
    _assert_invariants(column)
    assert len(column.values) == 3
    assert len(column.contributions) == 7  # six named + Other


@pytest.mark.parametrize("seed", range(40))
def test_contributions_sum_to_the_delta_over_random_shapes(seed: int) -> None:
    """STRUCTURAL invariant, not a quality check: ``Other`` is defined as the
    remainder of the delta, so a column sums to it by construction. This pins
    the bookkeeping (no value dropped, fsum precision, baseline shares) — it
    says nothing about whether the split is a good explanation."""
    rng = random.Random(seed)
    values = [f"v{index}" for index in range(rng.randint(1, 12))]
    baseline = {
        value: rng.choice([0.0, rng.uniform(0, 5000)]) for value in values if rng.random() > 0.1
    }
    actual = {value: rng.uniform(0, 5000) for value in values if rng.random() > 0.2}
    named_baseline = math.fsum(baseline.values())
    # Sometimes the scope carries more than the named values (an "Other").
    baseline_total = named_baseline * rng.choice([1.0, 1.0, 1.3, 2.0])
    actual_total = math.fsum(actual.values()) + rng.choice([0.0, rng.uniform(0, 3000)])
    expected_total = rng.uniform(0, 20000)

    column = column_contributions(
        "col",
        actual_total=actual_total,
        expected_total=expected_total,
        actual_by_value=actual,
        baseline_by_value=baseline,
        baseline_total=baseline_total or None,
    )
    if named_baseline <= 0:
        assert column is None
        return
    assert column is not None
    _assert_invariants(column)
    assert column.delta == pytest.approx(actual_total - expected_total)
    # Every value's expected is its baseline share of the expected total.
    for item in column.contributions:
        if not item.is_other:
            assert item.expected == pytest.approx(
                expected_total * baseline.get(item.value, 0.0) / baseline_total
            )


def test_rank_columns_orders_by_top_value_and_keeps_three() -> None:
    def column(name: str, drop: float) -> ColumnContribution | None:
        return column_contributions(
            name,
            actual_total=1000 - drop,
            expected_total=1000,
            actual_by_value={"a": 500 - drop, "b": 500},
            baseline_by_value={"a": 1, "b": 1},
        )

    ranked = rank_columns(
        [column("small", 10), None, column("big", 400), column("mid", 100), column("tiny", 1)]
    )
    assert [item.column for item in ranked] == ["big", "mid", "small"]


def test_headline_reads_the_stored_payload() -> None:
    payload = {
        "delta": -3390.0,
        "columns": [
            {
                "column": "platform",
                "explained_share": 0.95,
                "values": [
                    {
                        "value": "ios",
                        "delta": -3120.0,
                        "expected": 4000,
                        "actual": 880,
                        "share": 0.92,
                    },
                    {
                        "value": "web",
                        "delta": -140.0,
                        "expected": 500,
                        "actual": 360,
                        "share": 0.04,
                    },
                ],
            }
        ],
        "release": None,
    }
    assert attribution_headline(payload) == (
        "92% of the drop comes from platform = ios (−3,120 of −3,390)"
    )
    spike = {
        "delta": 600.0,
        "columns": [
            {
                "column": "country",
                "explained_share": 1.0,
                "values": [
                    {"value": "us", "delta": 700.0, "expected": 500, "actual": 1200, "share": 1.1}
                ],
            }
        ],
    }
    # A value that more than explains the change reads as 100%.
    assert attribution_headline(spike) == "100% of the spike comes from country = us (+700 of +600)"
    assert attribution_headline(None) is None
    assert attribution_headline({"delta": -10.0, "columns": []}) is None


def test_payload_round_trips_through_the_headline() -> None:
    column = column_contributions(
        "platform",
        actual_total=500,
        expected_total=1000,
        actual_by_value={"ios": 100, "android": 400},
        baseline_by_value={"ios": 600, "android": 400},
    )
    assert column is not None
    payload = attribution_payload(delta=-500.0, columns=[column], release=None)
    assert payload["columns"][0]["values"][0]["value"] == "ios"
    assert set(payload["columns"][0]["values"][0]) == {
        "value",
        "delta",
        "expected",
        "actual",
        "share",
    }
    assert attribution_headline(payload) == (
        "100% of the drop comes from platform = ios (−500 of −500)"
    )


def _value(value: str, delta: float) -> dict[str, float | str]:
    return {"value": value, "delta": delta, "expected": 0.0, "actual": 0.0, "share": 0.0}


def test_headline_quotes_the_column_with_the_highest_explained_share() -> None:
    payload = {
        "delta": -1000.0,
        "columns": [
            # Stored first (largest single value) but explains less.
            {
                "column": "country",
                "explained_share": 0.6,
                "gross_movement": 1400.0,
                "values": [_value("US", -600.0), _value("DE", 200.0)],
            },
            {
                "column": "platform",
                "explained_share": 0.9,
                "gross_movement": 1000.0,
                "values": [_value("ios", -500.0), _value("android", -400.0)],
            },
        ],
    }
    assert attribution_headline(payload) == (
        "50% of the drop comes from platform = ios (−500 of −1,000)"
    )


def test_offsetting_shifts_have_no_single_value_headline() -> None:
    # ios lost 900 while android gained ~850: a -50 net, ~1,750 of gross movement.
    column = column_contributions(
        "platform",
        actual_total=950,
        expected_total=1000,
        actual_by_value={"ios": 100, "android": 850},
        baseline_by_value={"ios": 1000, "android": 0.0001},
        baseline_total=1000,
    )
    assert column is not None
    payload = attribution_payload(delta=-50.0, columns=[column], release=None)
    assert payload["columns"][0]["gross_movement"] > 2 * 50
    assert attribution_headline(payload) == (
        "Platform shifted in both directions; no single value explains the drop"
    )


def test_offsetting_guard_without_a_stored_gross_uses_the_values() -> None:
    payload = {
        "delta": 100.0,
        "columns": [
            {
                "column": "country",
                "explained_share": 1.0,
                "values": [_value("US", 400.0), _value("DE", -300.0)],
            }
        ],
    }
    assert attribution_headline(payload, "spike") == (
        "Country shifted in both directions; no single value explains the spike"
    )


def test_an_opposite_signed_top_value_is_skipped_for_the_largest_same_sign_one() -> None:
    payload = {
        "delta": 1000.0,
        "columns": [
            {
                "column": "country",
                "explained_share": 0.75,
                "gross_movement": 1900.0,
                # DE moved the most, but against the spike.
                "values": [_value("DE", -450.0), _value("US", 400.0), _value("FR", 350.0)],
            }
        ],
    }
    assert attribution_headline(payload) == (
        "40% of the spike comes from country = US (+400 of +1,000)"
    )


def test_no_same_sign_value_reads_as_a_both_ways_shift() -> None:
    payload = {
        "delta": -100.0,
        "columns": [
            {
                "column": "platform",
                "explained_share": 0.0,
                "gross_movement": 150.0,
                "values": [_value("web", 50.0)],
            }
        ],
    }
    assert attribution_headline(payload, "drop") == (
        "Platform shifted in both directions; no single value explains the drop"
    )


def test_a_tiny_delta_is_quoted_whole_or_called_a_shift() -> None:
    consistent = {
        "delta": -3.0,
        "columns": [
            {
                "column": "platform",
                "explained_share": 1.0,
                "gross_movement": 3.0,
                "values": [_value("ios", -3.0)],
            }
        ],
    }
    assert attribution_headline(consistent) == (
        "100% of the drop comes from platform = ios (−3 of −3)"
    )
    # A near-flat total over a large internal reshuffle explains nothing.
    column = column_contributions(
        "app_store",
        actual_total=995,
        expected_total=1000,
        actual_by_value={"new_store": 400, "kept": 595},
        baseline_by_value={"gone_store": 400, "kept": 600},
    )
    assert column is not None
    payload = attribution_payload(delta=-5.0, columns=[column], release=None)
    assert attribution_headline(payload) == (
        "App_store shifted in both directions; no single value explains the drop"
    )
    assert attribution_headline({"delta": 0.0, "columns": payload["columns"]}) is None


def test_the_anomaly_direction_names_the_change() -> None:
    payload = {
        "delta": -10.0,
        "columns": [
            {
                "column": "os",
                "explained_share": 1.0,
                "gross_movement": 10.0,
                "values": [_value("ios", -10.0)],
            }
        ],
    }
    assert attribution_headline(payload, "drop") == (
        "100% of the drop comes from os = ios (−10 of −10)"
    )


# ── release context ──────────────────────────────────────────────────────────

_T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _hours(count: int) -> list[datetime]:
    return [_T0 + timedelta(hours=index) for index in range(count)]


def _rollout(
    new_shares: dict[int, float], *, hours: int = 11, total: float = 1000.0
) -> tuple[dict[str, dict[datetime, float]], dict[datetime, float]]:
    by_version: dict[str, dict[datetime, float]] = {"4.11": {}, "4.12": {}}
    traffic: dict[datetime, float] = {}
    for index, bucket in enumerate(_hours(hours)):
        share = new_shares.get(index, 0.0)
        by_version["4.12"][bucket] = total * share
        by_version["4.11"][bucket] = total * (1 - share)
        traffic[bucket] = total
    return by_version, traffic


def test_release_that_activated_hours_before_the_drop() -> None:
    by_version, traffic = _rollout({7: 0.10, 8: 0.20, 9: 0.30, 10: 0.38})
    anomaly_bucket = _T0 + timedelta(hours=10)
    release = release_context(
        by_version, traffic, anomaly_bucket=anomaly_bucket, window=timedelta(hours=24)
    )
    assert release is not None
    assert release.version == "4.12"
    assert release.previous_version == "4.11"
    assert release.reached_at == _T0 + timedelta(hours=7)
    assert release.share == pytest.approx(0.38)
    payload = attribution_payload(delta=-100.0, columns=[], release=release)
    assert release_line(payload, anomaly_bucket=anomaly_bucket) == (
        "Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop"
    )


def test_release_outside_the_window_is_not_context() -> None:
    by_version, traffic = _rollout(
        {3: 0.10, 4: 0.20, 5: 0.30, 6: 0.3, 7: 0.3, 8: 0.3, 9: 0.3, 10: 0.3}
    )
    release = release_context(
        by_version,
        traffic,
        anomaly_bucket=_T0 + timedelta(hours=10),
        window=timedelta(hours=4),
    )
    assert release is None


def test_release_activating_after_the_anomaly_is_ignored() -> None:
    by_version, traffic = _rollout({9: 0.02, 10: 0.2, 11: 0.4, 12: 0.5}, hours=13)
    # At the flagged bucket (hour 10) 4.12 has held the gate for one bucket
    # only; the buckets after it must not complete the run.
    release = release_context(
        by_version,
        traffic,
        anomaly_bucket=_T0 + timedelta(hours=10),
        window=timedelta(hours=24),
    )
    assert release is None


def test_prerelease_never_counts_as_a_release() -> None:
    by_version, traffic = _rollout({7: 0.10, 8: 0.20, 9: 0.30, 10: 0.38})
    by_version["4.12.0-beta.1"] = by_version.pop("4.12")
    release = release_context(
        by_version,
        traffic,
        anomaly_bucket=_T0 + timedelta(hours=10),
        window=timedelta(hours=24),
    )
    assert release is None


def _release_payload(reached_at: datetime, previous: str | None = "4.11") -> dict[str, object]:
    return {
        "delta": -100.0,
        "columns": [],
        "release": {
            "version": "4.12",
            "previous_version": previous,
            "share": 0.38,
            "reached_at": reached_at.isoformat(),
        },
    }


def test_release_lead_is_floored_to_whole_hours() -> None:
    bucket = _T0 + timedelta(hours=10)
    payload = _release_payload(bucket - timedelta(hours=2, minutes=59))
    assert release_line(payload, anomaly_bucket=bucket) == (
        "Release 4.12 (after 4.11) reached 38% of traffic 2h before the drop"
    )
    # Long leads stay in hours.
    payload = _release_payload(bucket - timedelta(hours=50, minutes=30), previous=None)
    assert release_line(payload, anomaly_bucket=bucket, direction="drop") == (
        "Release 4.12 reached 38% of traffic 50h before the drop"
    )


@pytest.mark.parametrize(
    "lead", [timedelta(0), timedelta(minutes=30), timedelta(hours=-2)], ids=str
)
def test_release_at_or_after_the_flagged_bucket_reads_at_the_change(lead: timedelta) -> None:
    bucket = _T0 + timedelta(hours=10)
    payload = _release_payload(bucket - lead, previous=None)
    assert release_line(payload, anomaly_bucket=bucket, direction="spike") == (
        "Release 4.12 reached 38% of traffic at the spike"
    )


@pytest.mark.parametrize(
    "shares",
    [
        {7: 0.10, 8: 0.20, 9: 0.30, 10: 0.38},
        {9: 0.02, 10: 0.2, 11: 0.4, 12: 0.5},
        {3: 0.10, 4: 0.20, 5: 0.30, 6: 0.3, 7: 0.3, 8: 0.3, 9: 0.3, 10: 0.3},
        {5: 0.01, 6: 0.2, 7: 0.01, 8: 0.2, 9: 0.2},
    ],
)
def test_once_per_run_activations_match_the_per_bucket_reference(
    shares: dict[int, float],
) -> None:
    """The worker's single pass gives every flagged bucket the same release the
    per-bucket reference (a slice truncated at that bucket) gives it."""
    # 400 a bucket: the first shape crosses the volume floor only after the
    # gate, so ``known_at`` is the volume bucket, not the gate's.
    by_version, traffic = _rollout(shares, hours=13, total=400.0)
    activations = release_activations(by_version, traffic)
    for hour in range(13):
        bucket = _T0 + timedelta(hours=hour)
        for window in (timedelta(hours=4), timedelta(hours=24)):
            assert release_context_at(
                activations, by_version, traffic, anomaly_bucket=bucket, window=window
            ) == release_context(by_version, traffic, anomaly_bucket=bucket, window=window)
