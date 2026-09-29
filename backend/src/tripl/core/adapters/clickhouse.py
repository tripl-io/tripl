from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any, override

import clickhouse_connect

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    SchemaColumn,
    SchemaTable,
    contract_bound_literal,
    field_contract_verdict,
    is_breakdown_field_of,
)
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.bucketing import format_utc_literal
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, classify_complex
from tripl.json_paths import (
    decode_json_path_value,
    json_safe,
    set_nested_value,
    split_property_field,
)
from tripl.models.domain_enums import MetricAggregation

# Hard cap on rows pulled from the catalog so a warehouse with thousands of
# wide tables can't blow up the autocomplete payload or the request. The cap is
# generous because introspection now spans every non-system database (one row
# per column per table across all of them), so a multi-database warehouse needs
# plenty of headroom before its visible tables get truncated.
_SCHEMA_ROW_LIMIT = 50000

# System databases that hold ClickHouse internals, not user data. They are
# excluded from catalog introspection so autocomplete only surfaces queryable
# user tables. ClickHouse exposes both `information_schema` and its uppercase
# alias `INFORMATION_SCHEMA`; exclude both.
_SYSTEM_DATABASES = ("system", "information_schema", "INFORMATION_SCHEMA")

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
_IDENTIFIER_PART_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# JSON path *discovery* (preview) enumeration functions. "dynamic" lists only the
# important typed subcolumn paths (fast); "all" lists every path incl. shared-data
# paths. Both are JSON-only: given a Map or a Tuple they raise
# `ILLEGAL_TYPE_OF_ARGUMENT`, so discovery is skipped for those families rather
# than run with a different function. The scan-time shape expression does branch
# per family — see `_json_paths_expression`.
_JSON_PATH_DISCOVERY_FUNCS = {"all": "JSONAllPaths", "dynamic": "JSONDynamicPaths"}
_DEFAULT_JSON_PATH_DISCOVERY = "dynamic"


def _as_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[object, ...]]:
    """clickhouse-connect yields rows as lists; the adapter contract is tuples."""
    return [tuple(row) for row in rows]


