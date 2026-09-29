"""Opt-in: parse String columns as JSON (F23.9, #306).

A scan config lists String (ClickHouse) / STRING (BigQuery) columns in
``json_string_columns``; every warehouse read of the scan's source runs on the
adapter's ``json_string_source`` wrapper, in which those columns are JSON. These
pin the save-time validation, the SQL each adapter emits for the wrapper and on
top of it (discovery, sampling, the breakdown, property extraction), the shared
resolver, and a scan, a dry run and a preview reading a text column as JSON,
malformed rows included.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from google.cloud import bigquery
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.core.adapters.base import ColumnInfo, FieldContractExpectation
from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.json_string_columns import (
    MAX_JSON_STRING_COLUMNS,
    resolve_json_string_source,
    scan_source_query,
)
from tripl.core.warehouse_types import is_string_type
from tripl.models import Base, DataSource, Project, ScanConfig, ScanJob
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.scan_config import (
    ScanConfigCreate,
    ScanConfigPreviewRequest,
    ScanConfigUpdate,
    ScanDryRunRequest,
)
from tripl.worker.tasks import scan as scan_tasks
from tripl.worker.tasks import scan_dry_run as dry_run_tasks
from tripl.worker.tasks._errors import ScanError
from tripl.worker.utils.scan_preset import preset_scan_columns

FROM = datetime(2026, 4, 1, tzinfo=UTC)
TO = datetime(2026, 4, 2, tzinfo=UTC)

CH_PARSE = (
    "CAST(if(isValidJSON(ifNull(toString(`props`), '')) AND "
    "JSONType(ifNull(toString(`props`), '')) = 'Object', "
    "ifNull(toString(`props`), ''), '{}'), 'JSON') AS `props`"
)
BQ_PARSE = (
    "IF(JSON_TYPE(SAFE.PARSE_JSON(`props`, wide_number_mode => 'round')) = 'object', "
    "SAFE.PARSE_JSON(`props`, wide_number_mode => 'round'), NULL) AS `props`"
)


# --- the save schema -------------------------------------------------------------


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


def test_schema_trims_and_dedupes_the_columns() -> None:
    config = _create(json_string_columns=[" props ", "props", "", "extra"])
    assert config.json_string_columns == ["props", "extra"]
    assert _create().json_string_columns == []


@pytest.mark.parametrize("bad", ["props.x", "1props", "pro ps", "p`s", "x" * 256])
def test_schema_refuses_a_name_that_is_not_a_plain_column(bad: str) -> None:
    with pytest.raises(ValidationError, match="not a plain column name"):
        _create(json_string_columns=[bad])
    with pytest.raises(ValidationError, match="not a plain column name"):
        ScanConfigUpdate.model_validate({"json_string_columns": [bad]})
    with pytest.raises(ValidationError, match="not a plain column name"):
        ScanConfigPreviewRequest.model_validate(
            {
                "data_source_id": str(uuid.uuid4()),
                "base_query": "SELECT 1",
                "json_string_columns": [bad],
            }
        )


def test_schema_caps_the_columns() -> None:
    at_cap = [f"c{index}" for index in range(MAX_JSON_STRING_COLUMNS)]
    assert _create(json_string_columns=at_cap).json_string_columns == at_cap
    with pytest.raises(ValidationError, match=f"at most {MAX_JSON_STRING_COLUMNS}"):
        _create(json_string_columns=[*at_cap, "one_more"])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"json_string_columns": ["event_type"]}, "cannot be the event_type_column"),
        ({"json_string_columns": ["ts"]}, "cannot be the time_column"),
        ({"json_string_columns": ["v"], "app_version_column": "v"}, "app_version_column"),
        ({"json_string_columns": ["p"], "platform_column": "p"}, "platform_column"),
        (
            {"json_string_columns": ["props"], "metric_breakdown_columns": ["props"]},
            "props.<key>",
        ),
        (
            {"json_string_columns": ["props"], "distribution_drift_fields": ["props"]},
            "distribution_drift_fields cannot list 'props'",
        ),
    ],
)
def test_schema_refuses_a_parsed_column_another_setting_reads_as_text(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _create(**overrides)


def test_schema_accepts_properties_of_a_parsed_column_as_breakdowns() -> None:
    config = _create(
        json_string_columns=["props"],
        metric_breakdown_columns=["props.plan"],
        distribution_drift_fields=["props.cart.total"],
    )
    assert config.metric_breakdown_columns == ["props.plan"]


def test_schema_preset_properties_column_may_be_parsed_but_not_the_event_column() -> None:
    preset = {
        "event_type_column": None,
        "setup_preset": "event_properties",
        "event_name_column": "event",
        "properties_column": "props",
    }
    assert _create(**preset, json_string_columns=["props"]).json_string_columns == ["props"]
    with pytest.raises(ValidationError, match="cannot be the event_name_column"):
        _create(**preset, json_string_columns=["event"])
    with pytest.raises(ValidationError, match="cannot be the event_name_column"):
        ScanDryRunRequest.model_validate(
            {
                "data_source_id": str(uuid.uuid4()),
                "base_query": "SELECT 1",
                **preset,
                "json_string_columns": ["event"],
            }
        )


def test_string_type_classifier() -> None:
    for name in ("String", "Nullable(String)", "LowCardinality(Nullable(String))", "STRING"):
        assert is_string_type(name)
    assert is_string_type("FixedString(16)")
    for name in ("JSON", "Int64", "Array(String)", "Map(String, String)", "DateTime"):
        assert not is_string_type(name)


# --- the API ---------------------------------------------------------------------


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects", json={"name": "Text JSON", "slug": "text-json", "description": ""}
    )
    assert resp.status_code == 201
    return resp.json()


async def _data_source(client: AsyncClient, db_type: str) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"{db_type} source",
            "db_type": db_type,
            "host": "localhost",
            "port": 8123 if db_type == "clickhouse" else 5432,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _body(data_source: dict, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "data_source_id": data_source["id"],
        "name": "Text events",
        "base_query": "SELECT * FROM analytics.events",
        "event_type_column": "event_type",
        "time_column": "ts",
        "json_string_columns": ["props"],
    }
    body.update(overrides)
    return body


class TestApi:
    async def test_create_and_update_store_the_columns(
        self, client: AsyncClient, project: dict
    ) -> None:
        ds = await _data_source(client, "clickhouse")
        created = await client.post(f"/api/v1/projects/{project['slug']}/scans", json=_body(ds))
        assert created.status_code == 201, created.text
        assert created.json()["json_string_columns"] == ["props"]
        url = f"/api/v1/projects/{project['slug']}/scans/{created.json()['id']}"

        # The merged config is checked: the parsed column cannot become the time.
        clash = await client.patch(url, json={"time_column": "props"})
        assert clash.status_code == 422
        assert "cannot be the time_column" in clash.text
        breakdown = await client.patch(url, json={"metric_breakdown_columns": ["props"]})
        assert breakdown.status_code == 422

        cleared = await client.patch(url, json={"json_string_columns": []})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["json_string_columns"] == []
        null = await client.patch(url, json={"json_string_columns": None})
        assert null.status_code == 422

    async def test_postgres_refuses_the_opt_in(self, client: AsyncClient, project: dict) -> None:
        ds = await _data_source(client, "postgres")
        resp = await client.post(f"/api/v1/projects/{project['slug']}/scans", json=_body(ds))
        assert resp.status_code == 422
        assert "ClickHouse and BigQuery data sources only" in resp.text

        plain = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json=_body(ds, json_string_columns=[]),
        )
        assert plain.status_code == 201, plain.text
        patched = await client.patch(
            f"/api/v1/projects/{project['slug']}/scans/{plain.json()['id']}",
            json={"json_string_columns": ["props"]},
        )
        assert patched.status_code == 422

        preview = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/preview",
            json={
                "data_source_id": ds["id"],
                "base_query": "SELECT 1",
                "json_string_columns": ["props"],
            },
        )
        assert preview.status_code == 422

    async def test_preview_and_dry_run_jobs_carry_the_columns(
        self, client: AsyncClient, project: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scan_tasks.preview_scan_config_async, "delay", lambda *a: None)
        monkeypatch.setattr(dry_run_tasks.dry_run_scan_config_async, "delay", lambda *a: None)
        ds = await _data_source(client, "clickhouse")
        preview = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/preview",
            json={
                "data_source_id": ds["id"],
                "base_query": "SELECT * FROM events",
                "json_string_columns": ["props"],
            },
        )
        assert preview.status_code == 202, preview.text
        dry_run = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": ds["id"],
                "base_query": "SELECT * FROM events",
                "event_type_column": "event_type",
                "json_string_columns": ["props"],
            },
        )
        assert dry_run.status_code == 202, dry_run.text
        from tripl.tests.conftest import TestSessionLocal

        async with TestSessionLocal() as session:
            preview_job = await session.get(ScanPreviewJob, uuid.UUID(preview.json()["id"]))
            dry_run_job = await session.get(ScanDryRunJob, uuid.UUID(dry_run.json()["id"]))
            assert preview_job is not None and preview_job.json_string_columns == ["props"]
            assert dry_run_job is not None and dry_run_job.json_string_columns == ["props"]


# --- ClickHouse SQL ----------------------------------------------------------------


@dataclass
class _CHType:
    name: str


@dataclass
class _CHResult:
    column_names: list[str] = field(default_factory=list)
    column_types: list[_CHType] = field(default_factory=list)
    result_rows: list[tuple[object, ...]] = field(default_factory=list)


class _CHClient:
    """Answers ``LIMIT 0`` introspection: ``props`` is String, or JSON once parsed."""

    def __init__(self, discovered: list[str] | None = None) -> None:
        self.sql: list[str] = []
        self.discovered = discovered or []

    def query(self, sql: str) -> _CHResult:
        self.sql.append(sql)
        if sql.endswith("LIMIT 0"):
            props = "JSON" if "_json_src" in sql else "Nullable(String)"
            return _CHResult(
                ["ts", "event_type", "props", "n"],
                [_CHType("DateTime"), _CHType("String"), _CHType(props), _CHType("Int64")],
            )
        if "SELECT _path" in sql:
            return _CHResult(["_path"], [], [(path,) for path in self.discovered])
        return _CHResult()


def _clickhouse(discovered: list[str] | None = None) -> tuple[ClickHouseAdapter, _CHClient]:
    client = _CHClient(discovered)
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client  # type: ignore[assignment]
    adapter._allowed_columns = set()
    adapter._json_path_discovery = "dynamic"
    return adapter, client


def test_clickhouse_source_parses_only_valid_json_objects() -> None:
    adapter, _client = _clickhouse()
    source = adapter.json_string_source("SELECT * FROM events", ["props"])
    assert source == (f"SELECT * REPLACE ({CH_PARSE}) FROM (SELECT * FROM events) AS _json_src")
    assert adapter.json_string_source("SELECT 1", []) == "SELECT 1"
    with pytest.raises(ValueError, match="Invalid column name"):
        adapter.json_string_source("SELECT 1", ["pro`ps"])


def test_clickhouse_resolver_introspects_both_sources_once_and_caches() -> None:
    adapter, client = _clickhouse()
    source = resolve_json_string_source(adapter, "SELECT * FROM events", ["props", "n", "gone"])
    # ``n`` is Int64 and ``gone`` is not selected: neither is parsed.
    assert source.count(" AS `") == 1 and CH_PARSE in source
    assert adapter._column_types["props"] == "JSON"
    assert len(client.sql) == 2
    config = ScanConfig(
        base_query="SELECT * FROM events", json_string_columns=["props", "n", "gone"]
    )
    count = len(client.sql)
    assert scan_source_query(adapter, config) == source
    assert len(client.sql) == count  # cached


def test_clickhouse_nothing_opted_in_is_the_bare_query_and_no_statement() -> None:
    adapter, client = _clickhouse()
    config = ScanConfig(base_query="SELECT * FROM events")
    assert scan_source_query(adapter, config) == "SELECT * FROM events"
    assert client.sql == []


def test_clickhouse_discovery_and_sampling_read_the_parsed_column() -> None:
    adapter, client = _clickhouse(discovered=["plan", "cart.total"])
    source = resolve_json_string_source(adapter, "SELECT * FROM events", ["props"])
    client.sql.clear()
    adapter.get_json_path_samples(
        source, ["props"], path_limit=10, sample_limit=2, sample_row_limit=100, include_objects=True
    )
    discovery, sample = client.sql
    assert "arrayJoin(JSONDynamicPaths(`props`))" in discovery
    assert f"FROM (SELECT * FROM ({source}) AS _src LIMIT 100)" in discovery
    assert "toJSONString(`props`.`plan`)" in sample
    assert "toJSONString(`props`.`cart`.`total`)" in sample
    assert CH_PARSE in sample


def test_clickhouse_breakdown_and_property_extraction_read_the_parsed_column() -> None:
    adapter, client = _clickhouse()
    source = resolve_json_string_source(adapter, "SELECT * FROM events", ["props"])
    client.sql.clear()
    adapter.get_full_breakdown(source, ["event_type"], ["props"], {"props": ["plan"]})
    [breakdown] = client.sql
    assert "arraySort(JSONAllPaths(`props`))" in breakdown
    assert "toJSONString(`props`.`plan`) AS `props.plan`" in breakdown
    assert CH_PARSE in breakdown

    client.sql.clear()
    adapter.get_time_bucketed_breakdown_counts_multi(
        source, "ts", "1h", ["props.plan"], ["event_type"], ["props"], {}, FROM, TO
    )
    [multi] = client.sql
    assert "ifNull(if(isNull(`props`.`plan`), NULL, toString(`props`.`plan`)), '')" in multi
    assert CH_PARSE in multi

    client.sql.clear()
    adapter.validate_field_contracts(
        source, [FieldContractExpectation("props.plan", "required_null_violation", 0.1)]
    )
    [contract] = client.sql
    assert "countIf(isNull(if(isNull(`props`.`plan`), NULL" in contract
    assert CH_PARSE in contract


# --- BigQuery SQL ------------------------------------------------------------------


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

    def query(self, sql: str, **_kwargs: object) -> _BQJob:
        self.sql.append(sql)
        if sql.endswith("LIMIT 0"):
            props = "JSON" if "_json_src" in sql else "STRING"
            return _BQJob(
                [
                    bigquery.SchemaField("ts", "TIMESTAMP"),
                    bigquery.SchemaField("event_type", "STRING"),
                    bigquery.SchemaField("props", props),
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


def test_bigquery_source_uses_the_safe_parse() -> None:
    adapter, _client = _bigquery()
    assert adapter.json_string_source("SELECT * FROM t", ["props"]) == (
        f"SELECT * REPLACE ({BQ_PARSE}) FROM (SELECT * FROM t) AS _json_src"
    )
    with pytest.raises(ValueError, match="invalid column name"):
        adapter.json_string_source("SELECT 1", ["a.b"])


def test_bigquery_breakdown_and_property_extraction_read_the_parsed_column() -> None:
    adapter, client = _bigquery()
    source = resolve_json_string_source(adapter, "SELECT * FROM t", ["props"])
    assert adapter._column_types["props"] == "JSON"
    client.sql.clear()
    adapter.get_full_breakdown(source, ["event_type"], ["props"], {"props": ["plan"]})
    breakdown = next(sql for sql in client.sql if not sql.endswith("LIMIT 0"))
    assert "JSON_KEYS(`props`, " in breakdown
    assert "JSON_QUERY(`props`, '$.plan')" in breakdown
    assert BQ_PARSE in breakdown

    client.sql.clear()
    adapter.get_time_bucketed_breakdown_counts_multi(
        source, "ts", "1h", ["props.plan"], ["event_type"], ["props"], {}, FROM, TO
    )
    multi = next(sql for sql in client.sql if not sql.endswith("LIMIT 0"))
    assert "IFNULL(JSON_VALUE(`props`, '$.plan'), '')" in multi
    assert BQ_PARSE in multi


def test_engines_without_the_parse_refuse_it() -> None:
    postgres = object.__new__(PostgresAdapter)
    with pytest.raises(WarehouseCapabilityError, match="only ClickHouse and BigQuery"):
        postgres.json_string_source("SELECT 1", ["props"])
    with pytest.raises(WarehouseCapabilityError, match="Parse as JSON"):
        resolve_json_string_source(SyntheticAdapter(), "SELECT * FROM events", ["props"])


# --- a scan, a dry run and a preview over a text column -----------------------------


class _TextJsonAdapter:
    """``event`` + ``props`` stored as text; ``props`` is JSON once parsed.

    Rows follow ``get_full_breakdown``'s layout. The ``[]`` row stands for
    rows whose text was not a JSON object: the parse makes them documents with
    no keys, so they count as rows missing every key.
    """

    supports_json_string_columns = True

    def __init__(self) -> None:
        self.breakdown_sources: list[str] = []

    def test_connection(self) -> bool:
        return True

    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        return f"PARSED[{','.join(columns)}]({base_query})"

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        parsed = base_query.startswith("PARSED[props]")
        return [
            ColumnInfo(name="event", type_name="String"),
            ColumnInfo(name="props", type_name="JSON" if parsed else "Nullable(String)"),
            ColumnInfo(name="ts", type_name="DateTime"),
        ]

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
        self.breakdown_sources.append(base_query)
        assert json_columns == ["props"]
        rows: list[tuple[object, ...]] = [
            ("page_view", ["url"], 15),
            ("page_view", [], 5),
            ("signup", ["plan"], 3),
        ]
        return (["event"], ["props"], [], rows[:limit])

    def get_preview_rows(
        self, base_query: str, limit: int = 10, **_kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return (["event", "props"], [("page_view", {"url": "/a"}), ("page_view", {})])

    def close(self) -> None:
        return None


def _seed(factory, *, preset: bool, json_string_columns: list[str]) -> tuple[uuid.UUID, uuid.UUID]:
    project_id, data_source_id, config_id, job_id, event_type_id = (uuid.uuid4() for _ in range(5))
    with factory() as session:
        session.add_all(
            [
                Project(id=project_id, name="P", slug="p-text-json", description=""),
                DataSource(
                    id=data_source_id,
                    name="DS",
                    db_type="clickhouse",
                    host="localhost",
                    port=8123,
                    database_name="default",
                    username="default",
                    password_encrypted="",
                ),
                EventType(id=event_type_id, project_id=project_id, name="web", display_name="Web"),
            ]
        )
        session.flush()
        if not preset:
            # A custom config reads the columns its event type has fields for;
            # the preset ensures its own two.
            session.add_all(
                [
                    FieldDefinition(
                        event_type_id=event_type_id,
                        name=name,
                        display_name=name,
                        field_type=field_type,
                        order=order,
                    )
                    for order, (name, field_type) in enumerate(
                        (("event", "string"), ("props", "json"))
                    )
                ]
            )
        preset_fields: dict[str, object] = (
            {
                "setup_preset": "event_properties",
                "event_name_column": "event",
                "properties_column": "props",
            }
            if preset
            else {}
        )
        session.add_all(
            [
                ScanConfig(
                    id=config_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    event_type_id=event_type_id,
                    name="Text",
                    base_query="SELECT * FROM events",
                    time_column="ts",
                    event_name_format="{event}",
                    json_string_columns=json_string_columns,
                    **preset_fields,
                ),
                ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
            ]
        )
        session.commit()
    return config_id, job_id


def _run(tmp_path, monkeypatch, name: str, adapter: _TextJsonAdapter, **seed: object):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config_id, job_id = _seed(factory, **seed)  # type: ignore[arg-type]
    for key, value in (
        ("_get_sync_session", factory),
        ("_build_adapter", lambda ds: adapter),
        ("reindex_main_branch_from_worker", lambda session, project_id: None),
    ):
        monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, key, value)
    return engine, factory, config_id, job_id


class TestScanOverText:
    @pytest.mark.parametrize("preset", [False, True])
    def test_run_catalogues_the_keys_of_a_text_column(
        self, tmp_path, monkeypatch, preset: bool
    ) -> None:
        adapter = _TextJsonAdapter()
        engine, factory, config_id, job_id = _run(
            tmp_path, monkeypatch, "text.db", adapter, preset=preset, json_string_columns=["props"]
        )
        try:
            summary = scan_tasks.run_scan.run(str(config_id), str(job_id))
            assert adapter.breakdown_sources == ["PARSED[props](SELECT * FROM events)"]
            assert summary["events_created"] == 2
            with factory() as session:
                events = {event.name: event.id for event in session.scalars(select(Event))}
                assert set(events) == {"page_view", "signup"}
                variables = {
                    variable.source_name: variable.id
                    for variable in session.scalars(select(Variable))
                }
                assert {"props.url", "props.plan"} <= set(variables)
                presence = session.scalar(
                    select(VariableValue.presence_rate).where(
                        VariableValue.variable_id == variables["props.url"],
                        VariableValue.event_id == events["page_view"],
                    )
                )
                # 5 of page_view's 20 rows held no JSON object: missing the key.
                assert presence == pytest.approx(0.75)
        finally:
            engine.dispose()

    def test_preset_without_the_opt_in_says_how_to_parse_the_text(
        self, tmp_path, monkeypatch
    ) -> None:
        engine, factory, config_id, job_id = _run(
            tmp_path, monkeypatch, "raw.db", _TextJsonAdapter(), preset=True, json_string_columns=[]
        )
        try:
            with pytest.raises(ScanError, match="Parse as JSON"):
                scan_tasks.run_scan.run(str(config_id), str(job_id))
        finally:
            engine.dispose()

    def test_preset_columns_of_a_parsed_source(self) -> None:
        config = ScanConfig(
            setup_preset="event_properties",
            event_name_column="event",
            properties_column="props",
            metric_breakdown_columns=[],
            distribution_drift_fields=[],
            event_group_rules=[],
        )
        adapter = _TextJsonAdapter()
        parsed = adapter.get_columns("PARSED[props](SELECT 1)")
        assert [c.name for c in preset_scan_columns(config, parsed[:2])] == ["event", "props"]


def test_dry_run_reads_the_parsed_source(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'dry.db'}")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        project_id, data_source_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with factory() as session:
            session.add(Project(id=project_id, name="P", slug="p-dry-text", description=""))
            session.add(
                DataSource(
                    id=data_source_id,
                    name="DS",
                    db_type="clickhouse",
                    host="localhost",
                    port=8123,
                    database_name="default",
                    username="default",
                    password_encrypted="",
                )
            )
            session.flush()
            session.add(
                ScanDryRunJob(
                    id=job_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    base_query="SELECT * FROM events",
                    time_column="ts",
                    setup_preset="event_properties",
                    event_name_column="event",
                    properties_column="props",
                    event_name_format="{event}",
                    json_string_columns=["props"],
                    status="pending",
                )
            )
            session.commit()
        adapter = _TextJsonAdapter()
        with factory() as session:
            job = session.get(ScanDryRunJob, job_id)
            assert job is not None
            config = dry_run_tasks._dry_run_config(session, job)
            assert config.json_string_columns == ["props"]
            payload = dry_run_tasks.build_dry_run_payload(
                session,
                adapter,  # type: ignore[arg-type]
                config,
                sample_row_limit=5000,
            )
        assert payload["errors"] == []
        assert {event["name"] for event in payload["events"]} == {"page_view", "signup"}
        assert ("props", "json") in {(f["name"], f["type"]) for f in payload["fields"]}
        assert adapter.breakdown_sources == ["PARSED[props](SELECT * FROM events)"]
    finally:
        engine.dispose()


def test_preview_job_reads_the_parsed_source(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'preview.db'}")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        project_id, data_source_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with factory() as session:
            session.add(Project(id=project_id, name="P", slug="p-prev-text", description=""))
            session.add(
                DataSource(
                    id=data_source_id,
                    name="DS",
                    db_type="clickhouse",
                    host="localhost",
                    port=8123,
                    database_name="default",
                    username="default",
                    password_encrypted="",
                )
            )
            session.flush()
            session.add(
                ScanPreviewJob(
                    id=job_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    base_query="SELECT * FROM events",
                    json_value_paths=[],
                    row_limit=5,
                    event_name_column="event",
                    properties_column="props",
                    json_string_columns=["props"],
                    status="pending",
                )
            )
            session.commit()
        for key, value in (
            ("_get_sync_session", factory),
            ("_build_adapter", lambda ds: _TextJsonAdapter()),
        ):
            monkeypatch.setitem(scan_tasks.preview_scan_config_async.run.__globals__, key, value)

        result = scan_tasks.preview_scan_config_async.run(str(job_id))

        types = {column["name"]: column["type_name"] for column in result["columns"]}
        assert types["props"] == "JSON"
        summary = result["event_properties"]
        assert summary["error"] is None
        [page_view] = summary["events"]
        assert page_view["properties"][0]["path"] == "url"
        assert page_view["properties"][0]["presence"] == 0.5
    finally:
        engine.dispose()
