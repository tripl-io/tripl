"""Pure bucket-alignment / ratio evaluator for ``event_composition`` metrics.

No I/O: every function takes already-loaded per-bucket series (a mapping from a
bucket ``datetime`` to a numeric value) and returns NEW dictionaries. The
collection task in ``metric_collect`` loads the numerator/denominator series
from Postgres (event_metrics) or the warehouse (distinct users) and feeds them
here, so this module stays trivially unit-testable.

Composition semantics (per the metrics epic):

* ``single``            -- the numerator count per bucket.
* ``ratio``             -- numerator / denominator per bucket; a zero (or
  absent) denominator yields ``None`` (divide-by-zero), NEVER ``0`` and NEVER a
  silently dropped bucket. The bucket stays in the result mapped to ``None``.
* ``per_distinct_user`` -- numerator / per-bucket distinct-user count; identical
  arithmetic to ``ratio`` with the distinct-user series as the denominator.

Both ratio-style operators densify the two series onto the UNION of their
buckets first, so misaligned numerator/denominator grids line up: a bucket
present in only one series is treated as ``0`` in the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tripl.core.bucketing import to_utc
from tripl.models.domain_enums import MetricComposition


def normalize_series(series: Mapping[datetime, float]) -> dict[datetime, float]:
    """Stamp every bucket key as aware UTC.

    This module is the one place two INDEPENDENTLY SOURCED series meet — the numerator
    comes out of ``event_metrics`` while a ``per_distinct_user`` denominator is queried
    from the warehouse right now — and the sources do not agree on tz-awareness:
    ClickHouse hands back naive bucket cells, PostgreSQL aware ones, and the app DB adds
    its own opinion (SQLite reads naive). Mixing them made ``sorted()`` in
    :func:`union_buckets` raise ``TypeError: can't compare offset-naive and offset-aware
    datetimes``, so every ``per_distinct_user`` metric on a ClickHouse source failed
    collection outright.

    Coercing one side alone only moves the crash: normalizing the warehouse series broke
    the stored series instead. Both sides are normalized here, at the junction, so no
    future series source can reintroduce it.

    Treating a naive bucket as UTC is not a guess — it is the contract in
    ``core/bucketing``, which every adapter's generated SQL is measured against.
    """
    return {to_utc(bucket): value for bucket, value in series.items()}


def union_buckets(*series: Mapping[datetime, float]) -> list[datetime]:
    """Return the sorted union of every bucket appearing in any given series."""
    buckets: set[datetime] = set()
    for one in series:
        buckets.update(to_utc(bucket) for bucket in one)
    return sorted(buckets)


def _divide_over_buckets(
    numerator: Mapping[datetime, float],
    denominator: Mapping[datetime, float],
) -> dict[datetime, float | None]:
    """Densify onto the union of buckets, then divide with zero-safe semantics."""
    result: dict[datetime, float | None] = {}
    for bucket in union_buckets(numerator, denominator):
        den = float(denominator.get(bucket, 0.0))
        num = float(numerator.get(bucket, 0.0))
        # Divide-by-zero (or an absent denominator) -> None: the bucket records
        # "no value" rather than a misleading 0.
        result[bucket] = None if den == 0 else num / den
    return result


def evaluate_composition(
    composition: MetricComposition | str,
    *,
    numerator: Mapping[datetime, float],
    denominator: Mapping[datetime, float] | None = None,
) -> dict[datetime, float | None]:
    """Combine numerator/denominator bucket series per the composition operator.

    ``single`` ignores the denominator and returns the numerator verbatim.
    ``ratio`` and ``per_distinct_user`` densify onto the union of buckets and
    divide, mapping a zero/absent denominator to ``None``. Raises ``ValueError``
    when a denominator-requiring operator is given none.
    """
    operator = (
        composition
        if isinstance(composition, MetricComposition)
        else MetricComposition(composition)
    )
    # Normalize BEFORE anything compares or joins buckets — the two series arrive from
    # different sources that disagree on tz-awareness (see normalize_series).
    numerator = normalize_series(numerator)
    if operator is MetricComposition.single:
        return {bucket: float(value) for bucket, value in numerator.items()}
    if denominator is None:
        msg = f"composition {operator.value!r} requires a denominator series"
        raise ValueError(msg)
    return _divide_over_buckets(numerator, normalize_series(denominator))
