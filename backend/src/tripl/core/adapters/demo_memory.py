"""The demo warehouse's in-memory engine.

Serves the demo warehouse model (:mod:`tripl.core.demo_warehouse`) through the
adapter contract with no database: the rows the writer would put in PostgreSQL
are generated on demand, an hour at a time (cached per process), and
aggregated in Python by :class:`~tripl.core.adapters.synthetic.SyntheticAdapter`'s
machinery. It is what a ``demo`` source runs on when the PostgreSQL warehouse
is not provisioned or not reachable, and what the SQLite test suite runs on.

It answers like :class:`~tripl.core.adapters.demo_postgres.DemoPostgresAdapter`
does over the same rows, and it is held to that, not only by intent:

* the tables, column names and type names are the warehouse's
  (:mod:`tripl.core.demo_warehouse.schema`), as psycopg reports them;
* a JSON column groups by its sorted top-level keys (a list) and a value path
  by the value's JSON text, as ``PostgresAdapter`` extracts them; a property
  breakdown (``properties.source``) reads the unquoted scalar, as ``#>>`` does;
* ``amount`` is a ``Decimal``, as ``numeric`` arrives through psycopg;
* the demo's distinct-sessions sql metric is recognized in its PostgreSQL
  spelling (:data:`~tripl.core.demo_warehouse.schema.ACTIVE_SESSIONS_SQL`).

Rows exist through the end of the current five-minute slice
(:func:`~tripl.core.demo_warehouse.rows.available_through`), which is what the
writer has materialized when it is on schedule.

Bounded on purpose. A query reads at most :data:`MAX_EVENTS_WINDOW` of
``events`` (about 220k rows): a collection is chunked by its scan config's
``replay_chunk_interval``, and the one statement that spans a whole replay, the
top-N ranking, is counted as a stream instead of a list. A query with no time
window reads the last :data:`DEFAULT_WINDOW`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from tripl.core.adapters.base import (
    ColumnInfo,
    SchemaColumn,
    SchemaTable,
    is_breakdown_field_of,
)
from tripl.core.adapters.sql_common import IDENTIFIER_PART_RE
from tripl.core.adapters.synthetic import (
    JsonLayout,
    SyntheticAdapter,
    SyntheticCapabilityError,
    _bval,
    _normalize_sql,
    _where_predicates,
)
from tripl.core.bucketing import floor_to_bucket, to_utc
from tripl.core.demo_warehouse import rows as model_rows
from tripl.core.demo_warehouse import schema
from tripl.json_paths import split_property_field

#: The longest ``events`` window one query may read (rows are held in memory).
MAX_EVENTS_WINDOW = timedelta(days=8)
#: ``orders`` is ~250 rows a day, so any retained window fits.
MAX_ORDERS_WINDOW = timedelta(days=45)
#: What a query without a time window reads: the newest day.
DEFAULT_WINDOW = timedelta(days=1)
#: How far back the distinct-sessions metric looks without a window.
ACTIVE_SESSIONS_DEFAULT_DAYS = 30

#: ``information_schema.columns.data_type`` of each PostgreSQL type, for the
#: SQL editor's table list (what ``PostgresAdapter.get_schema_tables`` reports).
_INFORMATION_SCHEMA_TYPES = {
    "timestamptz": "timestamp with time zone",
    "text": "text",
    "numeric": "numeric",
    "jsonb": "jsonb",
}
_ACTIVE_SESSIONS_STATEMENT = _normalize_sql(schema.ACTIVE_SESSIONS_SQL)
_MISSING = object()


class _KeyList(list[str]):
    """A JSON document's sorted top-level keys: a list, as psycopg returns
    ``text[]``, that can also sit in a grouping key."""

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(tuple(self))


def _json_text(value: object) -> str:
    """``(jsonb -> 'key')::text``: the value as JSON text, as PostgreSQL prints it."""
    return json.dumps(value, ensure_ascii=False)


def _scalar_text(value: object) -> str:
    """``jsonb #>> path``: a scalar unquoted, a nested value as JSON text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return _json_text(value)
    return str(value)


