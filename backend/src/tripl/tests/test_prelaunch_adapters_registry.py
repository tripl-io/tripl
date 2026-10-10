"""The registry names each type's adapter class; the JSON-string engines are listed once.

Compiling a metric's batch statement without a warehouse used to prime a
connection-free adapter in one ``if``/``elif`` branch per engine, and the
engines that can parse a text column as JSON were spelled out again in every
message about it. The registry now names each type's adapter class (imported
only when asked for), ``BaseAdapter.primed`` builds the connection-free
instance, and every message renders one list. The registry's two host checks
share one body as well.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from tripl.config import settings
from tripl.core.adapters import registry
from tripl.core.adapters.athena import AthenaAdapter
from tripl.core.adapters.base import AggregateSpec, BaseAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.multi_aggregate_sql import compile_time_bucketed_multi_aggregate_sql
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.redshift import RedshiftAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.adapters.trino import TrinoAdapter
from tripl.core.json_string_columns import (
    JSON_STRING_DB_TYPES,
    JSON_STRING_ENGINE_NAMES,
    JSON_STRING_UNSUPPORTED,
    check_json_string_db_type,
    resolve_json_string_source,
)
from tripl.models.domain_enums import MetricAggregation

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 2, tzinfo=UTC)
_BASE = "SELECT ts, amount FROM events"

#: A time and a measure column per SQL engine, typed the way its driver reports them.
_COLUMN_TYPES: dict[str, dict[str, str]] = {
    "clickhouse": {"ts": "DateTime64(3, 'UTC')", "amount": "Float64"},
    "postgres": {"ts": "timestamptz", "amount": "float8"},
    "greenplum": {"ts": "timestamptz", "amount": "float8"},
    "redshift": {"ts": "timestamptz", "amount": "float8"},
    "bigquery": {"ts": "TIMESTAMP", "amount": "FLOAT64"},
    "databricks": {"ts": "timestamp_ntz", "amount": "double"},
    "snowflake": {"ts": "TIMESTAMP_NTZ", "amount": "FLOAT"},
    "trino": {"ts": "timestamp(3) with time zone", "amount": "double"},
    "athena": {"ts": "timestamp(3) with time zone", "amount": "double"},
}


def _compile(db_type: str, column_types: dict[str, str]) -> tuple[list[str], str]:
    return compile_time_bucketed_multi_aggregate_sql(
        db_type=db_type,
        base_query=_BASE,
        time_column="ts",
        interval="1d",
        specs=[
            AggregateSpec(
                key="s", aggregation=MetricAggregation.sum, column="amount", filter_sql="amount > 0"
            )
        ],
        time_from=_FROM,
        time_to=_TO,
        column_types=column_types,
    )


# --------------------------------------------------------------------------- #
# the adapter class of each type
# --------------------------------------------------------------------------- #


def test_every_registered_type_names_its_adapter_class() -> None:
    for db_type in registry.supported_db_types():
        assert issubclass(registry.adapter_class(db_type), BaseAdapter), db_type
    assert registry.adapter_class("postgres") is PostgresAdapter
    assert registry.adapter_class("redshift") is RedshiftAdapter
    assert registry.adapter_class("trino") is TrinoAdapter
    assert registry.adapter_class("athena") is AthenaAdapter
    assert registry.adapter_class("synthetic") is SyntheticAdapter


def test_an_unknown_type_has_no_adapter_class() -> None:
    with pytest.raises(ValueError, match="Unsupported db_type: oracle"):
        registry.adapter_class("oracle")


def test_naming_the_classes_loads_no_warehouse_driver() -> None:
    """The registry, the SQL compiler and the JSON-string gate import no driver.

    An adapter module imports its driver; the class map holds module paths, so a
    process that builds no adapter (the API, importing the scan schemas) still
    loads none. Run in a fresh interpreter: this one has loaded every driver.
    """
    drivers = ("clickhouse_connect", "google.cloud.bigquery", "trino", "pyathena")
    code = (
        "import sys\n"
        "import tripl.core.adapters.registry, tripl.core.adapters.multi_aggregate_sql\n"
        "import tripl.core.json_string_columns, tripl.schemas.scan_config\n"
        f"loaded = [name for name in {drivers!r} if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# compiling without a warehouse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("db_type", sorted(_COLUMN_TYPES))
def test_the_compiler_primes_the_class_the_source_is_built_with(
    db_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cls = registry.adapter_class(db_type)
    original: Callable[..., Any] = cls.build_time_bucketed_multi_aggregate_sql
    seen: list[tuple[type[BaseAdapter], set[str], dict[str, str]]] = []

    def capture(self: BaseAdapter, *args: Any, **kwargs: Any) -> Any:
        seen.append((type(self), set(self._allowed_columns), dict(self._column_types)))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "build_time_bucketed_multi_aggregate_sql", capture)
    column_types = _COLUMN_TYPES[db_type]
    names, sql = _compile(db_type, column_types)
    assert seen == [(cls, set(column_types), column_types)]
    assert names == ["bucket", "s"]
    assert sql


def test_each_libpq_type_compiles_its_own_dialect() -> None:
    """Greenplum keeps PostgreSQL's FILTER clause; Redshift has none and spells CASE."""
    _, greenplum = _compile("greenplum", _COLUMN_TYPES["greenplum"])
    _, redshift = _compile("redshift", _COLUMN_TYPES["redshift"])
    assert "FILTER (WHERE amount > 0)" in greenplum
    assert "FILTER" not in redshift
    assert "CASE WHEN amount > 0" in redshift


