"""Pool claims and resets preserve a shared organization's identities and access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tripl.config import settings
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models.invitation import Invitation
from tripl.models.organization import DEMO_POOL_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import demo_pool, demo_service, project_access
from tripl.tests.conftest import TestSessionLocal


async def test_pool_claim_and_reset_preserve_colleagues_and_pending_invitations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "demo_enabled", True)
    monkeypatch.setattr(settings, "demo_pool_size", 1)

    async def no_content(*_args: object, **_kwargs: object) -> None:
        # Test identity/grant lifecycle independently of the expensive recipe.
        return None

    monkeypatch.setattr(demo_service, "_seed_demo_content", no_content)
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        org = Organization(slug="shared-demo", name="Shared demo")
        outsider_org = Organization(slug="other-demo", name="Other demo")
        pool_org = Organization(id=DEMO_POOL_ORG_ID, slug="demo-pool", name="Demo pool")
        owner = User(email="owner@example.com", password_hash="x")
        colleague = User(email="colleague@example.com", password_hash="x")
        outsider = User(email="outsider@example.com", password_hash="x")
        pool_user = User(email="throwaway@demo-pool.invalid", password_hash="x")
        session.add_all([org, outsider_org, pool_org, owner, colleague, outsider, pool_user])
        await session.flush()
        memberships = [
            OrganizationMember(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMember(organization_id=org.id, user_id=colleague.id, role="member"),
            OrganizationMember(organization_id=outsider_org.id, user_id=outsider.id, role="owner"),
            OrganizationMember(organization_id=pool_org.id, user_id=pool_user.id, role="member"),
        ]
        invitation = Invitation(
            organization_id=org.id,
            email="next-colleague@example.com",
            invited_by_user_id=owner.id,
            token_hash=uuid.uuid4().hex,
            expires_at=now + timedelta(hours=1),
        )
        project = Project(
            organization_id=pool_org.id,
            created_by_user_id=pool_user.id,
            slug="pooled-collaboration",
            name="Pooled demo",
            is_demo=True,
            generation_status="ready",
            demo_seeded_at=now,
        )
        session.add_all([*memberships, invitation, project])
        await session.commit()
        old_project_id = project.id
        invitation_id = invitation.id
        colleague_membership_id = memberships[1].id
        outsider_membership_id = memberships[2].id

        with bound_org(OrgRef(org.id, org.slug)):
            claimed = await demo_pool.claim_pooled_demo(
                session, visitor_id=owner.id, organization_id=org.id, name="Shared demo"
            )
            assert claimed is not None and claimed.organization_id == org.id
            assert await session.get(User, pool_user.id) is None
            session.add(
                ProjectMember(
                    project_id=claimed.id,
                    user_id=colleague.id,
                    role="viewer",
                    added_by_user_id=owner.id,
                )
            )
            await session.commit()
            reset = await demo_service.reset_demo_project(
                session, claimed.slug, created_by=owner.id
            )
            assert reset.id != old_project_id
            assert await project_access.member_role(session, colleague, reset.id) == "viewer"
            assert await project_access.member_role(session, outsider, reset.id) is None

        # Claiming/resetting content must not replace org-level membership or invites.
        for membership_id, org_id, user_id in (
            (colleague_membership_id, org.id, colleague.id),
            (outsider_membership_id, outsider_org.id, outsider.id),
        ):
            member = await session.get(OrganizationMember, membership_id)
            assert member is not None
            assert (member.organization_id, member.user_id) == (org_id, user_id)
        pending = await session.get(Invitation, invitation_id)
        assert pending is not None
        assert pending.organization_id == org.id and pending.invited_by_user_id == owner.id
        assert pending.used_at is None
        assert (
            await session.scalar(
                select(OrganizationMember.id).where(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_id == outsider.id,
                )
            )
            is None
        )
