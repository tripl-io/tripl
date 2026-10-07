import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tripl.core.adapters.measure_validator import SqlDialect
from tripl.models.fact_table import FactTable
from tripl.schemas.fact_table import FactTableRowFilter, FactTableUpdate
from tripl.schemas.metric_definition import FactCondition, FactMetricDefinition, FactOperand
from tripl.services.metric_definition_service import _validate_fact_condition_type
from tripl.services.metric_preview_service import (
    MAX_GENERATED_AGGREGATE_SPECS,
    MAX_GENERATED_SQL_CHARS,
    MAX_GENERATED_SQL_QUERIES,
    MAX_SAVED_FACT_FILTERS,
    _consume_generated_sql_size,
    _ensure_generated_sql_compile_budget,
    _ensure_generated_sql_query_capacity,
    _ensure_saved_fact_filter_input_budget,
)
from tripl.worker.tasks.metrics._fact_conditions import _resolve_condition_fragment


@pytest.mark.parametrize(
    ("value", "operator"),
    [
        (3, "gt"),
        (3.5, "lte"),
        ("-3.5e2", "eq"),
        ([1, "2.5"], "in"),
        ("1, 2.5", "not_in"),
    ],
)
def test_numeric_condition_accepts_native_and_legacy_numeric_literals(
    value: object, operator: str
) -> None:
    condition = FactCondition.model_validate(
        {"column": "amount", "operator": operator, "value": value}
    )

    _validate_fact_condition_type(condition, column_type="number", role="single")


@pytest.mark.parametrize("value", ["many", "true", True, [1, "many"]])
def test_numeric_condition_rejects_non_numeric_values(value: object) -> None:
    operator = "in" if isinstance(value, list) else "gt"
    condition = FactCondition.model_validate(
        {"column": "amount", "operator": operator, "value": value}
    )

    with pytest.raises(HTTPException, match="requires numeric") as error:
        _validate_fact_condition_type(condition, column_type="number", role="single")

    assert error.value.status_code == 422


@pytest.mark.parametrize("value", [True, False, "true", " FALSE "])
def test_boolean_condition_accepts_boolean_values(value: object) -> None:
    condition = FactCondition.model_validate(
        {"column": "is_active", "operator": "eq", "value": value}
    )

    _validate_fact_condition_type(condition, column_type="bool", role="single")


def test_string_condition_keeps_numeric_value_as_text() -> None:
    condition = FactCondition(column="external_id", operator="eq", value=123)

    _validate_fact_condition_type(condition, column_type="string", role="single")


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        # PostgreSQL leaves backslashes alone in a literal
        # (standard_conforming_strings), so the escape reaches LIKE as written.
        (SqlDialect.postgres, "'%100\\%%'"),
        # ClickHouse and BigQuery consume one backslash level when parsing the
        # literal, so the quoter doubles it — both engines still see `\%`.
        (SqlDialect.clickhouse, "'%100\\\\%%'"),
        (SqlDialect.bigquery, "'%100\\\\%%'"),
        (SqlDialect.databricks, "'%100\\\\%%'"),
        # Snowflake reads the literal like Databricks, but its LIKE has no default
        # escape character, so the backslash is named.
        (SqlDialect.snowflake, "'%100\\\\%%' ESCAPE '\\\\'"),
    ],
)
def test_contains_escapes_like_wildcards(dialect: SqlDialect, expected: str) -> None:
    """`contains "100%"` must not match everything starting with 100."""
    condition = FactCondition(column="plan", operator="contains", value="100%")

    sql = _resolve_condition_fragment(condition, dialect=dialect)

    assert sql.endswith(f"LIKE {expected}"), sql


def test_contains_escapes_single_character_wildcard_and_the_escape_itself() -> None:
    underscore = _resolve_condition_fragment(
        FactCondition(column="plan", operator="contains", value="a_b"),
        dialect=SqlDialect.postgres,
    )
    assert underscore.endswith("LIKE '%a\\_b%'"), underscore

    # A literal backslash is doubled FIRST, so it cannot escape the escape.
    backslash = _resolve_condition_fragment(
        FactCondition(column="plan", operator="contains", value="a\\b"),
        dialect=SqlDialect.postgres,
    )
    assert backslash.endswith("LIKE '%a\\\\b%'"), backslash


