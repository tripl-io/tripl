"""API-level tests for the FactTable CRUD catalog + preview."""

import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from tripl.models.data_source import DataSource
from tripl.models.scan_config import ScanConfig
from tripl.schemas.fact_table import NATIVE_TYPE_MAX_LEN, FactTableCreate
from tripl.tests.conftest import TestSessionLocal


async def _own_data_source(project_id: str, data_source_id: str) -> None:
    """Stamp ``data_sources.project_id`` — the stronger of the two claims.

    Where a ScanConfig only claims a workspace-global source, this column says
    outright whose the source is, and ``data_source_out_of_project_scope``
    decides on it alone: no ScanConfig is consulted, in either direction.
    """
    async with TestSessionLocal() as session:
        row = await session.get(DataSource, uuid.UUID(data_source_id))
        assert row is not None
        row.project_id = uuid.UUID(project_id)
        await session.commit()


async def _bind_data_source_to_project(project_id: str, data_source_id: str) -> None:
    """Point a project's ScanConfig at a data source.

    On a workspace-global source (``project_id`` NULL) this is what CLAIMS it:
    ``services/data_source_scope`` reads "scanned by some other project and not
    by this one" as "theirs", so binding is how these tests build an out-of-scope
    source. A source nobody scans is shared and reachable from every project.
    """
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project_id),
                data_source_id=uuid.UUID(data_source_id),
                name="scan-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Fact Tables Test", "slug": "fact-tables-test", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Test CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _fact_tables_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/fact-tables"


