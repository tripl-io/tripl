"""Turning stored metric config into validated SQL condition fragments.

Extracted from ``metric_collect`` unchanged. It is the vocabulary BOTH halves of
that module speak: the fact-metrics batch runner and the three per-metric
collectors in ``_COLLECTORS`` reach into it at seven points, so it belonged to
neither of them and sat in the middle instead.

It has no dependency on anything in ``metric_collect`` — the extraction is a
pure move, verified by the call graph before it was made. Keep it that way: the
import goes one direction only, or the collectors and this toolkit become a
cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from tripl.core.adapters.measure_validator import (
    TRINO_DIALECTS,
    SqlDialect,
    coerce_aggregation,
    parse_utc_timestamp,
    quote_identifier,
    quote_sql_literal,
    quote_sql_string_literal,
    quote_timestamp_literal,
    requires_measure,
    time_kind_of,
    validate_identifier,
    validate_measure_column,
    validate_sql_fragment,
)
from tripl.core.warehouse_types import TimeKind
from tripl.models.domain_enums import MetricAggregation
from tripl.models.fact_table import FactTable
from tripl.worker.tasks._errors import ScanError


def _config_str(config: Mapping[str, object], key: str) -> str | None:
    """Return ``config[key]`` only when it is a non-empty string, else ``None``."""
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _config_str_list(config: Mapping[str, object], key: str) -> list[str]:
    """Return ``config[key]`` as a list of non-empty strings (else ``[]``)."""
    value = config.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _effective_filter_names(config: Mapping[str, object]) -> tuple[str, ...]:
    """Effective named-filter set from a stored operand ``config``.

    Reads the new ``row_filters`` list and folds in a legacy single
    ``row_filter`` name (back-compat with configs written before multi-filter
    support). Order is preserved and duplicates removed so the WHERE assembled
    from it is deterministic and identical across the per-metric and batched
    collection paths.
    """
    effective: list[str] = list(_config_str_list(config, "row_filters"))
    legacy = _config_str(config, "row_filter")
    if legacy is not None and legacy not in effective:
        effective.append(legacy)
    return tuple(effective)


@dataclass(frozen=True)
class _FactCondition:
    """One stored visual condition row after defensive config parsing."""

    column: str
    operator: str
    value: object | None


_CONDITION_VALUELESS_OPERATORS = frozenset({"is_null", "is_not_null", "is_true", "is_false"})
_CONDITION_MULTI_VALUE_OPERATORS = frozenset({"in", "not_in"})
_CONDITION_BINARY_OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_CONDITION_OPERATORS = frozenset(
    {
        *_CONDITION_VALUELESS_OPERATORS,
        *_CONDITION_MULTI_VALUE_OPERATORS,
        *_CONDITION_BINARY_OPERATORS.keys(),
        "contains",
        "not_contains",
        "like",
        "not_like",
    }
)


type _SqlLiteralValue = str | int | float | bool


def _is_sql_literal_value(value: object) -> TypeGuard[_SqlLiteralValue]:
    return isinstance(value, (str, int, float, bool))


@dataclass(frozen=True)
class _FactOperand:
    """One resolved fact operand (the single metric, or one ratio side).

    ``row_filters`` is the effective named-filter set (legacy single
    ``row_filter`` already folded in), ``filter_sql`` is the optional free-text
    WHERE fragment, and ``conditions`` are visual column/operator/value rows; all
    are resolved to one ANDed WHERE expression at collection time.
    """

    fact_table_id: uuid.UUID
    aggregation: MetricAggregation
    measure_column: str | None
    distinct_column: str | None
    row_filters: tuple[str, ...]
    filter_sql: str | None
    conditions: tuple[_FactCondition, ...]


def _operand_from_config(raw: Mapping[str, object]) -> _FactOperand:
    """Parse a fact ratio operand from its stored ``config`` dict."""
    fact_table_id = raw.get("fact_table_id")
    aggregation = raw.get("aggregation")
    if not isinstance(fact_table_id, str) or not isinstance(aggregation, str):
        msg = "fact ratio operand requires fact_table_id and aggregation in config"
        raise ScanError(msg)
    return _FactOperand(
        fact_table_id=uuid.UUID(fact_table_id),
        aggregation=coerce_aggregation(aggregation),
        measure_column=_config_str(raw, "measure_column"),
        distinct_column=_config_str(raw, "distinct_column"),
        row_filters=_effective_filter_names(raw),
        filter_sql=_config_str(raw, "filter_sql"),
        conditions=_conditions_from_config(raw),
    )


def _conditions_from_config(raw: Mapping[str, object]) -> tuple[_FactCondition, ...]:
    raw_conditions = raw.get("conditions", [])
    if raw_conditions is None:
        return ()
    if not isinstance(raw_conditions, list):
        msg = "fact operand conditions must be a list"
        raise ScanError(msg)

    conditions: list[_FactCondition] = []
    for raw_condition in raw_conditions:
        if not isinstance(raw_condition, Mapping):
            msg = "fact operand condition entries must be objects"
            raise ScanError(msg)
        column = raw_condition.get("column")
        operator = raw_condition.get("operator")
        if not isinstance(column, str) or not isinstance(operator, str):
            msg = "fact operand condition requires column and operator"
            raise ScanError(msg)
        try:
            column = validate_identifier(column)
        except ValueError as exc:
            msg = f"fact operand condition has invalid column {column!r}"
            raise ScanError(msg) from exc
        if operator not in _CONDITION_OPERATORS:
            msg = f"fact operand condition has unsupported operator {operator!r}"
            raise ScanError(msg)
        conditions.append(
            _FactCondition(column=column, operator=operator, value=raw_condition.get("value"))
        )
    return tuple(conditions)


def _fact_operand_measure(operand: _FactOperand) -> str | None:
    """Effective measure column the adapter aggregates for this operand.

    ``count`` aggregates rows (no column); ``count_distinct`` distinct-counts the
    operand's ``distinct_column``; ``sum``/``avg``/``min``/``max`` aggregate its
    ``measure_column``.
    """
    if operand.aggregation is MetricAggregation.count:
        return None
    if operand.aggregation is MetricAggregation.count_distinct:
        return operand.distinct_column
    return operand.measure_column


def _resolve_named_filter_fragment(fact_table: FactTable, name: str) -> str:
    """Resolve one named row filter to its validated boolean WHERE fragment.

    ``name`` must match one of the fact table's stored row filters by NAME; the
    associated fragment is re-validated here as defence in depth. Raises a
    ``ScanError`` for an unknown name.
    """
    for row_filter in fact_table.row_filters or []:
        if isinstance(row_filter, Mapping) and row_filter.get("name") == name:
            fragment = row_filter.get("sql")
            if isinstance(fragment, str) and fragment:
                return validate_sql_fragment(fragment)
    msg = f"row filter {name!r} is not defined on fact table {fact_table.id}"
    raise ScanError(msg)


def _condition_literal(value: object, dialect: SqlDialect) -> str:
    if not _is_sql_literal_value(value):
        msg = "fact operand condition value must be a scalar"
        raise ScanError(msg)
    try:
        return quote_sql_literal(value, dialect)
    except ValueError as exc:
        msg = f"fact operand condition value is invalid: {exc}"
        raise ScanError(msg) from exc


def _condition_scalar_literal(
    value: object,
    *,
    dialect: SqlDialect,
    time_kind: TimeKind,
    column_type: str | None,
) -> str:
    """One comparison literal, typed as a timestamp when the column is a time column.

    A bare ``'2026-01-01 00:00:00'`` *parses* on all three engines, so this is not a
    syntax fix — it is the UTC contract in :mod:`tripl.core.bucketing`. An offset-less
    literal is re-read in the COLUMN's timezone (ClickHouse) or the SESSION's
    (PostgreSQL), which silently shifts the comparison; the typed literal pins UTC the
    same way the adapters' own window bounds do.

    Only a column whose INTROSPECTED type is a time type is retyped, and only when the
    value actually parses as a timestamp. Anything else falls through to the plain
    literal — a STRING column holding ``'2026-01-01'`` still compares as a string.
    """
    if time_kind is not TimeKind.unsupported and isinstance(value, str):
        moment = parse_utc_timestamp(value)
        if moment is not None:
            return quote_timestamp_literal(moment, dialect, kind=time_kind)

    normalized_type = column_type.strip().lower() if column_type is not None else None
    if normalized_type == "number":
        if isinstance(value, bool):
            msg = "numeric column requires a numeric value, not a boolean"
            raise ScanError(msg)
        literal = _condition_literal(value, dialect)
        if isinstance(value, str) and literal.startswith("'"):
            msg = "numeric column requires a numeric value"
            raise ScanError(msg)
        return literal
    if normalized_type == "bool":
        if isinstance(value, bool):
            return _condition_literal(value, dialect)
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return _condition_literal(value, dialect)
        msg = "boolean column requires true or false"
        raise ScanError(msg)
    if normalized_type == "string":
        if not _is_sql_literal_value(value):
            msg = "fact operand condition value must be a scalar"
            raise ScanError(msg)
        text = str(value).lower() if isinstance(value, bool) else str(value)
        try:
            return quote_sql_string_literal(text, dialect)
        except ValueError as exc:
            msg = f"fact operand condition value is invalid: {exc}"
            raise ScanError(msg) from exc
    return _condition_literal(value, dialect)


def _condition_text(value: object, dialect: SqlDialect) -> str:
    if not _is_sql_literal_value(value):
        msg = "fact operand condition value must be a scalar"
        raise ScanError(msg)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _condition_literal(value, dialect)
    text = value.strip()
    if not text:
        msg = "fact operand condition value must not be empty"
        raise ScanError(msg)
    return text


def _escape_like_wildcards(text: str) -> str:
    """Neutralise ``%`` and ``_`` so ``contains`` means *contains*.

    ``contains`` is a substring test in the UI, but it compiled to a bare
    ``LIKE '%value%'``, so a value holding a wildcard silently widened the match:
    ``contains "100%"`` matched every row starting ``100``, and ``contains "a_b"``
    matched ``axb``. Nothing raised — the metric was simply computed over the
    wrong rows. ``like`` / ``not_like`` deliberately do NOT come through here:
    there the pattern is the point.

    The escape character is a backslash, and no ``ESCAPE`` clause is emitted (but on Snowflake)
    because all three dialects converge on it once their own literal quoting has
    run: ``quote_sql_string_literal`` doubles backslashes for ClickHouse and
    BigQuery and leaves them alone for PostgreSQL (``standard_conforming_strings``),
    so a pattern of ``\\%`` here reaches every engine as the value ``\\%`` — an
    escaped percent, which is LIKE's default reading in all three. Snowflake and
    Trino (Athena) have no default escape character, so their LIKE names the
    backslash explicitly.
    Backslash is doubled FIRST so a value containing one cannot escape the escape.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _condition_list_literals(
    value: object,
    *,
    dialect: SqlDialect,
    time_kind: TimeKind,
    column_type: str | None,
) -> list[str]:
    values: list[object]
    if isinstance(value, list):
        values = list(value)
    elif isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    else:
        values = [value]

    literals = [
        _condition_scalar_literal(
            item,
            dialect=dialect,
            time_kind=time_kind,
            column_type=column_type,
        )
        for item in values
        if not (isinstance(item, str) and not item)
    ]
    if not literals:
        msg = "fact operand IN condition requires at least one value"
        raise ScanError(msg)
    return literals


