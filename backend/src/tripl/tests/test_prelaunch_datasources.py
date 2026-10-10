"""Pre-launch fixes to the data-source connection form and the metric dry run.

* A connection setting the warehouse refuses is one sentence about that
  setting, not every warehouse model's complaint (the union used to answer a
  dotted schema name with twenty-odd errors about ClickHouse and PostgreSQL).
* The schema-name and allowlist rules are one pair of helpers and one cap.
* A source the adapter can never sign in with is refused on save: a missing
  user name, or a Trino password with the HTTP scheme.
* A metric dry run never quotes a driver's failure to connect, which names the
  warehouse's host, port and user to an editor.
"""

import re
import uuid

import pytest
from httpx import AsyncClient, Response

import tripl.core.adapters.registry as adapter_registry
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.models.data_source import DataSource, DBType
from tripl.models.scan_config import ScanConfig
from tripl.schemas.connection_settings_base import (
    MAX_SCHEMA_ALLOWLIST,
    object_name,
    object_name_list,
)
from tripl.schemas.data_source import (
    ConnectionSettingsError,
    DataSourceUpdate,
    infer_connection_settings,
    parse_connection_settings,
)
from tripl.services import datasource_service
from tripl.services.warehouse_failure import probe_failure_kind, statement_failure_kind
from tripl.tests.conftest import TestSessionLocal

_DATA_SOURCES = "/api/v1/data-sources"

# Driver text as the installed drivers raise it: psycopg's connection failures
# carry the host and port, snowflake-connector's the account host and port.
_PSYCOPG_NO_ROUTE = (
    'connection failed: connection to server at "10.0.0.5", port 5432 failed: No route to host'
)
_PSYCOPG_NO_TLS = (
    'connection failed: connection to server at "10.0.0.5", port 5432 failed: '
    "server does not support SSL, but SSL was required"
)
_SNOWFLAKE_BAD_PASSWORD = (
    "Failed to connect to DB: acct.snowflakecomputing.com:443. "
    "Incorrect username or password was specified."
)


def _names(count: int, prefix: str = "s") -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


def _source(db_type: str, **overrides: object) -> dict[str, object]:
    """A create body that saves for ``db_type``; ``overrides`` replace its keys."""
    bodies: dict[str, dict[str, object]] = {
        "trino": {"host": "trino.internal", "port": 8080, "database_name": "hive"},
        "databricks": {
            "host": "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
            "port": 443,
            "database_name": "main",
            "password": "dapi-token",
            "connection_settings": {"http_path": "/sql/1.0/warehouses/abc123"},
        },
        "snowflake": {
            "host": "myorg-myaccount",
            "port": 443,
            "database_name": "ANALYTICS",
            "password": "hunter2",
            "connection_settings": {"warehouse": "COMPUTE_WH"},
        },
        "athena": {
            "host": "eu-west-1",
            "port": 443,
            "database_name": "analytics",
            "password": "secret",
        },
        "bigquery": {
            "host": "gcp-project",
            "database_name": "analytics",
            "password": '{"type": "service_account"}',
        },
        "clickhouse": {"host": "localhost", "port": 8123, "database_name": "analytics"},
    }
    username = {"trino": "tripl", "snowflake": "TRIPL", "athena": "AKIAEXAMPLE"}
    body: dict[str, object] = {
        "name": f"ds-{uuid.uuid4()}",
        "db_type": db_type,
        "username": username.get(db_type, ""),
        **bodies[db_type],
    }
    body.update(overrides)
    return body


def _only_error(resp: Response) -> dict[str, object]:
    """The 422's one detail item."""
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1, detail
    return detail[0]


# --------------------------------------------------------------------------- #
# adp-2: one sentence for a refused connection setting
# --------------------------------------------------------------------------- #

