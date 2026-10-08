import json
import uuid

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

from tripl import config, crypto
from tripl.core.adapters import bigquery as bigquery_module
from tripl.core.adapters import postgres as postgres_module
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.postgres import _resolve_sslmode
from tripl.core.adapters.registry import build_adapter
from tripl.crypto import decrypt_value, encrypt_value
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.schemas.data_source import (
    DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED,
    DEFAULT_TIMEOUT_SECONDS,
)
from tripl.schemas.data_source_schema import (
    ColumnSchema,
    DataSourceSchemaResponse,
    TableSchema,
)
from tripl.services import datasource_schema_service, datasource_service
from tripl.tests.conftest import TestSessionLocal


async def _scan_titles(client: AsyncClient, slug: str, query: str) -> list[str]:
    """The scan configs a project's own search surface still returns for ``query``.

    Deliberately the HTTP search endpoint rather than a ``SearchDocument`` row
    count: the index is only worth anything if the palette and /search stop
    offering a scan that no longer exists.
    """
    resp = await client.get(f"/api/v1/projects/{slug}/search?q={query}&limit=50")
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["items"] if item["entity_type"] == "scan_config"]


class TestDataSourcesCRUD:
    async def test_create_data_source(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "My CH",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 8123,
                "database_name": "analytics",
                "username": "default",
                "password": "secret",
                "timeout_seconds": 90,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My CH"
        assert data["db_type"] == "clickhouse"
        assert data["host"] == "localhost"
        assert data["port"] == 8123
        assert data["database_name"] == "analytics"
        assert data["username"] == "default"
        assert data["password_set"] is True
        assert data["timeout_seconds"] == 90
        assert "password" not in data
        assert "password_encrypted" not in data
        # project_id is now exposed so the UI can scope Source health to the current
        # project; a workspace-wide source (created here) reports null.
        assert data["project_id"] is None
        # Unset by default — the CH adapter falls back to "dynamic" discovery.
        assert data["json_path_discovery"] is None

    async def test_json_path_discovery_round_trips(self, client: AsyncClient):
        create = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "CH discovery",
                "db_type": "clickhouse",
                "host": "localhost",
                "database_name": "analytics",
                "json_path_discovery": "all",
            },
        )
        assert create.status_code == 201
        ds_id = create.json()["id"]
        assert create.json()["json_path_discovery"] == "all"

        got = await client.get(f"/api/v1/data-sources/{ds_id}")
        assert got.json()["json_path_discovery"] == "all"

        updated = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"json_path_discovery": "dynamic"},
        )
        assert updated.json()["json_path_discovery"] == "dynamic"

        cleared = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"json_path_discovery": None},
        )
        assert cleared.json()["json_path_discovery"] is None

    async def test_create_rejects_invalid_json_path_discovery(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "CH bad discovery",
                "db_type": "clickhouse",
                "host": "localhost",
                "database_name": "analytics",
                "json_path_discovery": "everything",
            },
        )
        assert resp.status_code == 422

    async def test_list_data_sources(self, client: AsyncClient):
        await client.post(
            "/api/v1/data-sources",
            json={
                "name": "DS1",
                "db_type": "clickhouse",
                "host": "h1",
                "port": 8123,
                "database_name": "db1",
            },
        )
        await client.post(
            "/api/v1/data-sources",
            json={
                "name": "DS2",
                "db_type": "clickhouse",
                "host": "h2",
                "port": 9000,
                "database_name": "db2",
            },
        )
        resp = await client.get("/api/v1/data-sources")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_get_data_source(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "GetMe",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]
        resp = await client.get(f"/api/v1/data-sources/{ds_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "GetMe"

    async def test_update_data_source(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Old",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"name": "New", "host": "new-host", "timeout_seconds": 120},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["host"] == "new-host"
        assert resp.json()["timeout_seconds"] == 120

        clear_resp = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"timeout_seconds": None},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["timeout_seconds"] is None

    async def test_rejects_invalid_timeout(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "BadTimeout",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
                "timeout_seconds": 0,
            },
        )
        assert resp.status_code == 422

    async def test_delete_data_source(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "DeleteMe",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/data-sources/{ds_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/api/v1/data-sources/{ds_id}")
        assert resp.status_code == 404

    async def test_delete_clears_the_scans_from_every_project_that_used_the_source(
        self, client: AsyncClient
    ):
        """One delete can empty SEVERAL project indexes, and the source names none.

        A data source is workspace-global by default — ``DataSource.project_id``
        is NULL for a shared source — so the projects whose search index has to be
        refreshed can only be read off the scan configs that cascade away with it.
        Two projects scanning one warehouse is the case that makes reindexing
        ``ds.project_id`` alone (or at all) visibly wrong.

        Nothing below reindexes by hand: deleting the source is the only trigger.
        """
        for slug in ("shared-scan-a", "shared-scan-b"):
            await client.post("/api/v1/projects", json={"name": slug, "slug": slug})

        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Shared warehouse",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        ds_id = create_resp.json()["id"]

        for slug in ("shared-scan-a", "shared-scan-b"):
            scan = await client.post(
                f"/api/v1/projects/{slug}/scans",
                json={
                    "data_source_id": ds_id,
                    "name": f"Pangolinscan {slug}",
                    "base_query": "SELECT * FROM warehouse.events",
                },
            )
            assert scan.status_code == 201, scan.text
            assert await _scan_titles(client, slug, "pangolinscan") == [f"Pangolinscan {slug}"]

        resp = await client.delete(f"/api/v1/data-sources/{ds_id}")
        assert resp.status_code == 204, resp.text

        for slug in ("shared-scan-a", "shared-scan-b"):
            assert await _scan_titles(client, slug, "pangolinscan") == []

    async def test_duplicate_name_conflict(self, client: AsyncClient):
        payload = {
            "name": "Dup",
            "db_type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database_name": "d",
        }
        resp1 = await client.post("/api/v1/data-sources", json=payload)
        assert resp1.status_code == 201
        resp2 = await client.post("/api/v1/data-sources", json=payload)
        assert resp2.status_code == 409

    async def test_not_found(self, client: AsyncClient):
        resp = await client.get("/api/v1/data-sources/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestDataSourceHealth:
    async def test_test_endpoint_persists_success(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Healthy",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]
        # Initially no health.
        assert create_resp.json()["last_test_status"] is None
        assert create_resp.json()["last_test_at"] is None

        monkeypatch.setattr(
            datasource_service,
            "_run_adapter_test",
            lambda _ds: (True, "Connection successful"),
        )

        resp = await client.post(f"/api/v1/data-sources/{ds_id}/test")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "Connection successful"
        assert body["data_source"]["last_test_status"] == "success"
        assert body["data_source"]["last_test_message"] == "Connection successful"
        assert body["data_source"]["last_test_at"] is not None

        # Subsequent list / get returns the persisted status.
        get_resp = await client.get(f"/api/v1/data-sources/{ds_id}")
        assert get_resp.json()["last_test_status"] == "success"

    async def test_test_endpoint_persists_failure(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Sick",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]

        monkeypatch.setattr(
            datasource_service,
            "_run_adapter_test",
            lambda _ds: (False, "DNS lookup failed"),
        )

        resp = await client.post(f"/api/v1/data-sources/{ds_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["message"] == "DNS lookup failed"
        assert body["data_source"]["last_test_status"] == "failed"
        assert body["data_source"]["last_test_message"] == "DNS lookup failed"


class TestDataSourceSchema:
    async def test_schema_returns_tables_and_columns(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        create_resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "SchemaDS",
                "db_type": "clickhouse",
                "host": "h",
                "port": 8123,
                "database_name": "d",
            },
        )
        ds_id = create_resp.json()["id"]

        monkeypatch.setattr(
            datasource_schema_service,
            "_run_schema_introspection",
            lambda _ds: DataSourceSchemaResponse(
                tables=[
                    TableSchema(
                        name="events",
                        columns=[
                            ColumnSchema(name="id", data_type="UInt64"),
                            ColumnSchema(name="name", data_type="String"),
                        ],
                    )
                ]
            ),
        )

        resp = await client.get(f"/api/v1/data-sources/{ds_id}/schema")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "tables": [
                {
                    "name": "events",
                    "columns": [
                        {"name": "id", "data_type": "UInt64"},
                        {"name": "name", "data_type": "String"},
                    ],
                }
            ]
        }

    async def test_schema_unknown_id_returns_404(self, client: AsyncClient):
        resp = await client.get("/api/v1/data-sources/00000000-0000-0000-0000-000000000000/schema")
        assert resp.status_code == 404

    async def test_schema_requires_auth(self, anon_client: AsyncClient):
        resp = await anon_client.get(
            "/api/v1/data-sources/00000000-0000-0000-0000-000000000000/schema"
        )
        assert resp.status_code == 401


class TestSyntheticGuards:
    """Synthetic sources are demo-only: never user-created, never edited into a
    real warehouse, and always labelled as synthetic in the API."""

    async def _seed_synthetic(self, name: str) -> uuid.UUID:
        ds_id = uuid.uuid4()
        async with TestSessionLocal() as session:
            session.add(
                DataSource(
                    id=ds_id,
                    project_id=None,
                    name=name,
                    db_type="synthetic",
                    host="synthetic",
                    port=0,
                    database_name="synthetic",
                )
            )
            await session.commit()
        return ds_id

    async def test_create_rejects_synthetic_db_type(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Sneaky synthetic",
                "db_type": "synthetic",
                "host": "localhost",
                "database_name": "d",
            },
        )
        assert resp.status_code == 422

    async def test_real_source_is_labelled_not_synthetic(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Real CH",
                "db_type": "clickhouse",
                "host": "localhost",
                "database_name": "d",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["is_synthetic"] is False

    async def test_synthetic_source_is_labelled_synthetic(self, client: AsyncClient):
        ds_id = await self._seed_synthetic("Demo warehouse label")
        resp = await client.get(f"/api/v1/data-sources/{ds_id}")
        assert resp.status_code == 200
        assert resp.json()["is_synthetic"] is True
        assert resp.json()["db_type"] == "synthetic"

    async def test_synthetic_cannot_be_edited_into_real_type(self, client: AsyncClient):
        ds_id = await self._seed_synthetic("Demo warehouse type")
        resp = await client.patch(f"/api/v1/data-sources/{ds_id}", json={"db_type": "clickhouse"})
        assert resp.status_code == 422

    async def test_synthetic_cannot_be_pointed_at_real_host(self, client: AsyncClient):
        ds_id = await self._seed_synthetic("Demo warehouse host")
        resp = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"host": "real.example.com", "password": "hunter2"},
        )
        assert resp.status_code == 422

    async def test_synthetic_rename_is_allowed(self, client: AsyncClient):
        ds_id = await self._seed_synthetic("Demo warehouse rename")
        resp = await client.patch(
            f"/api/v1/data-sources/{ds_id}", json={"name": "Demo warehouse renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Demo warehouse renamed"
        assert resp.json()["is_synthetic"] is True

    async def test_real_source_cannot_be_converted_to_synthetic(self, client: AsyncClient):
        create = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Convert me",
                "db_type": "clickhouse",
                "host": "localhost",
                "database_name": "d",
            },
        )
        ds_id = create.json()["id"]
        resp = await client.patch(f"/api/v1/data-sources/{ds_id}", json={"db_type": "synthetic"})
        assert resp.status_code == 422


CLIENT_KEY_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----\n"
CA_CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+gAwIBAg\n-----END CERTIFICATE-----\n"


@pytest.fixture
def encryption_key(monkeypatch: pytest.MonkeyPatch):
    """Run a test with a real Fernet key configured.

    The suite otherwise runs with an empty ENCRYPTION_KEY (dev/test passthrough),
    which would hide whether a secret is actually encrypted at rest.
    """
    monkeypatch.setattr(config.settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


async def _create(client: AsyncClient, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": f"ds-{uuid.uuid4()}",
        "db_type": "clickhouse",
        "host": "localhost",
        "database_name": "analytics",
    }
    payload.update(overrides)
    return await client.post("/api/v1/data-sources", json=payload)  # type: ignore[return-value]


class TestConnectionSettings:
    """Typed per-warehouse settings: they round-trip, and anything that does not
    apply to the warehouse is rejected rather than silently stored and ignored."""

    async def test_bigquery_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="bigquery",
            host="gcp-project",
            database_name="analytics",
            timeout_seconds=90,
            connection_settings={
                "location": "EU",
                "maximum_bytes_billed": 5_000_000,
                "dataset_allowlist": ["analytics", "marts"],
            },
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["timeout_seconds"] == 90
        assert body["connection_settings"]["location"] == "EU"
        assert body["connection_settings"]["maximum_bytes_billed"] == 5_000_000
        assert body["connection_settings"]["dataset_allowlist"] == ["analytics", "marts"]

        got = await client.get(f"/api/v1/data-sources/{body['id']}")
        assert got.json()["connection_settings"]["location"] == "EU"

        updated = await client.patch(
            f"/api/v1/data-sources/{body['id']}",
            json={"connection_settings": {"location": "us-east1"}},
        )
        assert updated.status_code == 200
        settings = updated.json()["connection_settings"]
        assert settings["location"] == "us-east1"
        # A PATCH replaces the settings wholesale: an omitted field is cleared.
        assert settings["maximum_bytes_billed"] is None
        assert settings["dataset_allowlist"] is None

    async def test_databricks_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="databricks",
            host="dbc-a1b2c3d4-e5f6.cloud.databricks.com",
            port=443,
            database_name="main",
            password="dapi-token",
            connection_settings={
                "http_path": "/sql/1.0/warehouses/abc123",
                "auth_type": "pat",
                "schema_name": "analytics",
                "schema_allowlist": ["analytics", "marts"],
            },
        )
        assert create.status_code == 201, create.text
        settings = create.json()["connection_settings"]
        assert settings["http_path"] == "/sql/1.0/warehouses/abc123"
        assert settings["auth_type"] == "pat"
        assert settings["schema_name"] == "analytics"
        assert settings["schema_allowlist"] == ["analytics", "marts"]
        assert "dapi-token" not in create.text

        # A PATCH that drops the HTTP path would leave a source nothing can reach.
        updated = await client.patch(
            f"/api/v1/data-sources/{create.json()['id']}",
            json={"connection_settings": {"schema_name": "marts"}},
        )
        assert updated.status_code == 422, updated.text

    async def test_databricks_without_an_http_path_is_rejected(self, client: AsyncClient):
        resp = await _create(
            client,
            db_type="databricks",
            host="dbc-a1b2c3d4-e5f6.cloud.databricks.com",
            port=443,
            database_name="main",
        )
        assert resp.status_code == 422, resp.text
        assert "http_path" in resp.text

    async def test_snowflake_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="snowflake",
            host="myorg-myaccount",
            port=443,
            database_name="ANALYTICS",
            username="TRIPL",
            password="hunter2",
            connection_settings={
                "warehouse": "COMPUTE_WH",
                "auth_type": "password",
                "role": "ANALYST",
                "schema_name": "EVENTS",
                "schema_allowlist": ["MARTS"],
            },
        )
        assert create.status_code == 201, create.text
        settings = create.json()["connection_settings"]
        assert settings["warehouse"] == "COMPUTE_WH"
        assert settings["auth_type"] == "password"
        assert settings["role"] == "ANALYST"
        assert settings["schema_name"] == "EVENTS"
        assert settings["schema_allowlist"] == ["MARTS"]
        assert "hunter2" not in create.text

        # A PATCH that drops the warehouse would leave a source nothing can run on.
        updated = await client.patch(
            f"/api/v1/data-sources/{create.json()['id']}",
            json={"connection_settings": {"schema_name": "MARTS"}},
        )
        assert updated.status_code == 422, updated.text

    async def test_snowflake_without_a_warehouse_is_rejected(self, client: AsyncClient):
        resp = await _create(
            client, db_type="snowflake", host="myorg-myaccount", port=443, database_name="DB"
        )
        assert resp.status_code == 422, resp.text
        assert "warehouse" in resp.text

    async def test_trino_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="trino",
            host="trino.example.com",
            port=443,
            database_name="hive",
            username="tripl",
            password="hunter2",
            connection_settings={
                "http_scheme": "https",
                "schema_name": "events",
                "schema_allowlist": ["marts"],
            },
        )
        assert create.status_code == 201, create.text
        settings = create.json()["connection_settings"]
        assert settings["http_scheme"] == "https"
        assert settings["schema_name"] == "events"
        assert settings["schema_allowlist"] == ["marts"]
        assert settings["warehouse"] is None
        assert "hunter2" not in create.text

    async def test_athena_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="athena",
            host="eu-west-1",
            port=443,
            database_name="analytics",
            username="AKIAEXAMPLE",
            password="secret",
            connection_settings={
                "work_group": "primary",
                "s3_output_location": "s3://my-bucket/athena/",
                "schema_allowlist": ["marts"],
            },
        )
        assert create.status_code == 201, create.text
        settings = create.json()["connection_settings"]
        assert settings["work_group"] == "primary"
        assert settings["s3_output_location"] == "s3://my-bucket/athena/"
        assert settings["catalog_name"] is None
        assert "secret" not in create.text.replace("password_set", "")

    async def test_postgres_settings_round_trip(self, client: AsyncClient):
        create = await _create(
            client,
            db_type="postgres",
            port=5432,
            connection_settings={
                "sslmode": "verify-full",
                "sslrootcert": CA_CERT_PEM,
                "search_path": "public, analytics",
            },
        )
        assert create.status_code == 201, create.text
        settings = create.json()["connection_settings"]
        assert settings["sslmode"] == "verify-full"
        assert settings["sslrootcert"].startswith("-----BEGIN CERTIFICATE-----")
        assert settings["search_path"] == "public, analytics"
        assert settings["sslkey_set"] is False

    async def test_defaults_are_unset_and_depend_on_db_type(self, client: AsyncClient):
        """No settings sent → nothing stored; the server-side default applies at
        adapter build (sslmode=prefer for Postgres, a 100 GiB cost guard for
        BigQuery), which is why the response carries nulls, not another db_type's
        fields."""
        create = await _create(client, db_type="postgres", port=5432)
        settings = create.json()["connection_settings"]
        assert settings["sslmode"] is None
        assert settings["location"] is None
        assert settings["sslkey_set"] is False

    @pytest.mark.parametrize(
        ("db_type", "host", "settings", "expected"),
        [
            # A BigQuery setting on a PostgreSQL source.
            ("postgres", "db.example.com", {"location": "EU"}, "postgres"),
            # A PostgreSQL setting on a BigQuery source.
            ("bigquery", "gcp-project", {"sslmode": "require"}, "bigquery"),
            # ClickHouse has no connection settings at all.
            ("clickhouse", "localhost", {"location": "EU"}, "clickhouse"),
            # A BigQuery setting on a Databricks source.
            (
                "databricks",
                "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
                {"location": "EU"},
                "databricks",
            ),
            # A Databricks setting on a Snowflake source.
            (
                "snowflake",
                "myorg-myaccount",
                {"http_path": "/sql/1.0/warehouses/x"},
                "snowflake",
            ),
            # An Athena setting on a Trino source, and a Trino one on Athena.
            ("trino", "trino.example.com", {"work_group": "primary"}, "trino"),
            ("athena", "eu-west-1", {"http_scheme": "https"}, "athena"),
        ],
    )
    async def test_inapplicable_settings_are_rejected_not_ignored(
        self,
        client: AsyncClient,
        db_type: str,
        host: str,
        settings: dict,
        expected: str,
    ):
        resp = await _create(client, db_type=db_type, host=host, connection_settings=settings)
        assert resp.status_code == 422, resp.text
        detail = str(resp.json()["detail"])
        assert "not a connection setting" in detail
        assert expected in detail

    async def test_unknown_setting_is_rejected(self, client: AsyncClient):
        resp = await _create(
            client,
            db_type="postgres",
            host="db.example.com",
            connection_settings={"sslmode": "require", "sneaky_option": "on"},
        )
        assert resp.status_code == 422

    async def test_update_rejects_inapplicable_setting(self, client: AsyncClient):
        create = await _create(client, db_type="postgres", host="db.example.com", port=5432)
        ds_id = create.json()["id"]
        resp = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"connection_settings": {"dataset_allowlist": ["analytics"]}},
        )
        assert resp.status_code == 422
        assert "not a connection setting" in str(resp.json()["detail"])

    @pytest.mark.parametrize(
        "settings",
        [
            {"sslmode": "totally-secure"},
            # Certificates are PEM content, never a path on the server.
            {"sslrootcert": "/etc/ssl/certs/ca.pem"},
            {"sslkey": "/etc/ssl/private/client.key"},
            # search_path is interpolated into SET search_path.
            {"search_path": "public; DROP TABLE users"},
        ],
    )
    async def test_malformed_postgres_settings_are_rejected(
        self, client: AsyncClient, settings: dict
    ):
        resp = await _create(
            client, db_type="postgres", host="db.example.com", connection_settings=settings
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize(
        "settings",
        [
            {"location": "not a region!"},
            {"maximum_bytes_billed": 0},
            {"dataset_allowlist": ["good", "bad-dataset!"]},
        ],
    )
    async def test_malformed_bigquery_settings_are_rejected(
        self, client: AsyncClient, settings: dict
    ):
        resp = await _create(
            client, db_type="bigquery", host="gcp-project", connection_settings=settings
        )
        assert resp.status_code == 422, resp.text

    async def test_private_key_is_encrypted_and_never_returned(
        self, client: AsyncClient, encryption_key: None
    ):
        create = await _create(
            client,
            db_type="postgres",
            host="db.example.com",
            port=5432,
            connection_settings={
                "sslmode": "verify-full",
                "sslrootcert": CA_CERT_PEM,
                "sslcert": CA_CERT_PEM,
                "sslkey": CLIENT_KEY_PEM,
            },
        )
        assert create.status_code == 201, create.text
        ds_id = create.json()["id"]

        # The response says a key is stored — and never carries the key itself.
        assert create.json()["connection_settings"]["sslkey_set"] is True
        assert "sslkey" not in create.json()["connection_settings"]
        for body in (create.text, (await client.get(f"/api/v1/data-sources/{ds_id}")).text):
            assert "BEGIN PRIVATE KEY" not in body
        listed = await client.get("/api/v1/data-sources")
        assert "BEGIN PRIVATE KEY" not in listed.text

        # At rest it is Fernet-encrypted under the same scheme as the password,
        # and the plaintext never reaches the audit log.
        async with TestSessionLocal() as session:
            ds = await session.get(DataSource, uuid.UUID(ds_id))
            assert ds is not None
            stored = ds.extra_params or {}
            assert "sslkey" not in stored
            assert stored["sslkey_encrypted"] != CLIENT_KEY_PEM
            assert decrypt_value(stored["sslkey_encrypted"]) == CLIENT_KEY_PEM
            # Public certs are stored as-is — only the private key is a secret.
            assert stored["sslrootcert"] == CA_CERT_PEM.strip()

            entries = await session.execute(
                select(AuditLog).where(AuditLog.action == "data_source.create")
            )
            for entry in entries.scalars().all():
                assert "BEGIN PRIVATE KEY" not in json.dumps(entry.payload)

    async def test_private_key_is_kept_when_omitted_and_cleared_when_blank(
        self, client: AsyncClient
    ):
        create = await _create(
            client,
            db_type="postgres",
            host="db.example.com",
            port=5432,
            connection_settings={"sslmode": "require", "sslkey": CLIENT_KEY_PEM},
        )
        ds_id = create.json()["id"]

        # Omitted, like an omitted password: the stored key survives the update.
        kept = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"connection_settings": {"sslmode": "verify-ca", "sslrootcert": CA_CERT_PEM}},
        )
        assert kept.status_code == 200
        assert kept.json()["connection_settings"]["sslkey_set"] is True

        cleared = await client.patch(
            f"/api/v1/data-sources/{ds_id}",
            json={"connection_settings": {"sslmode": "require", "sslkey": ""}},
        )
        assert cleared.status_code == 200
        assert cleared.json()["connection_settings"]["sslkey_set"] is False


