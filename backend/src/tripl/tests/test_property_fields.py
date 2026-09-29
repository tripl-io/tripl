"""Properties as breakdowns, drift fields and contracts (F23.6, #306).

A property is ``<json_column>.<path>``, the ``json_value_paths`` format. It is
accepted in ``metric_breakdown_columns`` and ``distribution_drift_fields``,
every adapter extracts it with its own JSON-path helper, and a typed property
(its variable's schema and values, the event property list) becomes the same
field-contract expectations a FieldDefinition does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from google.cloud import bigquery
from httpx import AsyncClient
from pydantic import ValidationError

from tripl.core.adapters.base import FieldContractExpectation, is_breakdown_field_of
from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter, SyntheticCapabilityError
from tripl.core.property_contracts import (
    MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE,
    PropertyListing,
    TypedProperty,
    property_contract_expectations,
)
from tripl.json_paths import MAX_PROPERTY_FIELDS, extract_json_path, split_property_field
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.event_type import EventType
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.schemas.scan_config import ScanConfigCreate, ScanConfigUpdate
from tripl.services.event_health_service import _property_contract_expectations
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics import schema_drift as metrics_schema_drift
from tripl.worker.tasks.metrics.metric_rows import (
    _collect_distribution_drift_rows,
    _is_supported_metric_breakdown_column,
)
from tripl.worker.tasks.metrics.property_contracts import load_typed_properties

FROM = datetime(2026, 4, 1, tzinfo=UTC)
TO = datetime(2026, 4, 2, tzinfo=UTC)


# --- the grammar and the save schema -----------------------------------------


def test_split_property_field() -> None:
    assert split_property_field("platform") is None
    assert split_property_field("props.plan") == ("props", "plan")
    assert split_property_field("props.cart.total") == ("props", "cart.total")
    for bad in ("props.", ".plan", "props..plan", "props.bad-key", "props.x'; DROP", "1a.b"):
        with pytest.raises(ValueError, match="Invalid property path"):
            split_property_field(bad)


def _create(**overrides: object) -> ScanConfigCreate:
    body: dict[str, object] = {
        "data_source_id": uuid.uuid4(),
        "name": "scan",
        "base_query": "SELECT * FROM events",
        "event_type_column": "event_type",
        "time_column": "ts",
    }
    body.update(overrides)
    return ScanConfigCreate.model_validate(body)


def test_scan_config_accepts_properties_next_to_scalar_columns() -> None:
    config = _create(
        metric_breakdown_columns=["platform", " props.plan ", "props.plan"],
        distribution_drift_fields=["props.cart.total", "country"],
    )
    assert config.metric_breakdown_columns == ["platform", "props.plan"]
    assert config.distribution_drift_fields == ["props.cart.total", "country"]


@pytest.mark.parametrize("field", ["metric_breakdown_columns", "distribution_drift_fields"])
def test_scan_config_refuses_a_malformed_property(field: str) -> None:
    with pytest.raises(ValidationError, match="Invalid property path"):
        _create(**{field: ["props.bad key"]})
    with pytest.raises(ValidationError, match="Invalid property path"):
        ScanConfigUpdate.model_validate({field: ["props.a'b"]})


@pytest.mark.parametrize("field", ["metric_breakdown_columns", "distribution_drift_fields"])
def test_scan_config_caps_properties(field: str) -> None:
    at_cap = [f"props.p{index}" for index in range(MAX_PROPERTY_FIELDS)]
    assert getattr(_create(**{field: [*at_cap, "platform"]}), field)[-1] == "platform"
    with pytest.raises(ValidationError, match=f"at most {MAX_PROPERTY_FIELDS} properties"):
        _create(**{field: [*at_cap, "props.one_more"]})


def test_breakdown_field_support() -> None:
    assert is_breakdown_field_of("platform", ["platform"], [])
    assert not is_breakdown_field_of("platform", [], ["platform"])
    assert is_breakdown_field_of("props.plan", ["platform"], ["props"])
    assert not is_breakdown_field_of("props.plan", ["props"], [])

    config = ScanConfig(event_type_column="event_type", time_column="ts")
    assert _is_supported_metric_breakdown_column(
        config, column="props.plan", regular_cols=["platform"], json_cols=["props"]
    )
    # A stored entry that predates the grammar is unsupported, not an error.
    assert not _is_supported_metric_breakdown_column(
        config, column="props.bad-key", regular_cols=[], json_cols=["props"]
    )
    assert not _is_supported_metric_breakdown_column(
        config, column="other.plan", regular_cols=[], json_cols=["props"]
    )


# --- SQL per adapter -----------------------------------------------------------


class _CHResult:
    column_names: list[str] = []
    result_rows: list[tuple[object, ...]] = []


class _CHClient:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str) -> _CHResult:
        self.sql.append(sql)
        return _CHResult()


def _clickhouse() -> tuple[ClickHouseAdapter, _CHClient]:
    client = _CHClient()
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client  # type: ignore[assignment]
    adapter._allowed_columns = {"ts", "event_type", "platform", "props", "attrs"}
    adapter._column_types = {"props": "JSON", "attrs": "Map(String, String)"}
    adapter._json_path_discovery = "dynamic"
    return adapter, client


def test_clickhouse_breaks_down_by_a_property() -> None:
    adapter, client = _clickhouse()
    for values_limit in (5, None):
        adapter.get_time_bucketed_breakdown_counts_multi(
            "SELECT * FROM events",
            "ts",
            "1h",
            ["platform", "props.cart.total", "attrs.tier"],
            ["event_type", "platform"],
            ["props", "attrs"],
            {},
            FROM,
            TO,
            values_limit=values_limit,
        )
    # The top-values pre-query, the limited statement, the unlimited one.
    top_sql, _limited, sql = client.sql
    extracted = (
        "ifNull(if(isNull(`props`.`cart`.`total`), NULL, toString(`props`.`cart`.`total`)), '')"
    )
    mapped = "ifNull(if(mapContains(`attrs`, 'tier'), toString(`attrs`['tier']), NULL), '')"
    for statement in (top_sql, sql):
        assert extracted in statement
        assert mapped in statement
        assert "ifNull(toString(`platform`), '')" in statement
    assert "'props.cart.total'" in sql  # the breakdown label is the property entry


def test_clickhouse_refuses_a_property_of_an_unread_column() -> None:
    adapter, _client = _clickhouse()
    with pytest.raises(ValueError, match="properties of a JSON column"):
        adapter.get_time_bucketed_breakdown_counts_multi(
            "SELECT * FROM events", "ts", "1h", ["props.plan"], ["platform"], [], {}, FROM, TO
        )
    with pytest.raises(ValueError, match="Unsupported JSON path"):
        adapter._property_value_expression("props", "a`b")


def test_clickhouse_contract_on_a_property() -> None:
    adapter, client = _clickhouse()
    adapter.validate_field_contracts(
        "SELECT * FROM events",
        [
            FieldContractExpectation("props.plan", "required_null_violation", 0.05),
            FieldContractExpectation("props.plan", "enum_violation", 0.0, enum_options=("pro",)),
        ],
    )
    [sql] = client.sql
    operand = "if(isNull(`props`.`plan`), NULL, toString(`props`.`plan`))"
    assert f"countIf(isNull({operand}))" in sql
    assert f"NOT isNull({operand}) AND ifNull(toString({operand}), '') NOT IN ('pro')" in sql


def test_a_property_the_engine_cannot_extract_costs_only_its_own_contract() -> None:
    adapter, client = _clickhouse()
    unreachable = FieldContractExpectation("attrs.a.b", "required_null_violation", 0.0)
    adapter.validate_field_contracts(
        "SELECT * FROM events",
        [unreachable, FieldContractExpectation("props.plan", "required_null_violation", 0.0)],
    )
    [sql] = client.sql
    assert "`props`.`plan`" in sql
    assert "attrs" not in sql
    assert adapter.take_skipped_field_contracts() == [unreachable]


class _PGCursor:
    def __init__(self, conn: _PGConn) -> None:
        self._conn = conn

    def __enter__(self) -> _PGCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _PGConn:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def cursor(self) -> _PGCursor:
        return _PGCursor(self)


def _postgres() -> tuple[PostgresAdapter, _PGConn]:
    conn = _PGConn()
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn  # type: ignore[assignment]
    adapter._allowed_columns = {"ts", "event_type", "platform", "props"}
    return adapter, conn


def test_postgres_breaks_down_by_a_property() -> None:
    adapter, conn = _postgres()
    for values_limit in (3, None):
        adapter.get_time_bucketed_breakdown_counts_multi(
            "SELECT * FROM events",
            "ts",
            "1h",
            ["props.cart.total"],
            ["event_type"],
            ["props"],
            {},
            FROM,
            TO,
            values_limit=values_limit,
        )
    top_sql, _limited, sql = conn.sql
    extracted = """COALESCE(("props" #>> ARRAY['cart', 'total']::text[]), '')"""
    assert extracted in top_sql
    assert extracted in sql
    assert "'props.cart.total'" in sql


def test_postgres_contract_on_a_property() -> None:
    adapter, conn = _postgres()
    adapter.validate_field_contracts(
        "SELECT * FROM events",
        [FieldContractExpectation("props.amount", "range_violation", 0.0, min_value=0.0)],
    )
    [sql] = conn.sql
    operand = """("props" #>> ARRAY['amount']::text[])"""
    assert f"{operand} IS NOT NULL" in sql
    assert f"COALESCE({operand}::text, '')" in sql


class _BQRow:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class _BQJob:
    def __init__(self, schema: list[object]) -> None:
        self._schema = schema

    def result(self, **_kwargs: object) -> object:
        schema = self._schema

        class _Result:
            def __init__(self) -> None:
                self.schema = schema

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter([])

        return _Result()


class _BQClient:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str) -> _BQJob:
        self.sql.append(sql)
        if sql.endswith("LIMIT 0"):
            return _BQJob(
                [
                    bigquery.SchemaField("ts", "TIMESTAMP"),
                    bigquery.SchemaField("event_type", "STRING"),
                    bigquery.SchemaField("props", "JSON"),
                ]
            )
        return _BQJob([])


def _bigquery() -> tuple[BigQueryAdapter, _BQClient]:
    client = _BQClient()
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client  # type: ignore[assignment]
    adapter._project = "proj"
    adapter._dataset = "wh"
    adapter._allowed_columns = set()
    adapter._column_types = {}
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    return adapter, client


def test_bigquery_breaks_down_by_a_property() -> None:
    adapter, client = _bigquery()
    adapter.get_time_bucketed_breakdown_counts_multi(
        "SELECT * FROM events",
        "ts",
        "1h",
        ["props.cart.total"],
        ["event_type"],
        ["props"],
        {},
        FROM,
        TO,
    )
    sql = next(statement for statement in client.sql if not statement.endswith("LIMIT 0"))
    assert "IFNULL(JSON_VALUE(`props`, '$.cart.total'), '')" in sql
    assert "'props.cart.total'" in sql


def test_bigquery_contract_on_a_property() -> None:
    adapter, client = _bigquery()
    adapter.validate_field_contracts(
        "SELECT * FROM events",
        [FieldContractExpectation("props.plan", "required_null_violation", 0.05)],
    )
    sql = next(statement for statement in client.sql if not statement.endswith("LIMIT 0"))
    # Not skipped as a column missing from the result: its column is ``props``.
    assert "COUNTIF(JSON_VALUE(`props`, '$.plan') IS NULL) AS _bad_0" in sql
    assert adapter.take_skipped_field_contracts() == []


def test_synthetic_refuses_a_property_breakdown_by_name() -> None:
    adapter = SyntheticAdapter()
    with pytest.raises(SyntheticCapabilityError, match="props.plan"):
        adapter.get_time_bucketed_breakdown_counts_multi(
            "SELECT * FROM events",
            "event_time",
            "1h",
            ["platform", "props.plan"],
            ["event_type"],
            [],
            {},
            FROM,
            TO,
        )


class _RowsAdapter(SyntheticAdapter):
    """The Python fallback of ``validate_field_contracts``, over fixed rows."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def get_preview_rows(  # type: ignore[override]
        self, base_query: str, limit: int = 10, **_kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return ["event_type", "props"], self._rows


def test_fallback_evaluates_a_property_contract_row_by_row() -> None:
    adapter = _RowsAdapter(
        [
            ("buy", '{"plan": "pro", "amount": 5}'),
            ("buy", {"plan": "gold", "amount": -1}),
            ("buy", {"amount": 3}),
            ("buy", None),
        ]
    )
    violations = adapter.validate_field_contracts(
        "SELECT * FROM events",
        [
            FieldContractExpectation("props.plan", "required_null_violation", 0.0),
            FieldContractExpectation("props.plan", "enum_violation", 0.0, enum_options=("pro",)),
            FieldContractExpectation("props.amount", "range_violation", 0.0, min_value=0.0),
        ],
    )
    summary = {(v.field_name, v.drift_type): (v.bad_count, v.total_count) for v in violations}
    assert summary == {
        ("props.plan", "required_null_violation"): (2, 4),
        ("props.plan", "enum_violation"): (1, 2),
        ("props.amount", "range_violation"): (1, 3),
    }
    assert extract_json_path('{"a": {"b": 1}}', "a.b") == 1


# --- contract derivation --------------------------------------------------------


def _prop(**overrides: object) -> TypedProperty:
    base: dict[str, object] = {
        "source": "props.plan",
        "variable_type": "string",
        "listings": (PropertyListing(required=True),),
        "type_event_count": 1,
    }
    base.update(overrides)
    return TypedProperty(**base)  # type: ignore[arg-type]


def _kinds(expectations: list[FieldContractExpectation]) -> dict[str, FieldContractExpectation]:
    return {expectation.drift_type: expectation for expectation in expectations}


def test_required_on_every_event_becomes_a_null_rate_contract() -> None:
    [required] = property_contract_expectations(
        [
            _prop(
                listings=(
                    PropertyListing(required=True, presence_threshold=0.9),
                    PropertyListing(required=True),
                ),
                type_event_count=2,
            )
        ]
    )
    assert required.field_name == "props.plan"
    assert required.drift_type == "required_null_violation"
    # The lowest threshold of the type's events: 1 - 0.9.
    assert required.threshold == pytest.approx(0.1)


def test_required_on_only_some_events_is_left_to_property_drift() -> None:
    assert property_contract_expectations([_prop(type_event_count=2)]) == []
    assert (
        property_contract_expectations(
            [
                _prop(
                    listings=(PropertyListing(required=True), PropertyListing(required=False)),
                    type_event_count=2,
                )
            ]
        )
        == []
    )


def test_documented_values_become_an_enum_with_overrides() -> None:
    expectations = _kinds(
        property_contract_expectations(
            [
                _prop(
                    allowed_values=("free", "pro"),
                    listings=(
                        PropertyListing(required=False),
                        PropertyListing(required=False, values=("trial",)),
                    ),
                    type_event_count=2,
                )
            ]
        )
    )
    assert set(expectations) == {"enum_violation"}
    assert expectations["enum_violation"].enum_options == ("free", "pro", "trial")
    assert expectations["enum_violation"].threshold == 0.0


def test_no_enum_when_an_event_documents_nothing() -> None:
    assert (
        property_contract_expectations(
            [
                _prop(
                    listings=(
                        PropertyListing(required=False, values=("pro",)),
                        PropertyListing(required=False),
                    ),
                )
            ]
        )
        == []
    )


def test_numbers_are_accepted_in_their_canonical_spelling() -> None:
    [enum] = property_contract_expectations(
        [
            _prop(
                variable_type="number",
                allowed_values=("10.0", "2.5"),
                listings=(PropertyListing(required=False),),
            )
        ]
    )
    assert enum.enum_options == ("10.0", "10", "2.5")


def test_schema_pattern_and_bounds_become_regex_and_range() -> None:
    expectations = _kinds(
        property_contract_expectations(
            [
                _prop(
                    variable_type="number",
                    json_schema={"type": "number", "minimum": 0, "maximum": 100},
                    listings=(PropertyListing(required=False),),
                ),
                _prop(
                    source="props.sku",
                    json_schema={"type": "string", "pattern": "^sku_[0-9]+$"},
                    listings=(PropertyListing(required=False),),
                ),
            ]
        )
    )
    assert expectations["range_violation"].min_value == 0.0
    assert expectations["range_violation"].max_value == 100.0
    assert expectations["regex_violation"].regex == "^sku_[0-9]+$"


def test_contracts_are_capped_per_event_type() -> None:
    props = [
        _prop(source=f"props.p{index:03d}")
        for index in range(MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE + 5)
    ]
    expectations = property_contract_expectations(props)
    assert len(expectations) == MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE
    assert expectations[-1].field_name == f"props.p{MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE - 1:03d}"


# --- loading the plan, and the worker path end to end ----------------------------


async def _seed_plan(client: AsyncClient, slug: str) -> tuple[str, str]:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    et_id = et.json()["id"]
    events = [
        (
            await client.post(
                f"/api/v1/projects/{slug}/events", json={"event_type_id": et_id, "name": name}
            )
        ).json()["id"]
        for name in ("purchase", "renewal")
    ]
    plan = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": "plan",
            "variable_type": "string",
            "allowed_values": ["free", "pro"],
            "bindings": ["props.plan"],
            "json_schema": {"type": "string", "pattern": "^[a-z]+$"},
        },
    )
    assert plan.status_code == 201, plan.text
    unbound = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "screen", "variable_type": "string"}
    )
    for event_id in events:
        for variable_id in (plan.json()["id"], unbound.json()["id"]):
            resp = await client.put(
                f"/api/v1/projects/{slug}/variables/{variable_id}/event-overrides/{event_id}",
                json={"required": True},
            )
            assert resp.status_code == 200, resp.text
    return et_id, events[0]


@pytest.mark.asyncio
async def test_the_worker_loads_and_checks_the_types_properties(client: AsyncClient) -> None:
    et_id, _ = await _seed_plan(client, "f236-worker")
    event_type_id = uuid.UUID(et_id)

    async with TestSessionLocal() as session:
        props = await session.run_sync(
            lambda sync: load_typed_properties(
                sync, event_type_id=event_type_id, json_columns={"props"}
            )
        )
        # Only the variable bound to a path of a column the run read.
        assert [prop.source for prop in props] == ["props.plan"]
        assert (
            await session.run_sync(
                lambda sync: load_typed_properties(
                    sync, event_type_id=event_type_id, json_columns={"other"}
                )
            )
            == []
        )

    adapter = _RowsAdapter([("track", {"plan": "pro"}), ("track", {"plan": "Gold"}), ("track", {})])

    def detect(sync):  # type: ignore[no-untyped-def]
        event_type = sync.get(EventType, event_type_id)
        outcome = metrics_schema_drift._detect_field_contract_violations(
            sync,
            adapter=adapter,
            event_type=event_type,
            base_query="SELECT * FROM events",
            columns=[
                metrics_schema_drift.ColumnInfo(name="event_type", type_name="String"),
                metrics_schema_drift.ColumnInfo(name="props", type_name="JSON"),
            ],
            skip_columns={"event_type"},
            scan_config_id=None,  # type: ignore[arg-type]
            time_column=None,
            time_from=FROM,
            time_to=TO,
        )
        sync.flush()
        return outcome

    async with TestSessionLocal() as session:
        outcome = await session.run_sync(detect)
        drifts = (
            (await session.execute(SchemaDrift.__table__.select())).mappings().all()  # type: ignore[attr-defined]
        )
        await session.commit()
    assert outcome.violations_detected == 3
    assert {(row["field_name"], row["drift_type"]) for row in drifts} == {
        ("props.plan", "required_null_violation"),
        ("props.plan", "enum_violation"),
        ("props.plan", "regex_violation"),
    }


@pytest.mark.asyncio
async def test_event_health_counts_property_contracts(client: AsyncClient) -> None:
    et_id, _ = await _seed_plan(client, "f236-health")
    async with TestSessionLocal() as session:
        expectations = await _property_contract_expectations(session, [uuid.UUID(et_id)])
    assert sorted(expectations[uuid.UUID(et_id)]) == [
        ("props.plan", "enum_violation"),
        ("props.plan", "regex_violation"),
        ("props.plan", "required_null_violation"),
    ]


# --- distribution drift on a property ---------------------------------------------


class _PropertyDriftAdapter:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.breakdown_columns: list[str] = []

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
        self.breakdown_columns = list(breakdown_columns)
        return [*regular_columns, *json_columns], [], list(self.rows)


def test_distribution_drift_on_a_property() -> None:
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        name="Drift",
        base_query="SELECT * FROM events",
        time_column="ts",
        event_type_column="event_type",
        distribution_drift_fields=["props.plan", "props.bad-key", "other.plan"],
        baseline_window_buckets=2,
        min_history_buckets=2,
    )
    base = datetime(2026, 1, 1)

    def row(hour: int, value: str, count: int) -> tuple[object, ...]:
        # (bucket, field, value, is_other, event_type, props shape, count)
        return (base + timedelta(hours=hour), "props.plan", value, 0, "buy", ["plan"], count)

    rows = [
        row(8, "free", 90),
        row(8, "pro", 10),
        row(9, "free", 90),
        row(9, "pro", 10),
        row(10, "free", 10),
        row(10, "pro", 90),
    ]
    adapter = _PropertyDriftAdapter(rows)
    output, significant, truncated = _collect_distribution_drift_rows(
        adapter=adapter,  # type: ignore[arg-type]
        config=config,
        interval_code="1h",
        interval_delta=timedelta(hours=1),
        regular_cols=["event_type"],
        json_cols=["props"],
        json_value_path_map={},
        time_from=base + timedelta(hours=10),
        time_to=base + timedelta(hours=11),
        query_row_limit=1000,
        reg_index={"event_type": 0},
        et_by_name={"buy": EventType(id=uuid.uuid4(), name="buy", display_name="Buy")},
    )
    # The malformed entry and the property of a column the scan did not read
    # are skipped; the property of ``props`` is collected.
    assert adapter.breakdown_columns == ["props.plan"]
    assert not truncated
    assert {row["field_name"] for row in output} == {"props.plan"}
    assert significant >= 1
    assert all(row["band"] == DistributionDriftBand.significant.value for row in output)