def _walk(document: object, parts: tuple[str, ...]) -> object:
    value = document
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _path_parts(path: str) -> tuple[str, ...]:
    parts = tuple(part for part in path.split(".") if part)
    if not parts or any(not IDENTIFIER_PART_RE.match(part) for part in parts):
        msg = f"Unsupported JSON path: {path}"
        raise ValueError(msg)
    return parts


@lru_cache(maxsize=64)
def _sessions_on_day(day: datetime) -> int:
    """Distinct ``session_id`` of one COMPLETE UTC day (cached: days do not change)."""
    return len(
        frozenset().union(*(model_rows.sessions_in_hour(day + h * _HOUR) for h in range(24)))
    )


_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)
_TICK = timedelta(microseconds=1)

type _Key = tuple[object, ...]
type _HourRows = tuple[dict[str, object], ...] | list[dict[str, object]]


class _Groups:
    """Row counts by ``(bucket, *lead, *rest)``, ordered as ``SyntheticAdapter`` orders them.

    ``rest`` is a row's dimension columns and JSON key part, the long tail of
    every grouping key; ``lead`` is what a statement puts before it (a
    breakdown value). Groups are held per distinct ``rest``, so its sort form
    (``_bval`` of each cell) is computed once rather than once per group, and
    each group carries the order it first appeared in: ties of the sort key
    then fall in insertion order, as the stable sort over the synthetic
    adapter's insertion-ordered groups leaves them. ``seq`` is that order: the
    ``(hour, row position)`` of a group's first row, in the scan's row order.
    One counter set per ``slots`` (the breakdowns of a multi-breakdown
    statement).
    """

    __slots__ = ("_ranks", "_rests", "_slots")

    def __init__(self, slots: int) -> None:
        self._slots = slots
        self._rests: dict[_Key, list[dict[_Key, list[object]]]] = {}
        self._ranks: dict[_Key, int] | None = None

    def cells(self, rest: _Key) -> list[dict[_Key, list[object]]]:
        found = self._rests.get(rest)
        if found is None:
            found = self._rests[rest] = [{} for _ in range(self._slots)]
        return found

    @staticmethod
    def count(cell: dict[_Key, list[object]], key: _Key, seq: tuple[int, int], n: int) -> None:
        entry = cell.get(key)
        if entry is None:
            cell[key] = [n, seq]
        else:
            entry[0] += n  # type: ignore[operator]

    def _rest_ranks(self) -> dict[_Key, int]:
        """Each ``rest``'s place in ``_bval`` order; equal sort forms share one."""
        if self._ranks is None:
            orders = {rest: tuple(_bval(value) for value in rest) for rest in self._rests}
            ranks: dict[_Key, int] = {}
            rank = -1
            previous: tuple[str, ...] | None = None
            for rest in sorted(orders, key=orders.__getitem__):
                if orders[rest] != previous:
                    rank += 1
                    previous = orders[rest]
                ranks[rest] = rank
            self._ranks = ranks
        return self._ranks

    def ordered(self, slot: int) -> list[tuple[object, _Key, _Key, int]]:
        """``(bucket, lead, rest, count)`` of one slot, in the synthetic adapter's order."""
        ranks = self._rest_ranks()
        keyed: list[tuple[tuple[object, ...], tuple[object, _Key, _Key, int]]] = []
        for rest, cells in self._rests.items():
            rank = ranks[rest]
            for (bucket, *lead), (count, seq) in cells[slot].items():
                sort_key = (bucket, *[_bval(value) for value in lead], rank, seq)
                keyed.append((sort_key, (bucket, tuple(lead), rest, int(count))))  # type: ignore[call-overload]
        keyed.sort(key=lambda item: item[0])
        return [group for _, group in keyed]


def _document_key(document: object) -> object:
    """A hashable stand-in for a JSON document, exact to type (``1`` is not ``True``)."""
    if isinstance(document, dict):
        return tuple((key, type(value), value) for key, value in document.items())
    return (type(document), document)