# What the form sends for each warehouse, with the one mistake a new user makes.
_REFUSED_SETTINGS = [
    pytest.param(
        "trino",
        {"http_scheme": "https", "schema_name": "analytics.events", "schema_allowlist": None},
        "schema_name 'analytics.events'",
        id="trino-dotted-schema",
    ),
    pytest.param(
        "databricks",
        {
            "http_path": "/sql/1.0/warehouses/abc123",
            "auth_type": "pat",
            "schema_name": "main.analytics",
            "schema_allowlist": None,
        },
        "schema_name 'main.analytics'",
        id="databricks-dotted-schema",
    ),
    pytest.param(
        "bigquery",
        {
            "location": None,
            "maximum_bytes_billed": None,
            "dataset_allowlist": ["my-proj.analytics"],
        },
        "dataset_allowlist entry 'my-proj.analytics'",
        id="bigquery-dotted-dataset",
    ),
    pytest.param(
        "snowflake",
        {
            "warehouse": "COMPUTE_WH",
            "auth_type": "password",
            "role": None,
            "schema_name": None,
            "schema_allowlist": _names(MAX_SCHEMA_ALLOWLIST + 1),
        },
        f"at most {MAX_SCHEMA_ALLOWLIST} schemas",
        id="snowflake-too-many-schemas",
    ),
]


def _assert_one_sentence(error: dict[str, object], expected: str) -> None:
    assert error["loc"] == ["body", "connection_settings"]
    message = str(error["msg"])
    assert message.startswith("Invalid connection settings — ")
    assert expected in message
    # Nothing about the warehouses the request is not for, and no pydantic prefix.
    assert "Settings" not in message
    assert "Value error" not in message


@pytest.mark.parametrize(("db_type", "settings", "expected"), _REFUSED_SETTINGS)
async def test_create_refuses_a_setting_in_one_sentence(
    client: AsyncClient, db_type: str, settings: dict[str, object], expected: str
) -> None:
    resp = await client.post(_DATA_SOURCES, json=_source(db_type, connection_settings=settings))
    _assert_one_sentence(_only_error(resp), expected)


@pytest.mark.parametrize(("db_type", "settings", "expected"), _REFUSED_SETTINGS)
async def test_the_draft_test_refuses_a_setting_in_one_sentence(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_type: str,
    settings: dict[str, object],
    expected: str,
) -> None:
    probed: list[object] = []
    monkeypatch.setattr(datasource_service, "_run_adapter_test", probed.append)
    resp = await client.post(
        f"{_DATA_SOURCES}/test", json=_source(db_type, connection_settings=settings)
    )
    _assert_one_sentence(_only_error(resp), expected)
    assert probed == []


@pytest.mark.parametrize(("db_type", "settings", "expected"), _REFUSED_SETTINGS)
async def test_patch_refuses_a_setting_in_one_sentence(
    client: AsyncClient, db_type: str, settings: dict[str, object], expected: str
) -> None:
    created = await client.post(_DATA_SOURCES, json=_source(db_type))
    assert created.status_code == 201, created.text
    resp = await client.patch(
        f"{_DATA_SOURCES}/{created.json()['id']}", json={"connection_settings": settings}
    )
    _assert_one_sentence(_only_error(resp), expected)


async def test_a_setting_of_another_warehouse_names_the_one_this_source_is(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        _DATA_SOURCES, json=_source("trino", connection_settings={"work_group": "primary"})
    )
    message = str(_only_error(resp)["msg"])
    assert "work_group is not a connection setting for a trino data source" in message


def test_a_patch_without_a_type_is_read_by_the_keys_it_sends() -> None:
    # The keys the form sends fit one warehouse; the service checks the row's type after.
    update = DataSourceUpdate.model_validate({"connection_settings": {"warehouse": "WH"}})
    assert type(update.connection_settings).__name__ == "SnowflakeSettings"
    # Keys of no warehouse, or of two, are one sentence too.
    with pytest.raises(ConnectionSettingsError, match="unknown connection settings: sneaky"):
        infer_connection_settings({"sneaky": 1})
    with pytest.raises(ConnectionSettingsError, match="no one warehouse takes all of"):
        infer_connection_settings({"location": "EU", "sslmode": "require"})
    # Several fit: the closest one explains, here Trino's lone schema rule.
    with pytest.raises(ConnectionSettingsError) as excinfo:
        infer_connection_settings({"schema_allowlist": ["a.b"]})
    assert str(excinfo.value) == (
        "Invalid connection settings — schema_allowlist entry 'a.b' is not a valid name"
    )


