"""Why did it change? Contribution breakdown of a flagged bucket (F02, #255).

Pure functions only — no database access — so the arithmetic is unit-testable
and shared by the metrics worker (which stores the result with the anomaly at
detection time) and the demo seeder.

The model, for ONE flagged bucket of ONE scope and ONE breakdown column:

* ``delta = actual_total - expected_total`` — the anomaly row's own numbers;
* each value's baseline share ``share_v = baseline_v / baseline_total``, read off
  the buckets before the flagged one;
* ``expected_v = expected_total * share_v`` and ``contribution_v = actual_v -
  expected_v``;
* an ``Other`` bucket takes whatever the named values leave, so the
  contributions of a column ALWAYS sum to ``delta`` — values the breakdown
  rolled into its own "Other" row, values that never reached the breakdown, and
  any gap between the breakdown's sum and the scope total all land there;
* ``explained_share`` is the part of ``delta`` the column's top values explain:
  the sum of their contributions that point the same way as ``delta``, divided
  by ``delta``, clipped to 0..1.

Columns are STORED ranked by their top value's absolute contribution; the
headline quotes the column with the highest ``explained_share``
(``attribution_headline``). ``Other`` is never a ranked value: it has no
filter to open and says nothing about where the change came from.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tripl.core.bucketing import to_utc
from tripl.services.release_annotations import releases_to_annotate
from tripl.services.version_activation import (
    DEFAULT_ACTIVATION_MIN_BUCKETS,
    DEFAULT_ACTIVE_SHARE_MIN,
    DEFAULT_MIN_RELEASE_VOLUME,
    released_versions,
)

# How many columns an attribution keeps, and how many values per column.
TOP_COLUMNS = 3
TOP_VALUES = 3
# Label of the remainder bucket in ``ColumnContribution.contributions``.
OTHER_LABEL = "Other"


@dataclass(frozen=True)
class ValueContribution:
    """One value's part of the delta. ``share`` is ``delta / scope delta``, signed."""

    value: str
    actual: float
    expected: float
    delta: float
    share: float
    is_other: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "delta": self.delta,
            "expected": self.expected,
            "actual": self.actual,
            "share": self.share,
        }


@dataclass(frozen=True)
class ColumnContribution:
    """One breakdown column's split of the delta.

    ``contributions`` is every named value plus the ``Other`` remainder and sums
    to ``delta``; ``values`` is the ranked top of the named values only.
    """

    column: str
    delta: float
    explained_share: float
    values: tuple[ValueContribution, ...]
    contributions: tuple[ValueContribution, ...]

    @property
    def top_magnitude(self) -> float:
        return abs(self.values[0].delta) if self.values else 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "explained_share": self.explained_share,
            # sum(|contribution|) over every value and Other: the headline's
            # offsetting-shift guard reads it.
            "gross_movement": math.fsum(abs(item.delta) for item in self.contributions),
            "values": [value.to_payload() for value in self.values],
        }


@dataclass(frozen=True)
class ReleaseContext:
    """A release that crossed the activation gate shortly before the anomaly."""

    version: str
    previous_version: str | None
    share: float
    reached_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "previous_version": self.previous_version,
            "share": self.share,
            "reached_at": self.reached_at.isoformat(),
        }


def _share_of(part: float, whole: float) -> float:
    return part / whole if whole != 0 else 0.0