def _resolve_condition_fragment(
    condition: _FactCondition,
    *,
    dialect: SqlDialect,
    column_types: Mapping[str, str] | None = None,
    native_column_types: Mapping[str, str] | None = None,
) -> str:
    """Compile ONE visual condition into a boolean WHERE fragment for ``dialect``.

    Dialect-aware on three axes, each of which used to be a latent worker crash:

    * **Identifier quoting.** The column is quoted for the selected engine, so a
      reserved name (``order``, ``select``) works. Unquoted, it is a syntax error on
      PostgreSQL and BigQuery while ClickHouse happily accepts it (verified live) —
      the exact asymmetry that let this ship.
    * **String escaping.** ``quote_sql_literal`` now escapes per dialect. BigQuery has
      no ``''`` escape, so before this every filter value containing an apostrophe was
      a hard GoogleSQL parse error raised inside a Celery worker.
    * **Timestamp literals.** A time column's bound is emitted as a UTC-pinned typed
      literal (see ``_condition_scalar_literal``).

    TWO type maps, on purpose, and they are not interchangeable:

    * ``column_types`` is the NORMALIZED/bucketed map — the strings ``number`` /
      ``bool`` / ``string`` / ``timestamp``. It is the validation contract:
      ``_condition_scalar_literal`` branches on those exact words, and
      ``metric_definition_service._validate_fact_condition_type`` branches on them
      again at save time. Feeding native warehouse types into it silently disables
      both checks, because ``DATE`` matches none of the branches.
    * ``native_column_types`` is the warehouse's own spelling (``DATE``,
      ``DATETIME``, ``DateTime64(3)``), and is used for ONE thing: deciding the time
      literal's type family. It has to be separate because the bucketed map folds
      BigQuery ``DATE``/``DATETIME``/``TIMESTAMP`` into one word, and BigQuery is
      the dialect that rejects ``DATETIME '...+00:00'``. When it is absent — a fact
      table saved before ``native_type`` was captured — the bucketed map is used, so
      such a table keeps the behaviour it had. ``metric_preview_service`` builds the
      same split for the SQL-disclosure path.

    Security: the column still clears ``validate_identifier``'s allowlist regex FIRST
    (inside ``quote_identifier``), and every value still becomes a quoted/escaped
    literal — there are no bound parameters and no new path by which user input
    reaches the statement unescaped.
    """
    quoted = quote_identifier(condition.column, dialect)
    operator = condition.operator
    value = condition.value
    time_kind = time_kind_of(condition.column, native_column_types or column_types)
    column_type = column_types.get(condition.column) if column_types is not None else None

    if operator == "is_null":
        return f"{quoted} IS NULL"
    if operator == "is_not_null":
        return f"{quoted} IS NOT NULL"
    if operator == "is_true":
        return f"{quoted} = TRUE"
    if operator == "is_false":
        return f"{quoted} = FALSE"

    if operator in _CONDITION_BINARY_OPERATORS:
        if value is None:
            msg = f"fact operand condition {operator!r} requires a value"
            raise ScanError(msg)
        try:
            literal = _condition_scalar_literal(
                value,
                dialect=dialect,
                time_kind=time_kind,
                column_type=column_type,
            )
        except ScanError as exc:
            if column_type is not None and str(exc).startswith("numeric column"):
                msg = f"numeric column {condition.column!r} requires a numeric value"
                raise ScanError(msg) from exc
            raise
        return f"{quoted} {_CONDITION_BINARY_OPERATORS[operator]} {literal}"

    if operator in _CONDITION_MULTI_VALUE_OPERATORS:
        if value is None:
            msg = f"fact operand condition {operator!r} requires a value"
            raise ScanError(msg)
        keyword = "NOT IN" if operator == "not_in" else "IN"
        try:
            literals = _condition_list_literals(
                value,
                dialect=dialect,
                time_kind=time_kind,
                column_type=column_type,
            )
        except ScanError as exc:
            if column_type is not None and str(exc).startswith("numeric column"):
                msg = f"numeric column {condition.column!r} requires numeric values"
                raise ScanError(msg) from exc
            raise
        return f"{quoted} {keyword} ({', '.join(literals)})"

    if operator in {"like", "not_like", "contains", "not_contains"}:
        if value is None:
            msg = f"fact operand condition {operator!r} requires a value"
            raise ScanError(msg)
        text = _condition_text(value, dialect)
        pattern = (
            f"%{_escape_like_wildcards(text)}%"
            if operator in {"contains", "not_contains"}
            else text
        )
        keyword = "NOT LIKE" if operator in {"not_like", "not_contains"} else "LIKE"
        try:
            literal = quote_sql_string_literal(pattern, dialect)
        except ValueError as exc:
            msg = f"fact operand condition value is invalid: {exc}"
            raise ScanError(msg) from exc
        # Snowflake's and Trino's LIKE have no default escape character; name the
        # backslash every other engine reads by default. Snowflake's literal
        # ``'\\'`` is one backslash; so is Trino's ``'\'`` (it has no escapes).
        escape = ""
        if dialect is SqlDialect.snowflake:
            escape = " ESCAPE '\\\\'"
        elif dialect in TRINO_DIALECTS:
            escape = " ESCAPE '\\'"
        return f"{quoted} {keyword} {literal}{escape}"

    msg = f"fact operand condition has unsupported operator {operator!r}"
    raise ScanError(msg)