class TestAdapterSettingsWiring:
    """The registry must hand the typed settings to the adapter — the whole point
    of storing them. BigQuery in particular used to get no timeout at all."""

    def _data_source(self, **overrides: object) -> DataSource:
        fields: dict[str, object] = {
            "id": uuid.uuid4(),
            "name": "wiring",
            "db_type": "bigquery",
            "host": "gcp-project",
            "port": 0,
            "database_name": "analytics",
            "username": "",
            "password_encrypted": "",
            "timeout_seconds": None,
            "json_path_discovery": None,
            "extra_params": None,
        }
        fields.update(overrides)
        return DataSource(**fields)

    def test_bigquery_gets_timeout_location_and_cost_guard(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict[str, object] = {}

        def fake_adapter(**kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(bigquery_module, "BigQueryAdapter", fake_adapter)
        ds = self._data_source(
            timeout_seconds=42,
            extra_params={"location": "EU", "dataset_allowlist": ["analytics"]},
        )

        build_adapter(ds)

        assert captured["timeout_seconds"] == 42
        assert captured["location"] == "EU"
        assert captured["dataset_allowlist"] == ["analytics"]
        # Unset → the server-side BigQuery default cost guard, not "unlimited".
        assert captured["maximum_bytes_billed"] == DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED

    def test_bigquery_timeout_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            bigquery_module, "BigQueryAdapter", lambda **kwargs: captured.update(kwargs) or object()
        )

        build_adapter(self._data_source(extra_params={"maximum_bytes_billed": 123}))

        assert captured["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS
        assert captured["maximum_bytes_billed"] == 123

    def test_postgres_gets_tls_settings_with_the_decrypted_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            postgres_module, "PostgresAdapter", lambda **kwargs: captured.update(kwargs) or object()
        )
        ds = self._data_source(
            db_type="postgres",
            host="db.example.com",
            port=5432,
            extra_params={
                "sslmode": "verify-full",
                "sslrootcert": CA_CERT_PEM,
                "search_path": "public",
                "sslkey_encrypted": encrypt_value(CLIENT_KEY_PEM),
            },
        )

        build_adapter(ds)

        assert captured["sslmode"] == "verify-full"
        assert captured["sslrootcert"] == CA_CERT_PEM.strip()
        assert captured["search_path"] == "public"
        # The adapter needs the key material itself; the column never holds it.
        assert captured["sslkey"] == CLIENT_KEY_PEM

    def test_unpinned_sslmode_reaches_the_adapter_as_none(self, monkeypatch: pytest.MonkeyPatch):
        """The registry must NOT substitute a static TLS default.

        The default is host-aware — remote gets ``require``, localhost gets ``prefer``
        — and only the adapter knows the host. The registry used to fill in a static
        ``"prefer"`` here, which made the adapter's ``require`` branch unreachable and
        silently left every *remote* warehouse tolerating a plaintext connection.
        """
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            postgres_module, "PostgresAdapter", lambda **kwargs: captured.update(kwargs) or object()
        )

        build_adapter(self._data_source(db_type="postgres", host="db.example.com", port=5432))

        assert captured["sslmode"] is None
        assert captured["sslkey"] is None
        assert captured["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS

    def test_a_remote_host_defaults_to_require_and_localhost_to_prefer(self):
        """``prefer`` accepts plaintext when the server offers no TLS, so it guarantees
        nothing: a stripped connection is indistinguishable from a healthy one. A remote
        warehouse must fail loudly instead. Localhost keeps ``prefer`` — dev/docker
        Postgres usually has no certificate and the traffic never leaves the machine.
        """
        assert _resolve_sslmode("db.example.com", None) == "require"
        assert _resolve_sslmode("localhost", None) == "prefer"
        # An explicit choice is always honored, including a deliberate downgrade.
        assert _resolve_sslmode("db.example.com", "disable") == "disable"


class TestConnectionErrorSanitization:
    """The connection-test sanitiser must not leak host/port/credential internals.

    It is the sole owner of ``last_test_message`` wording; that both probe paths
    actually route through it is pinned by ``TestConnectionProbeMessageHasOneOwner``.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "HTTPSConnectionPool(host='ch.internal', port=8443): Read timed out.",
            "ConnectionRefusedError: [Errno 111] Connection refused to db.internal:5432",
            "OperationalError: password authentication failed for user 'admin'",
            "some unexpected driver explosion",
        ],
    )
    def test_friendly_test_error_hides_internals(self, raw: str):
        msg = datasource_service._friendly_test_error(Exception(raw))
        lowered = msg.lower()
        assert "connection test failed" in lowered
        # No raw host/port values, error numbers, or usernames reach the message.
        assert not any(ch.isdigit() for ch in msg)
        for leak in ("ch.internal", "db.internal", "errno", "admin", "httpsconnectionpool"):
            assert leak not in lowered

    def test_a_server_without_tls_is_named_not_called_unreachable(self):
        """psycopg's no-TLS refusal also says "connection failed"; it is not a network fault."""
        exc = Exception(
            'connection failed: connection to server at "172.23.0.4", port 5432 failed: '
            "server does not support SSL, but SSL was required"
        )

        msg = datasource_service._friendly_test_error(exc)

        assert "does not offer TLS" in msg
        assert "SSL mode to disable" in msg
        assert "could not reach" not in msg
        assert not any(ch.isdigit() for ch in msg)

    def test_a_capability_error_reaches_the_user_verbatim(self):
        """Masking a driver string is right; masking OUR message is not.

        The PostgreSQL version guard tells the operator exactly what is wrong and how
        to fix it. Collapsed into the generic "check the connection settings", it sends
        them to re-check settings that are all correct. Note this message legitimately
        contains digits (version numbers), which is why it cannot be distinguished from
        a leaky driver string by pattern-matching — it needs its own type.
        """
        exc = WarehouseCapabilityError(
            "PostgreSQL 13.23 is too old for tripl: every time-bucket query uses "
            "date_bin(), which was added in PostgreSQL 14. Upgrade the server to 14 "
            "or newer."
        )

        msg = datasource_service._friendly_test_error(exc)

        assert "13.23 is too old" in msg
        assert "date_bin()" in msg
        assert "Upgrade the server to 14" in msg
        # Still nothing sensitive: the message is authored by tripl, not the driver.
        for leak in ("host", "port", "password", "user"):
            assert leak not in msg.lower()

    def test_an_unknown_sslmode_is_explained_rather_than_generalized(self):
        exc = WarehouseCapabilityError(
            "Unsupported sslmode: 'verify'. Supported modes are: disable, allow, "
            "prefer, require, verify-ca, verify-full."
        )

        msg = datasource_service._friendly_test_error(exc)

        assert "Unsupported sslmode: 'verify'" in msg
        assert "verify-full" in msg

    @pytest.mark.parametrize(
        ("host", "service_account_json", "quoted_in_docs"),
        [
            ("", '{"type": "service_account"}', "BigQuery: host (project_id) is required"),
            ("gcp-project", "", "BigQuery: service-account JSON credentials are required"),
            ("gcp-project", "not json", "BigQuery: invalid service-account JSON"),
        ],
    )
    def test_bigquery_configuration_errors_reach_the_user_verbatim(
        self, host: str, service_account_json: str, quoted_in_docs: str
    ):
        """website/docs/use/troubleshooting.md quotes these three strings as what the
        operator will see, so the probe has to actually produce them.

        As bare ``ValueError`` they did not: the sanitiser's substring hints matched
        "host" and "credentials" and answered "could not reach the data source" /
        "authentication was rejected" — a network and a password to go check, when the
        real fault is an empty field. They are ``WarehouseCapabilityError`` now.
        Built through the real registry, not a hand-made exception, so
        the test fails if the adapter stops raising the curated type.
        """
        ds = DataSource(
            id=uuid.uuid4(),
            name="bq",
            db_type="bigquery",
            host=host,
            port=0,
            database_name="analytics",
            username="",
            password_encrypted=encrypt_value(service_account_json),
            timeout_seconds=None,
            json_path_discovery=None,
            extra_params=None,
        )

        ok, message = datasource_service._run_adapter_test(ds)

        assert ok is False
        assert quoted_in_docs in message


class TestConnectionProbeErrorSanitization:
    """The in-request connection probe sanitizes driver errors."""

    PROBE_FAILURES = [
        pytest.param(
            ConnectionError("clickhouse-connect: Connection refused to warehouse.internal:8123"),
            id="unreachable",
        ),
        pytest.param(
            TimeoutError("HTTPSConnectionPool(host='ch.internal', port=8443): Read timed out."),
            id="timeout",
        ),
        pytest.param(
            OSError("OperationalError: password authentication failed for user 'admin'"),
            id="auth",
        ),
        pytest.param(RuntimeError("some unexpected driver explosion"), id="unrecognized"),
        pytest.param(
            WarehouseCapabilityError("BigQuery: host (project_id) is required"),
            id="curated-configuration",
        ),
    ]

    def _data_source(self, ds_id: uuid.UUID) -> DataSource:
        return DataSource(
            id=ds_id,
            name="DS",
            db_type="clickhouse",
            host="warehouse.internal",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )

    def _http_probe(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> str:
        def failing_build(ds: DataSource) -> object:
            raise exc

        monkeypatch.setattr("tripl.core.adapters.registry.build_adapter", failing_build)
        _, message = datasource_service._run_adapter_test(self._data_source(uuid.uuid4()))
        return message

    @pytest.mark.parametrize("exc", PROBE_FAILURES)
    def test_probe_error_is_sanitized(self, exc: Exception, monkeypatch: pytest.MonkeyPatch):
        message = self._http_probe(monkeypatch, exc)
        assert message.startswith("Connection test failed")
        assert "scan" not in message.lower()
        assert "warehouse.internal" not in message
