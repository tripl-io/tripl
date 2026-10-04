"""Regression coverage for project slug changes and slug-keyed cache eviction."""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl import cache
from tripl.models.search_document import SearchDocument
from tripl.tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_slug_rename_reindexes_search_documents(client: AsyncClient) -> None:
    old_slug = "rename-search-old"
    new_slug = "rename-search-new"
    created = await client.post(
        "/api/v1/projects", json={"name": "Rename search", "slug": old_slug}
    )
    assert created.status_code == 201
    event_type = await client.post(
        f"/api/v1/projects/{old_slug}/event-types",
        json={"name": "page_view", "display_name": "Page view"},
    )
    assert event_type.status_code == 201
    branch = await client.post(f"/api/v1/projects/{old_slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201

    async with TestSessionLocal() as session:
        before = (await session.scalars(select(SearchDocument.route_path))).all()
    assert any(path.startswith(f"/p/{old_slug}/") for path in before)

    response = await client.patch(f"/api/v1/projects/{old_slug}", json={"slug": new_slug})
    assert response.status_code == 200
    async with TestSessionLocal() as session:
        documents = (
            await session.execute(select(SearchDocument.branch_id, SearchDocument.route_path))
        ).all()
    after = [path for _branch_id, path in documents]
    assert after
    assert all(path.startswith(f"/p/{new_slug}/") for path in after)
    assert branch.json()["id"] in {str(branch_id) for branch_id, _path in documents}


@pytest.mark.asyncio
async def test_delete_evicts_slug_keyed_caches(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "delete-cached-project"
    created = await client.post("/api/v1/projects", json={"name": "Cache", "slug": slug})
    assert created.status_code == 201
    delete_prefix = AsyncMock()
    monkeypatch.setattr(cache, "delete_prefix", delete_prefix)

    response = await client.delete(f"/api/v1/projects/{slug}")
    assert response.status_code == 204
    prefixes = [call.args[0] for call in delete_prefix.await_args_list]
    project_id = uuid.UUID(created.json()["id"])
    assert cache.prefix_event_types(project_id) in prefixes
    assert cache.prefix_meta_fields(project_id) in prefixes
    assert cache.prefix_signals(project_id) in prefixes
