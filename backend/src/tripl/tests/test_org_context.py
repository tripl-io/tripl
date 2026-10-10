"""The organization contextvar and the org-filtered project lookup (F20 PR2).

An unbound organization is a programming error: every accessor that needs one
raises ``OrgContextMissing`` instead of defaulting, and ``bound_org`` leaves no
binding behind.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from tripl.middleware.org_context import (
    OrgContextMissing,
    OrgRef,
    bound_org,
    current_org,
    current_org_id,
    require_org_id,
)
from tripl.models.organization import DEFAULT_ORG_ID, DEFAULT_ORG_SLUG, Organization
from tripl.services.project_lookup import (
    project_slug_clause,
    resolve_project,
    resolve_project_id,
)
from tripl.tests.conftest import TestSessionLocal

_OTHER = OrgRef(id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"), slug="acme")


async def _make_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


@pytest.mark.no_default_org
async def test_require_org_id_raises_when_unbound() -> None:
    assert current_org() is None
    assert current_org_id() is None
    with pytest.raises(OrgContextMissing):
        require_org_id()


@pytest.mark.no_default_org
async def test_project_slug_clause_raises_when_unbound() -> None:
    with pytest.raises(OrgContextMissing):
        project_slug_clause("anything")


@pytest.mark.no_default_org
async def test_resolve_project_raises_without_org(client: AsyncClient) -> None:
    await _make_project(client, "ctx-unbound")
    async with TestSessionLocal() as session:
        with pytest.raises(OrgContextMissing):
            await resolve_project(session, "ctx-unbound")
        with pytest.raises(OrgContextMissing):
            await resolve_project_id(session, "ctx-unbound")


@pytest.mark.no_default_org
async def test_bound_org_binds_and_resets() -> None:
    with bound_org(_OTHER):
        assert current_org() == _OTHER
        assert require_org_id() == _OTHER.id
        with bound_org(OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)):
            assert current_org_id() == DEFAULT_ORG_ID
        assert current_org_id() == _OTHER.id
    assert current_org() is None


@pytest.mark.no_default_org
async def test_bound_org_resets_on_error() -> None:
    with pytest.raises(RuntimeError), bound_org(_OTHER):
        raise RuntimeError("boom")
    assert current_org() is None


async def test_the_suite_binds_the_default_org_by_default() -> None:
    assert current_org_id() == DEFAULT_ORG_ID


async def test_resolve_project_finds_the_bound_orgs_project(client: AsyncClient) -> None:
    await _make_project(client, "ctx-found")
    async with TestSessionLocal() as session:
        project = await resolve_project(session, "ctx-found")
        assert project.organization_id == DEFAULT_ORG_ID
        assert await resolve_project_id(session, "ctx-found") == project.id


async def test_resolve_project_hides_another_orgs_project(client: AsyncClient) -> None:
    await _make_project(client, "ctx-hidden")
    async with TestSessionLocal() as session:
        session.add(Organization(id=_OTHER.id, slug=_OTHER.slug, name="Acme"))
        await session.commit()
        with bound_org(_OTHER):
            with pytest.raises(HTTPException) as exc:
                await resolve_project(session, "ctx-hidden")
            assert exc.value.status_code == 404
            assert exc.value.detail == "Project not found"
            with pytest.raises(HTTPException) as exc:
                await resolve_project_id(session, "ctx-hidden")
            assert exc.value.detail == "Project not found"
