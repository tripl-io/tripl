"""The Athena adapter against a fake pyathena: its connection, deadline and outbound rule.

Athena engine version 3 is Trino SQL, so the statements are ``TrinoAdapter``'s
(``test_trino_adapter.py`` pins them and the Trino conformance job executes
them). What is Athena's own — the pyathena connection, the region-only host,
the client-side deadline — is pinned here against fakes. Nothing here contacts
AWS; ``athena-value-conformance.yml`` is the live check, and it has not run
against an account yet.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from tripl.core.adapters import athena_sql
from tripl.core.adapters.athena import AthenaAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.registry import build_adapter
from tripl.core.adapters.trino import TrinoAdapter
from tripl.crypto import encrypt_value
from tripl.models.data_source import DataSource
from tripl.schemas.data_source import ConnectionSettingsError, parse_connection_settings


class StoppedError(Exception):
    """What pyathena raises for a query whose state became CANCELLED."""


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.query_id: str | None = None
        self.description: list[tuple[object, ...]] = []
        self._rows: list[tuple[object, ...]] = []
        self._stopped = threading.Event()
        self.closed = False

    def execute(self, sql: str, parameters: object = None) -> FakeCursor:
        assert parameters is None, "no parameters: pyathena must never %-format a base query"
        self._conn.sql.append(sql)
        self.query_id = f"q{len(self._conn.sql)}"
        if self._conn.hang:
            if self._stopped.wait(5):
                raise StoppedError("Query cancelled")
            raise AssertionError("the deadline never stopped the query")
        names, rows = self._conn.answers.pop(0) if self._conn.answers else ([], [])
        self.description = [
            (name, type_name, None, None, 0, 0, "NULLABLE") for name, type_name in names
        ]
        self._rows = rows
        return self

    def cancel(self) -> None:
        self._conn.cancelled.append(self.query_id)
        self._stopped.set()

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.answers: list[tuple[list[tuple[str, str]], list[tuple[object, ...]]]] = []
        self.cancelled: list[str | None] = []
        self.hang = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        return None


class FakePyathena:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}
        self.conn = FakeConnection()

    def connect(self, **kwargs: object) -> FakeConnection:
        self.kwargs = kwargs
        return self.conn


def _source(host: str = "eu-west-1", **settings: object) -> DataSource:
    return DataSource(
        name="athena",
        db_type="athena",
        host=host,
        port=443,
        database_name="analytics",
        username="AKIAEXAMPLE",
        password_encrypted=encrypt_value("secret/key"),
        timeout_seconds=120,
        extra_params=dict(settings),
    )


def _build(
    monkeypatch: pytest.MonkeyPatch, **settings: object
) -> tuple[AthenaAdapter, FakePyathena]:
    driver = FakePyathena()
    monkeypatch.setattr(athena_sql, "import_driver", lambda: driver)
    adapter = build_adapter(_source(**settings))
    assert isinstance(adapter, AthenaAdapter)
    return adapter, driver


def test_athena_is_the_trino_adapter_on_another_connection() -> None:
    assert issubclass(AthenaAdapter, TrinoAdapter)
    assert AthenaAdapter.engine_label == "Athena"


def test_factory_wires_region_workgroup_output_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, driver = _build(
        monkeypatch,
        work_group="analytics",
        s3_output_location="s3://my-bucket/athena/",
        schema_allowlist=["marts"],
    )
    kwargs = driver.kwargs
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert kwargs["aws_secret_access_key"] == "secret/key"
    assert kwargs["work_group"] == "analytics"
    assert kwargs["s3_staging_dir"] == "s3://my-bucket/athena/"
    assert kwargs["catalog_name"] == "AwsDataCatalog"
    assert kwargs["schema_name"] == "analytics"
    assert adapter._catalog == "AwsDataCatalog"
    assert adapter._schemas_in_scope() == ["analytics", "marts"]


def test_defaults_are_the_primary_workgroup_and_the_glue_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, driver = _build(monkeypatch)
    assert driver.kwargs["work_group"] == "primary"
    assert driver.kwargs["s3_staging_dir"] is None


def test_statements_are_trino_sql_without_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, driver = _build(monkeypatch)
    driver.conn.answers.append(([("ts", "timestamp"), ("n", "bigint")], []))
    driver.conn.answers.append(([], [(datetime(2026, 4, 1), 3)]))
    base = "SELECT ts, n FROM t WHERE name LIKE '%x'"
    columns = adapter.get_columns(base)
    assert [(c.name, c.type_name) for c in columns] == [("ts", "timestamp"), ("n", "bigint")]
    _, _, rows = adapter.get_time_bucketed_counts(
        base,
        "ts",
        "1d",
        [],
        [],
        None,
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )
    assert driver.conn.sql[0] == f"SELECT * FROM ({base}) AS _src LIMIT 0"
    assert "date_trunc('day', \"ts\")" in driver.conn.sql[1]
    assert "LIKE '%x'" in driver.conn.sql[1]
    assert rows == [(datetime(2026, 4, 1, tzinfo=UTC), 3)]


def test_a_query_past_its_deadline_is_stopped_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, driver = _build(monkeypatch)
    adapter._timeout_seconds = 0.2
    driver.conn.hang = True
    with pytest.raises(TimeoutError, match="Athena: query exceeded the 0.2s timeout"):
        adapter._run("SELECT 1")
    assert driver.conn.cancelled == ["q1"]


def test_other_errors_pass_through_and_stop_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, driver = _build(monkeypatch)

    def boom(_cursor: FakeCursor, sql: str, parameters: object = None) -> None:
        raise RuntimeError("TABLE_NOT_FOUND")

    monkeypatch.setattr(FakeCursor, "execute", boom)
    with pytest.raises(RuntimeError, match="TABLE_NOT_FOUND"):
        adapter._run("SELECT 1")
    assert driver.conn.cancelled == []


@pytest.mark.parametrize(
    ("host", "region", "hostname"),
    [
        ("eu-west-1", "eu-west-1", "athena.eu-west-1.amazonaws.com"),
        ("US-EAST-1", "us-east-1", "athena.us-east-1.amazonaws.com"),
        ("athena.ap-south-1.amazonaws.com", "ap-south-1", "athena.ap-south-1.amazonaws.com"),
        (
            "https://athena.us-gov-west-1.amazonaws.com/",
            "us-gov-west-1",
            "athena.us-gov-west-1.amazonaws.com",
        ),
        ("cn-north-1", "cn-north-1", "athena.cn-north-1.amazonaws.com.cn"),
    ],
)
def test_the_host_resolves_to_a_region_and_its_athena_endpoint(
    host: str, region: str, hostname: str
) -> None:
    endpoint = athena_sql.resolve_endpoint(host)
    assert (endpoint.region, endpoint.hostname) == (region, hostname)


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "athena.eu-west-1.amazonaws.com.evil.com",
        "169.254.169.254",
        "eu-west-1:443",
        "s3.eu-west-1.amazonaws.com",
        "",
    ],
)
def test_anything_but_a_region_or_its_endpoint_is_refused(host: str) -> None:
    with pytest.raises(WarehouseCapabilityError, match="AWS region"):
        athena_sql.resolve_endpoint(host)


def test_public_hosts_only_vets_the_athena_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    monkeypatch.setattr(athena_sql, "import_driver", FakePyathena)
    resolved: list[str] = []

    def fake_resolve(host: str, *_a: object, **_k: object) -> list[object]:
        resolved.append(host)
        return [(None, None, None, None, ("10.0.0.5", 443))]

    monkeypatch.setattr("tripl.services.safe_http._resolve", fake_resolve)
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        build_adapter(_source())
    assert resolved == ["athena.eu-west-1.amazonaws.com"]


def test_keys_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(athena_sql, "import_driver", FakePyathena)
    source = _source()
    source.username = ""
    with pytest.raises(WarehouseCapabilityError, match="access key ID"):
        build_adapter(source)


@pytest.mark.parametrize(
    "raw",
    [
        {"s3_output_location": "https://bucket/x"},
        {"s3_output_location": "s3://b/x' OR 1=1"},
        {"work_group": "a b"},
        {"catalog_name": 'x"y'},
        {"schema_allowlist": ["ok", "bad name"]},
        {"http_scheme": "https"},
    ],
)
def test_malformed_settings_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ConnectionSettingsError):
        parse_connection_settings("athena", raw)


def test_settings_round_trip() -> None:
    parsed = parse_connection_settings(
        "athena",
        {
            "work_group": " analytics ",
            "s3_output_location": "s3://my-bucket/results/",
            "catalog_name": "AwsDataCatalog",
            "schema_allowlist": ["a", "a", "b-c"],
        },
    )
    assert parsed is not None
    assert parsed.model_dump(exclude_none=True) == {
        "work_group": "analytics",
        "s3_output_location": "s3://my-bucket/results/",
        "catalog_name": "AwsDataCatalog",
        "schema_allowlist": ["a", "b-c"],
    }