# --------------------------------------------------------------------------- #
# adp-6: one naming rule, one cap
# --------------------------------------------------------------------------- #

_NAME = re.compile(r"^[a-z]+$")


def test_object_name_trims_skips_blank_and_names_the_setting() -> None:
    assert object_name(None, pattern=_NAME, label="schema_name", kind="name") is None
    assert object_name("  ", pattern=_NAME, label="schema_name", kind="name") is None
    assert object_name(" events ", pattern=_NAME, label="schema_name", kind="name") == "events"
    with pytest.raises(ValueError, match="^schema_name 'a.b' is not a valid schema name$"):
        object_name("a.b", pattern=_NAME, label="schema_name", kind="schema name")


def test_object_name_list_dedupes_before_the_cap() -> None:
    def check(values: list[str], too_many: str | None = None) -> list[str] | None:
        return object_name_list(
            values, pattern=_NAME, label="schema_allowlist", kind="name", limit=2, too_many=too_many
        )

    assert check(["a", " a ", "", "b", "b"]) == ["a", "b"]
    assert check(["", " "]) is None
    with pytest.raises(ValueError, match="^schema_allowlist accepts at most 2 schemas$"):
        check(["a", "b", "c"])
    with pytest.raises(ValueError, match="^two is the most$"):
        check(["a", "b", "c"], too_many="two is the most")
    with pytest.raises(ValueError, match="^schema_allowlist entry 'x1' is not a valid name$"):
        check(["x1"])


@pytest.mark.parametrize(
    ("db_type", "required"),
    [
        ("databricks", {"http_path": "/sql/1.0/warehouses/x"}),
        ("snowflake", {"warehouse": "WH"}),
        ("trino", {}),
        ("athena", {}),
    ],
)
def test_every_schema_allowlist_has_the_one_cap(db_type: str, required: dict[str, object]) -> None:
    accepted = parse_connection_settings(
        db_type, {**required, "schema_allowlist": _names(MAX_SCHEMA_ALLOWLIST)}
    )
    assert accepted is not None
    with pytest.raises(ConnectionSettingsError, match=f"at most {MAX_SCHEMA_ALLOWLIST} schemas"):
        parse_connection_settings(
            db_type, {**required, "schema_allowlist": _names(MAX_SCHEMA_ALLOWLIST + 1)}
        )


@pytest.mark.parametrize(
    ("db_type", "raw", "message"),
    [
        ("trino", {"schema_name": "a.b"}, "schema_name 'a.b' is not a valid name"),
        ("athena", {"work_group": "a b"}, "work_group 'a b' is not a valid name"),
        (
            "databricks",
            {"http_path": "/sql/1.0/warehouses/x", "schema_name": "a.b"},
            "schema_name 'a.b' is not a valid schema name",
        ),
        (
            "snowflake",
            {"warehouse": "WH", "role": "a.b"},
            "role 'a.b' is not a valid Snowflake object name",
        ),
        (
            "bigquery",
            {"dataset_allowlist": ["a.b"]},
            "dataset_allowlist entry 'a.b' is not a valid BigQuery dataset id",
        ),
    ],
)
def test_each_warehouse_keeps_its_own_words(
    db_type: str, raw: dict[str, object], message: str
) -> None:
    with pytest.raises(ConnectionSettingsError) as excinfo:
        parse_connection_settings(db_type, raw)
    assert str(excinfo.value) == f"Invalid connection settings — {message}"