def test_a_measure_outside_the_stored_columns_is_refused() -> None:
    with pytest.raises(ValueError):
        _compile("trino", {"ts": "timestamp(3) with time zone"})


@pytest.mark.parametrize("db_type", ["synthetic", "oracle"])
def test_a_type_without_warehouse_sql_is_a_value_error(db_type: str) -> None:
    with pytest.raises(ValueError, match="Generated batch SQL is unavailable"):
        _compile(db_type, {"ts": "timestamp", "amount": "double"})


def test_primed_holds_identifiers_to_the_columns_and_runs_no_init() -> None:
    adapter = TrinoAdapter.primed({"ts": "date"})
    assert type(adapter) is TrinoAdapter
    assert adapter._allowed_columns == {"ts"}
    assert adapter._column_types == {"ts": "date"}
    assert not hasattr(adapter, "_conn")
    assert adapter._validate_column("ts") == "ts"
    with pytest.raises(ValueError):
        adapter._validate_column("amount")


# --------------------------------------------------------------------------- #
# the engines that parse a text column as JSON
# --------------------------------------------------------------------------- #


def test_the_json_string_set_matches_the_adapter_flags() -> None:
    flagged = {
        db_type
        for db_type in registry.supported_db_types()
        if registry.adapter_class(db_type).supports_json_string_columns
    }
    assert flagged == JSON_STRING_DB_TYPES


def test_every_json_string_message_names_the_same_engines() -> None:
    assert JSON_STRING_ENGINE_NAMES == (
        "ClickHouse, BigQuery, Databricks, Snowflake, Trino and Athena"
    )
    with pytest.raises(ValueError, match=JSON_STRING_ENGINE_NAMES):
        check_json_string_db_type("postgres", ["props"])
    with pytest.raises(WarehouseCapabilityError) as base_refusal:
        PostgresAdapter.primed({"props": "text"}).json_string_source("SELECT 1", ["props"])
    assert str(base_refusal.value) == JSON_STRING_UNSUPPORTED
    with pytest.raises(WarehouseCapabilityError) as gate_refusal:
        resolve_json_string_source(SyntheticAdapter(), "SELECT 1", ["props"])
    assert str(gate_refusal.value).startswith(JSON_STRING_UNSUPPORTED)
    assert "Parse as JSON" in str(gate_refusal.value)


# --------------------------------------------------------------------------- #
# the outbound host check
# --------------------------------------------------------------------------- #


def test_one_check_vets_a_configured_host_and_a_derived_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tripl.services.safe_http as safe_http

    monkeypatch.setattr(settings, "deployment_mode", "self_hosted")
    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    answers = {
        "warehouse.example.com": "8.8.4.4",
        "athena.eu-west-1.amazonaws.com": "10.0.0.5",
    }

    def fake_resolve(host: str, port: int, *_a: object, **_k: object) -> list[object]:
        return [(0, 0, 0, "", (answers[host], port))]

    monkeypatch.setattr(safe_http, "_resolve", fake_resolve)
    assert registry._public_or_refuse("warehouse.example.com", 443) == "8.8.4.4"
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        registry._public_or_refuse("athena.eu-west-1.amazonaws.com", 443)

    monkeypatch.setattr(settings, "outbound_public_hosts_only", False)
    assert registry._public_or_refuse("athena.eu-west-1.amazonaws.com", 443) is None