class DemoMemoryAdapter(SyntheticAdapter):
    """The demo warehouse model, served from memory."""

    #: Which engine serves a ``demo`` source (shown on its connection status).
    engine = "memory"

    def __init__(
        self,
        *,
        now: datetime | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        # ``SyntheticAdapter.__init__`` is deliberately not called: it builds a
        # dataset up front, and this engine generates rows per query window.
        # ``timeout_seconds`` is accepted for the factory signature and ignored,
        # as it is by the synthetic adapter (no wall clock to guard).
        current = to_utc(now) if now is not None else datetime.now(UTC)
        self._available_through = model_rows.available_through(current)
        # Whole hours grouped by row tail, per column set (``_grouped_hours``).
        self._grouped: dict[tuple[object, ...], dict[_Key, list[int]]] = {}

    @property
    def available_through(self) -> datetime:
        """The end (exclusive) of the data this engine holds."""
        return self._available_through

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        return None

    def test_connection(self) -> bool:
        # The model is code: there is nothing to reach. An hour of rows proves
        # it generates, and is the hour every scan reads first.
        return bool(model_rows.events_for_hour(self._available_through - _DAY))

    # -- schema --------------------------------------------------------------

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        table = self._table_for_query(base_query)
        # Nullable throughout, as PostgresAdapter reports every column.
        return [
            ColumnInfo(name=name, type_name=type_name, is_nullable=True)
            for name, type_name in self._columns_for_query(base_query, table)
        ]

    def get_schema_tables(self) -> list[SchemaTable]:
        return [
            SchemaTable(
                name=table,
                columns=[
                    SchemaColumn(name=name, data_type=_INFORMATION_SCHEMA_TYPES[type_name])
                    for name, type_name, _ in columns
                ],
            )
            for table, columns in schema.TABLE_COLUMNS.items()
        ]

    def _columns_for_table(self, table: str) -> tuple[tuple[str, str], ...]:
        columns = schema.TABLE_COLUMNS.get(table, schema.EVENTS_COLUMNS)
        return tuple((name, type_name) for name, type_name, _ in columns)

    def _column_names(self, table: str) -> frozenset[str]:
        return frozenset(name for name, _ in self._columns_for_table(table))

    # -- rows ----------------------------------------------------------------

    def _window(
        self,
        table: str,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> tuple[datetime, datetime]:
        """The window to generate: the caller's on the table's time column, else
        the newest :data:`DEFAULT_WINDOW`; never past :attr:`available_through`."""
        upper = self._available_through
        if time_column == schema.TIME_COLUMNS.get(table) and (
            time_from is not None or time_to is not None
        ):
            lower = to_utc(time_from) if time_from is not None else upper - DEFAULT_WINDOW
            if time_to is not None:
                upper = min(upper, to_utc(time_to))
        else:
            lower = upper - DEFAULT_WINDOW
        limit = MAX_ORDERS_WINDOW if table == "orders" else MAX_EVENTS_WINDOW
        if upper - lower > limit:
            msg = (
                f"The in-memory demo warehouse reads at most {limit.days} days of {table} "
                "per query; collect a longer range in chunks"
            )
            raise SyntheticCapabilityError(msg)
        return lower, upper

    def _rows_for_table(
        self,
        table: str,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> list[dict[str, object]]:
        lower, upper = self._window(table, time_column, time_from, time_to)
        return list(model_rows.rows_between(table, lower, upper))

    def _iter_scan(
        self, base_query: str, table: str, time_from: datetime, time_to: datetime
    ) -> Iterator[dict[str, object]]:
        """``_window_scan`` as a stream, for a window too long to hold as a list."""
        upper = min(to_utc(time_to), self._available_through)
        predicates = _where_predicates(base_query)
        for row in model_rows.rows_between(table, to_utc(time_from), upper):
            if all(self._row_matches_filter(table, row, predicate) for predicate in predicates):
                yield row

    def _ranked_top_values(
        self,
        base_query: str,
        table: str,
        time_column: str,
        windowed: list[dict[str, object]],
        breakdown: str,
        values_limit: int | None,
    ) -> set[str] | None:
        # Inside ``top_n_ranking_window`` the ranking spans the whole collection
        # (a replay's 23 days): count it as a stream, never as one list.
        window = self._top_n_ranking_window
        if window is None or values_limit is None:
            return self._top_values(windowed, breakdown, values_limit)
        rank_from, rank_to = window
        return self._ranked_once(
            ("top_values", base_query, time_column, breakdown, values_limit, rank_from, rank_to),
            lambda: self._top_values(
                self._iter_scan(base_query, table, rank_from, rank_to), breakdown, values_limit
            ),
        )

    def _top_values(
        self,
        windowed: Iterable[dict[str, object]],
        breakdown: str,
        values_limit: int | None,
    ) -> set[str] | None:
        return super()._top_values(windowed, breakdown, values_limit)  # type: ignore[arg-type]

    # -- JSON ----------------------------------------------------------------

    def _reject_json(self, json_columns: list[str]) -> None:
        known = set().union(*schema.JSON_COLUMNS.values())
        unknown = [column for column in json_columns if column not in known]
        if unknown:
            msg = f"Not a JSON column of the demo warehouse: {', '.join(unknown)}"
            raise SyntheticCapabilityError(msg)

    def _json_layout(
        self,
        table: str,
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
    ) -> JsonLayout:
        if not json_columns:
            return super()._json_layout(table, json_columns, json_value_paths)
        columns = [self._validate_json_column(table, column) for column in json_columns]
        paths = json_value_paths or {}
        extractions: list[tuple[str, tuple[str, ...]]] = []
        value_names: list[str] = []
        for column in columns:
            for path in paths.get(column, []):
                extractions.append((column, _path_parts(path)))
                value_names.append(f"{column}.{path}")

        def compute(row: dict[str, object]) -> tuple[object, ...]:
            shapes = [
                _KeyList(sorted(document)) if isinstance(document, dict) else _KeyList()
                for document in (row.get(column) for column in columns)
            ]
            extracted = [
                None if value is _MISSING else _json_text(value)
                for value in (_walk(row.get(column), parts) for column, parts in extractions)
            ]
            return (*shapes, *extracted)

        # A row's key part depends on its documents alone, and the model draws
        # them from short value lists: memoized per document, the shapes and the
        # value paths (a dozen and more JSON texts) are computed once per distinct
        # document instead of once per row and statement. A document that cannot
        # be hashed is computed every time.
        memo: dict[tuple[object, ...], tuple[object, ...]] = {}

        def values(row: dict[str, object]) -> tuple[object, ...]:
            try:
                key = tuple(_document_key(row.get(column)) for column in columns)
                found = memo.get(key)
            except TypeError:
                return compute(row)
            if found is None:
                found = memo[key] = compute(row)
            return found

        return JsonLayout(columns=columns, value_names=value_names, values=values)

    def _validate_json_column(self, table: str, column: str) -> str:
        self._validate_table_column(table, column)
        if column not in schema.JSON_COLUMNS.get(table, frozenset()):
            msg = f"Column {column!r} of {table} is not a JSON column"
            raise ValueError(msg)
        return column

    def _reject_property_breakdowns(self, breakdown_columns: list[str]) -> None:
        known = set().union(*schema.JSON_COLUMNS.values())
        for column in breakdown_columns:
            prop = split_property_field(column)
            if prop is not None and prop[0] not in known:
                msg = f"Not a JSON column of the demo warehouse: {prop[0]}"
                raise SyntheticCapabilityError(msg)

    def _validate_breakdown_column(self, table: str, name: str) -> str:
        prop = split_property_field(name)
        if prop is None:
            return self._validate_table_column(table, name)
        self._validate_json_column(table, prop[0])
        _path_parts(prop[1])
        return name

    def _breakdown_getter(self, breakdown: str) -> Callable[[dict[str, object]], str]:
        """``_breakdown_text`` for one breakdown, its path parsed once."""
        prop = split_property_field(breakdown)
        if prop is None:
            return lambda row: _bval(row.get(breakdown))
        column, parts = prop[0], _path_parts(prop[1])

        def text(row: dict[str, object]) -> str:
            value = _walk(row.get(column), parts)
            return "" if value is _MISSING or value is None else _scalar_text(value)

        return text

    def _breakdown_text(self, row: dict[str, object], breakdown: str) -> str:
        prop = split_property_field(breakdown)
        if prop is None:
            return _bval(row.get(breakdown))
        value = _walk(row.get(prop[0]), _path_parts(prop[1]))
        return "" if value is _MISSING or value is None else _scalar_text(value)

    # -- time-bucketed counts ----------------------------------------------------
    #
    # The statements every collection chunk issues — the counts, and the
    # breakdowns (platform, app version, the drift fields) — read the same hours
    # grouped by the same columns. Each hour is grouped once, per distinct row
    # tail (``rest``: the dimension columns and the JSON key part), and every
    # statement over it counts those groups: a plain count adds a group's size,
    # a breakdown reads only its column off the group's rows. Same rows, keys and
    # order as ``SyntheticAdapter``'s row-by-row implementations, which stay the
    # path for what this does not cover: a top-N limit, a window on another
    # column.

    #: Grouped hours kept per adapter: a chunk's day, read by each statement.
    _GROUPED_HOURS_KEPT = 30

    def _hourly_scan(
        self,
        base_query: str,
        table: str,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> Iterator[tuple[datetime, _HourRows]] | None:
        """``_window_scan``'s rows an hour at a time, or ``None`` off the time column."""
        if time_column is not None and time_column != schema.TIME_COLUMNS.get(table):
            return None
        lower, upper = self._window(table, time_column, time_from, time_to)
        predicates = _where_predicates(base_query)

        def hours() -> Iterator[tuple[datetime, _HourRows]]:
            for hour_start, rows in model_rows.hours_between(table, lower, upper):
                if predicates:
                    rows = [
                        row
                        for row in rows
                        if all(self._row_matches_filter(table, row, p) for p in predicates)
                    ]
                yield hour_start, rows

        return hours()

    def _hour_bucket(self, hour_start: datetime, interval: str) -> datetime | None:
        """The bucket every row of the hour falls in, or ``None`` if it spans two."""
        first = self._bucket_start(hour_start, interval)
        return first if self._bucket_start(hour_start + _HOUR - _TICK, interval) == first else None

    def _grouped_hours(
        self,
        base_query: str,
        table: str,
        time_column: str,
        time_from: datetime,
        time_to: datetime,
        reg: list[str],
        layout: JsonLayout,
    ) -> Iterator[tuple[int, datetime, _HourRows, dict[_Key, list[int]]]] | None:
        """Each hour of the window with its rows grouped by tail: ``rest -> positions``.

        An hour read whole is grouped once per adapter and column set (the
        statements of a chunk share it); an hour the window cuts, every time.
        """
        hours = self._hourly_scan(base_query, table, time_column, time_from, time_to)
        if hours is None:
            return None
        cache = self._grouped
        signature = (
            table,
            base_query,
            tuple(reg),
            tuple(layout.columns),
            tuple(layout.value_names),
        )

        def grouped() -> Iterator[tuple[int, datetime, _HourRows, dict[_Key, list[int]]]]:
            for hour_start, rows in hours:
                index = model_rows.epoch_hour(hour_start)
                whole = isinstance(rows, tuple)
                key = (*signature, index)
                groups = cache.get(key) if whole else None
                if groups is None:
                    groups = {}
                    for position, row in enumerate(rows):
                        rest = (*[row.get(column) for column in reg], *layout.values(row))
                        found = groups.get(rest)
                        if found is None:
                            groups[rest] = [position]
                        else:
                            found.append(position)
                    if whole:
                        cache[key] = groups
                        while len(cache) > self._GROUPED_HOURS_KEPT:
                            del cache[next(iter(cache))]
                yield index, hour_start, rows, groups

        return grouped()

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
        self._reject_json(json_columns)
        table = self._table_for_query(base_query)
        reg = [self._validate_table_column(table, column) for column in regular_columns]
        self._validate_table_column(table, time_column)
        layout = self._json_layout(table, json_columns, json_value_paths)
        hours = self._grouped_hours(base_query, table, time_column, time_from, time_to, reg, layout)
        if hours is None:
            return super().get_time_bucketed_counts(
                base_query,
                time_column,
                interval,
                regular_columns,
                json_columns,
                json_value_paths,
                time_from,
                time_to,
                limit,
            )
        out_groups = _Groups(1)
        for index, hour_start, rows, groups in hours:
            bucket = self._hour_bucket(hour_start, interval)
            for rest, positions in groups.items():
                cell = out_groups.cells(rest)[0]
                if bucket is not None:
                    _Groups.count(cell, (bucket,), (index, positions[0]), len(positions))
                    continue
                for position in positions:
                    row_bucket = self._bucket_start(rows[position][time_column], interval)
                    _Groups.count(cell, (row_bucket,), (index, position), 1)
        out = [(bucket, *rest, count) for bucket, _, rest, count in out_groups.ordered(0)]
        return [*reg, *layout.columns], layout.value_names, out[: max(int(limit), 0)]

    def _breakdown_groups(
        self,
        hours: Iterator[tuple[int, datetime, _HourRows, dict[_Key, list[int]]]],
        time_column: str,
        interval: str,
        breakdowns: list[str],
    ) -> _Groups:
        out_groups = _Groups(len(breakdowns))
        texts = [self._breakdown_getter(breakdown) for breakdown in breakdowns]
        for index, hour_start, rows, groups in hours:
            bucket = self._hour_bucket(hour_start, interval)
            for rest, positions in groups.items():
                cells = out_groups.cells(rest)
                for position in positions:
                    row = rows[position]
                    row_bucket = (
                        bucket
                        if bucket is not None
                        else self._bucket_start(row[time_column], interval)
                    )
                    seq = (index, position)
                    for slot, text in enumerate(texts):
                        _Groups.count(cells[slot], (row_bucket, text(row)), seq, 1)
        return out_groups

    def get_time_bucketed_breakdown_counts(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        self._reject_property_breakdowns([breakdown_column])
        table = self._table_for_query(base_query)
        reg = [self._validate_table_column(table, column) for column in regular_columns]
        self._validate_table_column(table, time_column)
        breakdown = self._validate_breakdown_column(table, breakdown_column)
        layout = self._json_layout(table, json_columns, json_value_paths)
        # A values limit folds the tail into "Other" by a ranking over the
        # rows; without one every value is kept as it is (``_fold`` of ``None``).
        hours = (
            None
            if values_limit is not None
            else self._grouped_hours(
                base_query, table, time_column, time_from, time_to, reg, layout
            )
        )
        if hours is None:
            return super().get_time_bucketed_breakdown_counts(
                base_query,
                time_column,
                interval,
                breakdown_column,
                regular_columns,
                json_columns,
                json_value_paths,
                time_from,
                time_to,
                values_limit,
                limit,
            )
        groups = self._breakdown_groups(hours, time_column, interval, [breakdown])
        out = [
            (bucket, lead[0], 0, *rest, count) for bucket, lead, rest, count in groups.ordered(0)
        ]
        return [*reg, *layout.columns], layout.value_names, out[: max(int(limit), 0)]

    def get_time_bucketed_breakdown_counts_multi(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_columns: list[str],
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        self._reject_property_breakdowns(breakdown_columns)
        if not breakdown_columns:
            return [], [], []
        table = self._table_for_query(base_query)
        reg = [self._validate_table_column(table, column) for column in regular_columns]
        self._validate_table_column(table, time_column)
        breakdowns = [
            self._validate_breakdown_column(table, column) for column in breakdown_columns
        ]
        # The PostgreSQL engine's rule, so a call only one engine accepts fails
        # on both (``test_demo_warehouse_pg`` holds them to the same answers).
        invalid = [c for c in breakdowns if not is_breakdown_field_of(c, reg, json_columns)]
        if invalid:
            msg = (
                "Breakdown columns must be scalar columns or properties of a JSON column: "
                f"{', '.join(invalid)}"
            )
            raise ValueError(msg)
        layout = self._json_layout(table, json_columns, json_value_paths)
        hours = (
            None
            if values_limit is not None
            else self._grouped_hours(
                base_query, table, time_column, time_from, time_to, reg, layout
            )
        )
        if hours is None:
            return super().get_time_bucketed_breakdown_counts_multi(
                base_query,
                time_column,
                interval,
                breakdown_columns,
                regular_columns,
                json_columns,
                json_value_paths,
                time_from,
                time_to,
                values_limit,
                limit,
            )
        groups = self._breakdown_groups(hours, time_column, interval, breakdowns)
        out: list[tuple[object, ...]] = []
        for slot, breakdown in enumerate(breakdowns):
            for bucket, lead, rest, count in groups.ordered(slot):
                out.append((bucket, breakdown, lead[0], 0, *rest, count))
        out.sort(key=lambda row: (row[0], row[1], row[2]))
        return [*reg, *layout.columns], layout.value_names, out[: max(int(limit), 0)]

    # -- preview ---------------------------------------------------------------

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """The window's first ``limit`` rows, read only as far as they reach.

        ``SyntheticAdapter`` sorts the whole window by the time column's text
        and keeps the head; the model's hours stream in time order already, in
        whole seconds, so that head is the stream's first ``limit`` rows.
        """
        if self._is_active_sessions_query(base_query):
            return self._active_sessions_rows(time_from, time_to, limit)
        self._reject_unsupported_scan(base_query)
        table = self._table_for_query(base_query)
        column_names = [name for name, _ in self._columns_for_query(base_query, table)]
        if time_column is not None:
            self._validate_table_column(table, time_column)
        order_key = time_column or (column_names[0] if column_names else None)
        hours = (
            self._hourly_scan(base_query, table, time_column, time_from, time_to)
            if order_key == schema.TIME_COLUMNS.get(table)
            else None
        )
        if hours is None:
            return super().get_preview_rows(
                base_query, limit, time_column=time_column, time_from=time_from, time_to=time_to
            )
        wanted = max(int(limit), 0)
        out: list[tuple[object, ...]] = []
        for _, rows in hours:
            if len(out) >= wanted:
                break
            out.extend(
                tuple(row.get(name) for name in column_names) for row in rows[: wanted - len(out)]
            )
        return column_names, out

    # -- sql metric ------------------------------------------------------------

    def _is_active_sessions_query(self, base_query: str) -> bool:
        return _normalize_sql(base_query) == _ACTIVE_SESSIONS_STATEMENT

    def _active_sessions_rows(
        self, time_from: datetime | None, time_to: datetime | None, limit: int
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Distinct sessions per UTC day, the window applied to the day (``ts``)."""
        upper = self._available_through
        if time_to is not None:
            upper = min(upper, to_utc(time_to))
        lower = (
            to_utc(time_from)
            if time_from is not None
            else upper - timedelta(days=ACTIVE_SESSIONS_DEFAULT_DAYS)
        )
        out: list[tuple[object, ...]] = []
        day = floor_to_bucket(lower, "1d")
        if day < lower:
            day += _DAY
        today = floor_to_bucket(self._available_through, "1d")
        while day < upper and len(out) < max(int(limit), 0):
            if day < today:
                sessions = _sessions_on_day(day)
            else:
                sessions = len(
                    {
                        row["session_id"]
                        for row in model_rows.rows_between("events", day, self._available_through)
                    }
                )
            if sessions:
                out.append((day, sessions))
            day += _DAY
        return ["ts", "value"], out
