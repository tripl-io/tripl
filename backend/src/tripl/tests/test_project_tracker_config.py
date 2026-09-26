import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal


async def _create_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_get_tracker_config_returns_defaults_without_row(client: AsyncClient) -> None:
    await _create_project(client, "tracker-defaults")

    resp = await client.get("/api/v1/projects/tracker-defaults/tracker-config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["tracker_type"] == "jira"
    assert body["base_url"] == ""
    assert body["project_key"] == ""
    assert body["issue_type"] == "Task"
    assert body["api_token_set"] is False
    # No row is written by a GET.
    assert body["id"] is None
    assert body["created_at"] is None
    async with TestSessionLocal() as session:
        rows = (await session.execute(select(ProjectTrackerConfig))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_update_tracker_config_persists_and_hides_token(client: AsyncClient) -> None:
    await _create_project(client, "tracker-update")

    resp = await client.patch(
        "/api/v1/projects/tracker-update/tracker-config",
        json={
            "enabled": True,
            "base_url": "https://example.atlassian.net/",
            "auth_email": "alice@example.com",
            "api_token": "super-secret-token",
            "project_key": "eng",
            "issue_type": "Bug",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    # base_url trailing slash stripped, project key uppercased by the validators.
    assert body["base_url"] == "https://example.atlassian.net"
    assert body["project_key"] == "ENG"
    assert body["issue_type"] == "Bug"
    # The token is stored but NEVER echoed back.
    assert body["api_token_set"] is True
    assert "api_token" not in body
    assert "super-secret-token" not in resp.text

    # A follow-up GET reflects the persisted config and still hides the token.
    get_resp = await client.get("/api/v1/projects/tracker-update/tracker-config")
    assert get_resp.status_code == 200
    assert get_resp.json()["api_token_set"] is True
    assert "super-secret-token" not in get_resp.text

    # The token is actually stored on the row (not blank).
    async with TestSessionLocal() as session:
        config = (await session.execute(select(ProjectTrackerConfig))).scalar_one()
        assert config.api_token_encrypted
        assert config.enabled is True


@pytest.mark.asyncio
async def test_update_tracker_config_rejects_invalid_base_url(client: AsyncClient) -> None:
    await _create_project(client, "tracker-badurl")

    resp = await client.patch(
        "/api/v1/projects/tracker-badurl/tracker-config",
        json={"base_url": "http://example.atlassian.net"},  # non-https
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_update_tracker_config_is_owner_only(anon_client: AsyncClient) -> None:
    # First registered user is the owner; create the project as owner.
    owner_reg = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "tracker-owner@example.com", "password": "Password123!", "name": "Owner"},
    )
    assert owner_reg.status_code == 201, owner_reg.text
    await anon_client.post("/api/v1/projects", json={"name": "Guarded", "slug": "tracker-rbac"})

    # Second user defaults to editor (non-owner).
    await anon_client.post("/api/v1/auth/logout")
    editor_reg = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "tracker-editor@example.com", "password": "Password123!", "name": "Editor"},
    )
    assert editor_reg.status_code == 201, editor_reg.text
    # A member, so the owner gate answers (403) rather than the membership gate (404).
    await add_member_by_slug("tracker-rbac", "tracker-editor@example.com", "editor")

    denied = await anon_client.patch(
        "/api/v1/projects/tracker-rbac/tracker-config",
        json={"enabled": True},
    )
    assert denied.status_code == 403, denied.text