# --------------------------------------------------------------------------- #
# adp-7 / adp-10: a sign-in the adapter refuses is refused on save
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("db_type", "overrides"),
    [
        ("trino", {}),
        ("snowflake", {}),
        ("athena", {}),
        (
            "databricks",
            {
                "connection_settings": {
                    "http_path": "/sql/1.0/warehouses/abc123",
                    "auth_type": "oauth_m2m",
                }
            },
        ),
    ],
)
async def test_a_missing_user_name_is_refused_under_its_field(
    client: AsyncClient, db_type: str, overrides: dict[str, object]
) -> None:
    resp = await client.post(_DATA_SOURCES, json=_source(db_type, username=" ", **overrides))
    error = _only_error(resp)
    assert error["loc"] == ["body", "username"]
    assert error["type"] == "missing"


async def test_user_names_stay_optional_where_the_adapter_reads_none(
    client: AsyncClient,
) -> None:
    for db_type in ("clickhouse", "databricks", "bigquery"):
        resp = await client.post(_DATA_SOURCES, json=_source(db_type, username=""))
        assert resp.status_code == 201, resp.text


async def test_the_draft_test_refuses_a_missing_user_name_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[object] = []
    monkeypatch.setattr(datasource_service, "_run_adapter_test", probed.append)
    resp = await client.post(f"{_DATA_SOURCES}/test", json=_source("trino", username=""))
    assert _only_error(resp)["loc"] == ["body", "username"]
    assert probed == []


async def test_a_trino_password_over_http_is_refused_under_the_password(
    client: AsyncClient,
) -> None:
    http = {"http_scheme": "http"}
    refused = await client.post(
        _DATA_SOURCES, json=_source("trino", password="hunter2", connection_settings=http)
    )
    error = _only_error(refused)
    assert error["loc"] == ["body", "password"]
    assert "HTTPS" in str(error["msg"])

    # Without a password, an unauthenticated coordinator over HTTP is fine.
    saved = await client.post(_DATA_SOURCES, json=_source("trino", connection_settings=http))
    assert saved.status_code == 201, saved.text


async def test_a_stored_trino_password_blocks_http_until_it_is_removed(
    client: AsyncClient,
) -> None:
    created = await client.post(_DATA_SOURCES, json=_source("trino", password="hunter2"))
    assert created.status_code == 201, created.text
    url = f"{_DATA_SOURCES}/{created.json()['id']}"
    http = {"connection_settings": {"http_scheme": "http"}}

    refused = await client.patch(url, json=http)
    assert _only_error(refused)["loc"] == ["body", "password"]

    # The way out the error names: remove the stored password with the switch.
    switched = await client.patch(url, json={**http, "password": ""})
    assert switched.status_code == 200, switched.text
    assert switched.json()["password_set"] is False
    assert switched.json()["connection_settings"]["http_scheme"] == "http"


async def test_clearing_a_required_user_name_is_refused(client: AsyncClient) -> None:
    created = await client.post(_DATA_SOURCES, json=_source("trino"))
    resp = await client.patch(f"{_DATA_SOURCES}/{created.json()['id']}", json={"username": ""})
    assert _only_error(resp)["loc"] == ["body", "username"]