def _fact_column_types(fact_table: FactTable) -> dict[str, str]:
    """``{column: bucketed type}`` from the fact table's introspected columns.

    The bucketed ``type`` — ``number`` / ``bool`` / ``string`` / ``timestamp`` —
    which is the form and validation contract. For the warehouse's own spelling,
    see ``_fact_native_column_types``.
    """
    types: dict[str, str] = {}
    for column in fact_table.columns or []:
        if not isinstance(column, Mapping):
            continue
        name = column.get("name")
        type_name = column.get("type")
        if isinstance(name, str) and isinstance(type_name, str):
            types[name] = type_name
    return types


def _fact_native_column_types(fact_table: FactTable) -> dict[str, str]:
    """``{column: native warehouse type}`` from the same introspected columns.

    ``native_type`` is what the warehouse called the column (``DATE``,
    ``TIMESTAMP``, ``DateTime64(3)``); introspection records it alongside the
    bucketed ``type``. Only the time classification reads this map — see
    ``_resolve_condition_fragment`` for why the two cannot be merged.

    The fallback to the bucketed ``type`` is deliberate rather than defensive: a
    fact table saved before ``native_type`` existed has no such value, and reading
    the bucket for it reproduces exactly the behaviour it has today instead of
    silently dropping its time typing. An empty-string ``native_type`` takes the
    same fallback, which is why this is ``or`` and not ``if "native_type" in``.
    """
    types: dict[str, str] = {}
    for column in fact_table.columns or []:
        if not isinstance(column, Mapping):
            continue
        name = column.get("name")
        native = column.get("native_type")
        bucketed = column.get("type")
        type_name = native if isinstance(native, str) and native else bucketed
        if isinstance(name, str) and isinstance(type_name, str):
            types[name] = type_name
    return types


