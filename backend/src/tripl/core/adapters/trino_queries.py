"""Trino field contracts and multi-column breakdowns: the widest statements.

The half of :class:`~tripl.core.adapters.trino.TrinoAdapter` that builds every
field contract of an event type in one scan, and the ``GROUPING SETS``
breakdown with its top-N pre-query, kept apart so the adapter module stays
readable. It runs statements only through the adapter's own ``_run`` /
``_timed``.

Trino does not accept an output alias in ``GROUP BY``, so every statement here
groups on columns of a subquery or by ordinal.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    FieldContractExpectation,
    FieldContractViolation,
    field_contract_verdict,
    is_breakdown_field_of,
)
from tripl.core.adapters.trino_expr import TrinoExpressions
from tripl.core.adapters.trino_sql import count_cell, quote_ident, quote_literal, truncate_sql

logger = logging.getLogger(__name__)


class TrinoQueries(TrinoExpressions):
    """Field contracts and ``GROUPING SETS`` breakdowns, over the adapter's connection."""

    if TYPE_CHECKING:
        # Supplied by TrinoAdapter and BaseAdapter; declared for the type checker
        # only, so none of them shadows the real one at runtime.
        def _run(
            self, sql: str, *, timeout_cap: float | None = None
        ) -> tuple[list[str], list[tuple[object, ...]]]: ...

        def _timed(self, label: str, sql: str) -> list[tuple[object, ...]]: ...

        def _ensure_column_types(self, base_query: str) -> None: ...

        def _decode_rows(
            self,
            rows: list[tuple[object, ...]],
            *,
            offset: int,
            reg_cols: list[str],
            json_cols: list[str],
        ) -> list[tuple[object, ...]]: ...

        def _utc_bucket_rows(self, rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]: ...

        def _fold(self, raw_expr: str, top_values: list[str]) -> tuple[str, str]: ...

        def _top_breakdown_values_multi(
            self,
            base_query: str,
            time_column: str,
            breakdown_columns: list[str],
            time_from: datetime,
            time_to: datetime,
            limit: int,
        ) -> dict[str, list[str]]: ...

        def _field_contract_is_inert(self, expectation: FieldContractExpectation) -> bool: ...

        def _contract_operand(
            self, expectation: FieldContractExpectation, render: Callable[[str], str]
        ) -> str | None: ...

        def _skip_field_contract(self, expectation: FieldContractExpectation) -> None: ...

        def contract_regex_is_compilable(self, pattern: str) -> bool: ...

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    # Overrides BaseAdapter's, which TrinoAdapter puts after this mixin.
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the engine compile the pattern, reading no table."""
        self._run(f"SELECT regexp_like('', {quote_literal(pattern)})")

    def _contract_bad_condition(self, expectation: FieldContractExpectation) -> str | None:
        if self._field_contract_is_inert(expectation):
            return None
        if self._contract_operand(expectation, self._field_operand) is None:
            return None
        regex_ok = True
        if expectation.drift_type == "regex_violation":
            assert expectation.regex is not None
            regex_ok = self.contract_regex_is_compilable(expectation.regex)
            if not regex_ok:
                self._skip_field_contract(expectation)
                return None
        return self._contract_bad_predicate(expectation, regex_ok)

    # Overrides BaseAdapter's, like _probe_contract_regex.
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
        """Count every contract warehouse-side over the FULL window, in one scan.

        The PostgreSQL shape: the aggregates of up to
        ``FIELD_CONTRACT_EXPECTATIONS_PER_QUERY`` expectations ride side by side
        over a single ``FROM (base_query)``, only the counts come back, and
        ``field_contract_verdict`` decides. ``limit`` bounds the violations
        returned, never the rows counted.
        """
        if not expectations:
            return []
        self._ensure_column_types(base_query)
        where_clause = self._contract_where_clause(
            time_column, time_from, time_to, group_column, group_value
        )
        compiled: list[tuple[FieldContractExpectation, str]] = []
        for expectation in expectations:
            source_column = expectation.field_name.split(".", 1)[0]
            if self._allowed_columns and source_column not in self._allowed_columns:
                self._skip_field_contract(expectation)
                continue
            condition = self._contract_bad_condition(expectation)
            if condition is not None:
                compiled.append((expectation, condition))
        if not compiled:
            return []

        label = self.engine_label
        violations: list[FieldContractViolation] = []
        t0 = time.monotonic()
        for start in range(0, len(compiled), FIELD_CONTRACT_EXPECTATIONS_PER_QUERY):
            chunk = compiled[start : start + FIELD_CONTRACT_EXPECTATIONS_PER_QUERY]
            selects = ", ".join(
                self._contract_aggregate_sql(expectation, condition, index=index)
                for index, (expectation, condition) in enumerate(chunk)
            )
            sql = f"SELECT {selects} FROM ({base_query}) AS _src{where_clause}"
            logger.debug("%s field contract query: %s", label, truncate_sql(sql))
            _, rows = self._run(sql)
            if not rows:
                continue
            row = rows[0]
            for index, (expectation, _condition) in enumerate(chunk):
                sample = row[index * 3 + 2]
                violation = field_contract_verdict(
                    expectation,
                    bad_count=count_cell(row[index * 3]),
                    total_count=count_cell(row[index * 3 + 1]),
                    sample_value=None if sample is None else str(sample),
                )
                if violation is not None:
                    violations.append(violation)
        logger.info(
            "%s field contracts done in %.2fs, %s violations",
            label,
            time.monotonic() - t0,
            len(violations),
        )
        return violations[: max(0, int(limit))]

    # ------------------------------------------------------------------ #
    # GROUPING SETS breakdowns
    # ------------------------------------------------------------------ #

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
        self._ensure_column_types(base_query)
        cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        window = self._time_condition(time_column, time_from, time_to)
        prepared = [
            f"{self._field_value_expression(c)} AS __bd_raw_{i}" for i, c in enumerate(cols)
        ]
        grouping_sets = ", ".join(f"(__bd_raw_{i})" for i in range(len(cols)))
        label_branches = " ".join(
            f"WHEN GROUPING(__bd_raw_{i}) = 0 THEN {quote_literal(c)}" for i, c in enumerate(cols)
        )
        value_branches = " ".join(
            f"WHEN GROUPING(__bd_raw_{i}) = 0 THEN __bd_raw_{i}" for i in range(len(cols))
        )
        sql = (
            "SELECT _breakdown_column, _breakdown_value FROM ("
            "SELECT _breakdown_column, _breakdown_value, "
            # The value tie-break the BaseAdapter top-N contract requires. Trino
            # compares varchar by code point, with no collation to set.
            "row_number() OVER (PARTITION BY _breakdown_column "
            "ORDER BY _cnt DESC, _breakdown_value) AS rn "
            "FROM ("
            f"SELECT CASE {label_branches} ELSE '' END AS _breakdown_column, "
            f"CASE {value_branches} ELSE '' END AS _breakdown_value, "
            "count(*) AS _cnt "
            f"FROM (SELECT {', '.join(prepared)} FROM ({base_query}) AS _src WHERE {window}) "
            "AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") AS _scored"
            ") AS _ranked "
            f"WHERE rn <= {int(limit)}"
        )
        logger.debug("%s breakdown top-values query: %s", self.engine_label, truncate_sql(sql))
        top: dict[str, list[str]] = {c: [] for c in cols}
        _, rows = self._run(sql)
        for column, value in rows:
            top.setdefault(str(column), []).append(str(value))
        return top

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
        if not breakdown_columns:
            return [], [], []
        self._ensure_column_types(base_query)
        tc = self._validate_column(time_column)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown_cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        invalid = [c for c in breakdown_cols if not is_breakdown_field_of(c, reg_cols, json_cols)]
        if invalid:
            msg = (
                "Breakdown columns must be scalar columns or properties of a JSON column: "
                f"{', '.join(invalid)}"
            )
            raise ValueError(msg)
        json_value_paths = json_value_paths or {}
        top_values_by_column: dict[str, list[str]] | None = None
        if values_limit is not None:
            top_values_by_column = self._top_breakdown_values_multi(
                base_query,
                time_column,
                breakdown_cols,
                time_from,
                time_to,
                max(values_limit - 1, 0),
            )

        prepared = [f"{self._bucket_expression(tc, interval)} AS _bucket"]
        prepared.extend(f"{quote_ident(c)} AS {quote_ident(c)}" for c in reg_cols)
        prepared.extend(f"{self._json_paths_expression(c)} AS {quote_ident(c)}" for c in json_cols)
        json_value_names: list[str] = []
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                prepared.append(
                    f"{self._json_path_expression(c, path)} AS {quote_ident(full_path)}"
                )
                json_value_names.append(full_path)
        grouping_columns = [quote_ident(n) for n in [*reg_cols, *json_cols, *json_value_names]]

        label_when: list[str] = []
        value_when: list[str] = []
        other_when: list[str] = []
        grouping_sets: list[str] = []
        for idx, column in enumerate(breakdown_cols):
            raw_expr = self._field_value_expression(column)
            if top_values_by_column is None:
                breakdown_expr, is_other_expr = raw_expr, "0"
            else:
                breakdown_expr, is_other_expr = self._fold(
                    raw_expr, top_values_by_column.get(column, [])
                )
            value_alias, other_alias = f"__bd_value_{idx}", f"__bd_other_{idx}"
            prepared.append(f"{breakdown_expr} AS {value_alias}")
            prepared.append(f"{is_other_expr} AS {other_alias}")
            check = f"GROUPING({value_alias}) = 0"
            label_when.append(f"WHEN {check} THEN {quote_literal(column)}")
            value_when.append(f"WHEN {check} THEN CAST({value_alias} AS varchar)")
            other_when.append(f"WHEN {check} THEN {other_alias}")
            grouping_sets.append(
                "(" + ", ".join(["_bucket", value_alias, other_alias, *grouping_columns]) + ")"
            )

        select_parts = [
            "_bucket",
            f"CASE {' '.join(label_when)} ELSE '' END AS _breakdown_column",
            f"CASE {' '.join(value_when)} ELSE '' END AS _breakdown_value",
            f"CASE {' '.join(other_when)} ELSE 0 END AS _is_other",
            *grouping_columns,
            "count(*) AS _cnt",
        ]
        window = self._time_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ("
            f"SELECT {', '.join(prepared)} FROM ({base_query}) AS _src WHERE {window}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({', '.join(grouping_sets)}) "
            f"ORDER BY _bucket, _breakdown_column, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed breakdown GROUPING SETS", sql)
        decoded = self._decode_rows(rows, offset=4, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)