async def test_a_rename_is_not_refused_over_a_sign_in_stored_before_the_rules(
    client: AsyncClient,
) -> None:
    created = await client.post(_DATA_SOURCES, json=_source("clickhouse"))
    ds_id = uuid.UUID(created.json()["id"])
    async with TestSessionLocal() as session:
        row = await session.get(DataSource, ds_id)
        assert row is not None
        # A Trino row saved without a user name, before save refused one.
        row.db_type = DBType.trino
        row.username = ""
        await session.commit()

    resp = await client.patch(f"{_DATA_SOURCES}/{ds_id}", json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# sec-6: a dry run never quotes a failure to connect
# --------------------------------------------------------------------------- #


def test_the_two_readers_differ_only_where_a_statement_may_have_spoken() -> None:
    for text, kind in [
        (_PSYCOPG_NO_ROUTE, "unreachable"),
        (_PSYCOPG_NO_TLS, "tls"),
        (_SNOWFLAKE_BAD_PASSWORD, "auth"),
        ('FATAL: password authentication failed for user "tripl"', "auth"),
        ("Read timed out.", "timeout"),
    ]:
        assert probe_failure_kind(RuntimeError(text)) == kind
        assert statement_failure_kind(RuntimeError(text)) == kind
    # The engine's verdict on the SQL: a probe ran none, a statement did.
    for text in ("permission denied for table orders", "Unrecognized name: author_id at [1:8]"):
        assert probe_failure_kind(RuntimeError(text)) == "auth"
        assert statement_failure_kind(RuntimeError(text)) is None


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects", json={"name": "Preview", "slug": "preview-leaks", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    """A source the project may preview against: bound to it by a scan."""
    resp = await client.post(_DATA_SOURCES, json=_source("clickhouse", name="Preview CH"))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(created["id"]),
                name="preview-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()
    return created


class _FailingAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def get_preview_rows(self, *args: object, **kwargs: object) -> object:
        raise self.error

    def close(self) -> None:
        self.closed = True


async def _preview(client: AsyncClient, project: dict, data_source: dict) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/metrics/preview",
        json={
            "data_source_id": data_source["id"],
            "sql": "SELECT t, value FROM e",
            "time_column": "t",
            "interval": "1h",
        },
    )
    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert isinstance(error, str)
    return error


_LEAKS = ("10.0.0.5", "5432", "acct.snowflakecomputing.com", "443", "db-7.internal", "5439")


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (RuntimeError(_PSYCOPG_NO_ROUTE), "Could not reach the data source"),
        (RuntimeError(_PSYCOPG_NO_TLS), "does not offer TLS"),
        (RuntimeError(_SNOWFLAKE_BAD_PASSWORD), "rejected the credentials"),
        # Text no reader knows is still never quoted when connecting failed.
        (RuntimeError("driver exploded at db-7.internal:5439"), "Could not connect"),
    ],
)
async def test_a_failure_to_connect_is_never_quoted(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected: str,
) -> None:
    def _refuse(_ds: object) -> object:
        raise raised

    monkeypatch.setattr(adapter_registry, "build_adapter", _refuse)
    error = await _preview(client, project, data_source)
    assert expected in error
    for leak in _LEAKS:
        assert leak not in error


async def test_tripls_own_sentence_from_connecting_passes_through(
    client: AsyncClient, project: dict, data_source: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(_ds: object) -> object:
        raise WarehouseCapabilityError("PostgreSQL 13 is too old for tripl.")

    monkeypatch.setattr(adapter_registry, "build_adapter", _refuse)
    assert await _preview(client, project, data_source) == "PostgreSQL 13 is too old for tripl."


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (_PSYCOPG_NO_ROUTE, "Could not reach the data source"),
        (_PSYCOPG_NO_TLS, "does not offer TLS"),
        (_SNOWFLAKE_BAD_PASSWORD, "rejected the credentials"),
    ],
)
async def test_a_connect_failure_raised_by_the_first_query_is_masked(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
    raised: str,
    expected: str,
) -> None:
    adapter = _FailingAdapter(RuntimeError(raised))
    monkeypatch.setattr(adapter_registry, "build_adapter", lambda _ds: adapter)
    error = await _preview(client, project, data_source)
    assert expected in error
    for leak in _LEAKS:
        assert leak not in error
    assert adapter.closed is True


@pytest.mark.parametrize(
    "verdict", ["permission denied for table orders", "Unrecognized name: author_id at [1:8]"]
)
async def test_the_engines_verdict_on_the_sql_still_reaches_the_editor(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
) -> None:
    adapter = _FailingAdapter(RuntimeError(verdict))
    monkeypatch.setattr(adapter_registry, "build_adapter", lambda _ds: adapter)
    assert await _preview(client, project, data_source) == verdict