def _resolve_combined_filter(
    fact_table: FactTable,
    *,
    dialect: SqlDialect,
    row_filters: tuple[str, ...],
    filter_sql: str | None,
    conditions: tuple[_FactCondition, ...] = (),
) -> str | None:
    """Resolve named filters + free-text SQL + conditions into one WHERE.

    Every name in ``row_filters`` is resolved to its stored SQL fragment (by
    NAME), then a present ``filter_sql`` and any structured conditions are
    appended; each fragment is wrapped in parentheses and the list is joined
    with ``" AND "``. An empty set returns ``None`` (no extra WHERE). The SAME
    combined string is consumed by both the per-metric path (a bounded ``WHERE``
    subquery) and the batched path (a conditional aggregate), preserving
    value-identity between them.

    Named filters and ``filter_sql`` are FREE-TEXT and stay explicitly
    dialect-specific: the user wrote them against a particular warehouse, so they
    are re-validated by the dialect-agnostic read-only gate
    (``validate_sql_fragment``) and passed through verbatim. Only the STRUCTURED
    conditions — which we compile — are rendered for ``dialect``.
    """
    fragments = [_resolve_named_filter_fragment(fact_table, name) for name in row_filters]
    if filter_sql is not None:
        fragments.append(validate_sql_fragment(filter_sql))
    column_types = _fact_column_types(fact_table)
    native_column_types = _fact_native_column_types(fact_table)
    fragments.extend(
        _resolve_condition_fragment(
            condition,
            dialect=dialect,
            column_types=column_types,
            native_column_types=native_column_types,
        )
        for condition in conditions
    )
    if not fragments:
        return None
    return " AND ".join(f"({fragment})" for fragment in fragments)


