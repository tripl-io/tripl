"""Amazon Redshift (provisioned and Serverless), over the PostgreSQL wire protocol.

Redshift forked PostgreSQL 8.0 and kept its protocol, so ``PostgresAdapter``'s
connection handling (psycopg, TLS, the vetted address) and most of its SQL apply.
What differs is overridden here, each at a named dialect seam:

* **No ``date_bin``** — buckets come from epoch arithmetic
  (:func:`~tripl.core.adapters.postgres.epoch_bucket_expression`).
* **No ``FILTER (WHERE ...)``** — a conditional aggregate folds its condition into
  ``CASE``, as on BigQuery.
* **No startup ``options``** — the session's timezone, statement timeout and search
  path are applied with ``SET`` once the connection is open. Redshift has neither
  ``standard_conforming_strings`` nor ``default_transaction_read_only``; the
  credential's own privileges are the write barrier, as on every warehouse.
* **Backslash is an escape in string literals** (PostgreSQL 8.0 behaviour, with no
  setting to turn it off), so a literal doubles the backslash as well as the quote.
  Without that, a value ending in ``\\`` would close the literal early.
* **``text`` is ``varchar(256)``** — values are rendered as ``varchar(65535)``
  instead, so a long value is not truncated before it is compared.
* **``numeric`` holds 38 digits** — a range contract compares in ``double
  precision``, behind a guard that admits only numbers float8 can hold.
* **No ``COLLATE "C"``** — Redshift's default collation already orders by bytes.
* **Introspection** reads ``svv_columns``, which lists external (Spectrum) and
  late-binding view columns that ``information_schema`` leaves out.
* **No JSON columns.** ``SUPER`` would need ``json_parse`` / ``UNPIVOT`` in place of
  the whole ``jsonb`` vocabulary; it is reported as an opaque scalar instead, and
  property fields and path discovery are refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import override

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import build_aggregate_sql, coerce_aggregation
from tripl.core.adapters.postgres import (
    _SCHEMA_ROW_LIMIT,
    PostgresAdapter,
    _quote_ident,
    _validated_search_path,
    epoch_bucket_expression,
)
from tripl.models.domain_enums import MetricAggregation

#: Redshift's own type oids that psycopg's PostgreSQL registry does not know.
_REDSHIFT_TYPE_NAMES = {
    2935: "hllsketch",
    3000: "geometry",
    3001: "geography",
    4000: "super",
    6551: "varbyte",
}

#: A number float8 can hold, and nothing else. At most 150 integer and 150
#: fractional digits and a two-digit exponent keep every match between 1e-249 and
#: 1e250, inside double precision's range in both directions: Redshift RAISES on a
#: cast that overflows, and one such value would end the whole contract statement.
#: A longer literal matches no arm and is reported BAD, never raised on.
_REDSHIFT_FINITE_NUMBER_RE = (
    r"^[+-]?([0-9]{1,150}(\.[0-9]{0,150})?|\.[0-9]{1,150})([eE][+-]?[0-9]{1,2})?$"
)

_NO_JSON = (
    "Redshift data sources do not support JSON columns: a SUPER column is read as an "
    "opaque value, so its paths cannot be listed or used as properties."
)


class RedshiftAdapter(PostgresAdapter):
    """``PostgresAdapter`` for an Amazon Redshift cluster or Serverless workgroup."""

    engine_label = "Redshift"
    # Redshift reports PostgreSQL 8.0.2 for every release; nothing to check.
    min_server_version = 0
    text_type = "varchar(65535)"
    contract_number_type = "double precision"
    contract_number_re = _REDSHIFT_FINITE_NUMBER_RE
    session_settings_at_startup = False

    @override
    def _session_settings(
        self, statement_timeout_seconds: int | None, search_path: str | None
    ) -> list[tuple[str, str]]:
        settings = [("timezone", "UTC")]
        if statement_timeout_seconds is not None:
            settings.append(("statement_timeout", str(statement_timeout_seconds * 1000)))
        if search_path is not None:
            settings.append(("search_path", _validated_search_path(search_path)))
        return settings

    @override
    def _type_name(self, oid: int) -> str:
        name = _REDSHIFT_TYPE_NAMES.get(oid)
        return name if name is not None else super()._type_name(oid)

    @override
    def _schema_columns_sql(self) -> str:
        # current_schema() runs on the leader node only, so it is read on its own
        # rather than beside the catalog view.
        with self._conn.cursor() as cur:
            cur.execute("SELECT current_schema()")
            row = cur.fetchone()
        current = self._quote_string(str(row[0])) if row and row[0] is not None else "NULL"
        # Every schema named pg_* is Redshift's own (the prefix is reserved).
        return (
            "SELECT table_schema, table_name, column_name, data_type, "
            f"(table_schema = {current}) AS is_current_schema "
            "FROM svv_columns "
            "WHERE table_schema <> 'information_schema' AND left(table_schema, 3) <> 'pg_' "
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {_SCHEMA_ROW_LIMIT}"
        )

    @override
    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        return epoch_bucket_expression(
            _quote_ident(self._validate_column(time_column)), interval_code
        )

    @override
    def _quote_string(self, value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    @override
    def _count_where(self, condition: str) -> str:
        return f"count(CASE WHEN {condition} THEN 1 END)"

    @override
    def _aggregate_where(
        self, aggregation: MetricAggregation, measure_sql: str | None, condition: str
    ) -> str:
        agg = coerce_aggregation(aggregation)
        if agg is MetricAggregation.count:
            return self._count_where(condition)
        if not measure_sql:
            msg = f"Aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        return build_aggregate_sql(agg, f"CASE WHEN {condition} THEN {measure_sql} END")

    @override
    def _byte_ordered(self, expression: str) -> str:
        return expression

    # --- JSON: not supported ---------------------------------------------------

    @override
    def _json_path_expression(self, column: str, path: str) -> str:
        raise WarehouseCapabilityError(_NO_JSON)

    @override
    def _property_value_expression(self, column: str, path: str) -> str:
        raise WarehouseCapabilityError(_NO_JSON)

    @override
    def _json_paths_expression(self, column: str) -> str:
        raise WarehouseCapabilityError(_NO_JSON)

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
        # No column is ever classified as JSON here, so nothing has paths.
        return {column: {} for column in json_columns}