def _clip01(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def column_contributions(
    column: str,
    *,
    actual_total: float,
    expected_total: float,
    actual_by_value: Mapping[str, float],
    baseline_by_value: Mapping[str, float],
    baseline_total: float | None = None,
    top_n: int = TOP_VALUES,
) -> ColumnContribution | None:
    """Split ``actual_total - expected_total`` across one column's values.

    ``actual_by_value`` / ``baseline_by_value`` hold NAMED values only; a
    breakdown's own "Other" row is left out and falls into the remainder.
    ``baseline_total`` is the denominator of the baseline shares — the scope's
    total over the baseline buckets. It defaults to the named values' baseline
    sum. Returns ``None`` when there is no baseline to take shares from.
    """
    if baseline_total is None:
        baseline_total = math.fsum(baseline_by_value.values())
    if baseline_total <= 0:
        return None

    delta = actual_total - expected_total
    named: list[ValueContribution] = []
    for value in sorted(set(actual_by_value) | set(baseline_by_value)):
        actual = float(actual_by_value.get(value, 0.0))
        expected = expected_total * float(baseline_by_value.get(value, 0.0)) / baseline_total
        value_delta = actual - expected
        named.append(
            ValueContribution(
                value=value,
                actual=actual,
                expected=expected,
                delta=value_delta,
                share=_share_of(value_delta, delta),
            )
        )

    other_actual = actual_total - math.fsum(item.actual for item in named)
    other_expected = expected_total - math.fsum(item.expected for item in named)
    # Taken as the remainder of the DELTA, not ``other_actual - other_expected``,
    # so the column sums to the scope's delta to the last bit fsum can give.
    other_delta = delta - math.fsum(item.delta for item in named)
    other = ValueContribution(
        value=OTHER_LABEL,
        actual=other_actual,
        expected=other_expected,
        delta=other_delta,
        share=_share_of(other_delta, delta),
        is_other=True,
    )

    ranked = sorted(
        (item for item in named if item.delta != 0),
        key=lambda item: (-abs(item.delta), item.value),
    )
    top = tuple(ranked[: max(top_n, 0)])
    if delta == 0:
        explained = 0.0
    else:
        same_sign = math.fsum(item.delta for item in top if (item.delta > 0) == (delta > 0))
        explained = _clip01(same_sign / delta)
    return ColumnContribution(
        column=column,
        delta=delta,
        explained_share=explained,
        values=top,
        contributions=(*named, other),
    )


def rank_columns(
    columns: Iterable[ColumnContribution | None], *, top_n: int = TOP_COLUMNS
) -> list[ColumnContribution]:
    """The ``top_n`` columns, by their top value's absolute contribution."""
    usable = [column for column in columns if column is not None and column.values]
    usable.sort(key=lambda column: (-column.top_magnitude, column.column))
    return usable[: max(top_n, 0)]


def _traffic_at_or_before(
    traffic: Mapping[datetime, float], bucket: datetime
) -> tuple[datetime, float] | None:
    candidates = [key for key, total in traffic.items() if key <= bucket and total > 0]
    if not candidates:
        return None
    latest = max(candidates)
    return latest, traffic[latest]


@dataclass(frozen=True)
class ReleaseActivation:
    """When a release crossed the activation gate, and from when that is KNOWN.

    ``reached_at`` is the first bucket of the gate run (the release marker's
    bucket). ``known_at`` is the first bucket by which a slice ending there
    already shows the activation: the gate has held for ``min_buckets``
    buckets AND the release has carried ``min_volume`` events. A flagged bucket
    before ``known_at`` must not see the release — that would be the future
    leaking into the past.
    """

    version: str
    reached_at: datetime
    known_at: datetime


def _gate_run(
    release_by_bucket: Mapping[datetime, float],
    buckets: list[datetime],
    all_by_bucket: Mapping[datetime, float],
    *,
    share_min: float,
    min_buckets: int,
) -> tuple[datetime, datetime] | None:
    """``(run start, bucket the run completed)`` — ``activation_bucket``'s rule."""
    run_start: datetime | None = None
    run_len = 0
    for bucket in buckets:
        total = all_by_bucket.get(bucket, 0)
        share = (release_by_bucket.get(bucket, 0) / total) if total > 0 else 0.0
        if share >= share_min:
            if run_len == 0:
                run_start = bucket
            run_len += 1
            if run_len >= min_buckets and run_start is not None:
                return run_start, bucket
        else:
            run_len = 0
            run_start = None
    return None


def release_activations(
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
    all_by_bucket: Mapping[datetime, float],
    *,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
    min_buckets: int = DEFAULT_ACTIVATION_MIN_BUCKETS,
    min_volume: float = DEFAULT_MIN_RELEASE_VOLUME,
    prerelease_pattern: re.Pattern[str] | None = None,
) -> dict[str, ReleaseActivation]:
    """Every release that shipped inside the slice — computed ONCE per run.

    The same rule as ``releases_to_annotate`` (absent from the slice's leading
    buckets, then clears the activation gate on ``min_volume`` events), but over
    the whole loaded slice, remembering from which bucket each activation is
    visible. ``releases_to_annotate`` over the slice truncated at bucket ``B``
    returns exactly the versions here with ``known_at <= B``, at the same
    ``reached_at`` — so one pass serves every flagged bucket of the run.
    """
    buckets = sorted(all_by_bucket)
    leading = buckets[: max(min_buckets, 1)]
    if not leading or sum(all_by_bucket.get(bucket, 0) for bucket in leading) <= 0:
        return {}
    eligible = released_versions(
        per_bucket_totals_by_version, prerelease_pattern=prerelease_pattern
    )
    result: dict[str, ReleaseActivation] = {}
    for version in eligible:
        by_bucket = per_bucket_totals_by_version[version]
        if any(by_bucket.get(bucket, 0) > 0 for bucket in leading):
            continue  # live before the slice began: not shipped inside it
        run = _gate_run(
            by_bucket, buckets, all_by_bucket, share_min=share_min, min_buckets=min_buckets
        )
        if run is None:
            continue
        volume_at: datetime | None = None
        running = 0.0
        for bucket in sorted(by_bucket):
            running += float(by_bucket[bucket])
            if running >= min_volume:
                volume_at = bucket
                break
        if volume_at is None:
            continue
        reached_at, completed_at = run
        result[version] = ReleaseActivation(
            version=version, reached_at=reached_at, known_at=max(completed_at, volume_at)
        )
    return result


def release_context_at(
    activations: Mapping[str, ReleaseActivation],
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
    all_by_bucket: Mapping[datetime, float],
    *,
    anomaly_bucket: datetime,
    window: timedelta,
    prerelease_pattern: re.Pattern[str] | None = None,
) -> ReleaseContext | None:
    """``release_context`` for one flagged bucket, from once-per-run activations.

    Only activations already visible at the flagged bucket (``known_at``) and
    reached inside ``window`` before it count; the most recent one wins. The
    share and the previous version read buckets at or before the flagged one
    only.
    """
    earliest = anomaly_bucket - window
    recent = [
        (activation.reached_at, activation.version)
        for activation in activations.values()
        if activation.known_at <= anomaly_bucket
        and earliest <= activation.reached_at <= anomaly_bucket
    ]
    if not recent:
        return None
    reached_at, version = max(recent)

    traffic = {bucket: total for bucket, total in all_by_bucket.items() if bucket <= anomaly_bucket}
    series = per_bucket_totals_by_version.get(version, {})
    at = _traffic_at_or_before(traffic, anomaly_bucket)
    share = 0.0
    if at is not None:
        bucket, total = at
        share = _share_of(float(series.get(bucket, 0.0)), float(total))

    before = {
        other: math.fsum(count for bucket, count in other_series.items() if bucket < reached_at)
        for other, other_series in per_bucket_totals_by_version.items()
        if other != version
    }
    eligible = released_versions(
        (other for other, volume in before.items() if volume > 0),
        prerelease_pattern=prerelease_pattern,
    )
    previous = max(sorted(eligible), key=lambda other: before[other]) if eligible else None
    return ReleaseContext(
        version=version,
        previous_version=previous,
        share=_clip01(share),
        reached_at=reached_at,
    )


def release_context(
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
    all_by_bucket: Mapping[datetime, float],
    *,
    anomaly_bucket: datetime,
    window: timedelta,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
    min_buckets: int = DEFAULT_ACTIVATION_MIN_BUCKETS,
    min_volume: float = DEFAULT_MIN_RELEASE_VOLUME,
    prerelease_pattern: re.Pattern[str] | None = None,
) -> ReleaseContext | None:
    """The release that activated within ``window`` before the flagged bucket.

    Uses the release-marker rule (``releases_to_annotate``: absent when the slice
    begins, then clears the activation gate) over the slice that ENDS at the
    flagged bucket, so nothing after the anomaly can influence it. When several
    releases qualify the most recent activation wins. ``share`` is the release's
    share of traffic at the flagged bucket (or the newest bucket before it that
    had traffic); ``previous_version`` is the released version that carried the
    most traffic before the new one activated.

    The one-bucket reference form; the worker computes ``release_activations``
    once per run and calls ``release_context_at`` per bucket, which gives the
    same answer.
    """
    traffic = {bucket: total for bucket, total in all_by_bucket.items() if bucket <= anomaly_bucket}
    if not traffic:
        return None
    by_version = {
        version: {bucket: count for bucket, count in series.items() if bucket <= anomaly_bucket}
        for version, series in per_bucket_totals_by_version.items()
    }
    activated = releases_to_annotate(
        by_version,
        traffic,
        share_min=share_min,
        min_buckets=min_buckets,
        min_volume=min_volume,
        prerelease_pattern=prerelease_pattern,
    )
    activations = {
        version: ReleaseActivation(version=version, reached_at=reached_at, known_at=reached_at)
        for version, reached_at in activated.items()
    }
    return release_context_at(
        activations,
        by_version,
        traffic,
        anomaly_bucket=anomaly_bucket,
        window=window,
        prerelease_pattern=prerelease_pattern,
    )


# ── one-liners, shared by the API headline and the alert messages ───────────
#
# THE wording of an attribution. The API returns these strings as
# ``SignalAttribution.headline`` / ``release_line`` and the UI renders them
# verbatim; an alert's line is exactly ``headline`` (+ ``"; " + release_line``).
# No other module may re-derive a percentage.

_MINUS = "−"
# A column whose values moved more than this multiple of the scope's delta in
# total (both directions) is a mix shift, not a cause: no single value explains
# the change, so the headline says so instead of quoting a percentage.
OFFSETTING_GROSS_RATIO = 2.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _fmt_count(value: float) -> str:
    """``−3,120`` / ``+412`` / ``0`` — whole counts, thousands separators."""
    rounded = round(value)
    text = f"{abs(rounded):,}"
    if rounded < 0:
        return f"{_MINUS}{text}"
    if rounded > 0:
        return f"+{text}"
    return text


def change_word(delta: float, direction: str | None = None) -> str:
    """``drop`` / ``spike``: the anomaly's own direction when given, else the sign."""
    if direction is not None and str(direction) in {"drop", "spike"}:
        return str(direction)
    return "drop" if delta < 0 else "spike"


def _column_label(column: str) -> str:
    return column[:1].upper() + column[1:]


def _headline_column(columns: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The column with the highest ``explained_share``; ties keep the stored order."""
    best = columns[0]
    best_share = _number(best.get("explained_share")) or 0.0
    for column in columns[1:]:
        share = _number(column.get("explained_share")) or 0.0
        if share > best_share:
            best, best_share = column, share
    return best


def _gross_movement(column: Mapping[str, Any], deltas: list[float], delta: float) -> float:
    """``sum(|contribution|)`` over the column, ``Other`` included.

    The worker stores it as ``gross_movement``. A payload without it (written
    before the field existed) falls back to the stored top values plus the rest
    of the delta as one lump — a lower bound, so the guard only errs towards
    quoting a value.
    """
    stored = _number(column.get("gross_movement"))
    if stored is not None:
        return abs(stored)
    return math.fsum(abs(item) for item in deltas) + abs(delta - math.fsum(deltas))


def attribution_headline(
    payload: Mapping[str, Any] | None, direction: str | None = None
) -> str | None:
    """``92% of the drop comes from platform = ios (−3,120 of −3,390)``.

    Reads the STORED payload shape (``{delta, columns: [...], release}``):

    * the column is the one with the highest ``explained_share``;
    * the value is that column's largest value pointing the same way as the
      delta; the percent is its delta over the scope's delta, clipped to 0..1
      and rounded to a whole percent;
    * when the column's gross movement (``sum(|contribution|)``) exceeds twice
      ``|delta|``, or no value points the same way, the headline is
      ``<Column> shifted in both directions; no single value explains the
      <drop|spike>`` — no percent.

    ``None`` when there is nothing to say (no delta, no column, no values).
    """
    if not payload or not isinstance(payload, Mapping):
        return None
    delta = _number(payload.get("delta"))
    raw_columns = payload.get("columns")
    if delta is None or delta == 0 or not isinstance(raw_columns, list | tuple):
        return None
    columns = [
        column
        for column in raw_columns
        if isinstance(column, Mapping)
        and isinstance(column.get("column"), str)
        and column["column"]
    ]
    if not columns:
        return None
    word = change_word(delta, direction)
    column = _headline_column(columns)
    name = str(column["column"])
    raw_values = column.get("values")
    entries: list[tuple[str, float]] = []
    if isinstance(raw_values, list | tuple):
        for entry in raw_values:
            if not isinstance(entry, Mapping):
                continue
            value_delta = _number(entry.get("delta"))
            if value_delta is None:
                continue
            entries.append((str(entry.get("value")), value_delta))
    if not entries:
        return None
    same_sign = [
        (value, amount) for value, amount in entries if amount != 0 and (amount > 0) == (delta > 0)
    ]
    gross = _gross_movement(column, [amount for _, amount in entries], delta)
    if not same_sign or gross > OFFSETTING_GROSS_RATIO * abs(delta):
        return (
            f"{_column_label(name)} shifted in both directions; no single value explains the {word}"
        )
    value, amount = max(same_sign, key=lambda item: abs(item[1]))
    percent = round(_clip01(amount / delta) * 100)
    return (
        f"{percent}% of the {word} comes from {name} = {value} "
        f"({_fmt_count(amount)} of {_fmt_count(delta)})"
    )


def _parse_utc(value: object) -> datetime | None:
    """A datetime, or the ISO-8601 text a stored payload holds, as aware UTC.

    ``None`` for anything else, so a malformed payload drops the release line
    instead of raising into an alert send.
    """
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return to_utc(value)


def release_line(
    payload: Mapping[str, Any] | None,
    *,
    anomaly_bucket: datetime,
    direction: str | None = None,
) -> str | None:
    """``Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop``.

    The lead is FLOORED to whole hours; a lead under an hour (or a release that
    reached the gate in the flagged bucket itself) reads ``at the drop``.
    ``(after Y)`` is left out when no previous release is known.
    """
    if not payload or not isinstance(payload, Mapping):
        return None
    release = payload.get("release")
    if not isinstance(release, Mapping):
        return None
    version = release.get("version")
    reached_at = _parse_utc(release.get("reached_at"))
    bucket = _parse_utc(anomaly_bucket)
    if not version or reached_at is None or bucket is None:
        return None
    word = change_word(_number(payload.get("delta")) or 0.0, direction)
    percent = round(_clip01(_number(release.get("share")) or 0.0) * 100)
    hours = math.floor((bucket - reached_at).total_seconds() / 3600)
    timing = f"{hours}h before the {word}" if hours > 0 else f"at the {word}"
    previous = release.get("previous_version")
    after = f" (after {previous})" if previous else ""
    return f"Release {version}{after} reached {percent}% of traffic {timing}"


def attribution_payload(
    *,
    delta: float,
    columns: Iterable[ColumnContribution],
    release: ReleaseContext | None,
) -> dict[str, Any]:
    """The stored shape: ``{delta, columns: [...], release}``."""
    return {
        "delta": delta,
        "columns": [column.to_payload() for column in columns],
        "release": release.to_payload() if release is not None else None,
    }