def _resolve_fact_operand_query(
    fact_table: FactTable, operand: _FactOperand, *, dialect: SqlDialect
) -> str:
    """Resolve a fact operand's base query, applying its combined row filter.

    The base query is the fact table's stored ``sql``. The operand's effective
    named filters and ``filter_sql`` are assembled into one ANDed boolean
    expression (see ``_resolve_combined_filter``); when present it wraps the
    source in a bounded ``WHERE`` subquery, else the source is returned as-is.

    The wrapper holds for a CTE-backed fact source too: ``SELECT * FROM (WITH x AS
    (...) SELECT * FROM x) AS _filtered WHERE ...`` is valid on ClickHouse,
    PostgreSQL and BigQuery alike (verified against live engines).
    """
    source = fact_table.sql
    combined = _resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=operand.row_filters,
        filter_sql=operand.filter_sql,
        conditions=operand.conditions,
    )
    if combined is None:
        return source
    return f"SELECT * FROM ({source}) AS _filtered WHERE {combined}"


def _resolve_fact_operand_filter(
    operand: _FactOperand, *, fact_table: FactTable, dialect: SqlDialect
) -> str | None:
    """Resolve an operand's combined WHERE fragment for the batched path.

    Unlike ``_resolve_fact_operand_query`` (which wraps the source in a bounded
    ``WHERE`` subquery for the single-aggregate path), this returns just the
    combined boolean fragment so it can be injected as a per-aggregate
    conditional in a shared multi-aggregate scan. The fragment is built by the
    SAME ``_resolve_combined_filter`` the per-metric path uses — and now for the
    SAME dialect — so the conditional aggregate computes the identical value.
    ``None`` means no filter.
    """
    return _resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=operand.row_filters,
        filter_sql=operand.filter_sql,
        conditions=operand.conditions,
    )