class ClickHouseAdapter(BaseAdapter):
    #: Declared ClickHouse type per column, captured by :meth:`get_columns` and read
    #: by :meth:`_nested_kind` to pick the nested-shape SQL. The default deliberately
    #: lives on the *class* rather than in ``__init__``: a site that builds an adapter
    #: with ``object.__new__`` never runs ``__init__``, so an instance attribute set
    #: there would simply not exist. ``core/adapters/multi_aggregate_sql.py`` does it
    #: in production and the unit tests do it to build an adapter without a live
    #: client; ``grep -rn "object.__new__(ClickHouseAdapter)"`` is the current set.
    #: A count was spelled out here and was wrong in the commit that wrote it — the
    #: same commit added two sites it does not name — so the invariant is stated
    #: instead of a census that every new test file invalidates.
    #: BigQuery keeps the equivalent map in ``__init__`` and has to be primed by hand
    #: at every such site; a class-level default cannot be forgotten by a new one. It
    #: is only ever rebound, never mutated in place, so the shared empty dict is safe.
    #: Empty means "never introspected", which :meth:`_nested_kind` reads as JSON —
    #: the behavior every caller had before this map existed.
    _column_types: dict[str, str] = {}

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str = "",
        password: str = "",
        json_path_discovery: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._client = clickhouse_connect.get_client(
            host=host,
            port=port,
            database=database,
            username=username or "default",
            password=password or "",
            **kwargs,
        )
        self._allowed_columns: set[str] = set()
        # Which path-enumeration function the JSON path discovery query uses.
        # Unknown/None falls back to the default ("dynamic").
        self._json_path_discovery = (
            json_path_discovery
            if json_path_discovery in _JSON_PATH_DISCOVERY_FUNCS
            else _DEFAULT_JSON_PATH_DISCOVERY
        )

    def close(self) -> None:
        self._client.close()

    def test_connection(self) -> bool:
        result = self._client.query("SELECT 1")
        first_row = result.first_row
        return bool(first_row is not None and first_row[0] == 1)

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        result = self._client.query(f"SELECT * FROM ({base_query}) AS _src LIMIT 0")
        columns: list[ColumnInfo] = []
        for name, type_info in zip(result.column_names, result.column_types, strict=False):
            # clickhouse-connect type objects render their object repr under
            # str() (e.g. "<...Float32 object at 0x...>"); their `.name` is the
            # real ClickHouse type ("Float32", "Nullable(Float32)"). Use it so
            # type-based logic (numeric measure detection, JSON inference) works.
            type_name = getattr(type_info, "name", None) or str(type_info)
            is_nullable = "Nullable" in type_name
            columns.append(ColumnInfo(name=name, type_name=type_name, is_nullable=is_nullable))
        self._allowed_columns = {c.name for c in columns}
        # Kept alongside the allowlist because the nested-shape SQL is type-directed:
        # ClickHouse has one path/shape function per nested family and none of them
        # accepts another family's argument.
        self._column_types = {c.name: c.type_name for c in columns}
        return columns

    def get_schema_tables(self) -> list[SchemaTable]:
        # Introspect every non-system database, not just the connection's current
        # one: on real ClickHouse the user's tables often live in a different
        # database than the connection default, or they reference `db.table`
        # across databases. `database = currentDatabase()` is computed server-side
        # so the "bare vs qualified" decision is correct even when the connection
        # has no/`default` database set (the server resolves currentDatabase()).
        excluded = ", ".join(f"'{name}'" for name in _SYSTEM_DATABASES)
        sql = (
            "SELECT database, table, name, type, "
            "database = currentDatabase() AS is_current_database "
            "FROM system.columns "
            f"WHERE database NOT IN ({excluded}) "
            f"ORDER BY database, table, position LIMIT {_SCHEMA_ROW_LIMIT}"
        )
        logger.debug("CH schema introspection query: %s", sql)
        # No per-query settings: tripl connects with read-only ClickHouse users,
        # which reject setting overrides ("Setting ... is unknown or readonly").
        # The row LIMIT and the connection-level timeout bound the query instead.
        result = self._client.query(sql)
        # Tables in the current/default database keep their bare name (`events`);
        # tables in any other database are qualified as `database.table`
        # (`analytics.orders`). The frontend relies on this: a table name has at
        # most one dot, and only when it lives outside the default database.
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for database, table, name, type_name, is_current_database in result.result_rows:
            qualified_name = str(table) if is_current_database else f"{database}.{table}"
            columns_by_table.setdefault(qualified_name, []).append(
                SchemaColumn(name=str(name), data_type=str(type_name))
            )
        return [
            SchemaTable(name=table, columns=columns) for table, columns in columns_by_table.items()
        ]

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        sql = f"SELECT * FROM ({base_query}) AS _src{where_clause} LIMIT {int(limit)}"
        logger.info("CH preview query: %s", sql)
        result = self._client.query(sql)
        return list(result.column_names), _as_rows(result.result_rows)

    supports_json_string_columns = True

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse String columns as ``JSON`` in a wrapper over ``base_query``.

        ``SELECT * REPLACE`` keeps every other column, and its position, as it
        was. The text is cast only when ``isValidJSON`` and ``JSONType`` agree
        it is a JSON object: a cast of anything else (malformed text, a bare
        scalar, an array, NULL) raises, so such a row reads as ``{}`` instead —
        no paths, which the scan counts as a row missing every key. The cast is
        one parse per row read; every statement already bounds the rows it
        reads (sample LIMITs, the scan window), so the cost scales with those
        and not with the table.
        """
        replacements: list[str] = []
        for column in columns:
            if not _IDENTIFIER_PART_RE.match(column):
                msg = f"Invalid column name: {column}"
                raise ValueError(msg)
            text = f"ifNull(toString(`{column}`), '')"
            replacements.append(
                f"CAST(if(isValidJSON({text}) AND JSONType({text}) = 'Object', {text}, '{{}}'), "
                f"'JSON') AS `{column}`"
            )
        if not replacements:
            return base_query
        return f"SELECT * REPLACE ({', '.join(replacements)}) FROM ({base_query}) AS _json_src"

    json_path_samples_are_text = True

    @override
    def get_json_path_samples(
        self,
        base_query: str,
        json_columns: list[str],
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        path_limit: int = 1000,
        sample_limit: int = 3,
        sample_row_limit: int = 1000,
        include_objects: bool = False,
    ) -> dict[str, dict[str, list[object]]]:
        """Discover JSON paths independently from visible preview rows.

        The enumeration function is chosen by the connection's json_path_discovery
        mode: "dynamic" (JSONDynamicPaths, default) surfaces only the important
        typed subcolumn paths and is much faster on wide JSON columns; "all"
        (JSONAllPaths) lists every path including shared-data ones. It runs across
        the source query so the UI sees path candidates even when they do not
        appear in the small preview sample. The scan's own shape column is built by
        ``_json_paths_expression``, which picks its function from the column's nested
        family and is unaffected by this setting.

        Only ``JSON`` columns are enumerated. ``json_columns`` is filled by a split
        that also counts ``Map`` and ``Tuple`` as nested, and for those this returns
        an empty mapping — no query, no error. Both discovery functions reject them
        outright, and even with an enumerator the sample query below could not read a
        Map leaf: ``_json_path_expression`` compiles a path to ``tupleElement``-style
        member access, which ClickHouse refuses on a Map. A Map/Tuple column still
        reaches the scan with its shape intact via ``_json_paths_expression``; what it
        has no route to is a *selectable value path*, so offering the user none is the
        honest answer rather than a half-open capability.
        """
        if not json_columns or path_limit <= 0 or sample_limit <= 0 or sample_row_limit <= 0:
            return {column: {} for column in json_columns}

        samples_by_column: dict[str, dict[str, list[object]]] = {}
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        # Bound discovery to a sample of source rows. The path enumeration below
        # otherwise scans the whole source (no LIMIT survives a GROUP BY), which
        # turns into a full-table scan and trips the timeout on large tables.
        sampled_source = (
            f"(SELECT * FROM ({base_query}) AS _src{where_clause} LIMIT {int(sample_row_limit)})"
        )
        path_fn = _JSON_PATH_DISCOVERY_FUNCS[self._json_path_discovery]
        for column in json_columns:
            c = self._validate_column(column)
            if self._nested_kind(c) is not ComplexKind.json:
                samples_by_column[c] = {}
                continue
            path_sql = (
                "SELECT _path "
                "FROM ("
                f"SELECT arrayJoin({path_fn}(`{c}`)) AS _path "
                f"FROM {sampled_source} AS _sample"
                ") "
                "WHERE _path != '' "
                "GROUP BY _path "
                "ORDER BY _path "
                f"LIMIT {int(path_limit)}"
            )
            logger.info("CH JSON path discovery query: %s", path_sql)
            path_result = self._client.query(path_sql)

            paths: list[str] = []
            for row in path_result.result_rows:
                path = str(row[0])
                try:
                    self._json_path_expression(c, path)
                except ValueError:
                    logger.info("Skipping unsupported JSON path %s.%s", c, path)
                    continue
                paths.append(path)

            column_samples: dict[str, list[object]] = {path: [] for path in paths}
            samples_by_column[c] = column_samples
            if not paths:
                continue

            select_parts: list[str] = []
            path_by_alias: dict[str, str] = {}
            for index, path in enumerate(paths):
                alias = f"__json_path_{index}"
                select_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{alias}`"
                )
                path_by_alias[alias] = path

            sample_sql = f"SELECT {', '.join(select_parts)} FROM {sampled_source} AS _sample"
            logger.info("CH JSON path sample query: %s", sample_sql)
            sample_result = self._client.query(sample_sql)
            index_to_path = {
                index: path_by_alias[name]
                for index, name in enumerate(sample_result.column_names)
                if name in path_by_alias
            }
            # Object paths are every proper prefix of a leaf. They are not read
            # with a query of their own: each sample row already holds all of
            # its leaves, so one row's object is rebuilt from them here.
            object_paths = (
                sorted(
                    {
                        ".".join(path.split(".")[:take])
                        for path in paths
                        for take in range(1, path.count(".") + 1)
                    }
                    - set(paths)
                )
                if include_objects
                else []
            )
            for path in object_paths:
                column_samples[path] = []
            seen_by_path: dict[str, set[str]] = {path: set() for path in column_samples}

            for row in sample_result.result_rows:
                if all(len(values) >= sample_limit for values in column_samples.values()):
                    break
                for index, path in index_to_path.items():
                    if len(column_samples[path]) >= sample_limit:
                        continue
                    value = row[index]
                    if value is None or value == "null":
                        continue
                    sample_key = str(value)
                    if sample_key in seen_by_path[path]:
                        continue
                    seen_by_path[path].add(sample_key)
                    column_samples[path].append(value)
                if object_paths:
                    self._sample_row_objects(
                        row, index_to_path, object_paths, column_samples, seen_by_path, sample_limit
                    )

        return samples_by_column

    @staticmethod
    def _sample_row_objects(
        row: Sequence[object],
        index_to_path: dict[int, str],
        object_paths: list[str],
        column_samples: dict[str, list[object]],
        seen_by_path: dict[str, set[str]],
        sample_limit: int,
    ) -> None:
        """Add one sample row's nested objects, as JSON text, to the samples.

        A leaf the row does not carry reads back as ``null``, the same as one
        it carries as null, so neither is a key of the rebuilt object; an
        object with no key left is no sample.
        """
        leaves: dict[str, object] = {}
        for index, path in index_to_path.items():
            value = decode_json_path_value(row[index])
            if value is not None:
                leaves[path] = value
        for object_path in object_paths:
            if len(column_samples[object_path]) >= sample_limit:
                continue
            prefix = object_path + "."
            rebuilt: dict[str, object] = {}
            for path, value in leaves.items():
                if path.startswith(prefix):
                    set_nested_value(rebuilt, path.removeprefix(prefix), value)
            if not rebuilt:
                continue
            # ``toJSONString``'s own spelling, so the text is the one the
            # leaves come back in.
            text = json.dumps(
                json_safe(rebuilt), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if text in seen_by_path[object_path]:
                continue
            seen_by_path[object_path].add(text)
            column_samples[object_path].append(text)

    def _validate_alias(self, alias: str) -> str:
        """Validate a caller-supplied output column alias before interpolation.

        ``AggregateSpec.key`` is machine-generated today, but this builder is now
        shared (``multi_aggregate_sql``) and both other dialects already escape
        (Postgres) or validate (BigQuery) the alias. Closing the gap keeps a
        future caller from turning a spec key into an injection point.
        """
        if not _IDENTIFIER_PART_RE.match(alias):
            msg = f"Invalid aggregate key alias: {alias!r}"
            raise ValueError(msg)
        return alias

    def _validate_column(self, column: str) -> str:
        if not _IDENTIFIER_RE.match(column):
            msg = f"Invalid column name: {column}"
            raise ValueError(msg)
        if self._allowed_columns and column not in self._allowed_columns:
            msg = f"Column {column!r} not found in query result"
            raise ValueError(msg)
        return column

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Translate an interval code into ClickHouse bucket SQL.

        Must agree with ``tripl.core.bucketing.floor_to_bucket``.

        The explicit ``'UTC'`` argument is load-bearing: without it
        ``toStartOfInterval`` buckets in the *column's* timezone, so a DateTime
        column declared in, say, ``Asia/Tokyo`` lands rows in the wrong day
        (verified on ClickHouse 26.6 — a Tokyo 05:30 row buckets to the Tokyo day,
        not the UTC one).

        The week form needs a word, because the obvious reading is wrong.
        ClickHouse's ``toStartOfInterval(col, INTERVAL 1 WEEK)`` does *not* bin off
        the epoch Thursday — it is already Monday-aligned and anchored at
        1970-01-05, i.e. it agrees with :data:`~tripl.core.bucketing.WEEK_ORIGIN`.
        ``toMonday`` is used anyway for a different reason: ``toStartOfInterval``
        with a WEEK interval returns a **Date**, not a DateTime, so a 1w bucket
        would come back from clickhouse-connect as ``datetime.date`` while every
        other interval yields ``datetime.datetime``. ``toDateTime(toMonday(...))``
        keeps the bucket column a DateTime across all five intervals.
        """
        spec = get_interval(interval_code)
        col = f"`{self._validate_column(time_column)}`"
        if spec.unit is IntervalUnit.week:
            return f"toDateTime(toMonday({col}, 'UTC'), 'UTC')"
        return f"toStartOfInterval({col}, INTERVAL {spec.count} {spec.unit.value.upper()}, 'UTC')"

    def _nested_kind(self, column: str) -> ComplexKind:
        """Which nested family a column belongs to: JSON document, Map or Tuple.

        The regular/nested split that fills ``json_columns`` is
        :func:`~tripl.core.warehouse_types.is_complex_type`, which puts all three
        families in the nested bucket — and that predicate is recomputed independently
        in eight modules across ``core`` and ``worker``, so narrowing it at the split
        is not an option. Narrowing it in the shared classifier is worse still:
        BigQuery relies on ``struct`` staying nested to expand its declared RECORD
        paths. The dialect knowledge has to live behind the adapter instead, which is
        also where BigQuery put it (``BigQueryAdapter._complex_kind``).

        Falls back to JSON when the column's type was never introspected. Every
        production read path runs ``get_columns`` on the same adapter instance first
        (``worker/tasks/scan.py``, ``worker/tasks/scan_dry_run.py``,
        ``worker/tasks/metrics/tasks.py``, ``core/analyzers/preview.py``), and the
        fact-metric paths in ``worker/tasks/metrics/metric_collect.py`` pass an empty
        ``json_columns`` list, so this fallback is not a live code path today — it
        exists so a caller that skips introspection gets the pre-existing behavior
        rather than a new ``AttributeError``-shaped surprise.
        """
        type_name = self._column_types.get(column)
        if type_name is None:
            return ComplexKind.json
        kind = classify_complex(type_name)
        if kind is None:
            msg = (
                f"ClickHouse: column {column!r} has scalar type {type_name} and holds "
                "no nested paths."
            )
            raise ValueError(msg)
        return kind

    def _json_paths_expression(self, column: str) -> str:
        """Sorted ``Array(String)`` describing ONE row's nested-value shape.

        Named after :meth:`PostgresAdapter._json_paths_expression` on purpose: it
        plays the same role — the single place a dialect renders "what shape does this
        row's nested value have" for the scan's ``GROUP BY`` — and the analyzer
        (``core/analyzers/cardinality.py``) counts each distinct array as one document
        shape.

        ``JSONAllPaths`` is not polymorphic over ClickHouse's three nested families.
        Verified on ClickHouse 26.5.4.14: ``arraySort(JSONAllPaths(map('a','b')))``
        and the same call on a ``Tuple`` both fail with ``Code: 43 ... requires
        argument with type JSON ... (ILLEGAL_TYPE_OF_ARGUMENT)``. Emitting it
        unconditionally is what made a single Map or Tuple column anywhere in the
        source query kill every scan and every metrics collection for that config.

        So, per family:

        - ``JSON``  -> ``JSONAllPaths``: full nested leaf paths, a columnar metadata
          read, and what this adapter has always emitted.
        - ``Map``   -> the row's key set. ``mapKeys`` alone leaks the key type into
          the result (verified: ``arraySort(mapKeys(map(2,'b',10,'a')))`` is
          ``Array(UInt8)``), so the cast to String happens INSIDE ``arraySort`` and
          the sort is therefore lexicographic: ``['10','2']``, not ``[2,10]``. That
          is deliberate — every Python consumer of this cell (``cardinality.py``,
          ``event_plan.py``, ``metric_rows.py``) re-imposes ``sorted(str(p) ...)``,
          so sorting the raw keys first would produce a SQL-side order the analyzer
          immediately contradicts. No engine error was observed with a non-String
          element type; the cast is here so all three branches answer
          ``Array(String)`` and the shape column's SQL type does not depend on which
          nested family the user's column happens to be. Those columns sit side by
          side as grouping keys in the GROUPING SETS statement built by
          ``get_time_bucketed_breakdown_counts_multi``.
        - ``Tuple`` -> ``tupleNames``, the declared field names (already
          ``Array(String)``). A Tuple's shape cannot vary row to row, so this column
          groups to a single constant — degenerate, but correct, and it keeps the
          returned ``json_col_names`` list byte-identical to what the caller passed,
          which the row-indexing contract in ``cardinality.py`` depends on.

        Alternatives rejected: dropping the column silently deletes a user's field
        from the event plan with no message, and demoting it to ``toString(col)``
        would move the name into the regular bucket while the seven modules other than
        ``cardinality.py`` that recompute the same predicate still classify it as
        nested and hunt for path combinations that no longer exist.
        """
        c = self._validate_column(column)
        kind = self._nested_kind(c)
        if kind is ComplexKind.map:
            return f"arraySort(arrayMap(k -> toString(k), mapKeys(`{c}`)))"
        if kind is ComplexKind.struct:
            return f"arraySort(tupleNames(`{c}`))"
        return f"arraySort(JSONAllPaths(`{c}`))"

    def _json_path_expression(self, column: str, path: str) -> str:
        parts = [part for part in path.split(".") if part]
        if not parts:
            raise ValueError(f"Invalid JSON path: {path}")
        if any(not _IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")

        expression = f"`{self._validate_column(column)}`"
        for part in parts:
            expression += f".`{part}`"
        return expression

    def _string_value_expression(self, column: str) -> str:
        return f"ifNull(toString(`{self._validate_column(column)}`), '')"

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property (a JSON path of a nested column) as a Nullable(String).

        NULL when the row does not carry the path, so a property behaves like a
        nullable scalar column in breakdowns and contracts (F23, #306). Built on
        ``_json_path_expression`` — the same validated, backticked subcolumn
        access the scan's ``json_value_paths`` use — for JSON and named Tuple
        columns; a Map has keys rather than paths, so only a one-segment path
        addresses it, and the key goes through ``_quote_string``.
        """
        c = self._validate_column(column)
        if self._nested_kind(c) is ComplexKind.map:
            if "." in path:
                msg = f"ClickHouse: Map column {c!r} has no nested path {path!r}"
                raise ValueError(msg)
            key = self._quote_string(path)
            return f"if(mapContains(`{c}`, {key}), toString(`{c}`[{key}]), NULL)"
        expr = self._json_path_expression(c, path)
        return f"if(isNull({expr}), NULL, toString({expr}))"

    def _field_operand(self, field: str) -> str:
        """A breakdown / drift / contract field as a nullable SQL operand.

        A scalar column is its backticked name, exactly as before; a property
        entry (``<json_column>.<path>``) is its extracted value.
        """
        prop = split_property_field(field)
        if prop is None:
            return f"`{self._validate_column(field)}`"
        return self._property_value_expression(*prop)

    def _field_value_expression(self, field: str) -> str:
        """``_string_value_expression`` for a scalar column or a property entry."""
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"ifNull({self._field_operand(field)}, '')"

    def _validate_breakdown_field(self, field: str) -> str:
        """Validate a breakdown entry: a column, or a property of a known column."""
        prop = split_property_field(field)
        if prop is None:
            return self._validate_column(field)
        self._validate_column(prop[0])
        return field

    def _quote_string(self, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

    def _utc_literal(self, value: datetime) -> str:
        """Render a datetime as an unambiguously-UTC ClickHouse timestamp.

        The old form here was ``value.strftime("%Y-%m-%d %H:%M:%S")``, which is
        wrong twice over: ``strftime`` silently drops ``tzinfo`` (an aware
        non-UTC datetime yields its *local* wall clock, not the UTC instant), and
        the resulting offset-less literal is read by ClickHouse in the **column's**
        timezone. Verified on ClickHouse 26.6: against a ``DateTime('Asia/Tokyo')``
        column, ``col >= '2026-07-12 05:30:00'`` compares the literal as Tokyo
        wall-clock and answers *true*, where the same instant expressed in UTC
        answers *false*. That is a whole-window shift on any non-UTC column.

        :func:`~tripl.core.bucketing.format_utc_literal` emits the instant with an
        explicit ``+00:00``. ``parseDateTime64BestEffort`` is the function that
        honors that offset (verified: ``...+03:00`` parses to 02:30Z, not 05:30Z)
        and it returns ``DateTime64(6, 'UTC')`` — scale 6 so the microseconds the
        contract preserves survive into the bound, and a pinned result timezone so
        the comparison cannot be reinterpreted in the column's or server's zone.
        """
        return f"parseDateTime64BestEffort('{format_utc_literal(value)}', 6, 'UTC')"

    def _time_condition(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> str:
        """The half-open ``[from, to)`` window predicate, or ``""`` if unbounded.

        The single definition of the time window for every query this adapter
        builds — preview, JSON path discovery, field contracts, breakdown
        top-values and all five bucketed scans. It used to be copy-pasted six
        times, which is how six of them ended up with the same timezone bug.
        """
        if time_column is None or time_from is None or time_to is None:
            return ""
        tc = self._validate_column(time_column)
        return f"`{tc}` >= {self._utc_literal(time_from)} AND `{tc}` < {self._utc_literal(time_to)}"

    def _time_window_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> str:
        condition = self._time_condition(time_column, time_from, time_to)
        return f" WHERE {condition}" if condition else ""

    def _contract_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
        group_column: str | None,
        group_value: str | None,
    ) -> str:
        conditions: list[str] = []
        time_condition = self._time_condition(time_column, time_from, time_to)
        if time_condition:
            conditions.append(time_condition)
        if group_column is not None:
            gc = self._validate_column(group_column)
            expected = self._quote_string(group_value or "")
            conditions.append(f"ifNull(toString(`{gc}`), '') = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    @override
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have ClickHouse compile the pattern with RE2, over no rows at all.

        ``match()`` against a constant empty haystack still has to build the
        regex, so a pattern RE2 rejects raises here — the same exception it would
        otherwise raise from inside the contract statement, except that here it
        costs one expectation instead of every contract in the scan. The
        statement reads no table, so it is not a scan of anything.

        The pattern goes through ``_quote_string`` exactly as the contract
        statement does it, and that is load-bearing rather than tidy: this
        adapter escapes a backslash on the way in, so quoting the probe any other
        way would compile a DIFFERENT pattern from the one it authorizes.
        """
        self._client.query(f"SELECT match('', {self._quote_string(pattern)})")

    def _contract_aggregates(
        self,
        expectation: FieldContractExpectation,
    ) -> tuple[str, str, str] | None:
        """The (bad, total, sample) aggregates for one expectation, or ``None``.

        Unaliased on purpose: the caller numbers them ``_bad_{i}`` / ``_total_{i}``
        / ``_sample_{i}`` by their position in the shared scan, which is the
        layout ``PostgresAdapter._contract_aggregate_sql`` and
        ``BigQueryAdapter._contract_fragments`` also emit, so all three engines
        hand the row to the same positional decode.

        An expectation this engine cannot evaluate yields ``None`` and is dropped,
        so it cannot manufacture a violation out of a contract that says nothing,
        nor end the scan the other expectations are riding in. Which ones those
        are is ``field_contract_is_inert``'s to say — this branch used to decide
        it locally and the three engines had drifted apart on it. Neither the
        threshold nor the comparison appears here either: see the field contract
        section of ``BaseAdapter`` for both rules.
        """
        if self._field_contract_is_inert(expectation):
            return None

        operand = self._contract_operand(expectation, self._field_operand)
        if operand is None:
            return None
        value_expr = f"ifNull(toString({operand}), '')"

        if expectation.drift_type == "required_null_violation":
            bad_condition = f"isNull({operand})"
            total_expr = "count()"
            sample_expr = f"anyIf('<NULL>', {bad_condition})"
        elif expectation.drift_type == "enum_violation":
            options = ", ".join(self._quote_string(option) for option in expectation.enum_options)
            present_condition = f"NOT isNull({operand})"
            bad_condition = f"{present_condition} AND {value_expr} NOT IN ({options})"
            total_expr = f"countIf({present_condition})"
            sample_expr = f"anyIf({value_expr}, {bad_condition})"
        elif expectation.drift_type == "regex_violation":
            present_condition = f"NOT isNull({operand})"
            # The assert narrows the type; a pattern-less regex is inert above.
            assert expectation.regex is not None
            # RE2 is stricter than the Python `re` the save gate screens with —
            # it has no lookaround and no backreferences — so the pattern is
            # offered to this engine before it is compiled into a statement every
            # other contract in the scan is riding in.
            if not self.contract_regex_is_compilable(expectation.regex):
                self._skip_field_contract(expectation)
                return None
            pattern = self._quote_string(expectation.regex)
            bad_condition = f"{present_condition} AND NOT match({value_expr}, {pattern})"
            total_expr = f"countIf({present_condition})"
            sample_expr = f"anyIf({value_expr}, {bad_condition})"
        elif expectation.drift_type == "range_violation":
            present_condition = f"NOT isNull({operand})"
            numeric_expr = f"toFloat64OrNull({value_expr})"
            range_conditions = [f"isNull({numeric_expr})"]
            # Rendered through the shared helper rather than an f-string of the
            # float. ClickHouse is the one engine that HAS `inf` and `nan`
            # literals, so a non-finite bound used to compile here and compare
            # perfectly quietly — against a bound no row can be below — while the
            # same contract raised on PostgreSQL and produced unparseable SQL on
            # BigQuery. The helper refuses to render one at all, which is what
            # keeps this branch from being the odd one out again.
            if expectation.min_value is not None:
                range_conditions.append(
                    f"{numeric_expr} < {contract_bound_literal(expectation.min_value)}"
                )
            if expectation.max_value is not None:
                range_conditions.append(
                    f"{numeric_expr} > {contract_bound_literal(expectation.max_value)}"
                )
            bad_condition = f"{present_condition} AND ({' OR '.join(range_conditions)})"
            total_expr = f"countIf({present_condition})"
            sample_expr = f"anyIf({value_expr}, {bad_condition})"
        else:
            return None

        return f"countIf({bad_condition})", total_expr, sample_expr

    @override
    def validate_field_contracts(
        self,
        base_query: str,
        expectations: list[FieldContractExpectation],
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        group_column: str | None = None,
        group_value: str | None = None,
        limit: int = 50000,
    ) -> list[FieldContractViolation]:
        """Count every field contract in one pass over the window.

        The expectations' aggregates are columns of a single flat SELECT over one
        ``FROM (base_query)``. What this replaced was a UNION ALL of one aggregate
        subquery per expectation: ClickHouse does not deduplicate an identical
        inline subquery across the arms, so a config with ten contracts read the
        whole window ten times, and ``catalog_sync`` repeats the call once per
        event-type group.

        Only counts come back — the threshold comparison is
        ``field_contract_verdict``'s, per the contract on ``BaseAdapter`` — so the
        caller's expectation order is preserved by walking it, and
        ``if(total_count = 0, ...)``, which existed only to keep the in-SQL rate
        from dividing by zero on an empty window, is gone with it.
        """
        if not expectations:
            return []

        where_clause = self._contract_where_clause(
            time_column,
            time_from,
            time_to,
            group_column,
            group_value,
        )
        # Compiled before any statement is built: expectations that check nothing
        # contribute no columns, and a set that is entirely inert must leave the
        # warehouse untouched.
        compiled = [
            (expectation, aggregates)
            for expectation in expectations
            if (aggregates := self._contract_aggregates(expectation)) is not None
        ]
        if not compiled:
            return []

        violations: list[FieldContractViolation] = []
        for start in range(0, len(compiled), FIELD_CONTRACT_EXPECTATIONS_PER_QUERY):
            chunk = compiled[start : start + FIELD_CONTRACT_EXPECTATIONS_PER_QUERY]
            selects = ", ".join(
                f"{bad} AS _bad_{index}, {total} AS _total_{index}, {sample} AS _sample_{index}"
                for index, (_expectation, (bad, total, sample)) in enumerate(chunk)
            )
            sql = f"SELECT {selects} FROM ({base_query}) AS _src{where_clause}"
            logger.info("CH field contract query: %s", sql)
            rows = self._client.query(sql).result_rows
            if not rows:
                continue
            row = rows[0]
            for index, (expectation, _aggregates) in enumerate(chunk):
                sample = row[index * 3 + 2]
                violation = field_contract_verdict(
                    expectation,
                    bad_count=int(row[index * 3]),
                    total_count=int(row[index * 3 + 1]),
                    sample_value=None if sample is None else str(sample),
                )
                if violation is not None:
                    violations.append(violation)

        # ``limit`` bounds the violation ROWS handed back, never the rows scanned,
        # which is what the LIMIT clause it replaces did.
        return violations[: max(0, int(limit))]

    def _query_top_breakdown_values_multi(
        self,
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        if limit <= 0 or not breakdown_columns:
            return {column: [] for column in breakdown_columns}

        tc = self._validate_column(time_column)
        breakdown_cols = [self._validate_breakdown_field(column) for column in breakdown_columns]
        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        prepared_parts = [
            f"{self._field_value_expression(column)} AS `__bd_raw_{idx}`"
            for idx, column in enumerate(breakdown_cols)
        ]
        branch_conditions = [
            f"GROUPING(`__bd_raw_{idx}`) = 0, {self._quote_string(column)}"
            for idx, column in enumerate(breakdown_cols)
        ]
        value_conditions = [
            f"GROUPING(`__bd_raw_{idx}`) = 0, `__bd_raw_{idx}`"
            for idx in range(len(breakdown_cols))
        ]
        grouping_sets = ", ".join(f"(`__bd_raw_{idx}`)" for idx in range(len(breakdown_cols)))
        sql = (
            "SELECT _breakdown_column, _breakdown_value "
            "FROM ("
            "SELECT "
            f"multiIf({', '.join(branch_conditions)}, '') AS _breakdown_column, "
            f"multiIf({', '.join(value_conditions)}, '') AS _breakdown_value, "
            "count() AS _cnt "
            "FROM ("
            f"SELECT {', '.join(prepared_parts)} "
            f"FROM ({base_query}) AS _src{where_clause}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") "
            # The third key is the tie-break the BaseAdapter top-N contract
            # requires. LIMIT BY cuts each column's list at n, and ordered by
            # count alone the engine was free to hand back either of two
            # equally-counted values at that cut, so the same window ranked
            # twice could keep a different set. ClickHouse compares String
            # byte-wise, which over UTF-8 is the code-point order the contract
            # names, so no explicit collation is needed here.
            "ORDER BY _breakdown_column, _cnt DESC, _breakdown_value "
            f"LIMIT {int(limit)} BY _breakdown_column"
        )
        logger.info("CH breakdown top-values GROUPING SETS query: %s", sql)
        result = self._client.query(sql)
        top_values: dict[str, list[str]] = {column: [] for column in breakdown_cols}
        for column, value in result.result_rows:
            top_values.setdefault(str(column), []).append(str(value))
        return top_values

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
        """Single GROUP BY ALL query: regular cols + nested-shape arrays + count().

        The returned tuple and the row layout are stated once, on
        :meth:`BaseAdapter.get_full_breakdown`, and deliberately not repeated here.
        This file used to carry four hand-maintained copies of that contract and
        three of them had drifted; this one named a 3-tuple while both the annotation
        and the ``return`` statement below say four. PostgreSQL's override carries no
        docstring at all for exactly this reason and never drifted.
        """
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}
        json_value_names: list[str] = []

        select_parts: list[str] = []
        for c in reg_cols:
            select_parts.append(f"`{c}`")
        for c in json_cols:
            select_parts.append(self._json_paths_expression(c))
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                select_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{full_path}`"
                )
                json_value_names.append(full_path)
        select_parts.append("count() AS _cnt")
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY ALL "
            f"ORDER BY _cnt DESC "
            f"LIMIT {int(limit)}"
        )

        short = sql[:300] + ("..." if len(sql) > 300 else "")
        logger.info(f"CH breakdown query: {short}")
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info(f"CH breakdown done in {elapsed:.2f}s, {n_rows} rows")

        return reg_cols, json_cols, json_value_names, _as_rows(result.result_rows)

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
        """Time-bucketed GROUP BY ALL with all columns, like get_full_breakdown.

        Returned tuple and row layout: see
        :meth:`BaseAdapter.get_time_bucketed_counts`.
        """
        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}

        select_parts = [f"{bucket_sql} AS _bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            select_parts.append(f"`{c}`")
            col_names.append(c)
        for c in json_cols:
            select_parts.append(self._json_paths_expression(c))
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                select_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{full_path}`"
                )
                json_value_names.append(full_path)
        select_parts.append("count() AS _cnt")

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY ALL "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.info(f"CH bucketed query: {sql}")
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info(f"CH bucketed done in {elapsed:.2f}s, {n_rows} rows")

        return col_names, json_value_names, _as_rows(result.result_rows)

    def _aggregate_value_sql(self, agg_fn: MetricAggregation, measure_column: str | None) -> str:
        """Validate + escape the measure and build the safe aggregate fragment."""
        measure_sql: str | None = None
        if measure_column is not None:
            measure_sql = f"`{validate_measure_column(measure_column, self._allowed_columns)}`"
        return build_aggregate_sql(agg_fn, measure_sql)

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        """Time-bucketed aggregate, mirroring get_time_bucketed_counts.

        Returned tuple and row layout: see
        :meth:`BaseAdapter.get_time_bucketed_aggregate`.
        """
        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        select_parts = [f"{bucket_sql} AS _bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            select_parts.append(f"`{c}`")
            col_names.append(c)
        for c in json_cols:
            select_parts.append(self._json_paths_expression(c))
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                select_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{full_path}`"
                )
                json_value_names.append(full_path)
        select_parts.append(f"{value_sql} AS _value")

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY ALL "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.info("CH bucketed aggregate query: %s", sql)
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info("CH bucketed aggregate done in %.2fs, %s rows", elapsed, n_rows)

        return col_names, json_value_names, _as_rows(result.result_rows)

    def get_time_bucketed_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        """Time-bucketed aggregate grouped by one breakdown column.

        Returned tuple and row layout: see
        :meth:`BaseAdapter.get_time_bucketed_aggregate_breakdown`.
        """
        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown = self._validate_column(breakdown_column)
        if breakdown not in reg_cols:
            msg = f"Breakdown column must be a scalar column: {breakdown}"
            raise ValueError(msg)
        json_value_paths = json_value_paths or {}
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        raw_expr = self._string_value_expression(breakdown)
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query,
            time_column,
            breakdown,
            raw_expr,
            time_from,
            time_to,
            values_limit,
        )

        select_parts = [f"{bucket_sql} AS _bucket"]
        select_parts.append(f"{breakdown_expr} AS _breakdown_value")
        select_parts.append(f"{is_other_expr} AS _is_other")
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            if c == breakdown:
                # The breakdown keeps its regular-column slot but carries the
                # FOLDED value there: see
                # BaseAdapter.get_time_bucketed_aggregate_breakdown for why the
                # raw column may not be a grouping key. Here the projection IS
                # the grouping — `GROUP BY ALL` groups by every non-aggregate
                # SELECT term — so emitting the fold leaves ALL with two terms
                # that always hold the same value, hence one group. Replacing
                # ALL with an explicit grouping list would work too and was
                # rejected: it means restating the whole projection a second
                # time and keeping the two copies in step by hand, for a query
                # that has never needed one.
                select_parts.append(f"{breakdown_expr} AS `{c}`")
            else:
                select_parts.append(f"`{c}`")
            col_names.append(c)
        for c in json_cols:
            select_parts.append(self._json_paths_expression(c))
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                select_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{full_path}`"
                )
                json_value_names.append(full_path)
        select_parts.append(f"{value_sql} AS _value")

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY ALL "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info("CH bucketed aggregate breakdown query for %s: %s", breakdown, sql)
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info("CH bucketed aggregate breakdown done in %.2fs, %s rows", elapsed, n_rows)

        return col_names, json_value_names, _as_rows(result.result_rows)

    def _breakdown_value_exprs(
        self,
        base_query: str,
        time_column: str,
        breakdown: str,
        raw_expr: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """Build the (breakdown_value, is_other) expressions with Other folding.

        Reuses the count path's top-values selection so the aggregate breakdown
        buckets identically to get_time_bucketed_breakdown_counts.
        """
        if values_limit is None:
            return raw_expr, "0"
        top_count = max(values_limit - 1, 0)
        top_values = self._top_breakdown_values_multi(
            base_query,
            time_column,
            [breakdown],
            time_from,
            time_to,
            top_count,
        ).get(breakdown, [])
        if not top_values:
            return "'Other'", "1"
        quoted = ", ".join(self._quote_string(value) for value in top_values)
        in_values = f"{raw_expr} IN ({quoted})"
        return f"if({in_values}, {raw_expr}, 'Other')", f"if({in_values}, 0, 1)"

    def _spec_aggregate_sql(self, spec: AggregateSpec) -> str:
        """Build one (optionally conditional) aggregate fragment for a spec.

        With no ``filter_sql`` this reuses the single-aggregate path
        (``validate_measure_column`` + ``build_aggregate_sql``) so the unfiltered
        value is IDENTICAL to ``get_time_bucketed_aggregate`` (``count(*)`` /
        ``sum(m)`` / ``avg(m)`` / ``min(m)`` / ``max(m)`` / ``count(DISTINCT m)``).
        When ``filter_sql`` is set, the aggregate becomes a ClickHouse conditional
        ``-If`` variant (``countIf`` / ``sumIf`` / ``avgIf`` / ``minIf`` /
        ``maxIf`` / ``uniqExactIf``) so specs with different filters share one
        scan. ``filter_sql`` is a pre-validated boolean fragment injected as-is,
        the same trust model as the single-metric row-filter path.

        Each conditional aggregate is wrapped in ``if(countIf(cond) = 0, NULL,
        ...)`` so a bucket with rows but none matching ``cond`` reads as NULL
        (absent) for that key. This is required for value-identity with the
        per-metric path, whose filtered scan emits no row at all for such a
        bucket: ClickHouse ``-If`` functions instead return the numeric default
        (``countIf``/``sumIf`` -> 0) or a type extreme/NaN (``minIf``/``maxIf``/
        ``avgIf``) for empty groups, none of which is NULL. The count sentinel
        also preserves a genuine aggregate of 0 (e.g. ``sumIf`` over rows that
        net to zero, or ``uniqExactIf`` over matching rows whose measure is NULL
        throughout) because the bucket still has matching rows.

        That last clause is the shared rule, not a ClickHouse detail: the gate
        counts ROWS, never the aggregate. It is stated once on
        :class:`~tripl.core.adapters.base.BaseAdapter`, and the other three
        adapters now gate the same way — PostgreSQL and BigQuery used to test a
        filtered ``count(DISTINCT m)`` for 0, which is also what an all-NULL
        measure over real rows returns.
        """
        agg = coerce_aggregation(spec.aggregation)
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = f"`{validate_measure_column(spec.column, self._allowed_columns)}`"

        if not spec.filter_sql:
            return build_aggregate_sql(agg, measure_sql)

        cond = spec.filter_sql
        if agg is MetricAggregation.count:
            inner = f"countIf({cond})"
        elif agg is MetricAggregation.count_distinct:
            if not measure_sql:
                msg = f"Aggregation {agg.value!r} requires a measure column"
                raise ValueError(msg)
            inner = f"uniqExactIf({measure_sql}, {cond})"
        else:
            if not measure_sql:
                msg = f"Aggregation {agg.value!r} requires a measure column"
                raise ValueError(msg)
            # sum/avg/min/max map their enum value onto the conditional -If variant.
            inner = f"{agg.value}If({measure_sql}, {cond})"
        return f"if(countIf({cond}) = 0, NULL, {inner})"

    def build_time_bucketed_multi_aggregate_sql(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], str]:
        """Build the many-aggregate statement without executing it.

        Mirrors get_time_bucketed_aggregate's windowing, bucketing, quoting and
        row limit, but emits one (optionally conditional) aggregate column per
        spec aliased by ``spec.key``.
        """
        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)

        select_parts = [f"{bucket_sql} AS _bucket"]
        col_names: list[str] = ["bucket"]
        for spec in specs:
            select_parts.append(
                f"{self._spec_aggregate_sql(spec)} AS `{self._validate_alias(spec.key)}`"
            )
            col_names.append(spec.key)

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY _bucket "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )
        return col_names, sql

    def get_time_bucketed_multi_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Many bucketed aggregates from ONE source scan."""
        col_names, sql = self.build_time_bucketed_multi_aggregate_sql(
            base_query,
            time_column,
            interval,
            specs,
            time_from,
            time_to,
            limit=limit,
        )

        logger.info("CH bucketed multi-aggregate query: %s", sql)
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info("CH bucketed multi-aggregate done in %.2fs, %s rows", elapsed, n_rows)

        return col_names, _as_rows(result.result_rows)

    def get_time_bucketed_multi_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Many bucketed aggregates grouped by one breakdown column, ONE scan.

        Reuses ``_breakdown_value_exprs``, so both the folding expression and
        the grouping match get_time_bucketed_aggregate_breakdown: one row per
        (bucket, folded value), never one per raw value behind ``'Other'``.
        Then one aggregate column per spec.

        "Matches exactly" was written here while that other method still also
        grouped by the raw breakdown column, so the two did not in fact agree —
        which is why the claim now names the property it is claiming instead of
        asserting sameness.
        """
        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)
        breakdown = self._validate_column(breakdown_column)

        raw_expr = self._string_value_expression(breakdown)
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query,
            time_column,
            breakdown,
            raw_expr,
            time_from,
            time_to,
            values_limit,
        )

        select_parts = [
            f"{bucket_sql} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        col_names: list[str] = ["bucket", "breakdown_value", "is_other"]
        for spec in specs:
            select_parts.append(
                f"{self._spec_aggregate_sql(spec)} AS `{self._validate_alias(spec.key)}`"
            )
            col_names.append(spec.key)

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY ALL "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info("CH bucketed multi-aggregate breakdown query for %s: %s", breakdown, sql)
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info("CH bucketed multi-aggregate breakdown done in %.2fs, %s rows", elapsed, n_rows)

        return col_names, _as_rows(result.result_rows)

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
        col_names, json_value_names, rows = self.get_time_bucketed_breakdown_counts_multi(
            base_query,
            time_column,
            interval,
            [breakdown_column],
            regular_columns,
            json_columns,
            json_value_paths,
            time_from,
            time_to,
            values_limit=values_limit,
            limit=limit,
        )
        return col_names, json_value_names, [(row[0], row[2], row[3], *row[4:]) for row in rows]

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
        """Time-bucketed GROUPING SETS query for independent breakdown columns.

        All event matching columns stay in every grouping set so rows can still
        be mapped to catalog events after ClickHouse has done the counting.
        """
        if not breakdown_columns:
            return [], [], []

        tc = self._validate_column(time_column)
        bucket_sql = self._bucket_expression(tc, interval)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown_cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        invalid_breakdown_cols = [
            column
            for column in breakdown_cols
            if not is_breakdown_field_of(column, reg_cols, json_cols)
        ]
        if invalid_breakdown_cols:
            msg = (
                "Breakdown columns must be scalar columns or properties of a JSON column: "
                f"{', '.join(invalid_breakdown_cols)}"
            )
            raise ValueError(msg)

        json_value_paths = json_value_paths or {}
        top_values_by_column: dict[str, list[str]] | None = None
        if values_limit is not None:
            top_count = max(values_limit - 1, 0)
            top_values_by_column = self._top_breakdown_values_multi(
                base_query,
                time_column,
                breakdown_cols,
                time_from,
                time_to,
                top_count,
            )

        prepared_parts = [f"{bucket_sql} AS _bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            prepared_parts.append(f"`{c}` AS `{c}`")
            col_names.append(c)
        for c in json_cols:
            prepared_parts.append(f"{self._json_paths_expression(c)} AS `{c}`")
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                prepared_parts.append(
                    f"toJSONString({self._json_path_expression(c, path)}) AS `{full_path}`"
                )
                json_value_names.append(full_path)

        grouping_columns = [f"`{name}`" for name in [*reg_cols, *json_cols, *json_value_names]]
        breakdown_label_conditions: list[str] = []
        breakdown_value_conditions: list[str] = []
        breakdown_other_conditions: list[str] = []
        grouping_sets: list[str] = []

        for idx, column in enumerate(breakdown_cols):
            raw_expr = self._field_value_expression(column)
            value_alias = f"__bd_value_{idx}"
            other_alias = f"__bd_other_{idx}"
            top_values = (
                None if top_values_by_column is None else top_values_by_column.get(column, [])
            )
            if top_values is None:
                breakdown_expr = raw_expr
                is_other_expr = "0"
            elif top_values:
                quoted_values = ", ".join(self._quote_string(value) for value in top_values)
                in_values = f"{raw_expr} IN ({quoted_values})"
                breakdown_expr = f"if({in_values}, {raw_expr}, 'Other')"
                is_other_expr = f"if({in_values}, 0, 1)"
            else:
                breakdown_expr = "'Other'"
                is_other_expr = "1"

            prepared_parts.append(f"{breakdown_expr} AS `{value_alias}`")
            prepared_parts.append(f"{is_other_expr} AS `{other_alias}`")
            grouping_check = f"GROUPING(`{value_alias}`) = 0"
            breakdown_label_conditions.append(f"{grouping_check}, {self._quote_string(column)}")
            breakdown_value_conditions.append(f"{grouping_check}, `{value_alias}`")
            breakdown_other_conditions.append(f"{grouping_check}, `{other_alias}`")
            grouping_sets.append(
                "("
                + ", ".join(
                    [
                        "`_bucket`",
                        f"`{value_alias}`",
                        f"`{other_alias}`",
                        *grouping_columns,
                    ]
                )
                + ")"
            )

        select_parts = [
            "`_bucket`",
            f"multiIf({', '.join(breakdown_label_conditions)}, '') AS _breakdown_column",
            f"multiIf({', '.join(breakdown_value_conditions)}, '') AS _breakdown_value",
            f"multiIf({', '.join(breakdown_other_conditions)}, 0) AS _is_other",
            *grouping_columns,
            "count() AS _cnt",
        ]

        where_clause = self._time_window_where_clause(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            "FROM ("
            f"SELECT {', '.join(prepared_parts)} "
            f"FROM ({base_query}) AS _src{where_clause}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({', '.join(grouping_sets)}) "
            "ORDER BY _bucket, _breakdown_column, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info(
            "CH bucketed breakdown GROUPING SETS query for %s: %s",
            ", ".join(breakdown_cols),
            sql,
        )
        t0 = time.monotonic()
        result = self._client.query(sql)
        elapsed = time.monotonic() - t0
        n_rows = len(result.result_rows)
        logger.info("CH bucketed breakdown GROUPING SETS done in %.2fs, %s rows", elapsed, n_rows)

        return col_names, json_value_names, _as_rows(result.result_rows)