@pytest.mark.parametrize("operator", ["like", "not_like"])
def test_like_keeps_the_users_wildcards(operator: str) -> None:
    """`like` is the operator where the pattern IS the point — never escaped."""
    condition = FactCondition(column="plan", operator=operator, value="pro_%")

    sql = _resolve_condition_fragment(condition, dialect=SqlDialect.postgres)

    assert sql.endswith("'pro_%'"), sql


@pytest.mark.parametrize(
    ("column_type", "operator"),
    [
        ("number", "contains"),
        ("timestamp", "like"),
        ("string", "is_true"),
        ("bool", "gt"),
    ],
)
def test_condition_operator_must_match_column_type(column_type: str, operator: str) -> None:
    payload: dict[str, object] = {"column": "value", "operator": operator}
    if operator not in {"is_true", "is_false"}:
        payload["value"] = "1"
    condition = FactCondition.model_validate(payload)

    with pytest.raises(HTTPException, match="not compatible|only compatible") as error:
        _validate_fact_condition_type(condition, column_type=column_type, role="single")

    assert error.value.status_code == 422


def test_fact_condition_rejects_oversized_string_value() -> None:
    with pytest.raises(ValidationError, match="at most 4096 characters"):
        FactCondition.model_validate({"column": "name", "operator": "eq", "value": "x" * 4097})

    with pytest.raises(ValidationError, match="at most 100 items"):
        FactCondition.model_validate({"column": "name", "operator": "in", "value": ["x"] * 101})


def test_fact_operand_caps_filter_and_condition_counts() -> None:
    base = {"fact_table_id": uuid.uuid4(), "aggregation": "count"}

    with pytest.raises(ValidationError, match="at most 32768 characters"):
        FactOperand.model_validate({**base, "filter_sql": "x" * 32769})

    condition = {"column": "amount", "operator": "gt", "value": 1}
    with pytest.raises(ValidationError, match="at most 100 items"):
        FactOperand.model_validate({**base, "conditions": [condition] * 101})

    single = {
        "kind": "fact",
        "interval": "1d",
        "fact_table_id": uuid.uuid4(),
        "aggregation": "count",
    }
    with pytest.raises(ValidationError, match="at most 32768 characters"):
        FactMetricDefinition.model_validate({**single, "filter_sql": "x" * 32769})


def test_fact_table_named_filters_are_bounded_before_sql_assembly() -> None:
    with pytest.raises(ValidationError, match="at most 32768 characters"):
        FactTableRowFilter(name="large", sql="x" * 32769)

    row_filter = {"name": "active", "sql": "is_active = true"}
    with pytest.raises(ValidationError, match="at most 100 items"):
        FactTableUpdate.model_validate({"row_filters": [row_filter] * 101})

    legacy_table = FactTable(row_filters=[{"name": "large", "sql": "x" * 32769}])
    with pytest.raises(ValueError, match="Fact table filter SQL exceeds"):
        _ensure_saved_fact_filter_input_budget(
            legacy_table,
            operand_filter_sql=None,
            named_filter_count=0,
            condition_count=0,
        )

    with pytest.raises(ValueError, match="Fact metric filters exceed"):
        _ensure_saved_fact_filter_input_budget(
            FactTable(row_filters=[row_filter]),
            operand_filter_sql=None,
            named_filter_count=MAX_SAVED_FACT_FILTERS + 1,
            condition_count=0,
        )


def test_generated_sql_preview_caps_statement_count_and_payload_size() -> None:
    _ensure_generated_sql_query_capacity(MAX_GENERATED_SQL_QUERIES - 1)
    assert _consume_generated_sql_size(10, "sql") == 13

    with pytest.raises(HTTPException, match="statement preview limit") as query_error:
        _ensure_generated_sql_query_capacity(MAX_GENERATED_SQL_QUERIES)
    assert query_error.value.status_code == 422

    with pytest.raises(HTTPException, match="character preview limit") as size_error:
        _consume_generated_sql_size(MAX_GENERATED_SQL_CHARS, "x")
    assert size_error.value.status_code == 422

    with pytest.raises(HTTPException, match="aggregate preview limit") as spec_error:
        _ensure_generated_sql_compile_budget(
            current_chars=0,
            base_query="SELECT 1",
            filter_sqls=[None] * (MAX_GENERATED_AGGREGATE_SPECS + 1),
        )
    assert spec_error.value.status_code == 422

    with pytest.raises(HTTPException, match="inputs exceed") as input_error:
        _ensure_generated_sql_compile_budget(
            current_chars=0,
            base_query="x" * (MAX_GENERATED_SQL_CHARS + 1),
            filter_sqls=[],
        )
    assert input_error.value.status_code == 422
