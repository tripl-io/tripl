"""Which organization a request acts in (F20 PR2).

The service-level matrix drives ``resolve_request_org`` directly; the HTTP tests
check the same rules end to end, including that resolution happens before the
project-bound key fence and that ``/auth/*`` needs no organization.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete, select

from tripl.config import DEPLOYMENT_SELF_HOSTED, settings
from tripl.middleware.org_context import OrgRef
from tripl.models.api_key import ApiKey
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services.org_resolution import (
    ORG_NOT_FOUND,
    ORG_REQUIRED,
    resolve_request_org,
)
from tripl.tests._tenancy import use_multi_tenant
from tripl.tests.conftest import TestSessionLocal

ACME_ID = uuid.UUID("00000000-0000-0000-0000-0000000ac3e1")
ACME_SLUG = "acme"

pytestmark = pytest.mark.no_default_org


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    use_multi_tenant(monkeypatch)


@pytest.fixture
def self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", DEPLOYMENT_SELF_HOSTED)


async def _add_acme() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME_SLUG, name="Acme"))
        await session.commit()


async def _add_member(user_id: uuid.UUID, org_id: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=user_id,
                role=OrganizationRole.member.value,
            )
        )
        await session.commit()


@pytest.fixture
async def user() -> AsyncIterator[User]:
    async with TestSessionLocal() as session:
        row = User(email="org-matrix@example.com", name="M", password_hash="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    await _add_acme()
    yield row


async def _resolve(user: User, *, key_org_id: uuid.UUID | None, path: str | None) -> OrgRef:
    async with TestSessionLocal() as session:
        return await resolve_request_org(
            session, user=user, key_org_id=key_org_id, path_org_slug=path
        )


async def _resolve_error(
    user: User, *, key_org_id: uuid.UUID | None, path: str | None
) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await _resolve(user, key_org_id=key_org_id, path=path)
    return exc.value


# --- service matrix ---------------------------------------------------------


async def test_key_org_is_the_org(user: User, hosted: None) -> None:
    org = await _resolve(user, key_org_id=ACME_ID, path=None)
    assert (org.id, org.slug) == (ACME_ID, ACME_SLUG)


async def test_key_default_org_needs_no_membership(user: User, hosted: None) -> None:
    org = await _resolve(user, key_org_id=DEFAULT_ORG_ID, path=None)
    assert org.id == DEFAULT_ORG_ID


async def test_path_org_differing_from_the_key_org_is_404(user: User) -> None:
    err = await _resolve_error(user, key_org_id=DEFAULT_ORG_ID, path=ACME_SLUG)
    assert (err.status_code, err.detail) == (404, ORG_NOT_FOUND)


async def test_path_org_matching_the_key_org_passes(user: User) -> None:
    org = await _resolve(user, key_org_id=ACME_ID, path=ACME_SLUG)
    assert org.id == ACME_ID


async def test_path_org_member_passes(user: User) -> None:
    await _add_member(user.id, ACME_ID)
    org = await _resolve(user, key_org_id=None, path=ACME_SLUG)
    assert org.id == ACME_ID


async def test_path_org_non_member_is_404(user: User) -> None:
    err = await _resolve_error(user, key_org_id=None, path=ACME_SLUG)
    assert (err.status_code, err.detail) == (404, ORG_NOT_FOUND)


async def test_unknown_path_org_is_404(user: User) -> None:
    for key_org_id in (None, DEFAULT_ORG_ID):
        err = await _resolve_error(user, key_org_id=key_org_id, path="nope")
        assert (err.status_code, err.detail) == (404, ORG_NOT_FOUND)


async def test_self_hosted_default_path_org_needs_no_membership(
    user: User, self_hosted: None
) -> None:
    # Rule 4 serves the legacy URL without a membership row, so the default
    # org's qualified URL must too, or the two forms disagree.
    org = await _resolve(user, key_org_id=None, path=DEFAULT_ORG_SLUG)
    assert (org.id, org.slug) == (DEFAULT_ORG_ID, DEFAULT_ORG_SLUG)


async def test_self_hosted_other_path_org_still_needs_membership(
    user: User, self_hosted: None
) -> None:
    err = await _resolve_error(user, key_org_id=None, path=ACME_SLUG)
    assert (err.status_code, err.detail) == (404, ORG_NOT_FOUND)


async def test_hosted_default_path_org_needs_membership(user: User, hosted: None) -> None:
    err = await _resolve_error(user, key_org_id=None, path=DEFAULT_ORG_SLUG)
    assert (err.status_code, err.detail) == (404, ORG_NOT_FOUND)


async def test_self_hosted_without_path_org_is_the_default_org(
    user: User, self_hosted: None
) -> None:
    # No membership row at all: self-hosted does not consult them.
    org = await _resolve(user, key_org_id=None, path=None)
    assert (org.id, org.slug) == (DEFAULT_ORG_ID, DEFAULT_ORG_SLUG)


async def test_hosted_single_org_is_that_org(user: User, hosted: None) -> None:
    await _add_member(user.id, ACME_ID)
    org = await _resolve(user, key_org_id=None, path=None)
    assert org.id == ACME_ID


async def test_hosted_multi_org_is_400(user: User, hosted: None) -> None:
    await _add_member(user.id, ACME_ID)
    await _add_member(user.id, DEFAULT_ORG_ID)
    err = await _resolve_error(user, key_org_id=None, path=None)
    assert (err.status_code, err.detail) == (400, ORG_REQUIRED)


async def test_hosted_zero_orgs_is_400(user: User, hosted: None) -> None:
    err = await _resolve_error(user, key_org_id=None, path=None)
    assert (err.status_code, err.detail) == (400, ORG_REQUIRED)


# --- over HTTP --------------------------------------------------------------


async def _user_id(email: str = "test@example.com") -> uuid.UUID:
    async with TestSessionLocal() as session:
        user_id: uuid.UUID | None = await session.scalar(select(User.id).where(User.email == email))
    assert user_id is not None
    return user_id


async def test_registration_writes_a_default_org_membership(client: AsyncClient) -> None:
    user_id = await _user_id()
    async with TestSessionLocal() as session:
        role: str | None = await session.scalar(
            select(OrganizationMember.role).where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id == DEFAULT_ORG_ID,
            )
        )
    # The first user of a self-hosted instance owns the default organization.
    assert role == OrganizationRole.owner.value


async def test_member_reaches_the_default_org_path(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Res", "slug": "res"})
    resp = await client.get("/api/v1/orgs/default/projects/res")
    assert resp.status_code == 200, resp.text


async def test_non_member_org_path_is_404(client: AsyncClient) -> None:
    await _add_acme()
    resp = await client.get("/api/v1/orgs/acme/projects")
    assert resp.status_code == 404
    assert resp.json()["detail"] == ORG_NOT_FOUND


async def test_hosted_multi_org_cookie_user_needs_the_org_in_the_path(
    client: AsyncClient, hosted: None
) -> None:
    assert (await client.get("/api/v1/projects")).status_code == 200
    await _add_acme()
    await _add_member(await _user_id(), ACME_ID)

    legacy = await client.get("/api/v1/projects")
    assert legacy.status_code == 400
    assert legacy.json()["detail"] == ORG_REQUIRED
    assert (await client.get("/api/v1/orgs/default/projects")).status_code == 200
    assert (await client.get("/api/v1/orgs/acme/projects")).status_code == 200
    # Identity routes act in no organization.
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    # Nor do instance-wide routes, which have no org-qualified form.
    assert (await client.get("/api/v1/project-templates")).status_code == 200
    settings_resp = await client.get("/api/v1/settings")
    assert settings_resp.status_code != 400, settings_resp.text


async def test_self_hosted_default_org_path_works_without_a_membership_row(
    client: AsyncClient, self_hosted: None
) -> None:
    # Resolution binds the default org without a membership row (migration
    # c9e1a3b5d7f9 gave every older account one); what the account may then do
    # is its roles' business: its creator row still reaches the project.
    assert (
        await client.post("/api/v1/projects", json={"name": "Nm", "slug": "nm"})
    ).status_code == 201
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(OrganizationMember.user_id == await _user_id())
        )
        await session.commit()

    for path in ("/api/v1/orgs/default/projects", "/api/v1/orgs/default/projects/nm"):
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text)


async def test_another_orgs_project_is_not_reachable_through_the_org_path(
    client: AsyncClient,
) -> None:
    assert (
        await client.post("/api/v1/projects", json={"name": "Iso", "slug": "iso"})
    ).status_code == 201
    await _add_acme()
    await _add_member(await _user_id(), ACME_ID)

    for suffix in ("", "/branches", "/event-types"):
        hidden = await client.get(f"/api/v1/orgs/acme/projects/iso{suffix}")
        assert hidden.status_code == 404, (suffix, hidden.text)
        assert hidden.json()["detail"] == "Project not found"
        shown = await client.get(f"/api/v1/orgs/default/projects/iso{suffix}")
        assert shown.status_code == 200, (suffix, shown.text)


async def test_creates_through_the_org_path_land_in_that_org(
    client: AsyncClient, self_hosted: None
) -> None:
    await _add_acme()
    await _add_member(await _user_id(), ACME_ID)

    created = await client.post(
        "/api/v1/orgs/acme/projects", json={"name": "Acme P", "slug": "acme-p"}
    )
    assert created.status_code == 201, created.text
    assert (await client.get("/api/v1/orgs/acme/projects/acme-p")).status_code == 200
    # Not in the default organization, which the legacy URL acts in (self-hosted).
    assert (await client.get("/api/v1/projects/acme-p")).status_code == 404

    key = await client.post(
        "/api/v1/orgs/acme/me/api-keys",
        json={"name": "k", "scope": "read", "project_slug": "acme-p"},
    )
    assert key.status_code == 201, key.text
    async with TestSessionLocal() as session:
        project_org: uuid.UUID | None = await session.scalar(
            select(Project.organization_id).where(Project.slug == "acme-p")
        )
        key_org: uuid.UUID | None = await session.scalar(
            select(ApiKey.organization_id).where(ApiKey.id == uuid.UUID(key.json()["id"]))
        )
    assert project_org == key_org == ACME_ID
    bearer = {"Authorization": f"Bearer {key.json()['token']}"}
    via_key = await client.get("/api/v1/projects/acme-p/event-types", headers=bearer)
    assert via_key.status_code == 200, via_key.text


async def test_api_key_is_held_to_its_org(client: AsyncClient) -> None:
    await _add_acme()
    await _add_member(await _user_id(), ACME_ID)
    created = await client.post("/api/v1/me/api-keys", json={"name": "k", "scope": "read"})
    assert created.status_code == 201, created.text
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}

    assert (await client.get("/api/v1/projects", headers=bearer)).status_code == 200
    assert (await client.get("/api/v1/orgs/default/projects", headers=bearer)).status_code == 200
    # A member of acme, but the key belongs to the default organization.
    other = await client.get("/api/v1/orgs/acme/projects", headers=bearer)
    assert other.status_code == 404
    assert other.json()["detail"] == ORG_NOT_FOUND


async def test_project_scoped_key_resolves_its_org_before_the_fence(
    client: AsyncClient,
) -> None:
    for slug in ("fenced", "elsewhere"):
        assert (
            await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
        ).status_code == 201
    created = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "project_slug": "fenced"},
    )
    assert created.status_code == 201, created.text
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}

    for prefix in ("/api/v1", "/api/v1/orgs/default"):
        ok = await client.get(f"{prefix}/projects/fenced/event-types", headers=bearer)
        assert ok.status_code == 200, ok.text
        hidden = await client.get(f"{prefix}/projects/elsewhere/event-types", headers=bearer)
        assert hidden.status_code == 404
        assert hidden.json()["detail"] == "Project not found"