async def _create_fact_table(
    client: AsyncClient,
    slug: str,
    name: str,
    **extra: object,
) -> dict:
    payload: dict = {
        "name": name,
        "display_name": name.upper(),
        "sql": "SELECT id, user_id, amount, created_at FROM orders",
        "timestamp_column": "created_at",
        "columns": [
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ],
        "identifier_columns": ["user_id"],
        "row_filters": [{"name": "paid", "sql": "status = 'paid'"}],
        **extra,
    }
    resp = await client.post(_fact_tables_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCrudLifecycle:
    async def test_create_list_get_update_delete(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        await _bind_data_source_to_project(project["id"], data_source["id"])

        # Create
        created = await _create_fact_table(client, slug, "orders", data_source_id=data_source["id"])
        assert created["name"] == "orders"
        assert created["display_name"] == "ORDERS"
        assert created["data_source_id"] == data_source["id"]
        assert created["timestamp_column"] == "created_at"
        assert created["columns"] == [
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ]
        assert created["identifier_columns"] == ["user_id"]
        assert created["row_filters"] == [{"name": "paid", "sql": "status = 'paid'"}]
        assert created["color"] == "#6366f1"
        assert created["project_id"] == project["id"]
        fact_table_id = created["id"]

        # List
        listing = await client.get(_fact_tables_url(slug))
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == fact_table_id
        assert body["items"][0]["name"] == "orders"

        # Get
        fetched = await client.get(f"{_fact_tables_url(slug)}/{fact_table_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["sql"].startswith("SELECT id, user_id")

        # Update
        updated = await client.patch(
            f"{_fact_tables_url(slug)}/{fact_table_id}",
            json={
                "display_name": "Orders v2",
                "timestamp_column": "updated_at",
                "identifier_columns": ["user_id", "account_id"],
            },
        )
        assert updated.status_code == 200, updated.text
        data = updated.json()
        assert data["display_name"] == "Orders v2"
        assert data["timestamp_column"] == "updated_at"
        assert data["identifier_columns"] == ["user_id", "account_id"]

        # Delete
        deleted = await client.delete(f"{_fact_tables_url(slug)}/{fact_table_id}")
        assert deleted.status_code == 204
        missing = await client.get(f"{_fact_tables_url(slug)}/{fact_table_id}")
        assert missing.status_code == 404

    async def test_create_without_data_source(self, client: AsyncClient, project: dict):
        created = await _create_fact_table(client, project["slug"], "no_source")
        assert created["data_source_id"] is None

    async def test_order_appends_at_end(self, client: AsyncClient, project: dict):
        first = await _create_fact_table(client, project["slug"], "first")
        second = await _create_fact_table(client, project["slug"], "second")
        assert second["order"] > first["order"]


class TestDataSourceScoping:
    """A fact table may not bind a data source that is another project's.

    The rule used to be "a ScanConfig must link the source to THIS project",
    which answered the same question differently from the ``sql``-metric doors
    and locked out a workspace-global warehouse nobody scans.
    It is now ownership — so these two build the out-of-scope source by having
    ANOTHER project claim it, which is the case that was always the point.
    """

    async def test_create_rejects_data_source_another_project_claims(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        other = await client.post(
            "/api/v1/projects",
            json={"name": "Other", "slug": "fact-tables-other", "description": ""},
        )
        assert other.status_code == 201, other.text
        await _bind_data_source_to_project(other.json()["id"], data_source["id"])

        resp = await client.post(
            _fact_tables_url(project["slug"]),
            json={
                "name": "leaky",
                "display_name": "Leaky",
                "sql": "SELECT id, ts FROM t",
                "timestamp_column": "ts",
                "data_source_id": data_source["id"],
            },
        )
        assert resp.status_code == 404, resp.text

    async def test_update_rejects_data_source_another_project_claims(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        other = await client.post(
            "/api/v1/projects",
            json={"name": "Other 2", "slug": "fact-tables-other-2", "description": ""},
        )
        assert other.status_code == 201, other.text
        await _bind_data_source_to_project(other.json()["id"], data_source["id"])

        created = await _create_fact_table(client, project["slug"], "no_source")
        resp = await client.patch(
            f"{_fact_tables_url(project['slug'])}/{created['id']}",
            json={"data_source_id": data_source["id"]},
        )
        assert resp.status_code == 404, resp.text

    async def test_create_accepts_a_data_source_this_project_owns(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """The branch the ``project_id`` COLUMN decides, which nothing else here
        reaches.

        This was ``test_create_accepts_bound_data_source``, and under the binding
        rule its ``_bind_data_source_to_project`` call was what made it pass.
        Under the ownership rule that call is inert: the fixture source is
        workspace-global and nobody else scans it, so it was already in scope
        with or without a ScanConfig, and the test asserted nothing about the
        door — ``test_metric_data_source_seam`` says that case out loud instead, in
        ``test_a_fact_table_may_bind_a_warehouse_nobody_scans``. Stamping the
        owning project covers the third branch, and it does so with NO
        ScanConfig anywhere.

        RED on a revert: restore "at least one ScanConfig links the source to
        this project" and this save answers 404, because this project has none.
        """
        await _own_data_source(project["id"], data_source["id"])
        created = await _create_fact_table(
            client, project["slug"], "owned", data_source_id=data_source["id"]
        )
        assert created["data_source_id"] == data_source["id"]


class TestConflictsAndValidation:
    async def test_reject_explicit_null_order_on_update(self, client: AsyncClient, project: dict):
        # ``order`` maps to a NOT NULL column; an explicit null must be rejected
        # at the schema boundary (422), not surface as a DB-level 500.
        created = await _create_fact_table(client, project["slug"], "ordered")
        resp = await client.patch(
            f"{_fact_tables_url(project['slug'])}/{created['id']}",
            json={"order": None},
        )
        assert resp.status_code == 422, resp.text

    async def test_duplicate_name_conflict(self, client: AsyncClient, project: dict):
        await _create_fact_table(client, project["slug"], "dup")
        resp = await client.post(
            _fact_tables_url(project["slug"]),
            json={
                "name": "dup",
                "display_name": "Dup 2",
                "sql": "SELECT id, ts FROM t",
                "timestamp_column": "ts",
            },
        )
        assert resp.status_code == 409, resp.text

    async def test_reject_unsafe_sql_at_api(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _fact_tables_url(project["slug"]),
            json={
                "name": "bad_sql",
                "display_name": "Bad SQL",
                "sql": "SELECT * FROM users UNION SELECT secret FROM admin",
                "timestamp_column": "ts",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_bad_timestamp_identifier(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _fact_tables_url(project["slug"]),
            json={
                "name": "bad_ts",
                "display_name": "Bad TS",
                "sql": "SELECT id, ts FROM t",
                "timestamp_column": "ts; DROP TABLE x --",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_row_filter_injection(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _fact_tables_url(project["slug"]),
            json={
                "name": "bad_filter",
                "display_name": "Bad Filter",
                "sql": "SELECT id, ts FROM t",
                "timestamp_column": "ts",
                "row_filters": [{"name": "evil", "sql": "1=1 UNION SELECT secret FROM users"}],
            },
        )
        assert resp.status_code == 422, resp.text


class TestSchemaBoundary:
    """The schema is the only gate before warehouse SQL with no bound params."""

    def test_sql_safety_rejects_union(self):
        with pytest.raises(ValidationError):
            FactTableCreate(
                name="x",
                display_name="X",
                sql="SELECT a FROM t UNION SELECT b FROM u",
                timestamp_column="ts",
            )

    def test_sql_safety_rejects_stacked_statement(self):
        with pytest.raises(ValidationError):
            FactTableCreate(
                name="x",
                display_name="X",
                sql="SELECT a FROM t; DROP TABLE u",
                timestamp_column="ts",
            )

    def test_identifier_column_rejects_injection(self):
        with pytest.raises(ValidationError):
            FactTableCreate(
                name="x",
                display_name="X",
                sql="SELECT a, ts FROM t",
                timestamp_column="ts",
                identifier_columns=["user_id", "bad col"],
            )

    def test_valid_select_is_accepted(self):
        model = FactTableCreate(
            name="x",
            display_name="X",
            sql="SELECT a, ts FROM t",
            timestamp_column="ts",
        )
        assert model.to_create_values()["sql"] == "SELECT a, ts FROM t"


class TestPreview:
    async def test_preview_maps_introspection(
        self, monkeypatch: pytest.MonkeyPatch, client: AsyncClient, project: dict
    ):
        # The introspection service is a sibling slice; skip cleanly until it
        # lands, then run with a monkeypatched, warehouse-free stand-in.
        introspection_mod = pytest.importorskip("tripl.services.fact_table_introspection_service")

        canned = SimpleNamespace(
            columns=[
                SimpleNamespace(name="amount", type="number", native_type="Float64"),
                SimpleNamespace(name="user_id", type="string", native_type="String"),
            ],
            identifier_candidates=["user_id"],
        )

        async def fake_introspect(
            session: object,
            *,
            project_id: object,
            data_source_id: object,
            sql: str,
            timestamp_column: object,
        ) -> SimpleNamespace:
            return canned

        monkeypatch.setattr(introspection_mod, "introspect_fact_table", fake_introspect)

        resp = await client.post(
            f"{_fact_tables_url(project['slug'])}/preview",
            json={"sql": "SELECT amount, user_id FROM orders", "timestamp_column": None},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == [
            {"name": "amount", "type": "number", "native_type": "Float64"},
            {"name": "user_id", "type": "string", "native_type": "String"},
        ]
        assert body["identifier_candidates"] == ["user_id"]
        # No sample_rows: the preview answers the query's SHAPE and never its
        # rows. Nothing in the product displayed them, so the
        # field returned up to twenty raw warehouse rows to no one.
        assert "sample_rows" not in body

    async def test_preview_survives_an_over_long_native_type(
        self, monkeypatch: pytest.MonkeyPatch, client: AsyncClient, project: dict
    ):
        """One unusable type name must not 500 the whole preview.

        ``native_type`` is descriptive and every consumer matches on its HEAD
        (``core.warehouse_types`` classify_time/classify_complex are startswith-based),
        so ``schemas.fact_table`` bounds it on the way in instead of rejecting it — a
        ``max_length`` with no before-validator turns one irrelevant column into a
        blanket 500 for every column beside it. Reverting the before-validator gives
        a 500 here.

        A separate canned result rather than an extra column on the shared one: the
        test above asserts ``body["columns"]`` by exact equality and is about the
        happy-path mapping.
        """
        introspection_mod = pytest.importorskip("tripl.services.fact_table_introspection_service")

        # 6 + 9*40 + 1 = 367 characters, and a real shape: a wide ClickHouse enum is
        # how this arrives in practice.
        long_native_type = "Enum8(" + "'x' = 1, " * 40 + ")"
        assert len(long_native_type) > NATIVE_TYPE_MAX_LEN
        canned = SimpleNamespace(
            columns=[
                SimpleNamespace(name="status", type="string", native_type=long_native_type),
                SimpleNamespace(name="amount", type="number", native_type="Float64"),
            ],
            identifier_candidates=[],
            sample_rows=[{"status": "x", "amount": 1}],
        )

        async def fake_introspect(
            session: object,
            *,
            project_id: object,
            data_source_id: object,
            sql: str,
            timestamp_column: object,
        ) -> SimpleNamespace:
            return canned

        monkeypatch.setattr(introspection_mod, "introspect_fact_table", fake_introspect)

        resp = await client.post(
            f"{_fact_tables_url(project['slug'])}/preview",
            json={"sql": "SELECT status, amount FROM orders", "timestamp_column": None},
        )

        assert resp.status_code == 200, resp.text
        columns = resp.json()["columns"]
        long_column = next(column for column in columns if column["name"] == "status")
        assert len(long_column["native_type"]) == NATIVE_TYPE_MAX_LEN
        # The head survives, because the head is the part anything reads.
        assert long_column["native_type"].startswith("Enum8(")
        assert long_column["native_type"].endswith("…")
        # ...and the column that had nothing wrong with it is untouched.
        assert columns[1] == {"name": "amount", "type": "number", "native_type": "Float64"}
