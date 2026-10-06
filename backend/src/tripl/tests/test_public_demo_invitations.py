"""Public-demo invitation safety and explicit project access."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tripl import extensions
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.invitation import Invitation
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import auth_service, demo_service, invitation_email, invitation_service
from tripl.tests._tenancy import use_public_demo
from tripl.tests.conftest import TestSessionLocal


async def _mint(client, email="invited@example.com", role="member"):
    return await client.post("/api/v1/users/invitations", json={"email": email, "role": role})


def _accept_url(minted):
    return "/api/v1/auth/invitations/" + minted.json()["accept_path"].split("/")[-1] + "/accept"


async def test_demo_mints_link_only_and_rejects_privileged_roles(client, monkeypatch):
    use_public_demo(monkeypatch)
    prepare = AsyncMock(side_effect=AssertionError("demo must never prepare mail"))
    monkeypatch.setattr(invitation_email, "prepare", prepare)
    minted = await _mint(client)
    assert minted.status_code == 201, minted.text
    assert minted.json()["invitation"]["role"] == "member"
    assert minted.json()["accept_path"].startswith("/invite/")
    for role in ("admin", "owner"):
        refused = await _mint(client, f"{role}@example.com", role)
        assert refused.status_code == 403
    prepare.assert_not_awaited()


async def test_demo_anonymous_acceptance_and_direct_redeem_cannot_create_accounts(
    client, monkeypatch
):
    minted = await _mint(client)
    assert minted.status_code == 201
    use_public_demo(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for body in ({}, {"password": "Password123!"}):
            response = await anon.post(_accept_url(minted), json=body)
            assert response.status_code == 403, response.text
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as error:
            await invitation_service.redeem_invitation(
                session,
                raw_token=minted.json()["accept_path"].split("/")[-1],
                password="Password123!",
                name=None,
            )
        assert error.value.status_code == 403
        assert (
            await session.scalar(select(User.id).where(User.email == "invited@example.com")) is None
        )
        assert (await session.scalar(select(Invitation))).used_at is None


@pytest.mark.parametrize("invite_role", ["member", "admin", "owner"])
async def test_demo_verified_acceptance_grants_only_ready_same_org_demos(
    client, monkeypatch, invite_role
):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as invitee:
        registered = await invitee.post(
            "/api/v1/auth/register",
            json={
                "email": "invited@example.com",
                "password": "Password123!",
            },
        )
        assert registered.status_code == 201
        async with TestSessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == "invited@example.com"))
            await session.execute(
                OrganizationMember.__table__.delete().where(
                    OrganizationMember.user_id == user.id,
                )
            )
            user.email_verified_at = None
            other_org = Organization(slug="other-demo-org", name="Other")
            session.add(other_org)
            await session.flush()
            projects = [
                Project(name="Ready", slug="ready", organization_id=DEFAULT_ORG_ID, is_demo=True),
                Project(name="Real", slug="real", organization_id=DEFAULT_ORG_ID),
                Project(
                    name="Seeding",
                    slug="seeding",
                    organization_id=DEFAULT_ORG_ID,
                    is_demo=True,
                    generation_status="seeding",
                ),
                Project(name="Foreign", slug="foreign", organization_id=other_org.id, is_demo=True),
                Project(
                    name="Existing", slug="existing", organization_id=DEFAULT_ORG_ID, is_demo=True
                ),
            ]
            session.add_all(projects)
            await session.flush()
            session.add(ProjectMember(project_id=projects[-1].id, user_id=user.id, role="editor"))
            await session.commit()
        use_public_demo(monkeypatch)
        minted = await _mint(client)
        assert minted.status_code == 201, minted.text
        refused = await invitee.post(_accept_url(minted), json={})
        assert refused.status_code == 403
        async with TestSessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == "invited@example.com"))
            user.email_verified_at = datetime.now(UTC)
            invitation = await session.scalar(select(Invitation))
            invitation.org_role = invite_role  # Legacy invite from before public-demo mode.
            await session.commit()
        accepted = await invitee.post(_accept_url(minted), json={})
        assert accepted.status_code == 200, accepted.text
        async with TestSessionLocal() as session:
            grants = (
                await session.execute(
                    select(Project.slug, ProjectMember.role)
                    .join(
                        ProjectMember,
                        ProjectMember.project_id == Project.id,
                    )
                    .where(ProjectMember.user_id == user.id)
                )
            ).all()
            assert set(grants) == {("ready", "viewer"), ("existing", "editor")}
            assert (
                await session.scalar(
                    select(OrganizationMember.role).where(
                        OrganizationMember.organization_id == DEFAULT_ORG_ID,
                        OrganizationMember.user_id == user.id,
                    )
                )
                == "member"
            )
            invitation = await session.scalar(select(Invitation))
            assert invitation.org_role == "member"
            audit = await session.scalar(
                select(AuditLog).where(AuditLog.action == "user.invite_accept")
            )
            assert audit.payload["role"] == "member"
        assert (await invitee.post(_accept_url(minted), json={})).status_code == 400


async def test_demo_capacity_counts_owner_and_live_invites_but_allows_replacement(
    client, monkeypatch
):
    use_public_demo(monkeypatch)
    for i in range(9):
        response = await _mint(client, f"person{i}@example.com")
        assert response.status_code == 201, response.text
    blocked = await _mint(client, "overflow@example.com")
    assert blocked.status_code == 409
    replacement = await _mint(client, "person0@example.com")
    assert replacement.status_code == 201
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Invitation)) == 9


async def test_demo_mint_quota_survives_replacement_and_revocation(client, monkeypatch):
    use_public_demo(monkeypatch)
    for _ in range(10):
        minted = await _mint(client)
        assert minted.status_code == 201, minted.text
        revoked = await client.delete(
            "/api/v1/users/invitations/" + minted.json()["invitation"]["id"]
        )
        assert revoked.status_code == 204
    refused = await _mint(client)
    assert refused.status_code == 429, refused.text
    async with TestSessionLocal() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.action == "user.invite")))
        assert len(rows) == 10
        for row in rows:
            row.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    assert (await _mint(client)).status_code == 201


@pytest.mark.parametrize("dimension", ["organization", "inviter"])
async def test_demo_quota_enforces_each_dimension_independently(client, monkeypatch, dimension):
    async with TestSessionLocal() as session:
        owner = await session.scalar(select(User))
        other_org = Organization(slug="other-quota-org", name="Other")
        other_user = User(email="other-sender@example.com", password_hash="unused")
        session.add_all([other_org, other_user])
        await session.flush()
        session.add_all(
            [
                AuditLog(
                    action="user.invite",
                    target_type="invitation",
                    organization_id=DEFAULT_ORG_ID if dimension == "organization" else other_org.id,
                    user_id=other_user.id if dimension == "organization" else owner.id,
                    created_at=datetime.now(UTC),
                )
                for _ in range(10)
            ]
        )
        await session.commit()
    use_public_demo(monkeypatch)
    refused = await _mint(client)
    assert refused.status_code == 429, refused.text
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Invitation)) == 0


async def test_demo_expired_pending_invitations_do_not_reserve_capacity(client, monkeypatch):
    for i in range(10):
        assert (await _mint(client, f"expired{i}@example.com")).status_code == 201
    async with TestSessionLocal() as session:
        for invite in await session.scalars(select(Invitation)):
            invite.expires_at = datetime.now(UTC) - timedelta(hours=1)
        for audit in await session.scalars(
            select(AuditLog).where(AuditLog.action == "user.invite")
        ):
            audit.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    use_public_demo(monkeypatch)
    assert (await _mint(client)).status_code == 201


async def test_demo_wrong_signed_in_identity_leaves_invitation_unused(client, monkeypatch):
    use_public_demo(monkeypatch)
    minted = await _mint(client)
    assert minted.status_code == 201
    refused = await client.post(_accept_url(minted), json={})
    assert refused.status_code == 403
    async with TestSessionLocal() as session:
        assert (await session.scalar(select(Invitation))).used_at is None


async def _add_org_member(email: str, role: str) -> User:
    async with TestSessionLocal() as session:
        user = User(email=email, password_hash="unused")
        session.add(user)
        await session.flush()
        session.add(OrganizationMember(organization_id=DEFAULT_ORG_ID, user_id=user.id, role=role))
        await session.commit()
        return user


async def _project_roles(slug: str) -> dict[str, str]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(User.email, ProjectMember.role)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(Project.slug == slug)
        )
        return {email: str(getattr(role, "value", role)) for email, role in rows.all()}


async def _skip_seed(*_args, **_kwargs) -> None:
    """Stands in for the ~10 s seed: only the promotion is under test."""


@pytest.mark.parametrize("public_demo", [True, False], ids=["public-demo", "self-hosted"])
async def test_a_demo_generated_after_acceptance_is_shared_with_colleagues(
    client, monkeypatch, public_demo
):
    # The colleague accepted before this demo existed, so acceptance could not
    # grant it; the promotion to ready must. Off a public demo, project access
    # stays explicit and the new demo is the creator's alone.
    await _add_org_member("colleague@example.com", "member")
    await _add_org_member("admin@example.com", "admin")
    monkeypatch.setattr(demo_service, "_seed_demo_content", _skip_seed)
    if public_demo:
        use_public_demo(monkeypatch)
    created = await client.post("/api/v1/projects/demo")
    assert created.status_code == 202, created.text
    roles = await _project_roles(created.json()["slug"])
    assert roles.pop("colleague@example.com", None) == ("viewer" if public_demo else None)
    # The admin sees every project as an org admin; no row is written for them.
    assert "admin@example.com" not in roles
    assert set(roles.values()) == {"editor"}  # the creator's own membership


async def test_a_claimed_pool_demo_is_shared_with_colleagues(client, monkeypatch):
    colleague = await _add_org_member("colleague@example.com", "member")
    async with TestSessionLocal() as session:
        pooled = Project(name="Pooled", slug="pooled", organization_id=DEFAULT_ORG_ID, is_demo=True)
        session.add(pooled)
        await session.commit()
        pooled_id = pooled.id

    class _Pool:
        async def on_ready_demo_claimed(self) -> None:
            return None

    async def claim(session, **_kwargs):
        return _Pool(), await session.get(Project, pooled_id)

    monkeypatch.setattr(extensions, "claim_ready_demo", claim)
    use_public_demo(monkeypatch)
    created = await client.post("/api/v1/projects/demo")
    assert created.status_code == 202, created.text
    assert created.json()["slug"] == "pooled"
    async with TestSessionLocal() as session:
        role = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == pooled_id, ProjectMember.user_id == colleague.id
            )
        )
    assert str(getattr(role, "value", role)) == "viewer"


async def test_a_colleague_keeps_an_existing_role_on_a_new_demo(monkeypatch):
    # Existing project roles win, as at acceptance: a row (even "none") is kept.
    colleague = await _add_org_member("colleague@example.com", "member")
    async with TestSessionLocal() as session:
        project = Project(
            name="Demo", slug="demo-kept", organization_id=DEFAULT_ORG_ID, is_demo=True
        )
        session.add(project)
        await session.flush()
        session.add(ProjectMember(project_id=project.id, user_id=colleague.id, role="none"))
        await session.commit()
        use_public_demo(monkeypatch)
        await invitation_service.share_demo_with_colleagues(session, project)
        await session.commit()
        roles = list(
            await session.scalars(
                select(ProjectMember.role).where(ProjectMember.project_id == project.id)
            )
        )
    assert [str(getattr(role, "value", role)) for role in roles] == ["none"]


@pytest.mark.parametrize("public_demo", [True, False], ids=["public-demo", "self-hosted"])
async def test_every_mint_files_exactly_one_audit_row(client, monkeypatch, public_demo):
    # One writer for ``user.invite`` (the service, in the mint's transaction),
    # whichever mode: the demo quota counts these rows.
    if public_demo:
        use_public_demo(monkeypatch)
    minted = await _mint(client)
    assert minted.status_code == 201, minted.text
    async with TestSessionLocal() as session:
        rows = list(
            await session.scalars(
                select(AuditLog).where(AuditLog.action == invitation_service.INVITE_AUDIT_ACTION)
            )
        )
    assert len(rows) == 1
    [row] = rows
    assert str(row.target_id) == minted.json()["invitation"]["id"]
    assert row.target_name == "invited@example.com"
    assert row.organization_id == DEFAULT_ORG_ID
    expected = {"role": "member", "public_demo": True} if public_demo else {"role": "member"}
    assert row.payload == expected


def test_the_inviter_mint_lock_has_its_own_key_space():
    # The same UUID as an organization and as an inviter must not share a lock.
    some_id = DEFAULT_ORG_ID
    assert invitation_service.demo_mint_lock_key(some_id) != auth_service.owner_set_lock_key(
        some_id
    )