def _validated_measure_column(measure: str, *, allowed_columns: set[str]) -> str:
    """Hold a measure column to the fact table's allowlist, failing as a ``ScanError``.

    The single owner of that check for BOTH collection paths — the batched
    ``_resolve_batch_operand`` below and the per-metric ``_aggregate_fact_window`` —
    so the two cannot drift apart again.

    Two things this adds over calling ``validate_measure_column`` directly:

    * An empty allowlist raises instead of passing. ``validate_measure_column``
      short-circuits its membership check on a falsy allowlist, so an allowlist that
      never got filled silently bypasses the only column guard there is.
    * A missing column is reported as a ``ScanError``, not a ``ValueError``.
      ``validate_measure_column`` lives in ``core`` and raises a plain ``ValueError``,
      which ``user_facing_error`` refuses to surface verbatim — so a measure column
      renamed in the warehouse reached the operator as "Scan failed due to an
      internal error." with nothing to act on. The text names only the column, which
      is already identifier-constrained, and is safe to show. Same move as
      ``_dialect_for_data_source`` and ``_reject_truncated_rows``.
    """
    if not allowed_columns:
        msg = "fact table query returned no columns; cannot validate measure column"
        raise ScanError(msg)
    try:
        return validate_measure_column(measure, allowed_columns)
    except ValueError as exc:
        raise ScanError(str(exc)) from exc


