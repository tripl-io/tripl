from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.multi_aggregate_sql import compile_time_bucketed_multi_aggregate_sql
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricAggregation
from tripl.models.fact_table import FactTable
from tripl.services import metric_preview_service


@pytest.mark.parametrize(
    ("native_type", "bucket_function", "literal_function"),
    [
        ("DATETIME", "DATETIME_BUCKET", "DATETIME '"),
        ("DATE", "DATE_BUCKET", "DATE '"),
    ],
)
def test_bigquery_batch_sql_uses_native_time_type(
    native_type: str,
    bucket_function: str,
    literal_function: str,
) -> None:
    _columns, sql = compile_time_bucketed_multi_aggregate_sql(
        db_type="bigquery",
        base_query="SELECT occurred_at FROM dataset.orders",
        time_column="occurred_at",
        interval="1d",
        specs=[AggregateSpec(key="k0", aggregation=MetricAggregation.count)],
        time_from=datetime(2026, 7, 1, tzinfo=UTC),
        time_to=datetime(2026, 7, 2, tzinfo=UTC),
        column_types={"occurred_at": native_type},
    )

    assert bucket_function in sql
    assert literal_function in sql
    assert "TIMESTAMP_BUCKET" not in sql


def test_saved_databricks_native_type_is_required_for_disclosure() -> None:
    """Databricks types its window literal from the native type too (DATE,
    TIMESTAMP_NTZ, TIMESTAMP), so a fact table saved without it is refused."""
    data_source = DataSource(name="DBX", db_type="databricks")
    fact_table = FactTable(
        name="orders",
        display_name="Orders",
        sql="SELECT occurred_at FROM main.sales.orders",
        timestamp_column="occurred_at",
        columns=[{"name": "occurred_at", "type": "timestamp"}],
    )

    with pytest.raises(ValueError, match="Re-preview and save"):
        metric_preview_service._saved_fact_column_types(fact_table, data_source)

    fact_table.columns = [
        {"name": "occurred_at", "type": "timestamp", "native_type": "timestamp_ntz"}
    ]
    assert metric_preview_service._saved_fact_column_types(fact_table, data_source) == {
        "occurred_at": "timestamp_ntz"
    }


def test_saved_bigquery_native_type_is_required_for_disclosure() -> None:
    data_source = DataSource(name="BQ", db_type="bigquery")
    fact_table = FactTable(
        name="orders",
        display_name="Orders",
        sql="SELECT occurred_at FROM dataset.orders",
        timestamp_column="occurred_at",
        columns=[{"name": "occurred_at", "type": "timestamp"}],
    )

    with pytest.raises(ValueError, match="Re-preview and save"):
        metric_preview_service._saved_fact_column_types(fact_table, data_source)

    fact_table.columns = [{"name": "occurred_at", "type": "timestamp", "native_type": "DATETIME"}]
    assert metric_preview_service._saved_fact_column_types(fact_table, data_source) == {
        "occurred_at": "DATETIME"
    }
