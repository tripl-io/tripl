"""A project-owned warehouse catalog must respect the project's membership.

A non-member does not see the source at all (404, like an unknown id); a viewer
member is refused (403); an editor member, the creator and the owner read it.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tripl.api.v1 import data_sources as data_sources_router
from tripl.main import app
from tripl.models.data_source import DataSource
from tripl.schemas.data_source_schema import DataSourceSchemaResponse
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_project_owned_schema_denies_other_editor_before_introspection(monkeypatch) -> None:
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") for _ in range(3)
    ]
    owner, creator, stranger = clients
    try:
        for client, email in zip(
            clients,
            ("schema-owner@example.com", "schema-creator@example.com", "schema-other@example.com"),
            strict=True,
        ):
            response = await client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": "Password123!", "name": email},
            )
            assert response.status_code == 201, response.text

        project = await creator.post(
            "/api/v1/projects", json={"name": "Schema private", "slug": "schema-private"}
        )
        assert project.status_code == 201, project.text
        source_id = uuid.uuid4()
        async with TestSessionLocal() as session:
            session.add(
                DataSource(
                    id=source_id,
                    project_id=uuid.UUID(project.json()["id"]),
                    name="schema-private-source",
                    db_type="synthetic",
                    host="",
                    port=0,
                    database_name="",
                    username="",
                    password_encrypted="",
                )
            )
            await session.commit()

        calls = []

        async def fake_schema(_session, _ds_id):
            calls.append(_ds_id)
            return DataSourceSchemaResponse(tables=[])

        monkeypatch.setattr(
            data_sources_router.datasource_schema_service, "get_schema_tables", fake_schema
        )
        path = f"/api/v1/data-sources/{source_id}/schema"

        hidden = await stranger.get(path)
        assert hidden.status_code == 404, hidden.text
        assert calls == []

        await add_member_by_slug("schema-private", "schema-other@example.com", "viewer")
        denied = await stranger.get(path)
        assert denied.status_code == 403, denied.text
        assert calls == []

        for client in (creator, owner):
            allowed = await client.get(path)
            assert allowed.status_code == 200, allowed.text
        assert calls == [source_id, source_id]
    finally:
        for client in clients:
            await client.aclose()