def _validate_breakdown_columns(
    breakdown_columns: Sequence[str], *, allowed_columns: set[str]
) -> None:
    """Hold breakdown dimensions to the same allowlist a measure column answers to.

    The save door now refuses an unknown dimension itself:
    ``_verify_fact_breakdown_columns`` in ``metric_definition_service`` answers 422
    for a breakdown column, ``app_version_column`` or ``platform_column`` that is
    not in the fact table's STORED ``columns`` snapshot. So this
    is the live-introspection backstop rather than the only check. It still has to
    exist, because the snapshot and the warehouse are two different facts: a
    dimension that was in the snapshot at save time can be dropped from the
    warehouse afterwards, the snapshot itself can be stale, and rows saved before
    that check landed were never asked.

    Without this the column is caught by the adapter's own ``_validate_column``, which
    raises a bare ``ValueError`` — the generic internal-error summary again. Fail here
    instead, naming the column, the way an unknown measure column already does. Used
    by both the batched planners and the per-metric breakdown collectors.
    """
    if not breakdown_columns:
        return
    if not allowed_columns:
        msg = "fact table query returned no columns; cannot validate breakdown columns"
        raise ScanError(msg)
    unknown = sorted({column for column in breakdown_columns if column not in allowed_columns})
    if unknown:
        listed = ", ".join(repr(column) for column in unknown)
        msg = f"breakdown column(s) {listed} are not columns of the fact table"
        raise ScanError(msg)


def _resolve_batch_operand(
    operand: _FactOperand,
    *,
    fact_table: FactTable,
    allowed_columns: set[str],
    dialect: SqlDialect,
) -> tuple[str | None, str | None]:
    """Validate one operand's measure column and resolve its row filter fragment.

    Returns ``(validated_measure, filter_sql)``. Shares ``_validated_measure_column``
    with ``_aggregate_fact_window`` so the batched path enforces the same column guard
    — and reports the same named, user-facing failure — as the per-metric path, and
    compiles the filter for the SAME ``dialect``, so the conditional aggregate and the
    bounded-subquery path stay value-identical.
    """
    measure = _fact_operand_measure(operand)
    if requires_measure(operand.aggregation):
        if measure is None:
            msg = (
                f"aggregation {operand.aggregation.value!r} requires a "
                "measure_column / distinct_column"
            )
            raise ScanError(msg)
        measure = _validated_measure_column(measure, allowed_columns=allowed_columns)
    _validate_condition_columns(operand, allowed_columns=allowed_columns)
    filter_sql = _resolve_fact_operand_filter(operand, fact_table=fact_table, dialect=dialect)
    return measure, filter_sql


def _validate_condition_columns(operand: _FactOperand, *, allowed_columns: set[str]) -> None:
    """Hold condition columns to the same allowlist a measure column answers to.

    Condition columns clear ``validate_identifier``'s regex when the metric is
    saved, and ``_verify_fact_operand`` checks them against the fact table's
    columns AS THEY WERE THEN. Nothing rechecks them at collection time, so a
    column dropped or renamed in the warehouse afterwards compiles into a query
    that fails deep inside a worker — or, through the generated-SQL disclosure,
    into SQL a user is invited to copy and run. Fail here instead, naming the
    column, the way an unknown measure column already does.
    """
    if not operand.conditions:
        return
    if not allowed_columns:
        msg = "fact table query returned no columns; cannot validate condition columns"
        raise ScanError(msg)
    unknown = sorted(
        {
            condition.column
            for condition in operand.conditions
            if condition.column not in allowed_columns
        }
    )
    if unknown:
        listed = ", ".join(repr(column) for column in unknown)
        msg = f"condition column(s) {listed} are not columns of the fact table"
        raise ScanError(msg)
