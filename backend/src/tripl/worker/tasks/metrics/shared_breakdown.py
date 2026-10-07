"""One warehouse read per scheduled tick instead of two, when the windows agree.

A scheduled tick reads its window twice: the catalog sync's breakdown
(``get_full_breakdown``: GROUP BY every column, ``ORDER BY _cnt DESC``) and the
metrics chunk (``get_time_bucketed_counts``: the same GROUP BY plus a time
bucket). Same base query, same columns, same JSON value paths. When the catalog
window IS the one collection chunk, the breakdown is just the bucketed rows
summed over ``_bucket`` — so the second read is pure cost. On a production
ClickHouse both reads were 3-20 GiB per window, and together the larger share of
the warehouse time a tick spends.

:func:`prefetch_tick_rows` runs the bucketed query once, before the catalog
sync. :class:`PrefetchedBreakdownAdapter` hands the catalog the folded rows for
the one ``get_full_breakdown`` call they answer, and delegates everything else
to the real adapter, so ``sync_catalog`` and the cardinality analyzers do not
change. ``process_chunk`` then reuses the same rows instead of querying again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tripl.core.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

_FrozenPaths = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class TickRows:
    """The bucketed rows of one chunk, with the arguments that produced them."""

    base_query: str
    regular_columns: tuple[str, ...]
    json_columns: tuple[str, ...]
    json_value_paths: _FrozenPaths
    time_column: str
    time_from: datetime
    time_to: datetime
    json_value_names: list[str]
    rows: list[tuple[object, ...]]


def _frozen_paths(json_value_paths: dict[str, list[str]] | None) -> _FrozenPaths:
    return tuple(
        sorted((column, tuple(paths)) for column, paths in (json_value_paths or {}).items())
    )


def prefetch_tick_rows(
    adapter: BaseAdapter,
    *,
    base_query: str,
    time_column: str,
    interval_code: str,
    regular_columns: list[str],
    json_columns: list[str],
    json_value_paths: dict[str, list[str]],
    time_from: datetime,
    time_to: datetime,
    metrics_row_limit: int,
) -> TickRows:
    """Run the chunk's bucketed query once, exactly as ``process_chunk`` would."""
    _names, json_value_names, rows = adapter.get_time_bucketed_counts(
        base_query,
        time_column,
        interval_code,
        regular_columns,
        json_columns,
        json_value_paths,
        time_from,
        time_to,
        limit=metrics_row_limit + 1,
    )
    return TickRows(
        base_query=base_query,
        regular_columns=tuple(regular_columns),
        json_columns=tuple(json_columns),
        json_value_paths=_frozen_paths(json_value_paths),
        time_column=time_column,
        time_from=time_from,
        time_to=time_to,
        json_value_names=list(json_value_names),
        rows=list(rows),
    )


def _hashable(value: object) -> object:
    """A grouping key for one cell: arrays and objects come back as lists/dicts."""
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable(v)) for k, v in value.items()))
    if isinstance(value, set):
        return frozenset(_hashable(v) for v in value)
    return value


def fold_buckets(rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    """``get_full_breakdown`` rows from ``get_time_bucketed_counts`` rows.

    Drops ``_bucket`` (the first cell), sums ``_cnt`` (the last) per remaining
    combination, and orders by that sum descending — the breakdown's
    ``ORDER BY _cnt DESC``. Ties keep first-seen order; the SQL leaves them
    unordered, so no caller may rely on them either way.
    """
    totals: dict[object, tuple[tuple[object, ...], list[int]]] = {}
    for row in rows:
        cells = tuple(row[1:-1])
        count = int(row[-1]) if row[-1] is not None else 0  # type: ignore[call-overload]
        key = _hashable(cells)
        entry = totals.get(key)
        if entry is None:
            totals[key] = (cells, [count])
        else:
            entry[1][0] += count
    folded = sorted(totals.values(), key=lambda entry: -entry[1][0])
    return [(*cells, total[0]) for cells, total in folded]


class PrefetchedBreakdownAdapter:
    """The real adapter, except for the one breakdown the prefetched rows answer.

    ``get_full_breakdown`` is served from :func:`fold_buckets` only when every
    argument matches the prefetch — base query, columns, JSON value paths and the
    time window — and otherwise goes to the warehouse as before. Every other
    attribute is the wrapped adapter's own.
    """

    def __init__(self, adapter: BaseAdapter, tick: TickRows) -> None:
        self._adapter = adapter
        self._tick = tick
        self.served = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def get_full_breakdown(
        self,
        base_query: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50000,
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        tick = self._tick
        if (
            base_query == tick.base_query
            and tuple(regular_columns) == tick.regular_columns
            and tuple(json_columns) == tick.json_columns
            and _frozen_paths(json_value_paths) == tick.json_value_paths
            and time_column == tick.time_column
            and time_from == tick.time_from
            and time_to == tick.time_to
        ):
            self.served = True
            rows = fold_buckets(tick.rows)[: int(limit)]
            logger.info(
                "Catalog breakdown folded from the tick's bucketed rows: %s combinations "
                "from %s bucketed rows, no second warehouse read",
                len(rows),
                len(tick.rows),
            )
            return list(regular_columns), list(json_columns), list(tick.json_value_names), rows
        logger.info("Catalog breakdown does not match the prefetched chunk; querying the warehouse")
        return self._adapter.get_full_breakdown(
            base_query,
            regular_columns,
            json_columns,
            json_value_paths=json_value_paths,
            time_column=time_column,
            time_from=time_from,
            time_to=time_to,
            limit=limit,
        )
