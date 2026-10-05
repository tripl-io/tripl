"""A suspended organization (F20 PR14).

* its members get 403 "This organization is suspended" on every org-scoped
  request — org-qualified project URLs, ``/orgs/{org}/...``, a hosted
  member's legacy paths and its API keys — while a stranger still gets 404;
* it stays listed in ``GET /orgs`` and ``/auth/me`` with its status;
* unsuspending restores everything as it was;
* a member's open event stream ends on suspension (a step-in's does not), and
  a deletion request that races a suspension loses (403, still suspended);
* a hosted legacy path binds the user's single ACTIVE organization;
* the scheduled jobs skip its projects (``services.active_org_scope``), and a
  deleting organization's too.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_health_snapshot import ProjectHealthSnapshot
from tripl.models.user import User
from tripl.services import org_deletion_service, project_access
from tripl.services.active_org_scope import in_active_org, project_in_active_org
from tripl.services.org_resolution import ORG_REQUIRED, ORG_SUSPENDED, resolve_request_org
from tripl.tests._platform_world import (
    API,
    GLOBEX,
    GLOBEX_ID,
    SHOP,
    World,
    add_org,
    build_world,
    new_client,
    set_org_status,
)
from tripl.tests._tenancy import use_multi_tenant
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.health import snapshot_all_projects

ORG = f"{API}/orgs/{GLOBEX}"
PROJECT = f"{ORG}/projects/{SHOP}"
INITECH_ID = uuid.UUID("00000000-0000-0000-0000-0000001417ec")
UMBRELLA_ID = uuid.UUID("00000000-0000-0000-0000-00000000ab11")


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = await build_world()
    yield built
    await built.close()


async def _suspend(world: World) -> None:
    resp = await world.operator.post(
        f"{API}/platform/orgs/{GLOBEX}/suspend", json={"reason": "Terms of service review"}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_members_are_refused_everywhere_in_the_org(world: World) -> None:
    key = await world.alice.post(f"{ORG}/me/api-keys", json={"name": "k", "scope": "read"})
    assert key.status_code == 201, key.text
    await _suspend(world)

    for method, path in (
        ("GET", f"{ORG}/projects"),
        ("GET", PROJECT),
        ("GET", f"{PROJECT}/event-types"),
        ("POST", f"{PROJECT}/event-types"),
        ("GET", ORG),
        ("GET", f"{ORG}/members"),
        ("GET", f"{ORG}/settings"),
        ("DELETE", ORG),
    ):
        resp = await world.alice.request(method, path, json={} if method != "GET" else None)
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["detail"] == ORG_SUSPENDED

    bearer = {"Authorization": f"Bearer {key.json()['token']}"}
    async with new_client() as bare:
        via_key = await bare.get(f"{API}/projects", headers=bearer)
    assert via_key.status_code == 403
    assert via_key.json()["detail"] == ORG_SUSPENDED

    # The default organization, where alice is also a member, is untouched.
    assert (await world.alice.get(f"{API}/projects")).status_code == 200


@pytest.mark.asyncio
async def test_a_stranger_still_gets_404(world: World) -> None:
    await _suspend(world)
    for path in (f"{ORG}/projects", ORG):
        resp = await world.bob.get(path)
        assert resp.status_code == 404, (path, resp.text)


@pytest.mark.asyncio
async def test_it_stays_listed_with_its_status(world: World) -> None:
    await _suspend(world)
    orgs = {o["slug"]: o for o in (await world.alice.get(f"{API}/orgs")).json()}
    assert orgs[GLOBEX]["status"] == "suspended"
    assert orgs["default"]["status"] == "active"
    me = (await world.alice.get(f"{API}/auth/me")).json()
    assert {o["slug"]: o["status"] for o in me["orgs"]} == {
        "default": "active",
        GLOBEX: "suspended",
    }
    # The reason is the operator's; members see only the status.
    assert "suspended_reason" not in orgs[GLOBEX]


@pytest.mark.asyncio
async def test_unsuspending_restores_access(world: World) -> None:
    await _suspend(world)
    assert (await world.alice.get(f"{ORG}/projects")).status_code == 403
    resp = await world.operator.post(f"{API}/platform/orgs/{GLOBEX}/unsuspend")
    assert resp.status_code == 200, resp.text
    listed = await world.alice.get(f"{ORG}/projects")
    assert listed.status_code == 200, listed.text
    assert [p["slug"] for p in listed.json()] == [SHOP]


@pytest.mark.asyncio
async def test_a_hosted_members_legacy_paths_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    use_multi_tenant(monkeypatch)
    async with TestSessionLocal() as session:
        user = User(email="solo@example.com", name="Solo", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    await add_org(GLOBEX_ID, GLOBEX, "Globex", user.id)
    await set_org_status(GLOBEX_ID, OrganizationStatus.suspended)
    async with TestSessionLocal() as session:
        for path_org in (None, GLOBEX):
            with pytest.raises(HTTPException) as exc:
                await resolve_request_org(
                    session, user=user, key_org_id=None, path_org_slug=path_org
                )
            assert (exc.value.status_code, exc.value.detail) == (403, ORG_SUSPENDED)


# ── the scheduled jobs ───────────────────────────────────────────────────────


async def _project_in(org_id: uuid.UUID, slug: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        project = Project(name=slug, slug=slug, organization_id=org_id)
        session.add(project)
        await session.commit()
        return project.id


@pytest.mark.asyncio
async def test_the_scheduler_scope_is_active_orgs_only() -> None:
    async with TestSessionLocal() as session:
        owner = User(email="owner@example.com", name="Owner", password_hash="x")
        session.add(owner)
        await session.commit()
        owner_id = owner.id
    await add_org(GLOBEX_ID, GLOBEX, "Globex", owner_id)
    await add_org(INITECH_ID, "initech", "Initech", owner_id)
    active = await _project_in(DEFAULT_ORG_ID, "live")
    suspended = await _project_in(GLOBEX_ID, "paused")
    deleting = await _project_in(INITECH_ID, "leaving")
    await set_org_status(GLOBEX_ID, OrganizationStatus.suspended)
    await set_org_status(INITECH_ID, OrganizationStatus.deleting)

    async with TestSessionLocal() as session:
        joined = set(
            (await session.scalars(select(Project.id).where(project_in_active_org()))).all()
        )
        by_id = set(
            (await session.scalars(select(Project.id).where(in_active_org(Project.id)))).all()
        )
    assert joined == by_id == {active}
    assert suspended not in joined and deleting not in joined

    # A real beat job: only the active organization's project is attempted.
    async with TestSessionLocal() as session:
        stats = await snapshot_all_projects(session, datetime.now(UTC))
    assert stats["written"] + stats["failed"] == 1
    async with TestSessionLocal() as session:
        snapped = set((await session.scalars(select(ProjectHealthSnapshot.project_id))).all())
    assert snapped <= {active}


@pytest.mark.asyncio
async def test_suspension_keeps_the_org_rows(world: World) -> None:
    await _suspend(world)
    async with TestSessionLocal() as session:
        org = await session.get(Organization, GLOBEX_ID)
        assert org is not None
        assert org.status == OrganizationStatus.suspended.value
        assert org.suspended_reason == "Terms of service review"
        assert org.suspended_at is not None
        members = (
            await session.scalars(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == GLOBEX_ID
                )
            )
        ).all()
    assert list(members) == [world.alice_id]


# ── an open event stream, a racing deletion, the hosted fallback ─────────────


async def _shop_id() -> uuid.UUID:
    async with TestSessionLocal() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.organization_id == GLOBEX_ID, Project.slug == SHOP)
        )
    assert project_id is not None
    return project_id


@pytest.mark.asyncio
async def test_an_open_streams_guard_is_revoked_by_suspension(world: World) -> None:
    shop_id = await _shop_id()
    assert await project_access.still_member(
        TestSessionLocal, user_id=world.alice_id, project_id=shop_id
    )
    await _suspend(world)
    assert not await project_access.still_member(
        TestSessionLocal, user_id=world.alice_id, project_id=shop_id
    )
    # Deleting ends it just the same.
    await set_org_status(GLOBEX_ID, OrganizationStatus.deleting)
    assert not await project_access.still_member(
        TestSessionLocal, user_id=world.alice_id, project_id=shop_id
    )


@pytest.mark.asyncio
async def test_a_step_in_stream_survives_suspension_but_not_deletion(world: World) -> None:
    shop_id = await _shop_id()
    started = await world.operator.post(
        f"{API}/platform/orgs/{GLOBEX}/step-in", json={"reason": "Support ticket 1234"}
    )
    assert started.status_code == 201, started.text
    await _suspend(world)
    with bound_org(OrgRef(id=GLOBEX_ID, slug=GLOBEX, step_in_user_id=world.operator_id)):
        assert await project_access.still_member(
            TestSessionLocal, user_id=world.operator_id, project_id=shop_id
        )
        await set_org_status(GLOBEX_ID, OrganizationStatus.deleting)
        assert not await project_access.still_member(
            TestSessionLocal, user_id=world.operator_id, project_id=shop_id
        )


@pytest.mark.asyncio
async def test_a_deletion_cannot_overwrite_a_suspension_that_raced_it(world: World) -> None:
    # The owner's DELETE passed the gate while the org was active; the platform
    # admin suspends it before the status flips. The compare-and-set loses.
    await _suspend(world)
    async with TestSessionLocal() as session:
        with pytest.raises(org_deletion_service.OrgNotActiveError) as exc:
            await org_deletion_service.request_deletion(
                session, org_id=GLOBEX_ID, slug=GLOBEX, confirm_slug=GLOBEX
            )
        assert exc.value.suspended is True
        await session.rollback()
    async with TestSessionLocal() as session:
        org = await session.get(Organization, GLOBEX_ID)
        assert org is not None
        assert org.status == OrganizationStatus.suspended.value

    # Already on its way out (or gone): the not-found flavour.
    await set_org_status(GLOBEX_ID, OrganizationStatus.deleting)
    async with TestSessionLocal() as session:
        with pytest.raises(org_deletion_service.OrgNotActiveError) as exc:
            await org_deletion_service.request_deletion(
                session, org_id=GLOBEX_ID, slug=GLOBEX, confirm_slug=GLOBEX
            )
        assert exc.value.suspended is False


@pytest.mark.asyncio
async def test_the_deletion_route_answers_403_when_suspended_under_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = org_deletion_service.request_deletion

    async def suspend_first(session: AsyncSession, **kwargs: object) -> None:
        # The platform admin's suspension lands between the gate and the flip.
        await session.execute(
            update(Organization)
            .where(Organization.id == GLOBEX_ID)
            .values(status=OrganizationStatus.suspended.value)
        )
        await session.commit()
        await real(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(org_deletion_service, "request_deletion", suspend_first)
    resp = await world.alice.request("DELETE", ORG, json={"confirm_slug": GLOBEX})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == ORG_SUSPENDED
    async with TestSessionLocal() as session:
        org = await session.get(Organization, GLOBEX_ID)
        assert org is not None
        assert org.status == OrganizationStatus.suspended.value


async def _hosted_user_of(*orgs: tuple[uuid.UUID, str, OrganizationStatus]) -> User:
    async with TestSessionLocal() as session:
        user = User(email="multi@example.com", name="Multi", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    for org_id, slug, org_status in orgs:
        await add_org(org_id, slug, slug.title(), user.id)
        if org_status is not OrganizationStatus.active:
            await set_org_status(org_id, org_status)
    return user


@pytest.mark.asyncio
async def test_the_hosted_fallback_binds_the_single_active_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_multi_tenant(monkeypatch)
    user = await _hosted_user_of(
        (GLOBEX_ID, GLOBEX, OrganizationStatus.suspended),
        (INITECH_ID, "initech", OrganizationStatus.active),
    )
    async with TestSessionLocal() as session:
        ref = await resolve_request_org(session, user=user, key_org_id=None, path_org_slug=None)
    assert (ref.id, ref.slug) == (INITECH_ID, "initech")


@pytest.mark.asyncio
async def test_the_hosted_fallback_still_needs_an_org_among_several_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_multi_tenant(monkeypatch)
    user = await _hosted_user_of(
        (GLOBEX_ID, GLOBEX, OrganizationStatus.suspended),
        (INITECH_ID, "initech", OrganizationStatus.active),
        (UMBRELLA_ID, "umbrella", OrganizationStatus.active),
    )
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await resolve_request_org(session, user=user, key_org_id=None, path_org_slug=None)
    assert (exc.value.status_code, exc.value.detail) == (400, ORG_REQUIRED)
