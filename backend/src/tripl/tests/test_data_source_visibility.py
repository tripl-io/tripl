"""Non-owners must not read warehouse connection metadata.

``GET /api/v1/data-sources`` used to hand every authenticated user — viewers
included — the host, port, database, username, stored-secret flag, TLS material
and last driver error of every warehouse. Data sources are managed by the
organization's owners and admins (F20 PR4), so the
read side is narrowed to the identity fields the scan and metric surfaces
actually render.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tripl.api.v1 import data_sources as data_sources_router
from tripl.extensions import Extension, override_extensions
from tripl.main import app
from tripl.schemas.data_source_schema import DataSourceSchemaResponse
from tripl.services import project_permissions
from tripl.tests._members import add_member_by_slug

PASSWORD = "Password123!"

# Everything a non-owner keeps: enough to say *which* warehouse a scan or metric
# points at and whether it is healthy.
VISIBLE_FIELDS = {
    "id",
    "project_id",
    "name",
    "db_type",
    "is_synthetic",
    "last_test_at",
    "last_test_status",
    "created_at",
    "updated_at",
}
# Everything that describes how to *reach* the warehouse.
REDACTED = {
    "host": "",
    "port": 0,
    "database_name": "",
    "username": "",
    "password_set": False,
    "timeout_seconds": None,
    "json_path_discovery": None,
    "last_test_message": None,
}


VIEWER_PROJECT = "ds-viewer-project"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def stand():
    """An owner with a real-looking data source, plus an editor and a viewer.

    Since F20 PR4 there is no instance ``viewer`` role: the viewer is a member of
    the organization whose only project membership is ``viewer`` in
    :data:`VIEWER_PROJECT`; the editor edits the same project. Both non-owners
    are organization members, never owners or admins, which is what the
    connection redaction keys on.
    """
    owner = _new_client()
    editor = _new_client()
    viewer = _new_client()

    for client, email, name in (
        (owner, "ds-owner@example.com", "Owner"),
        (editor, "ds-editor@example.com", "Editor"),
        (viewer, "ds-viewer@example.com", "Viewer"),
    ):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "name": name},
        )
        assert resp.status_code == 201, resp.text
        if client is viewer:
            viewer_id = resp.json()["id"]

    del viewer_id
    project = await owner.post(
        "/api/v1/projects", json={"name": VIEWER_PROJECT, "slug": VIEWER_PROJECT}
    )
    assert project.status_code == 201, project.text
    await add_member_by_slug(VIEWER_PROJECT, "ds-viewer@example.com", "viewer")
    await add_member_by_slug(VIEWER_PROJECT, "ds-editor@example.com", "editor")

    created = await owner.post(
        "/api/v1/data-sources",
        json={
            "name": "prod-clickhouse",
            "db_type": "clickhouse",
            "host": "clickhouse.internal.example.com",
            "port": 9440,
            "database_name": "analytics",
            "username": "tripl_ro",
            "password": "hunter2",
            "timeout_seconds": 90,
        },
    )
    assert created.status_code == 201, created.text

    yield owner, editor, viewer, created.json()["id"]

    for client in (owner, editor, viewer):
        await client.aclose()


@pytest.mark.asyncio
async def test_owner_still_sees_the_full_connection(stand) -> None:
    owner, _editor, _viewer, ds_id = stand

    listing = await owner.get("/api/v1/data-sources")
    assert listing.status_code == 200, listing.text
    (row,) = listing.json()
    assert row["host"] == "clickhouse.internal.example.com"
    assert row["port"] == 9440
    assert row["database_name"] == "analytics"
    assert row["username"] == "tripl_ro"
    assert row["password_set"] is True

    detail = await owner.get(f"/api/v1/data-sources/{ds_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["host"] == "clickhouse.internal.example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["editor", "viewer"])
async def test_non_owners_get_the_connection_redacted(stand, actor: str) -> None:
    owner, editor, viewer, ds_id = stand
    del owner
    client = editor if actor == "editor" else viewer

    for resp in (
        await client.get("/api/v1/data-sources"),
        await client.get(f"/api/v1/data-sources/{ds_id}"),
    ):
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        row = payload[0] if isinstance(payload, list) else payload

        for field, blank in REDACTED.items():
            assert row[field] == blank, f"{actor} can still read {field}: {row[field]!r}"
        assert row["connection_settings"] == {
            "location": None,
            "maximum_bytes_billed": None,
            "dataset_allowlist": None,
            "sslmode": None,
            "sslrootcert": None,
            "sslcert": None,
            "search_path": None,
            "sslkey_set": False,
            "http_path": None,
            "auth_type": None,
            "schema_name": None,
            "schema_allowlist": None,
            "warehouse": None,
            "role": None,
            "http_scheme": None,
            "work_group": None,
            "s3_output_location": None,
            "catalog_name": None,
        }
        # No hostname or credential leaks anywhere else in the payload.
        assert "internal.example.com" not in resp.text
        assert "tripl_ro" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["editor", "viewer"])
async def test_non_owners_keep_what_the_scan_and_metric_forms_need(stand, actor: str) -> None:
    """Redaction must not break picking a source or choosing a SQL dialect."""
    owner, editor, viewer, ds_id = stand
    del owner
    client = editor if actor == "editor" else viewer

    listing = await client.get("/api/v1/data-sources")
    assert listing.status_code == 200, listing.text
    (row,) = listing.json()

    assert row["id"] == ds_id
    assert row["name"] == "prod-clickhouse"
    assert row["db_type"] == "clickhouse"
    assert row["is_synthetic"] is False
    assert row["project_id"] is None
    assert set(row) >= VISIBLE_FIELDS


@pytest.mark.asyncio
async def test_viewer_cannot_enumerate_the_warehouse_schema(stand) -> None:
    """A viewer must not read the warehouse's table and column names.

    Redacting host/port off the data-source payload was pointless while this
    route handed the same viewer a map of every table and column in the
    warehouse. It matters more now that registration ships open: a stranger who
    registers reaches this with no further access.
    """
    _owner, _editor, viewer, ds_id = stand
    # The viewer role is a PROJECT role now. The source is organization-wide, and
    # plain org membership does not open its catalog: that takes org owner/admin
    # or an editing role in a project the source is in scope for (F20 PR4).
    schema = await viewer.get(f"/api/v1/data-sources/{ds_id}/schema")
    assert schema.status_code == 403

    stats = await viewer.get(f"/api/v1/data-sources/{ds_id}/stats")
    assert stats.status_code == 403


@pytest.mark.asyncio
async def test_editor_keeps_schema_access_but_not_stats(stand, monkeypatch) -> None:
    """Editors author scans, metrics and fact tables, so they still need /schema.

    Pins the boundary deliberately: locking /schema to owner-only would silently
    break the column pickers in the scan form, the metric form and the
    fact-table form, all of which are editor surfaces. /stats has no consumer at
    all, so it stays owner-only.

    The schema service is stubbed for the success path: reaching the handler at
    all means dialling the deliberately unreachable fixture warehouse, and the
    driver raises straight through the HTTP layer rather than answering with a
    status. What is under test is the authorization decision, not the driver.
    """
    _owner, editor, _viewer, ds_id = stand

    async def _fake_schema(_session, _ds_id):
        return DataSourceSchemaResponse(tables=[])

    monkeypatch.setattr(
        data_sources_router.datasource_schema_service, "get_schema_tables", _fake_schema
    )

    schema = await editor.get(f"/api/v1/data-sources/{ds_id}/schema")
    assert schema.status_code == 200

    stats = await editor.get(f"/api/v1/data-sources/{ds_id}/stats")
    assert stats.status_code == 403


@pytest.mark.asyncio
async def test_an_editor_an_extension_narrowed_gets_no_schema(stand, monkeypatch) -> None:
    """``Extension.project_permission_check`` refusing ``data_sources.manage``
    takes the catalog away from that editor too, and the owner keeps it."""
    owner, editor, _viewer, ds_id = stand

    class _NoDataSources(Extension):
        async def project_permission_check(self, session, user, project_id, permission):  # type: ignore[no-untyped-def]
            return False if permission == project_permissions.DATA_SOURCES_MANAGE else None

    async def _fake_schema(_session, _ds_id):
        return DataSourceSchemaResponse(tables=[])

    monkeypatch.setattr(
        data_sources_router.datasource_schema_service, "get_schema_tables", _fake_schema
    )
    with override_extensions([_NoDataSources()]):
        refused = await editor.get(f"/api/v1/data-sources/{ds_id}/schema")
        kept = await owner.get(f"/api/v1/data-sources/{ds_id}/schema")
    assert refused.status_code == 403, refused.text
    assert kept.status_code == 200, kept.text


@pytest.mark.asyncio
async def test_redaction_does_not_poison_the_shared_list_cache(stand) -> None:
    """The service caches the full list; redaction happens per request, not in cache."""
    owner, editor, _viewer, _ds_id = stand

    first = await editor.get("/api/v1/data-sources")
    assert first.json()[0]["host"] == ""

    after = await owner.get("/api/v1/data-sources")
    assert after.json()[0]["host"] == "clickhouse.internal.example.com"
